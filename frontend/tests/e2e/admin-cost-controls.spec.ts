import { test, expect, loginAsAdmin } from './setup/fixtures'
import type { Page } from '@playwright/test'

// The Cost Controls tab renders titled DataCards. BaseCard owns the padding for
// both the header and the content, so a card left at `padding="none"` (p-0)
// strips the inset from its CardHeader title as well as its body (FAR-2694).
async function mockLocalCostRoutes(page: Page) {
  await page.route('**/api/v1/admin/costs*', (route) => {
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ period: 'month', group_by: 'team', items: [] }) })
  })
  await page.route('**/api/v1/admin/costs/controls*', (route) => {
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ teams: [], budget: 1000, currency: 'USD', billing_period: 'monthly', alert_thresholds: [50, 75, 90], circuit_breaker_enabled: false }) })
  })
  await page.route('**/api/v1/admin/costs/limits*', (route) => {
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ organisation_id: 'test-org', org_daily_spend_limit: 100, team_limits: [] }) })
  })
}

test.describe('Admin Spend Limits', { tag: "@regression" }, () => {
  test('renders the Spend Limits page', { tag: "@regression" }, async ({ page, env }) => {
    if (env.name === 'local') {
      await page.route('**/api/v1/admin/costs/limits*', (route) => {
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ organisation_id: 'test-org', org_daily_spend_limit: 100, team_limits: [] }) })
      })
    }
    await loginAsAdmin(page, env)
    await page.goto('/admin/costs/limits', { timeout: 60000 })
    await expect(page.locator('h1')).toContainText('Spend Limits')
    await expect(
      page.getByTestId('admin-spend-limits-org-limit').or(page.getByTestId('feature-gate-disabled'))
    ).toBeVisible({ timeout: 15000 })
  })
})

test.describe('Admin Cost Controls', { tag: "@regression" }, () => {
  test('renders the Cost Controls page', { tag: "@regression" }, async ({ page, env }) => {
    if (env.name === 'local') {
      await mockLocalCostRoutes(page)
    }
    await loginAsAdmin(page, env)
    await page.goto('/admin/costs/controls', { timeout: 60000 })
    await expect(page.locator('h1')).toContainText('Cost Controls')
    await expect(
      page.getByTestId('cc-total-spend').or(page.getByTestId('feature-gate-disabled'))
    ).toBeVisible({ timeout: 15000 })
  })

  test('insets the titled card headers inside their cards (FAR-2694)', { tag: "@regression" }, async ({ page, env }) => {
    if (env.name === 'local') {
      await mockLocalCostRoutes(page)
    }
    await loginAsAdmin(page, env)
    await page.goto('/admin/costs/controls', { timeout: 60000 })
    await expect(page.locator('h1')).toContainText('Cost Controls')

    const gatedOut = page.getByTestId('feature-gate-disabled')
    await expect(
      page.getByTestId('cc-total-spend').or(gatedOut)
    ).toBeVisible({ timeout: 15000 })

    if (await gatedOut.isVisible()) {
      // The target has the feature switched off, so no card renders to measure
      // and the gate-disabled alternative path stays green. The local mock
      // target always grants admin_cost_controls (MOCK_PLAN carries the
      // cost_governance grant), so a local run must never legitimately reach
      // this branch - the padding assertion below is its regression guard.
      expect(env.name, 'the local mock target grants admin_cost_controls, so the card must render').not.toBe('local')
      return
    }

    for (const title of ['Budget Overview', 'Team Budgets']) {
      const heading = page.getByRole('heading', { name: title, exact: true })
      await expect(heading).toBeVisible()
      const card = page.locator('.card-base', { has: heading })
      await expect(card, `"${title}" must render inside exactly one card`).toHaveCount(1)

      const headingBox = await heading.boundingBox()
      const cardBox = await card.boundingBox()
      expect(headingBox, `the "${title}" heading must have a measurable box`).not.toBeNull()
      expect(cardBox, `the "${title}" card must have a measurable box`).not.toBeNull()
      // The design-system card padding is p-4 (16px). A card left at p-0 puts
      // the heading flush against the card edge (a 0px inset).
      expect(
        headingBox!.x - cardBox!.x,
        `the "${title}" heading must be inset from the card edge by the card padding`
      ).toBeGreaterThan(8)
    }
  })
})
