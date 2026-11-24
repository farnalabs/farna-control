import { test, expect } from './setup/fixtures'
import { installEntitlementNavigationGuard } from './setup/target-tier'
import type { TestEnv } from './setup/env'

/**
 * Unit-level proof of the entitlement navigation guard installed by the `page`
 * fixture (setup/target-tier.ts). Uses a fake Page so it needs neither a
 * browser nor a live target, and runs on every target.
 *
 * FAR-2665: the guard reads the manifest's `required_entitlement` and the
 * target's GRANTED entitlements from the plan payload — the old `required_tier`
 * key no longer exists, so the guard must be pinned against the entitlement
 * vocabulary or it silently goes inert.
 */

const staging = { name: 'staging' } as TestEnv
const app = { name: 'app' } as TestEnv

interface FakePage {
  route: (pattern: string | RegExp) => Promise<void>
  goto: (url: string) => Promise<null>
}

function fakePage(): { page: FakePage; calls: string[] } {
  const calls: string[] = []
  const page: FakePage = {
    route: async (pattern) => {
      calls.push(`route:${String(pattern)}`)
    },
    goto: async (url) => {
      calls.push(`goto:${url}`)
      return null
    },
  }
  return { page, calls }
}

async function run(
  env: TestEnv,
  path: string,
  granted: readonly string[],
  controlPlan = false,
): Promise<{ skips: string[]; calls: string[]; threw: boolean }> {
  const { page, calls } = fakePage()
  const skips: string[] = []
  // The real skip (`testInfo.skip`) throws to abort the test; model that so
  // the assertion also proves the navigation never happened.
  installEntitlementNavigationGuard(
    page as never,
    env,
    (reason) => {
      skips.push(reason)
      throw new Error('SKIP')
    },
    async () => new Set(granted),
  )
  let threw = false
  try {
    if (controlPlan) await page.route('**/api/v1/plan*')
    await page.goto(path)
  } catch (err) {
    if ((err as Error).message !== 'SKIP') throw err
    threw = true
  }
  return { skips, calls, threw }
}

test.describe('Entitlement navigation guard', { tag: '@regression' }, () => {
  test('community staging skips an entitled route without navigating', { tag: '@regression' }, async () => {
    const result = await run(staging, '/admin/audit', [])
    expect(result.threw).toBe(true)
    expect(result.skips).toHaveLength(1)
    expect(result.skips[0]).toContain('/admin/audit')
    expect(result.skips[0]).toContain('audit_evidence')
    expect(result.calls).not.toContain('goto:/admin/audit')
  })

  test('community staging still navigates a free route', { tag: '@regression' }, async () => {
    const result = await run(staging, '/admin/errors', [])
    expect(result.threw).toBe(false)
    expect(result.skips).toHaveLength(0)
    expect(result.calls).toContain('goto:/admin/errors')
  })

  test('community staging navigates a legacy alias whose target is now free', { tag: '@regression' }, async () => {
    const result = await run(staging, '/admin/sandbox-concurrency', [])
    expect(result.threw).toBe(false)
    expect(result.skips).toHaveLength(0)
    expect(result.calls).toContain('goto:/admin/sandbox-concurrency')
  })

  test('a granted entitlement navigates the nested route', { tag: '@regression' }, async () => {
    const result = await run(staging, '/admin/costs/limits', ['cost_governance'])
    expect(result.threw).toBe(false)
    expect(result.skips).toHaveLength(0)
    expect(result.calls).toContain('goto:/admin/costs/limits')
  })

  test('a spec-owned plan mock disables the guard', { tag: '@regression' }, async () => {
    const result = await run(staging, '/admin/audit', [], true)
    expect(result.threw).toBe(false)
    expect(result.calls).toContain('goto:/admin/audit')
  })

  test('the production target is never guarded', { tag: '@regression' }, async () => {
    const result = await run(app, '/admin/connectors', [])
    expect(result.threw).toBe(false)
    expect(result.calls).toContain('goto:/admin/connectors')
  })
})
