import { afterEach, describe, expect, it, vi } from 'vitest'

/**
 * FAR-2594: the route-chunk recovery contract.
 *
 * A tab left open across a deploy keeps the previous index.html in memory, so
 * any route it had not visited yet requests a chunk filename the deploy has
 * deleted. The click must not become a permanent silent no-op. The production
 * handlers are exported from `router/index.ts` because `router.onError` keeps
 * its listeners private — a navigation-based test would have to mount the
 * whole app graph (real components, plan store, API mocks) just to reach the
 * handler, and could not make a lazy chunk fail deterministically.
 *
 * The retry budget is module state, so every test re-imports the module after
 * `vi.resetModules()` to start from a clean budget.
 */
const CHUNK_ERROR = new TypeError(
  'Failed to fetch dynamically imported module: https://control.farna.dev/assets/AdminSpendLimitsView-DT2hhTK.js',
)
const DESTINATION = '/admin/costs/limits'

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
  return { ...mod, router, replace, assign }
}

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
