import { test, expect, loginAsAdmin } from './setup/fixtures'

/**
 * FAR-2594: the Costs page "buttons" (Spend Limits / Cost Components / Cost
 * Controls) are `PageTabs` router-links rendered by `PageHeader`, not
 * `<button>`s. Production reported that none of them did anything.
 *
 * No spec clicked them. `costs-navigation.spec.ts` reaches each page with
 * `page.goto(path)` and then leaves via a sidebar link, and the per-page specs
 * (`admin-costs`, `admin-cost-controls`, `admin-cost-components`) all `goto`
 * their destination directly — so the click path was never executed anywhere.
 * Staging could never have caught it either: it runs Community-tier, and
 * `setup/target-tier.ts` skips every Team-tier route there, so the one
 * deployment that has these tabs (production) was only covered by the
 * `@smoke` suite, which had no Costs coverage at all.
 */

interface CostsTab {
  /** The tab's visible label (its accessible name when not plan-locked). */
  label: string
  /** Destination pathname. */
  path: string
  /** The destination page's `<h1>`. */
  heading: string
}

const TABS: CostsTab[] = [
  { label: 'Overview', path: '/admin/costs', heading: 'Cost Breakdown' },
  { label: 'Spend Limits', path: '/admin/costs/limits', heading: 'Spend Limits' },
  { label: 'Cost Components', path: '/admin/costs/components', heading: 'Cost Components' },
  { label: 'Cost Controls', path: '/admin/costs/controls', heading: 'Cost Controls' },
]

/** The tab the page starts on, left out of the forward walk and visited last. */
const START_TAB = TABS[0]
const FORWARD_TABS = TABS.slice(1)

test.describe('Costs tabs navigate on click', () => {
  test('every Costs tab click renders its own page', { tag: '@smoke' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)

    // A goto (not a click) is the entry point, and is also what the
    // Community-tier staging guard in setup/target-tier.ts inspects before
    // skipping the Team-gated surface.
    await page.goto('/admin/costs')
    await expect(
      page.getByRole('heading', { level: 1, name: START_TAB.heading }),
    ).toBeVisible({ timeout: 15000 })

    // Walk every other tab, then come back to the starting one, so each tab is
    // exercised as a real click and never merely as the page we mounted on.
    for (const tab of [...FORWARD_TABS, START_TAB]) {
      await page.getByRole('tab', { name: new RegExp(`^${tab.label}$`) }).click()

      await expect(page).toHaveURL((url) => url.pathname === tab.path)
      await expect(
        page.getByRole('heading', { level: 1, name: tab.heading }),
      ).toBeVisible({ timeout: 15000 })
      // The clicked tab is the selected one — a click that silently no-ops
      // would leave the previous tab marked active.
      await expect(
        page.getByRole('tab', { name: new RegExp(`^${tab.label}$`) }),
      ).toHaveAttribute('aria-selected', 'true')
    }
  })
})

test.describe('Costs tabs recover from a stale bundle', { tag: '@regression' }, () => {
  test('a route chunk that no longer exists reloads into the destination instead of doing nothing', { tag: '@regression' }, async ({ page, env }) => {
    // Fault injection needs the dev server's module graph; production-rendered
    // chunk names are hashed and change every deploy.
    test.skip(env.name !== 'local', 'chunk fault injection is a local-target test')

    await loginAsAdmin(page, env)
    await page.goto('/admin/costs')
    await expect(
      page.getByRole('heading', { level: 1, name: START_TAB.heading }),
    ).toBeVisible({ timeout: 15000 })

    // Reproduce the state a tab left open across a deploy is in: the running
    // bundle still names the destination's now-deleted chunk. The first in-app
    // attempt and its in-app retry must fail; once the router falls back to a
    // full document load the chunk is fetchable again (a real deploy serves
    // the new filename).
    let abortedRequests = 0
    let documentReloaded = false
    page.on('request', (request) => {
      if (request.resourceType() === 'document' && new URL(request.url()).pathname === '/admin/costs/limits') {
        documentReloaded = true
      }
    })
    await page.route('**/src/views/AdminSpendLimitsView.vue*', (route) => {
      if (documentReloaded) return route.continue()
      abortedRequests += 1
      return route.abort()
    })

    await page.getByRole('tab', { name: /^Spend Limits$/ }).click()

    // Before FAR-2594 this never happened: the recovery retried the page the
    // user was already on and reset its own retry budget, so the click was a
    // permanent, silent no-op.
    await expect(page).toHaveURL((url) => url.pathname === '/admin/costs/limits', { timeout: 20000 })
    await expect(
      page.getByRole('heading', { level: 1, name: 'Spend Limits' }),
    ).toBeVisible({ timeout: 20000 })

    expect(abortedRequests).toBeGreaterThan(0)
    expect(documentReloaded).toBe(true)
  })
})
