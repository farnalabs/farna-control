import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// The unit suite's setup.ts replaces `vue-router` with a bare stub
// (`createRouter` returns an object whose push/resolve do nothing), so the
// wiring block below would assert against the stub instead of the real router.
// Restore the real implementation for this file, exactly as
// `routerGuards.spec.ts` does.
vi.mock('vue-router', async () => await vi.importActual<typeof import('vue-router')>('vue-router'))

/**
 * FAR-2594: the route-chunk recovery contract.
 *
 * A tab left open across a deploy keeps the previous index.html in memory, so
 * any route it had not visited yet requests a chunk filename the deploy has
 * deleted. The click must not become a permanent silent no-op. The production
 * handlers are exported from `router/index.ts` because `router.onError` keeps
 * its listeners private — a navigation-based test would have to mount the
 * whole app graph (real components, plan store, API mocks) just to reach the
 * handler, and could not make a lazy chunk fail deterministically. The last
 * describe block covers the other half of that trade: that the real router
 * still registers both halves of the recovery.
 *
 * The retry budget is module state, so every test re-imports the module after
 * `vi.resetModules()` to start from a clean budget. The storm guard lives in
 * `sessionStorage`, which deliberately survives the module reset — that is how
 * the tests simulate a second document load.
 */
const CHUNK_ERROR = new TypeError(
  'Failed to fetch dynamically imported module: https://control.farna.dev/assets/AdminSpendLimitsView-DT2hhTK.js',
)
const DESTINATION = '/admin/costs/limits'
const OTHER_DESTINATION = '/admin/costs/controls'
const DASHBOARD = '/'
const RECOVERY_GUARD_KEY = 'modulo.chunk-recovery:/admin/costs/limits'

async function loadRecovery() {
  vi.resetModules()
  const mod = await import('../router')
  const router = mod.default
  const replace = vi.spyOn(router, 'replace').mockResolvedValue(undefined)
  // `vi.spyOn` can hand back the spy installed by an earlier test in this file
  // (the module namespace is cached per worker), so clear its history rather
  // than assume a fresh mock.
  replace.mockClear()
  // jsdom's `window.location` is non-configurable, so `Location.assign` cannot
  // be spied. The handler takes its document-navigation boundary as an
  // injectable argument (default `window.location.assign`) instead.
  const assign = vi.fn<(target: string) => void>()
  const alert = vi.spyOn(window, 'alert').mockImplementation(() => {})
  return { ...mod, router, replace, assign, alert }
}

beforeEach(() => {
  sessionStorage.clear()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('route-chunk recovery (FAR-2594)', () => {
  it('retries the attempted destination, never the route the user is already on', async () => {
    const { noteNavigationStarted, handleRouteChunkError, router, replace, assign } = await loadRecovery()

    // Precondition for the assertion below: a failed navigation never commits,
    // so `currentRoute` is still somewhere else (the page the user is on).
    expect(router.currentRoute.value.fullPath).not.toBe(DESTINATION)

    noteNavigationStarted({ fullPath: DESTINATION })
    handleRouteChunkError(CHUNK_ERROR, assign)

    expect(replace).toHaveBeenCalledTimes(1)
    expect(replace).toHaveBeenCalledWith(DESTINATION)
    // The old implementation retried `router.currentRoute`, which cannot
    // re-attempt the chunk that actually failed.
    expect(replace.mock.calls[0]?.[0]).not.toBe(router.currentRoute.value.fullPath)
    expect(assign).not.toHaveBeenCalled()
  })

  it('retries only the in-flight destination when a superseded navigation fails late', async () => {
    const { noteNavigationStarted, handleRouteChunkError, replace, assign } = await loadRecovery()

    // The user clicked Spend Limits, then clicked Cost Controls before the
    // first chunk settled. A late error from the FIRST navigation must not
    // yank them back to it.
    noteNavigationStarted({ fullPath: DESTINATION })
    noteNavigationStarted({ fullPath: OTHER_DESTINATION })
    handleRouteChunkError(CHUNK_ERROR, assign)

    expect(replace).toHaveBeenCalledTimes(1)
    expect(replace).toHaveBeenCalledWith(OTHER_DESTINATION)
    expect(replace).not.toHaveBeenCalledWith(DESTINATION)
  })

  it('keeps the retry budget across a FAILED navigation, so the hard load stays reachable', async () => {
    const { noteNavigationStarted, noteNavigationSettled, handleRouteChunkError, replace, assign } = await loadRecovery()

    noteNavigationStarted({ fullPath: DESTINATION })
    handleRouteChunkError(CHUNK_ERROR, assign) // budget 1: in-app retry
    // `afterEach(to, from, failure)` — the retried navigation failed too.
    noteNavigationSettled(new Error('navigation aborted'))
    handleRouteChunkError(CHUNK_ERROR, assign) // budget exhausted: full document load

    expect(replace).toHaveBeenCalledTimes(1)
    expect(assign).toHaveBeenCalledTimes(1)
    expect(assign).toHaveBeenCalledWith(DESTINATION)
  })

  it('refills the retry budget once a navigation actually lands', async () => {
    const { noteNavigationStarted, noteNavigationSettled, handleRouteChunkError, replace, assign } = await loadRecovery()

    noteNavigationStarted({ fullPath: DESTINATION })
    handleRouteChunkError(CHUNK_ERROR, assign) // budget 1: in-app retry
    noteNavigationSettled(undefined) // `afterEach` with no failure: it landed
    handleRouteChunkError(CHUNK_ERROR, assign) // budget was refilled: in-app retry again

    expect(replace).toHaveBeenCalledTimes(2)
    expect(assign).not.toHaveBeenCalled()
  })

  it('falls back to a full document navigation to the destination, not a reload of the current page', async () => {
    const { noteNavigationStarted, handleRouteChunkError, router, assign } = await loadRecovery()

    noteNavigationStarted({ fullPath: DESTINATION })
    handleRouteChunkError(CHUNK_ERROR, assign) // budget 1
    handleRouteChunkError(CHUNK_ERROR, assign) // exhausted

    // Exactly one document navigation, and it goes to the destination the user
    // asked for. `reload()` would have re-opened the page they are stuck on.
    expect(assign).toHaveBeenCalledTimes(1)
    expect(assign).toHaveBeenCalledWith(DESTINATION)
    expect(assign.mock.calls[0]?.[0]).not.toBe(router.currentRoute.value.fullPath)
  })

  it('does not reload forever when the destination chunk is still missing after the reload', async () => {
    // First document: retry, then one full load of the destination.
    const firstDocument = await loadRecovery()
    firstDocument.noteNavigationStarted({ fullPath: DESTINATION })
    firstDocument.handleRouteChunkError(CHUNK_ERROR, firstDocument.assign)
    firstDocument.handleRouteChunkError(CHUNK_ERROR, firstDocument.assign)

    expect(firstDocument.assign).toHaveBeenCalledTimes(1)
    expect(firstDocument.assign).toHaveBeenCalledWith(DESTINATION)
    // The guard survives the document load, keyed by the destination.
    expect(
      sessionStorage.getItem(RECOVERY_GUARD_KEY),
      'the reload guard must survive the document load',
    ).toBe('1')

    // Second document: a fresh bundle is still missing the same chunk. Without
    // the guard this assigns the destination again, forever.
    const secondDocument = await loadRecovery()
    secondDocument.noteNavigationStarted({ fullPath: DESTINATION })
    secondDocument.handleRouteChunkError(CHUNK_ERROR, secondDocument.assign)
    secondDocument.handleRouteChunkError(CHUNK_ERROR, secondDocument.assign)

    // It does NOT reload the broken destination a second time; it recovers to
    // the dashboard and tells the user, visibly.
    expect(secondDocument.assign).toHaveBeenCalledTimes(1)
    expect(secondDocument.assign, 'the second document must not reload the broken destination').toHaveBeenCalledWith(DASHBOARD)
    expect(secondDocument.assign).not.toHaveBeenCalledWith(DESTINATION)
    expect(secondDocument.alert).toHaveBeenCalledTimes(1)
    expect(String(secondDocument.alert.mock.calls[0]?.[0])).toContain('dashboard')
  })

  it('clears the reload guard once a navigation lands, so a later deploy can recover', async () => {
    const { noteNavigationStarted, noteNavigationSettled, handleRouteChunkError, assign } = await loadRecovery()

    noteNavigationStarted({ fullPath: DESTINATION })
    handleRouteChunkError(CHUNK_ERROR, assign) // budget 1
    handleRouteChunkError(CHUNK_ERROR, assign) // exhausted: guard spent, reload
    expect(
      sessionStorage.getItem(RECOVERY_GUARD_KEY),
      'the guard is set while the destination is unreachable',
    ).toBe('1')

    noteNavigationSettled(undefined) // a navigation landed
    expect(sessionStorage.getItem(RECOVERY_GUARD_KEY)).toBeNull()

    // A later, unrelated deploy reaches exhaustion again and still reloads the
    // destination instead of inheriting the spent guard.
    noteNavigationStarted({ fullPath: DESTINATION })
    handleRouteChunkError(CHUNK_ERROR, assign) // budget 1 (reset by the landing)
    handleRouteChunkError(CHUNK_ERROR, assign) // exhausted again: reload again

    expect(assign).toHaveBeenCalledTimes(2)
    expect(assign).toHaveBeenLastCalledWith(DESTINATION)
  })

  it('ignores an error that is not a route-chunk load failure', async () => {
    const { noteNavigationStarted, handleRouteChunkError, replace, assign } = await loadRecovery()

    noteNavigationStarted({ fullPath: DESTINATION })
    // A plain fetch failure is not a stale chunk map; recovering from it by
    // reloading the document would discard the user's work for nothing.
    handleRouteChunkError(new TypeError('Failed to fetch'), assign)

    expect(replace).not.toHaveBeenCalled()
    expect(assign).not.toHaveBeenCalled()
  })
})

describe('route-chunk recovery wiring (FAR-2594)', () => {
  it('the real router records the attempt and routes a chunk error into the recovery', async () => {
    vi.resetModules()
    const auth = await import('../lib/api/auth')
    const { default: router } = await import('../router')
    vi.spyOn(console, 'error').mockImplementation(() => {})

    // The guard needs an authenticated session; this route carries no manifest
    // gate, so no plan/tier response is needed.
    const b64url = (obj: Record<string, unknown>) =>
      btoa(JSON.stringify(obj)).replaceAll('+', '-').replaceAll('/', '_').replaceAll('=', '')
    auth.setAccessToken(
      `${b64url({ alg: 'none', typ: 'JWT' })}.${b64url({ org_role: 'admin' })}.test-signature`,
    )

    const replace = vi.spyOn(router, 'replace').mockResolvedValue(undefined)
    replace.mockClear()

    const path = '/__far-2594-chunk-wiring'
    router.addRoute({
      path,
      name: 'far-2594-chunk-wiring',
      component: () =>
        Promise.reject(
          new TypeError(
            'Failed to fetch dynamically imported module: https://example.test/assets/Missing.js',
          ),
        ),
    })

    await router.push(path).catch(() => {})

    // Both registration lines are load-bearing here: without
    // `noteNavigationStarted(to)` in `beforeEach` there is no destination to
    // retry, and without the `router.onError(...)` hook the handler never runs.
    // Either one missing leaves `replace` untouched.
    await vi.waitFor(() => expect(replace).toHaveBeenCalledWith(path))

    auth.clearAccessToken()
  })
})
