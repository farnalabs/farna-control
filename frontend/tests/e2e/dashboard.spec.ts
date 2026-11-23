import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('Dashboard', () => {
  test('redirects to login when unauthenticated', { tag: '@smoke' }, async ({ page }) => {
    await page.goto('/')

    await expect(page).toHaveURL(/\/login/)
  })

  test('displays dashboard heading when authenticated', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await page.reload()

    await expect(page.locator('h1')).toContainText('Dashboard')
  })

  test('trend window toggles reflect selection', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    // FAR-92: the rolling-window toggle is gated behind the analytics_page
    // feature flag. Mock the plan fetch so the flag resolves enabled for this
    // regression run (mirrors the sibling DashboardView unit specs, which turn
    // analytics_page on). Registered after loginAsAdmin so it takes precedence
    // over the local-target catch-all API mock. FAR-1809 repointed the plan
    // bootstrap at the caller-scoped GET /api/v1/plan, so the mock owns that
    // endpoint.
    await page.route('**/api/v1/plan*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ license: { tier: 'community' }, flags: [{ name: 'analytics_page', currently_active: true }] }) })
    })
    await page.reload()

    const heading = page.locator('[data-testid="dashboard-title"]')
    await expect(heading).toBeVisible()

    // A fresh session has no persisted preference, and loadTrendWindow() falls
    // back to the 3-day rolling window when localStorage holds nothing.
    const defaultToggle = page.locator('[data-testid="trend-toggle-3"]')
    await expect(defaultToggle).toHaveAttribute('aria-pressed', 'true')

    const weekToggle = page.locator('[data-testid="trend-toggle-7"]')
    await weekToggle.click()

    await expect(weekToggle).toHaveAttribute('aria-pressed', 'true')
    await expect(defaultToggle).toHaveAttribute('aria-pressed', 'false')
  })

  test('run activity card shows HITL and rejection rate labels', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    // The card renders its chart labels only when the trend series has more
    // than one point; otherwise it shows the "no data yet" empty state. Staging
    // carries sparse run history, so the live trends endpoint is not a
    // deterministic source for this presentation assertion. Serve a populated
    // 3-day window (registered after loginAsAdmin so it wins over any target
    // catch-all) and assert the labels render when the window is populated.
    await page.route('**/api/v1/dashboard/trends*', (route) => {
      const dates = ['2026-07-05', '2026-07-06', '2026-07-07']
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          days: dates.length,
          run_counts: dates.map((date, i) => ({ date, run_count: 10 + i })),
          eval_pass_rates: dates.map((date, i) => ({ date, total_evals: 8, passed_evals: 7, pass_rate: 88 + i })),
          token_spend: dates.map((date, i) => ({ date, total_spend_usd: 3.5 + i })),
          hitl_volume: dates.map((date, i) => ({ date, total_decisions: 4, approved_count: 3, rejected_count: 1, rejection_rate: 25, avg_time_to_approve_ms: 120000 + i * 5000 })),
          rejection_trend: dates.map((date, i) => ({ date, rolling_rejection_rate: 5 + i, raw_rejection_rate: 6 + i })),
          correlation: dates.map((date, i) => ({ date, rejection_rate: 5 + i, eval_pass_rate: 88 + i })),
          feedback_volume: dates.map((date) => ({ date, feedback_count: 2, resolved_count: 1, correcting_count: 1 })),
        }),
      })
    })
    await page.reload()

    const runActivity = page.locator('[data-testid="dashboard-run-activity"]')
    await expect(runActivity).toBeVisible()
    await expect(runActivity).toContainText('Avg approval time')
    await expect(runActivity).toContainText('Rejection rate')
  })

  test('system overview defaults collapsed when no map has been published', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    // FAR-2695: with nothing published, the panel must not push the dashboard's
    // real stat cards down with an empty state. The card only renders when the
    // caller has System Maps access, which the local mock target grants nothing
    // of by default, so declare the entitlement explicitly and serve an empty
    // map list (the "nothing published yet" signal).
    await page.route('**/api/v1/plan*', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          license: { tier: 'community' },
          flags: [
            { name: 'system_maps', currently_active: true },
            { name: 'analytics_page', currently_active: true },
          ],
        }),
      })
    })
    await page.route('**/api/v1/auth/permissions*', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ permissions: ['system_maps_access'] }),
      })
    })
    await page.route('**/api/v1/system-maps*', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: [], total: 0, page: 1, page_size: 20 }),
      })
    })
    // No stored collapse preference: this is a fresh visitor.
    await page.evaluate(() => localStorage.removeItem('dashboard-system-overview-collapsed'))
    await page.reload()

    const card = page.locator('[data-testid="dashboard-system-overview"]')
    await expect(card).toBeVisible()
    const toggle = page.locator('[data-testid="system-map-overview-toggle"]')
    await expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await expect(page.locator('[data-testid="system-map-overview-content"]')).toContainText(
      'System overview is collapsed',
    )
    // Discovery is exactly what an unpublished org needs, so it stays visible.
    await expect(page.locator('[data-testid="dashboard-system-discover"]')).toBeVisible()
  })

  test('system overview card is absent when System Maps is not enabled', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    // No `system_maps` flag and no permission: the dashboard must not render the
    // System Overview card at all.
    await page.route('**/api/v1/plan*', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ license: { tier: 'community' }, flags: [] }),
      })
    })
    await page.route('**/api/v1/auth/permissions*', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ permissions: [] }) })
    })
    await page.reload()

    await expect(page.locator('[data-testid="dashboard-title"]')).toBeVisible()
    await expect(page.locator('[data-testid="dashboard-system-overview"]')).toHaveCount(0)
  })
})
