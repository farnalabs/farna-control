import type { Locator, Page } from '@playwright/test'
import { test, expect, setupLocalMockApi, mintMockAccessToken } from './setup/fixtures'

// FAR-2538 - the mobile header/actions pattern (docs/frontend-mobile-layout.md)
// on the MCP server-status row, the Triggers pause-all card and the Environment
// Profiles toolbar. At 390px: no horizontal overflow, button labels on one line,
// actions stacked below the content. At 1280px the geometry must equal the
// pre-change layout exactly (DESKTOP values below were measured on origin/main).
// Dashboard and Pipelines were already fixed by FAR-2537 and are not covered here.
test.describe.configure({ retries: 0 })

const MOCK_ACCESS_TOKEN = mintMockAccessToken()
const MOCK_REFRESH_TOKEN = 'mock-refresh-token-for-e2e-tests'
const APP_MOUNT_TIMEOUT_MS = 30_000
const MOBILE = { width: 390, height: 844 }
const DESKTOP = { width: 1280, height: 800 }
const JSON_HEADERS = { contentType: 'application/json' }
const LONG_MCP_URL = 'https://mcp.modulo.run/very/long/server/url/that/must/wrap/instead/of/overlapping'
const SHORT_MCP_URL = 'https://mcp.modulo.run'

// Desktop geometry measured on origin/main (1280x800), in CSS px.
const DESKTOP_MCP = { urlToActionsGap: 0, actionsToRowRight: 16 }
const DESKTOP_TRIGGERS = { labelWidth: 861.9, labelToButtonGap: 0, buttonToCardRight: 0 }
const DESKTOP_PROFILES = { buttonToRowRight: 0, centreOffset: 0 }

const PROFILE = {
  id: 'p1',
  name: 'Default profile',
  description: 'A profile',
  organisation_id: '1',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

async function boot(page: Page, path: string, mcpUrl = LONG_MCP_URL): Promise<void> {
  test.setTimeout(90_000)
  await setupLocalMockApi(page)
  await page.route('**/api/v1/api-keys**', (route) =>
    route.fulfill({
      status: 200,
      ...JSON_HEADERS,
      body: JSON.stringify(route.request().url().includes('/mcp-config') ? { mcp_url: mcpUrl } : []),
    }),
  )
  await page.route('**/api/v1/mcp/oauth/clients**', (route) => route.fulfill({ status: 200, ...JSON_HEADERS, body: '[]' }))
  await page.route('**/api/v1/environment-profiles**', (route) =>
    route.fulfill({ status: 200, ...JSON_HEADERS, body: JSON.stringify({ items: [PROFILE], total: 1 }) }),
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

async function box(locator: Locator) {
  await locator.waitFor({ state: 'visible' })
  const b = await locator.boundingBox()
  expect(b).not.toBeNull()
  return b!
}

/** The button renders its label on a single line. */
async function expectSingleLine(button: Locator): Promise<void> {
  const b = await box(button)
  const lineHeight = await button.evaluate((el) => parseFloat(getComputedStyle(el).lineHeight) || 20)
  expect(b.height).toBeLessThan(lineHeight * 2 + 8)
}

const round = (n: number) => Math.round(n * 10) / 10

function mcpParts(page: Page) {
  const copy = page.locator('[data-testid="settings-mcp-copy-url"]')
  return {
    copy,
    row: copy.locator('xpath=../..'),
    urlBlock: copy.locator('xpath=../../div[1]'),
    actions: copy.locator('xpath=..'),
  }
}

function triggersParts(page: Page) {
  const pause = page.locator('[data-testid="settings-triggers-pause-all"]')
  return { pause, label: pause.locator('xpath=preceding-sibling::div[1]'), card: pause.locator('xpath=..') }
}

function profilesParts(page: Page) {
  const button = page.locator('[data-testid="envprofile-list-new"]')
  return { button, search: page.locator('[data-testid="envprofile-list-search"]'), row: button.locator('xpath=..') }
}

test.describe('Mobile pages 390px (FAR-2538)', () => {
  test.use({ viewport: MOBILE, hasTouch: true, isMobile: true })

  test('MCP server status: long URL stays inside the card and the buttons sit below it', { tag: ['@regression', '@mobile'] }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'needs local mock API')
    await boot(page, '/settings/mcp')
    const { copy, urlBlock, actions } = mcpParts(page)
    const url = await box(urlBlock)
    const actionsBox = await box(actions)
    expect(await overflow(page)).toBeLessThanOrEqual(0)
    expect(actionsBox.y).toBeGreaterThanOrEqual(url.y + url.height - 1)
    expect(url.x + url.width).toBeLessThanOrEqual(MOBILE.width)
    await expectSingleLine(copy)
  })

  test('Triggers: Pause all stacks below its label and keeps its text on one line', { tag: ['@regression', '@mobile'] }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'needs local mock API')
    await boot(page, '/settings/triggers')
    const { pause, label } = triggersParts(page)
    const labelBox = await box(label)
    const pauseBox = await box(pause)
    expect(await overflow(page)).toBeLessThanOrEqual(0)
    expect(pauseBox.y).toBeGreaterThanOrEqual(labelBox.y + labelBox.height - 1)
    expect(pauseBox.x + pauseBox.width).toBeLessThanOrEqual(MOBILE.width)
    await expectSingleLine(pause)
  })

  test('Environment Profiles: New Profile sits below the search and keeps its label on one line', { tag: ['@regression', '@mobile'] }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'needs local mock API')
    await boot(page, '/admin/runners/profiles')
    const { button, search } = profilesParts(page)
    const searchBox = await box(search)
    const buttonBox = await box(button)
    expect(await overflow(page)).toBeLessThanOrEqual(0)
    expect(buttonBox.y).toBeGreaterThanOrEqual(searchBox.y + searchBox.height - 1)
    await expectSingleLine(button)
  })
})

test.describe('Desktop 1280px is unchanged (FAR-2538)', () => {
  test.use({ viewport: DESKTOP })

  test('MCP server status row: no gap added, buttons flush right', { tag: ['@regression'] }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'needs local mock API')
    await boot(page, '/settings/mcp', SHORT_MCP_URL)
    const { row, urlBlock, actions } = mcpParts(page)
    const r = await box(row)
    const u = await box(urlBlock)
    const a = await box(actions)
    expect(a.y).toBeLessThan(u.y + u.height)
    expect({
      urlToActionsGap: round(a.x - (u.x + u.width)),
      actionsToRowRight: round(r.x + r.width - (a.x + a.width)),
    }).toEqual(DESKTOP_MCP)
  })

  test('Triggers pause-all card: label-to-button spacing unchanged', { tag: ['@regression'] }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'needs local mock API')
    await boot(page, '/settings/triggers')
    const { pause, label, card } = triggersParts(page)
    const l = await box(label)
    const b = await box(pause)
    const c = await box(card)
    expect(b.y).toBeLessThan(l.y + l.height)
    expect({
      labelWidth: round(l.width),
      labelToButtonGap: round(b.x - (l.x + l.width)),
      buttonToCardRight: round(c.x + c.width - (b.x + b.width)),
    }).toEqual(DESKTOP_TRIGGERS)
  })

  test('Environment Profiles toolbar: New Profile stays right-aligned on the search row', { tag: ['@regression'] }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'needs local mock API')
    await boot(page, '/admin/runners/profiles')
    const { button, search, row } = profilesParts(page)
    const s = await box(search)
    const b = await box(button)
    const r = await box(row)
    expect({
      buttonToRowRight: round(r.x + r.width - (b.x + b.width)),
      centreOffset: round(b.y + b.height / 2 - (s.y + s.height / 2)),
    }).toEqual(DESKTOP_PROFILES)
  })
})
