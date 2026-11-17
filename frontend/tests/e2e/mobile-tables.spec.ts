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
