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
    await loginAsAdmin(page, env)
    // Register the limits fixture AFTER loginAsAdmin: Playwright runs route
    // handlers in reverse registration order, so the `**/api/v1/**` catch-all
    // that loginAsAdmin installs would otherwise win and this mock would
    // never run (FAR-2694).
    if (env.name === 'local') {
      await page.route('**/api/v1/admin/costs/limits*', (route) => {
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ organisation_id: 'test-org', org_daily_spend_limit: 100, team_limits: [] }) })
      })
    }
    await page.goto('/admin/costs/limits', { timeout: 60000 })
    await expect(page.locator('h1')).toContainText('Spend Limits')
    const orgLimitInput = page.getByTestId('admin-spend-limits-org-limit')
    if (env.name === 'local') {
      // Prove the fixture above actually won over the `**/api/v1/**` catch-all
      // that `loginAsAdmin` installs: the org-limit input must carry the
      // fixture's `org_daily_spend_limit` (100). With the specific route dead,
      // the catch-all answers `{items,total}` and the input renders EMPTY, so a
      // bare visibility assertion would pass with the mock reorder reverted
      // (FAR-2694 review).
      await expect(orgLimitInput).toHaveValue('100', { timeout: 15000 })
    } else {
      await expect(
        orgLimitInput.or(page.getByTestId('feature-gate-disabled'))
      ).toBeVisible({ timeout: 15000 })
    }
  })
})

test.describe('Admin Cost Controls', { tag: "@regression" }, () => {
  test('renders the Cost Controls page', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    // Register the cost fixtures AFTER loginAsAdmin: Playwright runs route
    // handlers in reverse registration order, so the `**/api/v1/**` catch-all
    // that loginAsAdmin installs would otherwise win and these mocks would
    // never run (FAR-2694).
    if (env.name === 'local') {
      await mockLocalCostRoutes(page)
    }
    await page.goto('/admin/costs/controls', { timeout: 60000 })
    await expect(page.locator('h1')).toContainText('Cost Controls')
    if (env.name === 'local') {
      // Prove the controls fixture won over the catch-all: the budget card
      // must render the fixture's `budget` (1000 USD), not the catch-all
      // default. A bare `cc-total-spend` visibility check passes either way
      // because the card renders regardless of the payload (FAR-2694 review).
      await expect(page.getByTestId('cc-budget')).toContainText('1,000', { timeout: 15000 })
    } else {
      await expect(
        page.getByTestId('cc-total-spend').or(page.getByTestId('feature-gate-disabled'))
      ).toBeVisible({ timeout: 15000 })
    }
  })

  test('insets the titled card headers inside their cards (FAR-2694)', { tag: "@regression" }, async ({ page, env }) => {
    await loginAsAdmin(page, env)
    // Register the cost fixtures AFTER loginAsAdmin: Playwright runs route
    // handlers in reverse registration order, so the `**/api/v1/**` catch-all
    // that loginAsAdmin installs would otherwise win and these mocks would
    // never run (FAR-2694).
    if (env.name === 'local') {
      await mockLocalCostRoutes(page)
    }
    await page.goto('/admin/costs/controls', { timeout: 60000 })
    await expect(page.locator('h1')).toContainText('Cost Controls')

    // Settle the page first: a rendered card OR the gate-disabled notice means
    // the plan/feature state resolved.
    const gatedOut = page.getByTestId('feature-gate-disabled')
    await expect(
      page.getByTestId('cc-total-spend').or(gatedOut)
    ).toBeVisible({ timeout: 15000 })

    // FeatureGate renders the same slot, dimmed, when the feature is switched
    // off (`show-disabled`): `<slot />` sits inside an opacity-40 wrapper and
    // the notice is an absolutely-positioned overlay, so the cards and their
    // headings DO render and their geometry stays measurable. The inset
    // assertion therefore runs in both the enabled and the operator-disabled
    // state instead of skipping the disabled one (a skipped assertion would
    // let the operator kill-switch hide a padding regression).
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
