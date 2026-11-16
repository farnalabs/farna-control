import type { Page } from '@playwright/test'
import { test, expect, loginAsAdmin, setupLocalMockApi, mintMockAccessToken } from './setup/fixtures'
import { getTarget, type TestEnv } from './setup/env'

// FAR-2540 - mobile overlays at 390x844:
//  1. the Notifications popover has a visible close (X) button that dismisses it
//     (it used to close only by tapping behind it);
//  2. the MCP "Configuration Snippets" Client select panel renders fully inside
//     the viewport (never off-screen / detached).
test.describe.configure({ retries: 0 })
test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })

if (getTarget() !== 'local') {
  test.use({ storageState: 'storageState-staging.json' })
}

const MOCK_ACCESS_TOKEN = mintMockAccessToken()
const MOCK_REFRESH_TOKEN = 'mock-refresh-token-for-e2e-tests'
const APP_MOUNT_TIMEOUT_MS = 30_000
const MIN_TAP_TARGET_PX = 44

async function boot(page: Page, env: TestEnv, path: string): Promise<void> {
  test.setTimeout(90_000)
  if (env.name === 'local') {
    await setupLocalMockApi(page)
    await page.addInitScript(([token, refresh]) => {
      localStorage.setItem('modulo_access_token', token)
      localStorage.setItem('modulo_refresh_token', refresh)
    }, [MOCK_ACCESS_TOKEN, MOCK_REFRESH_TOKEN] as [string, string])
    await page.goto(path, { waitUntil: 'commit' })
  } else {
    await page.goto(path, { waitUntil: 'commit' })
    if (page.url().includes('/login')) {
      await loginAsAdmin(page, env)
      await page.goto(path)
    }
  }
  await page.waitForFunction(
    () => (document.querySelector('#app')?.children.length ?? 0) > 0,
    undefined,
    { timeout: APP_MOUNT_TIMEOUT_MS },
  )
}

test.describe('mobile overlays (FAR-2540)', () => {
  test('notifications popover has a visible close button that dismisses it', { tag: ['@regression', '@mobile'] }, async ({ page, env }) => {
    await boot(page, env, '/')

    const bell = page.locator('[data-testid="notification-bell-button"]:visible').first()
    await bell.click()

    const dropdown = page.locator('[data-testid="notification-dropdown"]')
    await expect(dropdown).toBeVisible()

    const close = page.locator('[data-testid="notification-dropdown-close"]')
    await expect(close).toBeVisible()
    await expect(close).toHaveAttribute('aria-label', 'Close notifications')
    const box = await close.boundingBox()
    expect(box?.width).toBeGreaterThanOrEqual(MIN_TAP_TARGET_PX)
    expect(box?.height).toBeGreaterThanOrEqual(MIN_TAP_TARGET_PX)

    await close.click()
    await expect(dropdown).toHaveCount(0)

    // Escape still dismisses.
    await bell.click()
    await expect(dropdown).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(dropdown).toHaveCount(0)
  })

  test('MCP Client select panel stays fully inside the viewport', { tag: ['@regression', '@mobile'] }, async ({ page, env }) => {
    await boot(page, env, '/settings/mcp')

    const select = page.locator('#settingsmcpview-client')
    await select.scrollIntoViewIfNeeded()
    await select.click()

    const panel = page.locator('.p-select-overlay')
    await expect(panel).toBeVisible()
    const box = await panel.boundingBox()
    const viewport = page.viewportSize()
    expect(box).not.toBeNull()
    expect(box!.x).toBeGreaterThanOrEqual(0)
    expect(box!.x + box!.width).toBeLessThanOrEqual(viewport!.width)
  })
})
