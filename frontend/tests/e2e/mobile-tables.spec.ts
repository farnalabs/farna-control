import { test, expect, loginAsAdmin } from './setup/fixtures'

// FAR-2539: on a 390px phone the Dashboard Team Breakdown table must not paint
// outside its card, and the Risk Assessment empty state must render at the
// container width (outside the horizontally scrolling area, no fade mask).
// Deterministic layout checks: no retries.
test.describe.configure({ retries: 0 })
test.use({ viewport: { width: 390, height: 844 } })

test.describe('Mobile tables (390px)', () => {
  test('Dashboard Team Breakdown stays inside its card', { tag: '@regression' }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'needs the mocked dashboard summary with seeded teams')
    await loginAsAdmin(page, env)
    // The shared local mock serves a populated summary with a seeded team.
    await page.goto('/')

    const scroll = page.locator('[data-scroll="team-breakdown"]')
    await expect(scroll).toBeVisible()

    const geometry = await page.evaluate(() => {
      const el = document.querySelector('[data-scroll="team-breakdown"]') as HTMLElement
      const cardEl = el.closest('.card') as HTMLElement
      const cardRect = cardEl.getBoundingClientRect()
      const scrollRect = el.getBoundingClientRect()
      return {
        overflowX: getComputedStyle(el).overflowX,
        scrollRight: scrollRect.right,
        cardRight: cardRect.right,
        viewport: window.innerWidth,
        docScrollWidth: document.documentElement.scrollWidth,
      }
    })
    expect(geometry.overflowX).toBe('auto')
    expect(geometry.scrollRight).toBeLessThanOrEqual(geometry.cardRight + 0.5)
    expect(geometry.docScrollWidth).toBeLessThanOrEqual(geometry.viewport)
  })

  test('Risk Assessment empty state is container width with no fade mask', { tag: '@regression' }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'needs the mocked empty risk-assessment evidence response')
    await loginAsAdmin(page, env)
    await page.route('**/api/v1/evidence/risk-assessment*', (route) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ summary: { total_rows: 0, action_counts: {}, facet_degraded: false }, rows: [] }),
    }))
    await page.goto('/risk-assessment')

    const empty = page.locator('[data-testid="risk-assessment-empty"]')
    await expect(empty).toBeVisible()

    const geometry = await page.evaluate(() => {
      const emptyEl = document.querySelector('[data-testid="risk-assessment-empty"]') as HTMLElement
      const root = emptyEl.closest('[data-testid="risk-assessment-table"]') as HTMLElement
      return {
        emptyWidth: emptyEl.getBoundingClientRect().width,
        rootWidth: root.getBoundingClientRect().width,
        insideScroll: emptyEl.closest('.overflow-x-auto') !== null,
        fadeCount: root.querySelectorAll('[data-table-part="scroll-fade"]').length,
        docScrollWidth: document.documentElement.scrollWidth,
        viewport: window.innerWidth,
      }
    })
    expect(geometry.insideScroll).toBe(false)
    expect(geometry.fadeCount).toBe(0)
    expect(geometry.emptyWidth).toBeLessThanOrEqual(geometry.rootWidth + 0.5)
    expect(geometry.docScrollWidth).toBeLessThanOrEqual(geometry.viewport)
  })
})

// FAR-2584: the unified table system. Every table scrolls horizontally inside
// the same hairline shell while the page keeps scrolling vertically, uses the
// same 16px cell padding, and lists sit at the page width with the page rhythm
// between a filter card and the table.
test.describe('Table system (FAR-2584)', () => {
  test('the page still scrolls when the wheel is over a horizontally scrolling table', { tag: '@regression' }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'needs the mocked dashboard summary with seeded teams')
    await loginAsAdmin(page, env)
    await page.setViewportSize({ width: 390, height: 420 })
    await page.goto('/')

    const scroll = page.locator('[data-scroll="team-breakdown"]')
    await expect(scroll).toBeVisible()

    const behaviour = await scroll.evaluate((el) => {
      const style = getComputedStyle(el)
      return {
        x: style.overscrollBehaviorX,
        y: style.overscrollBehaviorY,
      }
    })
    // Only horizontal overscroll is contained. The both-axes shorthand here
    // swallowed the vertical wheel and froze the page.
    expect(behaviour.x).toBe('contain')
    expect(behaviour.y).toBe('auto')

    const before = await page.evaluate(() => {
      const main = document.querySelector('main[data-main-landmark]') as HTMLElement
      return main.scrollTop
    })
    await scroll.hover()
    await page.mouse.wheel(0, 600)
    await page.waitForFunction(
      (top) => (document.querySelector('main[data-main-landmark]') as HTMLElement).scrollTop > top,
      before,
    )
  })

  test('Team Breakdown cells carry the canonical 16px horizontal padding', { tag: '@regression' }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'needs the mocked dashboard summary with seeded teams')
    await loginAsAdmin(page, env)
    await page.goto('/')

    // The dashboard summary loads asynchronously, so the table (and its
    // seeded team row) is not in the DOM immediately after navigation.
    const scroll = page.locator('[data-scroll="team-breakdown"]')
    await expect(scroll).toBeVisible()
    await expect(scroll.locator('tbody tr td').first()).toBeVisible()

    const padding = await page.evaluate(() => {
      const header = document.querySelector('[data-scroll="team-breakdown"] thead th') as HTMLElement
      const cell = document.querySelector('[data-scroll="team-breakdown"] tbody tr td') as HTMLElement
      return {
        headerLeft: getComputedStyle(header).paddingLeft,
        cellLeft: getComputedStyle(cell).paddingLeft,
      }
    })
    expect(padding.headerLeft).toBe('16px')
    expect(padding.cellLeft).toBe('16px')
  })

  test('Notification Preferences uses the full page width', { tag: '@regression' }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'needs the mocked notification-preferences response')
    await loginAsAdmin(page, env)
    await page.goto('/settings/notifications')

    // The route component is lazily imported and loads its preferences
    // asynchronously; the card is not in the DOM immediately after navigation.
    await expect(page.locator('.page-wide .card').first()).toBeVisible()

    const widths = await page.evaluate(() => {
      const pageEl = document.querySelector('.page-wide') as HTMLElement
      const card = document.querySelector('.page-wide .card') as HTMLElement
      return {
        page: pageEl.getBoundingClientRect().width,
        card: card.getBoundingClientRect().width,
        pagePadding: parseFloat(getComputedStyle(pageEl).paddingLeft),
      }
    })
    // The card spans the page content box (page width minus its two paddings),
    // not a narrower max-w-3xl column.
    expect(Math.abs(widths.card - (widths.page - 2 * widths.pagePadding))).toBeLessThanOrEqual(1)
  })

  test('Audit Log keeps the page rhythm between the filter card and the table', { tag: '@regression' }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'needs the mocked audit ledger response')
    await loginAsAdmin(page, env)
    // The generic local mock returns an empty ledger, which renders the empty
    // state instead of the table. Seed one event so the table shell exists.
    await page.route('**/api/v1/admin/audit**', (route) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        items: [{
          id: 'evt-1',
          event_type: 'pipeline.created',
          actor_user_id: '1',
          created_at: '2026-07-07T09:15:00Z',
          resource_type: 'pipeline',
          resource_id: '00000000-0000-0000-0000-000000000001',
          payload_json: {},
          request_id: null,
          previous_hash: null,
        }],
        total: 1,
        next_cursor: null,
      }),
    }))
    await page.goto('/admin/audit')

    // The route component is lazily imported and loads its ledger
    // asynchronously; wait for the table before measuring the gap.
    await expect(page.locator('#admin-audit-source-org .table-wrapper')).toBeVisible()

    const gap = await page.evaluate(() => {
      const panel = document.querySelector('#admin-audit-source-org') as HTMLElement
      const card = panel.querySelector('.card') as HTMLElement
      const table = panel.querySelector('.table-wrapper') as HTMLElement
      return table.getBoundingClientRect().top - card.getBoundingClientRect().bottom
    })
    expect(gap).toBeGreaterThanOrEqual(12)
  })
})
