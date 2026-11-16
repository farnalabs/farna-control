import { test, expect, loginAsAdmin } from './setup/fixtures'

/**
 * FAR-2541: /settings/sso provider card must not squeeze its text column on a
 * phone. Providers are mocked at the network layer (GET only), so no real
 * provider is created and the spec is safe against any target.
 */

const LONG_CLIENT_ID = `${'1234567890'.repeat(12)}.apps.googleusercontent.com`

const MOCK_PROVIDER = {
  id: 'sso-mobile-1',
  provider_type: 'oidc',
  name: 'Google',
  client_id: LONG_CLIENT_ID,
  discovery_url: 'https://accounts.google.com/.well-known/openid-configuration',
  metadata_url: null,
  metadata_xml: null,
  entity_id: null,
  scopes: ['openid', 'email'],
  auto_provision: false,
  default_role: 'runner',
  enabled: true,
  allowed_domains: [],
  preset: 'google',
}

test.describe('Settings SSO provider card layout', { tag: '@regression' }, () => {
  test.beforeEach(async ({ page, env }) => {
    await loginAsAdmin(page, env)
    // Registered after login so it takes precedence over earlier routes.
    await page.route('**/api/v1/admin/sso/providers', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback()
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([MOCK_PROVIDER]) })
    })
  })

  test('390px phone: stacked card, no horizontal overflow, readable details', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/settings/sso')
    const card = page.locator('[data-sso-layout="card"]')
    await expect(card).toBeVisible({ timeout: 15000 })

    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
    expect(overflow).toBeLessThanOrEqual(0)

    const details = page.locator('[data-sso-layout="details"]')
    await expect(details).toBeVisible()
    const box = await details.boundingBox()
    expect(box?.width ?? 0).toBeGreaterThan(200)

    const direction = await card.evaluate((el) => getComputedStyle(el).flexDirection)
    expect(direction).toBe('column')

    const cardBox = await card.boundingBox()
    const actionsBox = await page.locator('[data-sso-layout="actions"]').boundingBox()
    expect((actionsBox?.x ?? 0) + (actionsBox?.width ?? 0)).toBeLessThanOrEqual((cardBox?.x ?? 0) + (cardBox?.width ?? 0) + 1)
  })

  test('1280px desktop: card stays a horizontal row', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 })
    await page.goto('/settings/sso')
    const card = page.locator('[data-sso-layout="card"]')
    await expect(card).toBeVisible({ timeout: 15000 })

    const direction = await card.evaluate((el) => getComputedStyle(el).flexDirection)
    expect(direction).toBe('row')
    await expect(page.locator('[data-sso-layout="details"]')).toBeHidden()
  })
})
