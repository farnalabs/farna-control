import { describe, it, expect, vi, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { useRoute } from 'vue-router'
import manifest from '@/manifest.yaml'
import SidebarLink from '../../components/SidebarLink.vue'
import {
  getNavGroups,
  getPinnedNavItem,
  isNavItemActive,
  resolveActiveNavPath,
  type NavActiveRoute,
} from '../../config/navigation'

/**
 * FAR-2583 — sidebar active state is derived from the route's manifest
 * ancestry (the `parent` chain), not from a path prefix. These tests are the
 * complete sweep of the bug class: every nav item that owns child/detail
 * routes has a table row, and every row asserts BOTH the resolved owner and
 * that no other sidebar item is active for that route.
 */

interface RouteFixture {
  path: string
  name?: string
  matched?: Array<{ name?: string; path: string }>
}

function routeFor(fixture: RouteFixture | string): NavActiveRoute {
  if (typeof fixture === 'string') return { path: fixture, matched: [] }
  return { path: fixture.path, name: fixture.name, matched: fixture.matched ?? [] }
}

function mountAt(fixture: RouteFixture | string) {
  vi.mocked(useRoute).mockReturnValue(routeFor(fixture) as unknown as ReturnType<typeof useRoute>)
}

function mountLink(to: string, exact?: boolean) {
  return mount(SidebarLink, {
    props: { to, icon: 'File', label: to, exact },
    global: { stubs: { SvgIcon: { template: '<span class="svg-stub" />' } } },
  })
}

// FAR-2605: the Dashboard is a pinned row above the groups, not part of any
// group, so include it explicitly when sweeping the rendered nav items.
const pinnedNavItem = getPinnedNavItem()
const navItems = [
  ...(pinnedNavItem ? [pinnedNavItem] : []),
  ...getNavGroups().flatMap((group) => group.items),
]

function activeItemPaths(fixture: RouteFixture | string): string[] {
  const route = routeFor(fixture)
  return navItems.filter((item) => isNavItemActive(item, route)).map((item) => item.to)
}

interface OwnedCase extends RouteFixture {
  owner: string
}

/** One row per nav item that owns subpages; `subpages` is exhaustive. */
const OWNED_SUBPAGES: Array<{ owner: string; subpages: RouteFixture[] }> = [
  {
    owner: '/library',
    subpages: [
      { path: '/library/collection-1/create-pipeline', name: 'library-pipeline-wizard' },
      { path: '/library/collections/new', name: 'library-collection-create' },
      { path: '/library/collections/collection-1', name: 'library-collection-detail' },
    ],
  },
  {
    owner: '/pipelines',
    subpages: [
      { path: '/pipelines/pipeline-1/editor', name: 'pipeline-editor' },
      { path: '/composites/composite-1/editor', name: 'composite-editor' },
      // The copy wizard is a Pipelines surface: its own view links back to
      // /pipelines, and its manifest parent is /pipelines (FAR-2583).
      { path: '/pipelines/copy', name: 'pipeline-copy' },
    ],
  },
  {
    owner: '/runs',
    subpages: [{ path: '/runs/run-1', name: 'run-detail' }],
  },
  {
    owner: '/schemas',
    subpages: [
      { path: '/schemas/editor', name: 'schema-editor' },
      { path: '/schemas/editor/schema-1', name: 'schema-editor' },
      { path: '/schemas/infer', name: 'schema-infer' },
    ],
  },
  {
    owner: '/admin/costs',
    subpages: [
      { path: '/admin/costs/limits', name: 'admin-costs-limits' },
      { path: '/admin/costs/controls', name: 'admin-costs-controls' },
      { path: '/admin/costs/components', name: 'admin-costs-components' },
    ],
  },
  {
    owner: '/admin/errors',
    subpages: [{ path: '/admin/errors/error-1', name: 'admin-error-detail' }],
  },
  {
    owner: '/system-maps',
    subpages: [{ path: '/system-maps/map-1', name: 'system-map-detail' }],
  },
  {
    owner: '/variants/compare',
    subpages: [{ path: '/variants/compare/batch-1', name: 'variant-compare-detail' }],
  },
  {
    owner: '/admin/runners/profiles',
    subpages: [
      { path: '/admin/runners/profiles', name: 'admin-runners-profiles' },
      { path: '/admin/runners/concurrency', name: 'admin-runners-concurrency' },
      { path: '/admin/runners/profiles/new', name: 'admin-runners-profile-new' },
      { path: '/admin/runners/profiles/profile-1/edit', name: 'admin-runners-profile-edit' },
    ],
  },
]

const OWNED_CASES: OwnedCase[] = OWNED_SUBPAGES.flatMap(({ owner, subpages }) =>
  subpages.map((subpage) => ({ owner, ...subpage })),
)

/**
 * Single-page items must keep activating on their own path only.
 */
const SINGLE_PAGE_CASES = [
  '/',
  '/governance',
  '/analytics',
  '/pipelines',
  '/library',
  '/runs',
  '/determination',
  '/schemas',
  '/admin/costs',
  '/admin/runners/profiles',
  '/system-maps',
  '/variants/compare',
  '/settings/mcp',
  '/admin/users',
]

describe('FAR-2583 sidebar active state — manifest ancestry', () => {
  afterEach(() => {
    vi.mocked(useRoute).mockReturnValue(routeFor('/') as unknown as ReturnType<typeof useRoute>)
  })

  describe.each(OWNED_CASES)('$path', ({ owner, path, name }) => {
    it(`resolves the owner "${owner}" from the route name`, () => {
      expect(resolveActiveNavPath(routeFor({ path, name }))).toBe(owner)
    })

    it(`resolves the owner "${owner}" from the path alone`, () => {
      // The same route with no name exercises the manifest path-pattern index
      // (dynamic routes and detail pages must not depend on route names).
      expect(resolveActiveNavPath(routeFor(path))).toBe(owner)
    })

    it(`marks only "${owner}" active`, () => {
      expect(activeItemPaths({ path, name })).toEqual([owner])
    })
  })

  it('covers every nav item that owns subpages in the manifest', () => {
    const routes = (manifest as {
      routes: Record<
        string,
        { parent?: string | null; sidebar_group?: string | null; sidebar_order?: number | null; type?: string }
      >
    }).routes
    const isNavItem = (path: string) => {
      const entry = routes[path]
      return (
        Boolean(entry?.sidebar_group) &&
        typeof entry?.sidebar_order === 'number' &&
        entry?.type !== 'detail_page' &&
        path !== '/'
      )
    }
    const owners = new Set<string>()
    for (const path of Object.keys(routes)) {
      if (isNavItem(path)) continue
      let current = routes[path]?.parent ?? null
      while (typeof current === 'string' && routes[current]) {
        if (isNavItem(current)) {
          owners.add(current)
          break
        }
        current = routes[current].parent ?? null
      }
    }
    expect([...owners].sort()).toEqual([...OWNED_SUBPAGES.map((row) => row.owner)].sort())
  })

  it.each(SINGLE_PAGE_CASES)('activates "%s" on its own path only', (path) => {
    expect(activeItemPaths(path)).toEqual([path])
  })

  it('never activates the Dashboard item for a route that merely parents to "/"', () => {
    // /onboarding and /admin/my-profile sit under the root in the manifest but
    // are not Dashboard surfaces, so no sidebar item owns them.
    expect(activeItemPaths('/onboarding')).toHaveLength(0)
    expect(activeItemPaths('/admin/my-profile')).toHaveLength(0)
    expect(activeItemPaths('/admin/my-profile')).not.toContain('/')
  })

  it('activates Dashboard on the root route', () => {
    expect(activeItemPaths('/')).toEqual(['/'])
  })

  it('keeps a nested sidebar item from double-activating its broad sibling', () => {
    // /runs/diff is its own nav item; the /runs item must not also light up.
    const fixture = { path: '/runs/diff', name: 'runs-diff' }
    expect(activeItemPaths(fixture)).toEqual(['/runs/diff'])

    mountAt(fixture)
    const runs = mountLink('/runs')
    expect(runs.find('a').classes()).not.toContain('active')
    runs.unmount()
    const diff = mountLink('/runs/diff')
    expect(diff.find('a').classes()).toContain('active')
    diff.unmount()
  })

  it('resolves a nested child route from matched records when the name is absent', () => {
    const route = routeFor({
      path: '/admin/runners/profiles',
      matched: [{ path: '/admin/runners/profiles', name: 'admin-runners-profiles' }],
    })
    expect(resolveActiveNavPath(route)).toBe('/admin/runners/profiles')
  })

  it('leaves the sidebar inactive for a route the manifest does not describe', () => {
    expect(resolveActiveNavPath({ path: '/dev/metrics', name: 'dev-metrics' })).toBeUndefined()
  })

  it('has a resolvable, acyclic parent chain for every manifest route', () => {
    const routes = (manifest as { routes: Record<string, { parent?: string | null }> }).routes
    for (const path of Object.keys(routes)) {
      const seen = new Set<string>()
      let current = routes[path]?.parent ?? null
      while (typeof current === 'string') {
        expect(routes[current]).toBeDefined()
        expect(seen.has(current)).toBe(false)
        seen.add(current)
        current = routes[current]?.parent ?? null
      }
    }
  })

  it('resolves a dynamic route with no name or matched records from the path', () => {
    expect(resolveActiveNavPath({ path: '/runs/run-1' })).toBe('/runs')
  })

  it('resolves a matched record whose path is in the manifest even without a name', () => {
    const route = routeFor({ path: '/unknown', matched: [{ path: '/runs' }] })
    expect(resolveActiveNavPath(route)).toBe('/runs')
  })

  it('skips matched records that the manifest does not describe', () => {
    const route = routeFor({
      path: '/unknown',
      matched: [{ name: 'not-a-manifest-name', path: '/not-in-manifest' }],
    })
    expect(resolveActiveNavPath(route)).toBeUndefined()
  })

  it('ignores a non-string route name', () => {
    expect(
      resolveActiveNavPath({ path: '/dev/metrics', name: Symbol('x'), matched: [] }),
    ).toBeUndefined()
  })

  describe('isNavItemActive fallback for routes outside the manifest', () => {
    const unknown = { path: '/dev/metrics', matched: [] }

    it('matches an item whose target equals the unknown path', () => {
      expect(isNavItemActive({ to: '/dev/metrics', exact: true }, unknown)).toBe(true)
    })

    it('keeps prefix matching for a non-exact item', () => {
      expect(isNavItemActive({ to: '/dev', exact: false }, unknown)).toBe(true)
    })

    it('does not prefix-match an exact item', () => {
      expect(isNavItemActive({ to: '/dev', exact: true }, unknown)).toBe(false)
    })

    it('never prefix-matches the root item', () => {
      expect(isNavItemActive({ to: '/', exact: false }, unknown)).toBe(false)
    })
  })

  describe('rendered SidebarLink state', () => {
    it.each(OWNED_CASES)('sets class=active and aria-current on "$owner" for $path', ({ owner, path, name }) => {
      mountAt({ path, name })

      const expected = mountLink(owner)
      expect(expected.find('a').classes()).toContain('active')
      expect(expected.find('a').attributes('aria-current')).toBe('page')
      expected.unmount()

      // Dashboard is the highest-risk false positive (every manifest chain
      // ends at "/") and must never light up on a subpage.
      if (owner !== '/') {
        const dashboard = mountLink('/')
        expect(dashboard.find('a').classes()).not.toContain('active')
        expect(dashboard.find('a').attributes('aria-current')).toBeUndefined()
        dashboard.unmount()
      }
    })

    it('keeps the Dashboard link inert away from the root while a parent item is active', () => {
      mountAt({ path: '/schemas/infer', name: 'schema-infer' })

      const dashboard = mountLink('/')
      expect(dashboard.find('a').classes()).not.toContain('active')
      expect(dashboard.find('a').attributes('aria-current')).toBeUndefined()
      dashboard.unmount()

      const schemas = mountLink('/schemas', true)
      expect(schemas.find('a').classes()).toContain('active')
      expect(schemas.find('a').attributes('aria-current')).toBe('page')
      schemas.unmount()
    })
  })
})
