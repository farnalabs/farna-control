import type { Page } from '@playwright/test'
import { test, expect, setupLocalMockApi, mintMockAccessToken } from './setup/fixtures'

// FAR-2538 - the mobile header/actions pattern (docs/frontend-mobile-layout.md)
// on Dashboard, Pipelines, MCP settings, Triggers settings and Environment
// Profiles. At 390px: no horizontal overflow, button labels on one line, header
// text not squeezed or overlapped by its actions. At 1280px the rows stay rows.
test.describe.configure({ retries: 0 })

const MOCK_ACCESS_TOKEN = mintMockAccessToken()
const MOCK_REFRESH_TOKEN = 'mock-refresh-token-for-e2e-tests'
const APP_MOUNT_TIMEOUT_MS = 30_000
const MOBILE = { width: 390, height: 844 }
const DESKTOP = { width: 1280, height: 800 }
const JSON_HEADERS = { contentType: 'application/json' }
const LONG_MCP_URL = 'https://mcp.modulo.run/very/long/server/url/that/must/wrap/instead/of/overlapping'

const PROFILE = {
  id: 'p1',
  name: 'Default profile',
  description: 'A profile',
  organisation_id: '1',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

async function boot(page: Page, path: string): Promise<void> {
  test.setTimeout(90_000)
  await setupLocalMockApi(page)
  await page.route('**/api/v1/api-keys**', (route) =>
    route.fulfill({
      status: 200,
      ...JSON_HEADERS,
      body: JSON.stringify(route.request().url().includes('/mcp-config') ? { mcp_url: LONG_MCP_URL } : []),
    }),
  )
  await page.route('**/api/v1/mcp/oauth/clients**', (route) => route.fulfill({ status: 200, ...JSON_HEADERS, body: '[]' }))
  await page.route('**/api/v1/environment-profiles**', (route) =>
    route.fulfill({ status: 200, ...JSON_HEADERS, body: JSON.stringify({ items: [PROFILE], total: 1 }) }),
  )
  // The dashboard System Overview card only renders for a caller with System Maps access.
  await page.route('**/api/v1/auth/permissions', (route) =>
    route.fulfill({
      status: 200,
      ...JSON_HEADERS,
      body: JSON.stringify({ permissions: ['system_maps.read', 'system_maps_access'] }),
    }),
  )
  await page.route('**/api/v1/system-maps**', (route) =>
    route.fulfill({ status: 200, ...JSON_HEADERS, body: JSON.stringify({ items: [], total: 0, page: 1, page_size: 20 }) }),
  )
  await page.addInitScript(([token, refresh]) => {
    localStorage.setItem('modulo_access_token', token)
    localStorage.setItem('modulo_refresh_token', refresh)
  }, [MOCK_ACCESS_TOKEN, MOCK_REFRESH_TOKEN] as [string, string])
  await page.goto(path, { waitUntil: 'commit' })
  await page.waitForFunction(() => (document.querySelector('#app')?.children.length ?? 0) > 0, undefined, {
    timeout: APP_MOUNT_TIMEOUT_MS,
  })
}

async function overflow(page: Page): Promise<number> {
  return page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
}

/** The button with this label renders it on a single line. */
async function expectSingleLine(page: Page, name: RegExp): Promise<void> {
  const button = page.getByRole('button', { name }).or(page.getByRole('link', { name })).first()
  await button.scrollIntoViewIfNeeded()
  await expect(button).toBeVisible()
  const box = await button.boundingBox()
  const lineHeight = await button.evaluate((el) => parseFloat(getComputedStyle(el).lineHeight) || 20)
  expect(box?.height ?? 0).toBeLessThan(lineHeight * 2 + 8)
}

async function boxOf(page: Page, selector: string) {
  const box = await page.locator(selector).first().boundingBox()
  expect(box).not.toBeNull()
  return box!
}

type PageCase = { path: string; ready: string; button: RegExp; title: string; actions: string }

const CASES: Record<string, PageCase> = {
  dashboard: {
    path: '/',
    ready: '[data-testid="dashboard-system-overview"]',
    button: /discover my system/i,
    title: '[data-testid="dashboard-system-overview"] h2',
    actions: '[data-testid="dashboard-system-overview"] [data-testid="card-header-actions"]',
  },
  pipelines: {
    path: '/pipelines',
    ready: '[data-testid="pipeline-list-new-pipeline"]',
    button: /new pipeline/i,
    title: 'h1',
    actions: '[data-testid="page-header-right"]',
  },
  mcp: {
    path: '/settings/mcp',
    ready: '[data-testid="settings-mcp-create-key"]',
    button: /create mcp api key/i,
    title: '.settings-mcp-api-keys-card h2',
    actions: '.settings-mcp-api-keys-card [data-testid="card-header-actions"]',
  },
  profiles: {
    path: '/admin/runners/profiles',
    ready: '[data-testid="envprofile-list-new"]',
    button: /new profile/i,
    title: '[data-testid="envprofile-list-search"]',
    actions: '[data-testid="envprofile-list-new"]',
  },
}

test.describe('Mobile pages 390px (FAR-2538)', () => {
  test.use({ viewport: MOBILE, hasTouch: true, isMobile: true })

  for (const [name, c] of Object.entries(CASES)) {
    test(`${name}: no overflow, button label on one line, actions do not cover the title`, { tag: ['@regression', '@mobile'] }, async ({ page, env }) => {
      test.skip(env.name !== 'local', 'needs local mock API')
      await boot(page, c.path)
      await page.locator(c.ready).first().waitFor({ state: 'visible' })
      expect(await overflow(page)).toBeLessThanOrEqual(0)
      await expectSingleLine(page, c.button)
      const title = await boxOf(page, c.title)
      const actions = await boxOf(page, c.actions)
      // Plain vertical stack: the actions start below the title/search block.
      expect(actions.y).toBeGreaterThanOrEqual(title.y + title.height - 1)
    })
  }

  test('MCP server status: URL stays inside the card and the buttons sit below it', { tag: ['@regression', '@mobile'] }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'needs local mock API')
    await boot(page, '/settings/mcp')
    await page.locator('[data-testid="settings-mcp-copy-url"]').waitFor({ state: 'visible' })
    expect(await overflow(page)).toBeLessThanOrEqual(0)
    const url = await boxOf(page, '[data-mobile="mcp-server-url"]')
    const copy = await boxOf(page, '[data-testid="settings-mcp-copy-url"]')
    expect(copy.y).toBeGreaterThanOrEqual(url.y + url.height - 1)
    expect(url.x + url.width).toBeLessThanOrEqual(MOBILE.width)
  })

  test('Triggers: Pause all keeps its label on one line inside the card', { tag: ['@regression', '@mobile'] }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'needs local mock API')
    await boot(page, '/settings/triggers')
    await page.locator('[data-testid="settings-triggers-pause-all"]').waitFor({ state: 'visible' })
    expect(await overflow(page)).toBeLessThanOrEqual(0)
    await expectSingleLine(page, /pause all/i)
    const box = await boxOf(page, '[data-testid="settings-triggers-pause-all"]')
    expect(box.x + box.width).toBeLessThanOrEqual(MOBILE.width)
  })
})

test.describe('Desktop 1280px keeps rows (FAR-2538)', () => {
  test.use({ viewport: DESKTOP })

  for (const [name, c] of Object.entries(CASES)) {
    test(`${name}: title and actions share a row`, { tag: ['@regression'] }, async ({ page, env }) => {
      test.skip(env.name !== 'local', 'needs local mock API')
      await boot(page, c.path)
      await page.locator(c.ready).first().waitFor({ state: 'visible' })
      expect(await overflow(page)).toBeLessThanOrEqual(0)
      const title = await boxOf(page, c.title)
      const actions = await boxOf(page, c.actions)
      expect(actions.y).toBeLessThan(title.y + title.height)
    })
  }

  test('MCP server status stays a row', { tag: ['@regression'] }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'needs local mock API')
    await boot(page, '/settings/mcp')
    await page.locator('[data-testid="settings-mcp-copy-url"]').waitFor({ state: 'visible' })
    const url = await boxOf(page, '[data-mobile="mcp-server-url"]')
    const copy = await boxOf(page, '[data-testid="settings-mcp-copy-url"]')
    expect(copy.y).toBeLessThan(url.y + url.height)
  })

  test('Triggers pause card stays a row', { tag: ['@regression'] }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'needs local mock API')
    await boot(page, '/settings/triggers')
    const pause = page.locator('[data-testid="settings-triggers-pause-all"]')
    await pause.waitFor({ state: 'visible' })
    const label = await boxOf(page, '[data-mobile="pause-all-label"]')
    const btn = await boxOf(page, '[data-testid="settings-triggers-pause-all"]')
    expect(btn.y).toBeLessThan(label.y + label.height)
  })
})
