import { describe, it, expect, vi, afterEach } from 'vitest'

/**
 * FAR-2605 — the pinned nav row is a single row rendered ABOVE the groups.
 *
 * `buildPinnedNavItem()` picks the route the manifest marks
 * `sidebar_pinned: true`. The real manifest pins exactly one route (the
 * Dashboard), so the sort/first-wins/gate paths never execute against it.
 * These tests drive the builder with synthetic manifests to pin those paths,
 * including the documented "more than one pinned route is not supported — the
 * first by `sidebar_order` wins" rule.
 */

const pipelinesGroup = { label: 'PIPELINES', order: 1, default_expanded: true }

type RouteFixture = Record<string, unknown>

function mockManifest(routes: RouteFixture, groups: Record<string, unknown> = { pipelines: pipelinesGroup }) {
  return { default: { routes, sidebar_groups: groups } }
}

describe('getPinnedNavItem (FAR-2605)', () => {
  afterEach(() => {
    vi.resetModules()
    vi.doUnmock('@/manifest.yaml')
    vi.restoreAllMocks()
  })

  it('returns null when the manifest is not valid', async () => {
    vi.resetModules()
    vi.doMock('@/manifest.yaml', () => ({ default: null }))
    const { getPinnedNavItem } = await import('../../config/navigation')
    expect(getPinnedNavItem()).toBeNull()
  })

  it('returns null when no route is pinned', async () => {
    vi.resetModules()
    vi.doMock(
      '@/manifest.yaml',
      () =>
        mockManifest({
          '/pipelines': { name: 'pipeline-list', breadcrumb: 'Pipelines', sidebar_group: 'pipelines', sidebar_order: 1 },
        }),
    )
    const { getPinnedNavItem } = await import('../../config/navigation')
    expect(getPinnedNavItem()).toBeNull()
  })

  it('selects the lowest sidebar_order when more than one route is pinned', async () => {
    vi.resetModules()
    vi.doMock(
      '@/manifest.yaml',
      () =>
        mockManifest({
          '/later': { name: 'later', breadcrumb: 'Later', sidebar_pinned: true, sidebar_order: 20 },
          '/earlier': { name: 'earlier', breadcrumb: 'Earlier', sidebar_pinned: true, sidebar_order: 5 },
        }),
    )
    const { getPinnedNavItem } = await import('../../config/navigation')
    expect(getPinnedNavItem()?.to).toBe('/earlier')
  })

  it('treats a missing sidebar_order as 0 when the earlier entry is unpinned-ordered', async () => {
    vi.resetModules()
    vi.doMock(
      '@/manifest.yaml',
      () =>
        mockManifest({
          '/no-order': { name: 'no-order', breadcrumb: 'No order', sidebar_pinned: true },
          '/ordered': { name: 'ordered', breadcrumb: 'Ordered', sidebar_pinned: true, sidebar_order: 2 },
        }),
    )
    const { getPinnedNavItem } = await import('../../config/navigation')
    expect(getPinnedNavItem()?.to).toBe('/no-order')
  })

  it('treats a missing sidebar_order as 0 when the later entry is unpinned-ordered', async () => {
    vi.resetModules()
    vi.doMock(
      '@/manifest.yaml',
      () =>
        mockManifest({
          '/ordered': { name: 'ordered', breadcrumb: 'Ordered', sidebar_pinned: true, sidebar_order: 2 },
          '/no-order': { name: 'no-order', breadcrumb: 'No order', sidebar_pinned: true },
        }),
    )
    const { getPinnedNavItem } = await import('../../config/navigation')
    expect(getPinnedNavItem()?.to).toBe('/no-order')
  })

  it("carries a pinned route's own system-admin gate", async () => {
    vi.resetModules()
    vi.doMock(
      '@/manifest.yaml',
      () =>
        mockManifest({
          '/admin-only': {
            name: 'admin-only',
            breadcrumb: 'Admin only',
            sidebar_pinned: true,
            sidebar_order: 1,
            system_admin_only: true,
          },
        }),
    )
    const { getPinnedNavItem } = await import('../../config/navigation')
    expect(getPinnedNavItem()?.systemAdminOnly).toBe(true)
  })

  /**
   * The real Dashboard is community-tier with no gate, so
   * `getVisiblePinnedNavItem`'s hidden branch never executes against it. Drive
   * it with a synthetic pinned route that carries a role gate.
   */
  const baseCtx = {
    isSystemAdmin: false,
    userRole: 'viewer',
    userPermissions: [] as string[],
    devMode: false,
    tierInfoLoaded: true,
    planLoaded: true,
    isEntitlementGranted: () => true,
    isDemoSession: false,
    isFeatureEnabled: () => true,
  }

  it('hides a gated pinned route from a user who fails its role gate', async () => {
    vi.resetModules()
    vi.doMock(
      '@/manifest.yaml',
      () =>
        mockManifest({
          '/admin-only': {
            name: 'admin-only',
            breadcrumb: 'Admin only',
            sidebar_pinned: true,
            sidebar_order: 1,
            required_roles: ['admin'],
          },
        }),
    )
    const { getVisiblePinnedNavItem } = await import('../../config/navigation')
    expect(getVisiblePinnedNavItem({ ...baseCtx })).toBeNull()
  })

  it('shows the same gated pinned route to a user who passes its role gate', async () => {
    vi.resetModules()
    vi.doMock(
      '@/manifest.yaml',
      () =>
        mockManifest({
          '/admin-only': {
            name: 'admin-only',
            breadcrumb: 'Admin only',
            sidebar_pinned: true,
            sidebar_order: 1,
            required_roles: ['admin'],
          },
        }),
    )
    const { getVisiblePinnedNavItem } = await import('../../config/navigation')
    expect(getVisiblePinnedNavItem({ ...baseCtx, userRole: 'admin' })?.to).toBe('/admin-only')
  })
})
