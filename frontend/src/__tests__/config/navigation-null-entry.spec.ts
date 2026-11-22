import { describe, it, expect, vi, afterEach } from 'vitest'

/**
 * The manifest is YAML, so nothing type-level stops a route entry from being
 * null or a scalar. `isManifest` only validates the top-level shape
 * (`routes`/`sidebar_groups` are objects), so every per-entry dereference must
 * guard itself. `buildSidebarGroups` already skips invalid entries; the
 * module-init route-name map did not, so one null entry threw a `TypeError`
 * while the module was being imported and took down every consumer of the nav
 * config (SidebarNav, CommandPalette, the router helpers).
 *
 * These tests drive the module with a manifest that carries a null entry and a
 * malformed scalar entry, and pin both halves of the contract: the module
 * initialises and the valid entries behave exactly as before.
 */

const buildGroup = { label: 'BUILD', order: 1, default_expanded: true, labelKey: 'nav.group_build' }

function partialManifest(routes: Record<string, unknown>) {
  return { default: { routes, sidebar_groups: { build: buildGroup } } }
}

const validRoutes = {
  '/': {
    name: 'dashboard',
    breadcrumb: 'Dashboard',
    sidebar_pinned: true,
    sidebar_order: 0,
    type: 'page',
  },
  // Deliberately out of sidebar_order sequence: the builder sorts by
  // `sidebar_order`, so the rendered order proves the sort still runs.
  '/pipelines': {
    name: 'pipeline-list',
    breadcrumb: 'Pipelines',
    sidebar_group: 'build',
    sidebar_order: 2,
    type: 'list_page',
  },
  '/runs': {
    name: 'runs-list',
    breadcrumb: 'Runs',
    sidebar_group: 'build',
    sidebar_order: 1,
    type: 'list_page',
  },
  '/schemas': {
    name: 'schemas',
    breadcrumb: 'Schemas',
    sidebar_group: 'build',
    sidebar_order: 3,
    type: 'list_page',
  },
}

const routesWithInvalidEntries = {
  ...validRoutes,
  '/broken/null': null,
  '/broken/scalar': 'not-a-route',
}

async function importNavigation() {
  return import('../../config/navigation')
}

describe('navigation config with a null/malformed manifest route entry', () => {
  afterEach(() => {
    vi.resetModules()
    vi.doUnmock('@/manifest.yaml')
    vi.restoreAllMocks()
  })

  it('initialises without throwing when a route entry is null or malformed', async () => {
    vi.resetModules()
    vi.doMock('@/manifest.yaml', () => partialManifest(routesWithInvalidEntries))

    const navigation = await importNavigation()

    expect(navigation.getNavGroups).toBeTypeOf('function')
    // Both lazy builders iterate the same route table, so exercise them too.
    expect(() => navigation.getNavGroups()).not.toThrow()
    expect(() => navigation.getPinnedNavItem()).not.toThrow()
  })

  it('still resolves a valid route name to its manifest path', async () => {
    vi.resetModules()
    vi.doMock('@/manifest.yaml', () => partialManifest(routesWithInvalidEntries))

    const { resolveActiveNavPath } = await importNavigation()

    // The path is NOT a manifest key, so only the route-name map can place it —
    // this fails if the guard dropped valid entries instead of invalid ones.
    expect(resolveActiveNavPath({ path: '/somewhere-else', name: 'runs-list', matched: [] })).toBe(
      '/runs',
    )
  })

  it('keeps the count and ordering of valid sidebar entries unchanged', async () => {
    vi.resetModules()
    vi.doMock('@/manifest.yaml', () => partialManifest(routesWithInvalidEntries))

    const { getNavGroups } = await importNavigation()

    const groups = getNavGroups()
    expect(groups).toHaveLength(1)
    expect(groups[0].id).toBe('build')
    expect(groups[0].items.map((item) => item.to)).toEqual(['/runs', '/pipelines', '/schemas'])
  })

  it('still builds the pinned row from the valid entries', async () => {
    vi.resetModules()
    vi.doMock('@/manifest.yaml', () => partialManifest(routesWithInvalidEntries))

    const { getPinnedNavItem } = await importNavigation()

    expect(getPinnedNavItem()?.to).toBe('/')
  })
})
