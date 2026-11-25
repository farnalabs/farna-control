// FAR-1913 — this spec's name promises a contrast gate; until now the shared
// `filterViolations()` allowlist dropped `color-contrast` from every scan, so it
// could not see one. That rule id is no longer tolerated: the failed-status
// badge it found (destructive text on a destructive tint) is fixed via the
// `--destructive-text` token, and every destructive-text-on-tint pairing in the
// app was swept to it — including the tinted delete-confirm blocks and the
// run-detail diagnostics cards, which render only after interaction and so are
// not reached by the route scans below. The only entry still filtered is
// `aria-hidden-focus` (documented, PrimeVue-internal) — see tests/e2e/setup/wcag.ts.
import { test, expect, systemAdminJwt } from './setup/fixtures'
import AxeBuilder from '@axe-core/playwright'
import { WCAG_TAGS, filterViolations } from './setup/wcag'

// Flipping the theme triggers PrimeVue's 0.2s colour transition on every
// control. A cross-fade between the two themes passes through a low-contrast
// midpoint, so an axe scan that lands mid-transition reports a `color-contrast`
// violation the settled theme does not have. Suppress transitions before any
// theme flip and audit the settled colours.
const SUPPRESS_TRANSITIONS_CSS = '*, *::before, *::after { transition: none !important; }'

// The authenticated route loops navigate and load the full SPA once per route.
// The FIRST request against a cold Vite dev server pays the initial bundle
// compile, which can exceed the 30 s local default and time out on
// `page.goto('/login')` (observed locally: the first three routes of a filtered
// run failed at 30.5 s, then every later route took 7-9 s once warm). Mirrors
// json-viewer.spec.ts's `test.setTimeout(90_000)` for the same reason.
const ROUTE_AUDIT_TIMEOUT = 90_000

/**
 * Force the light theme and audit the settled colours (see SUPPRESS_TRANSITIONS_CSS).
 *
 * `:root` is the dark theme; AppLayout's toggle flips `documentElement` between
 * `.light` and `.dark`. Adding `.light` alone is enough because light mode's
 * variables are a complete override of `:root`'s. There is no per-view theme
 * any more: the retired `data-theme="agent"` override was removed (FAR-2756),
 * so every route inherits this one palette. A light scan therefore audits
 * every view in its real light-mode colours.
 */
async function forceLightMode(page: import('@playwright/test').Page) {
  await page.addStyleTag({ content: SUPPRESS_TRANSITIONS_CSS })
  await page.evaluate(() => {
    document.documentElement.classList.add('light')
    document.documentElement.classList.remove('dark')
  })
  await page.evaluate(() => new Promise(r => requestAnimationFrame(r)))
}

/** Minimal shape of an axe violation, enough to log the failing nodes and their ratios. */
interface LoggableViolation {
  id: string
  impact?: string | null
  help: string
  nodes: { target: unknown[]; any: { message: string }[] }[]
}

/**
 * Log every violation with per-node detail. A rule-level count alone cannot be
 * diagnosed (which element, which colour pair, which ratio); the node's `any`
 * messages carry the measured foreground/background and the actual ratio, so a
 * CI failure is actionable without reproducing it locally.
 */
function logViolations(label: string, violations: LoggableViolation[]) {
  if (violations.length === 0) return
  console.log(`\n=== ${label} ===`)
  for (const v of violations) {
    console.log(`[${v.impact}] ${v.id} (${v.nodes.length} nodes): ${v.help}`)
    for (const node of v.nodes) {
      console.log(`  - ${node.target.map(String).join(' ')} :: ${node.any.map(a => a.message).join(' | ')}`)
    }
  }
}

test.describe('PrimeVue Select overlay WCAG + keyboard nav', { tag: "@regression" }, () => {
  test('open filter-bar-status overlay has no unexpected WCAG AA violations', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'Uses setupLocalMockApi — only runs locally')
    const { setupLocalMockApi, loginAsAdmin } = await import('./setup/fixtures')
    await setupLocalMockApi(page)
    // dev_mode:true (set by the local mock API) renders the Assistant floating
    // panel on every route; it overlaps the /runs filter bar and intercepts
    // pointer events, blocking the Select trigger. Close it (mirrors the
    // staging e2e global-setup) so the overlay can be opened.
    await page.addInitScript(() => { try { localStorage.setItem('assistant-panel-state', 'closed') } catch (err) { console.warn(err) } })
    await loginAsAdmin(page, env)

    await page.goto('/runs')
    await page.waitForFunction(() => (document.querySelector('#app')?.children.length ?? 0) > 0)

    // Open the migrated PrimeVue Select panel (teleported to body).
    const trigger = page.getByTestId('filter-bar-status')
    await trigger.click()
    await expect(page.locator('[role="listbox"]')).toBeVisible()

    const results = await new AxeBuilder({ page })
      .withTags(WCAG_TAGS)
      .analyze()

    const violations = filterViolations(results.violations)
    if (violations.length > 0) {
      console.log(`\n=== /runs open select overlay new violations ===`)
      for (const v of violations) {
        console.log(`[${v.impact}] ${v.id} (${v.nodes.length} nodes): ${v.help}`)
      }
    }
    expect(violations).toEqual([])
  })

  test('PrimeVue Select supports keyboard navigation (arrow + escape)', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'Uses setupLocalMockApi — only runs locally')
    const { setupLocalMockApi, loginAsAdmin } = await import('./setup/fixtures')
    await setupLocalMockApi(page)
    // dev_mode:true (set by the local mock API) renders the Assistant floating
    // panel on every route; it overlaps the /runs filter bar and intercepts
    // pointer events, blocking the Select trigger. Close it (mirrors the
    // staging e2e global-setup) so the overlay can be opened.
    await page.addInitScript(() => { try { localStorage.setItem('assistant-panel-state', 'closed') } catch (err) { console.warn(err) } })
    await loginAsAdmin(page, env)

    await page.goto('/runs')
    await page.waitForFunction(() => (document.querySelector('#app')?.children.length ?? 0) > 0)

    const trigger = page.getByTestId('filter-bar-status')
    await trigger.click()
    const listbox = page.locator('[role="listbox"]')
    await expect(listbox).toBeVisible()

    // ArrowDown moves the active option. PrimeVue keeps real focus on the
    // combobox trigger and tracks the active option via the
    // aria-activedescendant / data-p-focused attributes (not DOM focus), so
    // assert that some option became active after ArrowDown.
    await page.keyboard.press('ArrowDown')
    await expect(page.locator('[role="option"][data-p-focused="true"]')).toHaveCount(1)

    // Escape closes the panel.
    await page.keyboard.press('Escape')
    await expect(listbox).not.toBeVisible()
  })
})

test.describe('WCAG AA audit (CI — Vite dev server)', { tag: "@regression" }, () => {
  const pages = [
    { path: '/login', name: 'login page' },
    { path: '/', name: 'root page' },
  ]

  for (const { path, name } of pages) {
    test(`${name} — light mode has no unexpected WCAG AA violations`, { tag: "@regression" }, async ({ page }) => {
      await page.goto(path)

      // Guard: storageState may redirect /login to / (dashboard)
      if (path === '/login' && !page.url().includes('/login')) {
        console.log(`  Skipping ${path} — redirected by valid session`)
        return
      }

      // Wait for the Vue app to mount before running axe
      await page.waitForFunction(() => (document.querySelector('#app')?.children.length ?? 0) > 0)

      // Flipping the theme triggers PrimeVue's 0.2s colour transition on every
      // control. A cross-fade between the two themes passes through a
      // low-contrast midpoint (dark ink on a bright fill fading to light ink
      // on a dark fill), so an axe scan that lands mid-transition reports a
      // `color-contrast` violation the settled theme does not have (observed:
      // the login submit button measures 4.33:1 mid-transition, 4.60:1 once
      // settled). The settled colours are what the theme actually ships, so
      // suppress transitions before the flip and audit those.
      await page.addStyleTag({ content: SUPPRESS_TRANSITIONS_CSS })
      await page.evaluate(() => {
        document.documentElement.classList.add('light')
        document.documentElement.classList.remove('dark')
      })
      await page.evaluate(() => new Promise(r => requestAnimationFrame(r)))

      const results = await new AxeBuilder({ page })
        .withTags(WCAG_TAGS)
        .analyze()

      const violations = filterViolations(results.violations)

      if (violations.length > 0) {
        console.log(`\n=== ${path} (light) new violations ===`)
        for (const v of violations) {
          console.log(`[${v.impact}] ${v.id} (${v.nodes.length} nodes): ${v.help}`)
        }
      }

      expect(violations).toEqual([])
    })

    test(`${name} — dark mode has no unexpected WCAG AA violations`, { tag: "@regression" }, async ({ page }) => {
      await page.goto(path)

      // Guard: storageState may redirect /login to / (dashboard)
      if (path === '/login' && !page.url().includes('/login')) {
        console.log(`  Skipping ${path} — redirected by valid session`)
        return
      }

      // Wait for the Vue app to mount before running axe
      await page.waitForFunction(() => (document.querySelector('#app')?.children.length ?? 0) > 0)

      // Same transition-suppression guard as the light-mode test: a stored
      // theme that differs from the default means this class-flip cross-fades,
      // and an axe scan landing mid-transition reports a false positive.
      await page.addStyleTag({ content: SUPPRESS_TRANSITIONS_CSS })
      await page.evaluate(() => {
        document.documentElement.classList.remove('light')
        document.documentElement.classList.remove('dark')
      })
      await page.evaluate(() => new Promise(r => requestAnimationFrame(r)))

      const results = await new AxeBuilder({ page })
        .withTags(WCAG_TAGS)
        .analyze()

      const violations = filterViolations(results.violations)

      if (violations.length > 0) {
        console.log(`\n=== ${path} (dark) new violations ===`)
        for (const v of violations) {
          console.log(`[${v.impact}] ${v.id} (${v.nodes.length} nodes): ${v.help}`)
        }
      }

      expect(violations).toEqual([])
    })
  }
})

// FAR-1907 — the sidebar profile links carried aria-label="User profile" over
// visible text, which is a label-content-name-mismatch (WCAG 2.5.3) violation.
// This spec could not see it before: the local mock access token is not a JWT,
// so `userId` (the JWT `sub` claim) resolved to null, the identity link
// rendered no text at all, and the avatar rendered "?" — a single non-digit
// character, which axe treats as non-human-interpretable and reports as
// `incomplete`, never as a violation. The tests below drive the authenticated
// shell with a real `sub`, which is the state every real session is in.
//
// FAR-2761: FAR-2756 retired the per-view `data-theme="agent"` palette, so the
// 18 views that used to render a dark monospace theme now inherit the standard
// palette and must be audited like every other route. 17 of them were never
// scanned; they are added below and split by the identity their router gate
// requires.
//
// Plain org-admin routes for the DARK scan — reachable with `loginAsAdmin` (a
// non-system org admin, `is_system_admin: false`). The first eight predate
// FAR-2761.
const AUTHENTICATED_ROUTES = [
  // pre-existing coverage
  '/runs', '/', '/pipelines', '/admin/my-profile', '/settings/triggers', '/admin/users', '/admin/trigger-events', '/admin/audit',
  // FAR-2761 de-themed additions (plain org admin)
  '/admin/assistant',
  '/admin/costs', '/admin/costs/limits', '/admin/costs/controls', '/admin/costs/components',
  '/admin/org',
  '/settings/assistant', '/settings/email', '/settings/guardrails', '/settings/license', '/settings/mcp', '/settings/notifications',
] as const

// SYSTEM sidebar-group routes (and the two with `system_admin_only: true`). The
// router's `requiresSystemAdmin` gate (FAR-938) redirects a plain org admin to
// the locked page, so axe would audit the wrong page. These need the
// system-admin JWT variant (`loginForSystemRoute`), which only the local mock
// target can mint — staging authenticates as a real non-system org admin and
// has no in-product way to grant `is_system_admin`.
const SYSTEM_ADMIN_ROUTES = [
  '/admin/feature-flags',
  '/admin/system/orgs',
  '/admin/product-analytics',
  '/settings/error-forwarders',
  '/settings/rate-limits',
] as const

// Routes withheld from the LIGHT-theme scan only, each with the concrete reason,
// so the gap is visible in the code rather than invisible. These are still
// DARK-scanned (they must be in AUTHENTICATED_ROUTES/SYSTEM_ADMIN_ROUTES — the
// integrity test enforces that). Add an entry ONLY for a route with a measured
// light-theme finding, never because it is awkward.
const LIGHT_EXCLUDED_ROUTES: readonly string[] = [
  // /settings/mcp — REAL light-theme WCAG AA failure, blocked on a shared
  // PrimeVue token/design decision (not hacked around here):
  //   rule: color-contrast, impact serious, 1 node
  //   target: button[data-testid="settings-mcp-copy-url"]
  //   fg #64748b (PrimeVue secondary-outlined label = colorScheme.light
  //   .outlined.secondary.color, surface.500) on bg #f4f6f8 (the card's
  //   `bg-muted/30` surface): measured 4.39:1, AA floor 4.5:1.
  // LIGHT-ONLY by construction: the dark counterpart is a different pair
  // (surface.400 on a dark surface), which the DARK scan covers and passes. The
  // label colour is a shared PrimeVue secondary-button token, so a global
  // change needs a design decision and a full-suite re-verification.
  '/settings/mcp',
]

const LIGHT_AUTHENTICATED_ROUTES = AUTHENTICATED_ROUTES.filter((p) => !LIGHT_EXCLUDED_ROUTES.includes(p))

/**
 * FAR-2761 anti-vacuous guard. A Vue render crash (a `TypeError` thrown while
 * rendering the route's subtree) leaves the page as a bare shell: the URL is
 * right and axe sees no violations, so a `pathname` guard alone passes while
 * the actual content never rendered. That is exactly how /admin/feature-flags
 * and /admin/assistant once slipped through (a wrong mock response, not a
 * layout problem). Attach this BEFORE the navigation and assert it is empty
 * afterwards so a crashed render FAILS the route instead of passing vacuously.
 *
 * Signals captured:
 *  - every `pageerror` (uncaught exception / rejection) — always a failure;
 *  - `console.error` messages that carry a crash signature (Vue render errors,
 *    `TypeError`/`ReferenceError`/`Cannot read propert…`/`is not iterable`).
 *
 * Deliberately NOT captured (measured on this fixture): `console.warn` (the Vue
 * devtools notice, i18n fallbacks) and resource-load `console.error`s such as
 * "Failed to load resource: … 502 (Bad Gateway)". Those 502s are the fixture's
 * own `route.fallback()` auth probing hitting the Vite proxy with no backend
 * attached — benign noise present on otherwise-healthy routes, not a crash.
 */
const RENDER_CRASH_CONSOLE = /\[vue\]|render function|\b(TypeError|ReferenceError|RangeError|SyntaxError)\b|is not iterable|is not a function|Cannot read propert/i

function collectRenderErrors(page: import('@playwright/test').Page) {
  const errors: string[] = []
  const onPageError = (err: Error) => errors.push(`pageerror: ${err.message}`)
  const onConsole = (msg: import('@playwright/test').ConsoleMessage) => {
    if (msg.type() !== 'error') return
    const text = msg.text()
    if (RENDER_CRASH_CONSOLE.test(text)) errors.push(`console.error: ${text}`)
  }
  page.on('pageerror', onPageError)
  page.on('console', onConsole)
  return {
    errors,
    dispose: () => {
      page.off('pageerror', onPageError)
      page.off('console', onConsole)
    },
  }
}

// Integrity guard for the route sets: a route must not be scanned twice (a
// duplicate would run the same page twice and read as extra coverage), and a
// LIGHT exclusion must still be DARK-scanned (otherwise it is not a light-only
// gap, it is an untested route). Cheap and env-independent.
test('scanned route sets are unique and every light exclusion is dark-covered', { tag: "@local-mock" }, () => {
  const darkScanned = [...AUTHENTICATED_ROUTES, ...SYSTEM_ADMIN_ROUTES]
  expect(new Set(darkScanned).size, 'a route must not be scanned twice').toBe(darkScanned.length)
  for (const path of LIGHT_EXCLUDED_ROUTES) {
    expect(darkScanned, `${path} is light-excluded but must still be dark-scanned`).toContain(path)
  }
})

// Tagged @local-mock rather than @regression: these tests drive the
// setupLocalMockApi target, so they run in the `Frontend and WCAG` CI job
// (which selects this file by name, with the default local target) but must
// NOT run in the deployed-build @regression gate. That gate checks out main
// and runs against the live staging APIs; a mock-backed test there would
// assert the runner's fixtures against the deployed bundle, and it would fail
// for any branch whose fix is not yet deployed. A tag keeps that separation
// declarative instead of leaving a runtime environment gate in the test body.
test.describe('authenticated shell — sidebar profile links (FAR-1907)', { tag: "@local-mock" }, () => {
  test('profile link names contain the visible text for a UUID sub', { tag: "@local-mock" }, async ({ page, env }) => {
    const { setupLocalMockApi, loginAsAdmin } = await import('./setup/fixtures')
    await setupLocalMockApi(page)
    await page.addInitScript(() => { try { localStorage.setItem('assistant-panel-state', 'closed') } catch (err) { console.warn(err) } })
    await loginAsAdmin(page, env)

    // A UUID `sub` (what the backend mints) renders an identity link whose
    // visible text is the UUID and an avatar whose initial is a digit — the
    // exact state in which label-content-name-mismatch fires.
    await page.goto('/runs')
    await page.evaluate((token) => localStorage.setItem('modulo_access_token', token), systemAdminJwt('3f9a1b2c-4d5e-6f70-8192-a3b4c5d6e7f8'))
    await page.reload()
    await page.waitForFunction(() => (document.querySelector('#app')?.children.length ?? 0) > 0)

    const links = page.locator('a[href$="/admin/my-profile"]')
    // Layout-agnostic: the number of profile links rendered on /runs depends on
    // which sidebar breakpoints are active, so assert at least one renders and
    // that EVERY rendered link satisfies the rule — do not pin the exact count.
    await expect(links.first()).toBeVisible()

    // WCAG 2.5.3: the accessible name must contain the visible label text. The
    // pre-fix markup rendered aria-label="User profile" over "3f9a…" and "3".
    const pairs = await links.evaluateAll(els => els.map(el => ({
      label: el.getAttribute('aria-label') ?? '',
      visible: (el.textContent ?? '').trim(),
    })))
    expect(pairs.length, 'at least one sidebar profile link must render').toBeGreaterThan(0)
    for (const { label, visible } of pairs) {
      expect(visible.length, 'each sidebar profile link must render visible text').toBeGreaterThan(0)
      expect(label, `aria-label ${JSON.stringify(label)} must contain the visible text ${JSON.stringify(visible)}`).toContain(visible)
    }

    const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze()
    const violations = filterViolations(results.violations)
    if (violations.length > 0) {
      console.log(`\n=== /runs authenticated shell new violations ===`)
      for (const v of violations) {
        console.log(`[${v.impact}] ${v.id} (${v.nodes.length} nodes): ${v.help}`)
      }
    }
    expect(violations).toEqual([])
  })

  for (const path of AUTHENTICATED_ROUTES) {
    test(`authenticated ${path} has no unexpected WCAG AA violations`, { tag: "@local-mock" }, async ({ page, env }) => {
      test.setTimeout(ROUTE_AUDIT_TIMEOUT)
      const { setupLocalMockApi, loginAsAdmin } = await import('./setup/fixtures')
      await setupLocalMockApi(page)
      await page.addInitScript(() => { try { localStorage.setItem('assistant-panel-state', 'closed') } catch (err) { console.warn(err) } })
      await loginAsAdmin(page, env)

      const guard = collectRenderErrors(page)
      await page.goto(path)
      await page.waitForFunction(() => (document.querySelector('#app')?.children.length ?? 0) > 0)

      // Guard the route: a role/tier/system gate redirect would make axe audit
      // the WRONG page (the locked page or the dashboard) and report a vacuous
      // pass. Assert the navigation landed on the route under test.
      expect(new URL(page.url()).pathname, `${path} must render its own route, not a redirect`).toBe(path)

      // MINOR A proof (single targeted assertion, not a general content anchor):
      // /settings/assistant must render its REAL empty state, not junk rows. The
      // old `includes('/api/v1/me')` catch-all answered `/me/assistant/skills`
      // with the profile object, which `v-for` rendered as 3 junk rows and left
      // `skills.length === 0` undefined, so the EmptyState never showed — with
      // no throw and no console.error, invisible to the render-error guard.
      if (path === '/settings/assistant') {
        await expect(
          page.locator('[data-testid^="assistant-user-skill-row-"]'),
          'the skills table must render no junk rows from a wrong-shaped body',
        ).toHaveCount(0)
        await expect(
          page.getByText('No personal skills configured'),
          'the real empty state must render when there are no skills',
        ).toBeVisible({ timeout: ROUTE_AUDIT_TIMEOUT })
      }

      const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze()
      const violations = filterViolations(results.violations)
      if (violations.length > 0) {
        console.log(`\n=== ${path} (authenticated) new violations ===`)
        for (const v of violations) {
          console.log(`[${v.impact}] ${v.id} (${v.nodes.length} nodes): ${v.help}`)
        }
      }
      expect(violations).toEqual([])
      // A crashed render leaves the shell in place with zero axe violations; the
      // collector is what makes that fail instead of pass vacuously.
      expect(guard.errors, `${path} must render without a render crash`).toEqual([])
      guard.dispose()
    })
  }
})

// FAR-2761 — the SYSTEM-group routes need `is_system_admin: true`, which a plain
// org admin does not have. `loginForSystemRoute` swaps in the system-admin JWT on
// the local mock target (it returns false on staging, where no in-product grant
// exists). This is the dark-theme counterpart for those five routes; the light
// counterpart is the loop in the FAR-1938 describe below.
test.describe('authenticated SYSTEM routes — dark theme (FAR-2761)', { tag: "@local-mock" }, () => {
  for (const path of SYSTEM_ADMIN_ROUTES) {
    test(`authenticated ${path} has no unexpected WCAG AA violations`, { tag: "@local-mock" }, async ({ page, env }) => {
      test.setTimeout(ROUTE_AUDIT_TIMEOUT)
      const { setupLocalMockApi, loginForSystemRoute } = await import('./setup/fixtures')
      await setupLocalMockApi(page)
      await page.addInitScript(() => { try { localStorage.setItem('assistant-panel-state', 'closed') } catch (err) { console.warn(err) } })
      const canRender = await loginForSystemRoute(page, env)
      test.skip(!canRender, 'SYSTEM routes need is_system_admin; the deployed target cannot grant it')

      const guard = collectRenderErrors(page)
      await page.goto(path)
      await page.waitForFunction(() => (document.querySelector('#app')?.children.length ?? 0) > 0)

      // Guard the route: the system-admin gate must not have redirected, or axe
      // would audit the locked page and report a vacuous pass.
      expect(new URL(page.url()).pathname, `${path} must render its own route, not a redirect`).toBe(path)

      const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze()
      const violations = filterViolations(results.violations)
      logViolations(`${path} (authenticated, system admin)`, violations)
      expect(violations).toEqual([])
      // Anti-vacuous: a crashed subtree passes axe on the shell; this fails it.
      expect(guard.errors, `${path} must render without a render crash`).toEqual([])
      guard.dispose()
    })
  }
})

// FAR-1938 — every scan above runs the default (dark) theme, so the light
// palette was never asserted. The light theme is a full token override on
// `:root`, and several of its text tokens measured below the WCAG AA 4.5:1
// floor (`success` at 2.30:1 on white, muted on the light surface at 3.42:1,
// `--warning-text` at 4.01:1, primary on its tint at 4.16-4.33:1). Each route
// is scanned in BOTH themes: a fix that darkens a light token must not lighten
// its dark counterpart below AA, and vice versa.
//
// Deliberately @regression, unlike the mock-backed tests above: the deployed
// `--grep @regression` gate is the only CI job that audits the shipped bundle,
// so the light-theme AA gate must run there (it also runs in the per-PR
// `Frontend and WCAG` job, which selects this file by name). The mock API only
// supplies data; the colours under audit come from the deployed stylesheet.
test.describe('authenticated light-theme contrast (FAR-1938)', { tag: "@regression" }, () => {
  for (const path of LIGHT_AUTHENTICATED_ROUTES) {
    test(`authenticated ${path} has no unexpected WCAG AA violations (light theme)`, { tag: "@regression" }, async ({ page, env }) => {
      test.setTimeout(ROUTE_AUDIT_TIMEOUT)
      const { setupLocalMockApi, loginAsAdmin } = await import('./setup/fixtures')
      await setupLocalMockApi(page)
      await page.addInitScript(() => { try { localStorage.setItem('assistant-panel-state', 'closed') } catch (err) { console.warn(err) } })
      await loginAsAdmin(page, env)

      const guard = collectRenderErrors(page)
      await page.goto(path)
      await page.waitForFunction(() => (document.querySelector('#app')?.children.length ?? 0) > 0)

      // Guard the route: a gate redirect would make the light scan audit the
      // wrong page and report a vacuous pass.
      expect(new URL(page.url()).pathname, `${path} must render its own route, not a redirect`).toBe(path)

      await forceLightMode(page)

      // Guard the harness itself: if the theme did not actually flip, the scan
      // below would silently re-audit dark and this test would prove nothing.
      expect(
        await page.evaluate(() => document.documentElement.classList.contains('light')),
        'the light theme must be active before scanning',
      ).toBe(true)

      const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze()
      const violations = filterViolations(results.violations)
      logViolations(`${path} (authenticated, light)`, violations)
      expect(violations).toEqual([])
      // Anti-vacuous: a crashed subtree passes axe on the shell; this fails it.
      expect(guard.errors, `${path} must render without a render crash`).toEqual([])
      guard.dispose()
    })
  }
})

// FAR-2761 — light-theme counterpart for the SYSTEM routes, mirroring the
// dark-theme loop in the FAR-2761 describe above. Its OWN @local-mock describe
// (not nested in the @regression light describe above) is deliberate: Playwright
// ORs describe + test tags into the title, so a test inside the @regression
// describe would still be selected by `--grep @regression` on staging. A SYSTEM
// route needs `is_system_admin`, which the deployed staging identity cannot
// hold, so running it there would do a real UI login and skip anyway — wasted
// logins for no coverage. Local runs (the per-PR `Frontend and WCAG` job selects
// this file by name) cover it.
test.describe('authenticated SYSTEM routes — light theme (FAR-2761)', { tag: "@local-mock" }, () => {
  for (const path of SYSTEM_ADMIN_ROUTES) {
    test(`authenticated ${path} has no unexpected WCAG AA violations (light theme)`, { tag: "@local-mock" }, async ({ page, env }) => {
      test.setTimeout(ROUTE_AUDIT_TIMEOUT)
      const { setupLocalMockApi, loginForSystemRoute } = await import('./setup/fixtures')
      await setupLocalMockApi(page)
      await page.addInitScript(() => { try { localStorage.setItem('assistant-panel-state', 'closed') } catch (err) { console.warn(err) } })
      const canRender = await loginForSystemRoute(page, env)
      test.skip(!canRender, 'SYSTEM routes need is_system_admin; the deployed target cannot grant it')

      const guard = collectRenderErrors(page)
      await page.goto(path)
      await page.waitForFunction(() => (document.querySelector('#app')?.children.length ?? 0) > 0)

      expect(new URL(page.url()).pathname, `${path} must render its own route, not a redirect`).toBe(path)

      await forceLightMode(page)

      expect(
        await page.evaluate(() => document.documentElement.classList.contains('light')),
        'the light theme must be active before scanning',
      ).toBe(true)

      const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze()
      const violations = filterViolations(results.violations)
      logViolations(`${path} (authenticated, light, system admin)`, violations)
      expect(violations).toEqual([])
      // Anti-vacuous: a crashed subtree passes axe on the shell; this fails it.
      expect(guard.errors, `${path} must render without a render crash`).toEqual([])
      guard.dispose()
    })
  }
})

// FAR-2351 — the neutral PrimeVue badge (`.p-badge-secondary`, the sidebar's
// plan-tier chip) measured 1.11:1 on /admin/trigger-events and /admin/audit:
// `#d4d4d8` ink on a `#dbe0e6` fill, 10px bold. The finding only reproduces in
// the state the light-mode audit above cannot reach, because our palette and
// PrimeVue's colour scheme flip on DIFFERENT switches: the page is light when
// `.light` is present, while PrimeVue picks its palette from `.dark`
// (`darkModeSelector: '.dark'` in main.ts, and `index.html` ships
// `class="dark"`). A page that has `.light` but still carries `.dark` — which is
// exactly how a light-mode audit that only ADDS `.light` measures it (see
// json-viewer.spec.ts) — therefore read the badge ink from PrimeVue's dark
// scheme (`--p-surface-300` = zinc-300 `#d4d4d8`) while its fill came from our
// light palette (the token bridge maps `--p-surface-800` to
// `hsl(var(--muted))` = `#dbe0e6`).
//
// So this scan deliberately reproduces that mixed state, and asserts the
// `color-contrast` rule is clean. Reverting the `html.light .p-badge-secondary`
// override in style.css brings the violation straight back, which the
// drop-`.dark` light-theme loop above would not catch.
test.describe('light page with a stale .dark class — PrimeVue badges (FAR-2351)', { tag: "@regression" }, () => {
  for (const path of ['/admin/trigger-events', '/admin/audit']) {
    test(`${path} has no color-contrast violations when .light is added over .dark`, { tag: "@regression" }, async ({ page, env }) => {
      const { setupLocalMockApi, loginAsAdmin } = await import('./setup/fixtures')
      await setupLocalMockApi(page)
      await page.addInitScript(() => { try { localStorage.setItem('assistant-panel-state', 'closed') } catch (err) { console.warn(err) } })
      await loginAsAdmin(page, env)

      await page.goto(path)
      await page.waitForFunction(() => (document.querySelector('#app')?.children.length ?? 0) > 0)

      await page.addStyleTag({ content: SUPPRESS_TRANSITIONS_CSS })
      await page.evaluate(() => {
        document.documentElement.classList.add('light')
        // Deliberately KEEP .dark: the two theme switches are independent, and
        // the reported finding lives exactly in their disagreement.
      })
      await page.evaluate(() => new Promise(r => requestAnimationFrame(r)))

      // Guard the harness: the page must be light AND still carry the stale
      // class, otherwise the scan below audits a different state than the one
      // the finding was reported in.
      const classes = await page.evaluate(() => Array.from(document.documentElement.classList))
      expect(classes, 'the light theme must be active').toContain('light')
      expect(classes, 'the stale dark class must still be present').toContain('dark')

      // Guard the target: without a rendered neutral badge the scan would be
      // vacuously clean.
      const badges = page.locator('.p-badge')
      await expect(badges.first()).toBeVisible()

      const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze()
      const contrastViolations = filterViolations(results.violations).filter(v => v.id === 'color-contrast')
      logViolations(`${path} (light + stale .dark)`, contrastViolations)
      expect(contrastViolations).toEqual([])
    })
  }
})

// FAR-2761 — guard the focus ring the deleted per-view rule used to supply. The
// retired `[data-theme="agent"] :where(:not(html)):focus-visible` rule was
// UNLAYERED, so it beat `outline-none` utilities on the formerly agent-themed
// pages. Those pages now rely on the layered base rule at style.css:10-13
// (`:where(:root :not(html)):focus-visible { outline: 2px solid ... }`). axe
// cannot see a keyboard focus ring, so this one test — not one per route —
// drives real keyboard focus and asserts the BASE RULE'S signature survives.
//
// Target choice (measured, see the comment inside): a sidebar nav link on
// /settings/mcp. It has no `outline-none` utility and no component ring, so the
// base rule is its ONLY focus indicator — deleting the base rule removes it and
// this test fails (verified: focused gives `outline: solid / 2px /
// rgba(0,255,208,0.8)`; injecting `:where(:root :not(html)):focus-visible{
// outline:0 !important }` drops it to `none / 0px`). An `outline-none` element
// is NOT usable for this assertion: `outline-none` sets `outline-style: none`
// (measured on the command-palette input: `outline-width 2px` but
// `outline-style none` => no visible outline), so it could never satisfy the
// base-rule signature.
test.describe('keyboard focus indicator on a formerly agent-themed page (FAR-2761)', { tag: "@local-mock" }, () => {
  test('/settings/mcp — a keyboard-focused control keeps the base focus outline', { tag: "@local-mock" }, async ({ page, env }) => {
    test.setTimeout(ROUTE_AUDIT_TIMEOUT)
    const { setupLocalMockApi, loginAsAdmin } = await import('./setup/fixtures')
    await setupLocalMockApi(page)
    await page.addInitScript(() => { try { localStorage.setItem('assistant-panel-state', 'closed') } catch (err) { console.warn(err) } })
    await loginAsAdmin(page, env)

    await page.goto('/settings/mcp')
    await page.waitForFunction(() => (document.querySelector('#app')?.children.length ?? 0) > 0)

    // A real interactive control: the sidebar nav link. It carries no
    // `outline-none` utility and no component ring, so its focus outline is
    // painted solely by the layered base rule (the regression this guards).
    const control = page.locator('a.sidebar-link').first()
    await expect(control).toBeVisible({ timeout: ROUTE_AUDIT_TIMEOUT })

    // Drive focus the way a keyboard user does: press Tab until the control
    // holds focus, so the browser applies `:focus-visible`. A programmatic
    // `.focus()` can leave `:focus-visible` unset (making the assertion vacuous
    // about keyboard focus), so Tab is the honest driver.
    let reached = false
    for (let i = 0; i < 120 && !reached; i++) {
      await page.keyboard.press('Tab')
      reached = await control.evaluate((el) => document.activeElement === el)
    }
    expect(reached, 'Tab must reach the sidebar link on /settings/mcp').toBe(true)

    const indicator = await page.evaluate(() => {
      const el = document.activeElement as HTMLElement | null
      if (!el) return null
      const cs = getComputedStyle(el)
      return {
        tag: el.tagName,
        outlineStyle: cs.outlineStyle,
        outlineWidthPx: Number.parseFloat(cs.outlineWidth) || 0,
        outlineColor: cs.outlineColor,
        boxShadow: cs.boxShadow,
      }
    })
    expect(indicator, 'an element must hold focus').not.toBeNull()
    // The base rule's signature: a >=2px solid outline. This is exactly what
    // disappears if style.css:10-13 is removed, so the test can fail for the
    // regression it documents (a 1px library outline would not satisfy it).
    expect(
      indicator!.outlineStyle,
      `keyboard-focused ${indicator?.tag} must use a solid outline; got ${JSON.stringify(indicator)}`,
    ).toBe('solid')
    expect(
      indicator!.outlineWidthPx,
      `keyboard-focused ${indicator?.tag} must paint the base rule's >=2px outline; got ${JSON.stringify(indicator)}`,
    ).toBeGreaterThanOrEqual(2)
  })
})
