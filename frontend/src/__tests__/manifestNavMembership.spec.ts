/**
 * FAR-2605 — structural guard for the ratified sidebar shape.
 *
 * The sidebar is driven entirely by `frontend/src/manifest.yaml`, so a
 * regroup is a pure data edit: nothing in the code says "these are the nav
 * rows". That makes a dropped row invisible — delete one `sidebar_group` line
 * and the page silently leaves the nav with every other gate green.
 *
 * This spec pins membership explicitly against the REAL manifest (no mock), so
 * a future regroup has to state its intent here before it can ship. The
 * expectations are one route path per line, ordered exactly as the sidebar
 * renders them, so a reviewer can diff the shape.
 *
 * The arithmetic the ratified shape rests on:
 *
 * - 49 rendered grouped rows (the 49 nav rows of the ticket minus the pinned
 *   Dashboard, then the merged FAR-2597 change folds the separate Eval Editor
 *   and Eval Proposals rows into the single `/evals` list row, then FAR-2619
 *   gives the two previously URL-only system pages — `/admin/system/config`
 *   and `/admin/system/orgs` — a home in the SYSTEM group),
 * - + 3 detail routes that must keep a `sidebar_group` so the manifest
 *   validators resolve (`/runs/:id`, `/admin/errors/:id`,
 *   `/variants/compare/:batchId`) — they are skipped from rendering by
 *   `type: detail_page`,
 * - = 52 routes carrying a `sidebar_group`,
 * - + 1 pinned route (`/`) = 53 accounted routes,
 * - of which 49 + 1 pinned = 50 are rendered rows: the 51 live rows the nav
 *   had before FAR-2605 minus the 2 removals below, adjusted for FAR-2597,
 *   then plus the 2 FAR-2619 system pages.
 */
import { describe, it, expect } from 'vitest'
import manifest from '../manifest.yaml'
import enUS from '../locales/en-US.js'

interface ManifestRouteEntry {
  name?: string
  type?: string
  sidebar_group?: string | null
  sidebar_order?: number | null
  sidebar_pinned?: boolean | null
}

interface ManifestGroupEntry {
  label?: string
  labelKey?: string
}

const routes = (manifest as { routes: Record<string, ManifestRouteEntry> }).routes
const groups = (manifest as { sidebar_groups: Record<string, ManifestGroupEntry> }).sidebar_groups

/**
 * The rendered rows of each group, in `sidebar_order` order. 49 paths.
 * Rendered order is asserted because the ticket ratifies "most-used first".
 */
const EXPECTED_GROUPED_ROWS: Record<string, string[]> = {
  pipelines: [
    '/pipelines',
    '/library',
    '/schemas',
    '/admin/parameter-schemas',
    '/settings/triggers',
    '/admin/trigger-events',
    '/system-maps',
    '/determination',
  ],
  controls: [
    '/governance',
    '/settings/guardrails',
    '/evals',
  ],
  runs: [
    '/runs',
    '/settings/hitl-review',
    '/evidence',
    '/analytics',
    '/admin/errors',
    '/admin/audit',
    '/admin/costs',
    '/risk-assessment',
    '/admin/compliance/evidence-index',
    '/runs/diff',
    '/admin/notification-delivery',
    '/feedback/inbox',
    '/variants/compare',
  ],
  configuration: [
    '/admin/connectors',
    '/admin/model-backends',
    '/admin/runners/profiles',
    '/settings/mcp',
    '/admin/plugins',
    '/admin/assistant',
    '/settings/assistant',
    '/admin/node-categories',
  ],
  administration: [
    '/admin/users',
    '/admin/org',
    '/settings/license',
    '/settings/email',
    '/settings/teams',
    '/settings/sso',
    '/admin/evidence-redaction',
  ],
  system: [
    '/settings/runtime-config',
    '/settings/rate-limits',
    '/admin/feature-flags',
    '/settings/observability',
    '/settings/error-forwarders',
    '/admin/run-retention',
    '/admin/housekeeping',
    '/settings/monitoring',
    '/admin/system/config',
    '/admin/system/orgs',
  ],
}

/**
 * Detail routes that keep a `sidebar_group` reference (never rendered).
 * Their group must track the list page they belong to, or the manifest
 * validators (and the router's system-admin derivation) resolve against a
 * group that no longer exists.
 */
const EXPECTED_DETAIL_GROUP_ROUTES: Record<string, string[]> = {
  pipelines: ['/runs/:id'],
  runs: ['/admin/errors/:id', '/variants/compare/:batchId'],
}

/** Every route path carrying a `sidebar_group` — 49 rendered + 3 detail. */
const EXPECTED_GROUPED_PATHS = Object.keys(EXPECTED_GROUPED_ROWS).reduce<string[]>(
  (paths, group) => paths.concat(EXPECTED_GROUPED_ROWS[group], EXPECTED_DETAIL_GROUP_ROUTES[group] ?? []),
  [],
)

/** Routes deliberately removed from the nav: reachable elsewhere, never rows. */
const EXPECTED_REMOVED_FROM_NAV = ['/notifications', '/settings/notifications']

function routesInGroup(group: string): string[] {
  return Object.entries(routes)
    .filter(([, route]) => route.sidebar_group === group && route.type !== 'detail_page')
    .sort((a, b) => (a[1].sidebar_order ?? 0) - (b[1].sidebar_order ?? 0))
    .map(([path]) => path)
}

function detailRoutesInGroup(group: string): string[] {
  return Object.entries(routes)
    .filter(([, route]) => route.sidebar_group === group && route.type === 'detail_page')
    .map(([path]) => path)
    .sort()
}

describe('manifest nav membership (FAR-2605)', () => {
  it('renders exactly the ratified 49 rows, in order, across the 6 groups', () => {
    for (const [group, expected] of Object.entries(EXPECTED_GROUPED_ROWS)) {
      expect(routesInGroup(group), `rendered rows of sidebar group '${group}'`).toEqual(expected)
    }
  })

  it('keeps a group reference on every detail route that needs one', () => {
    for (const [group, expected] of Object.entries(EXPECTED_DETAIL_GROUP_ROUTES)) {
      expect(detailRoutesInGroup(group), `detail routes of sidebar group '${group}'`).toEqual(expected)
    }
  })

  it('carries a sidebar_group on exactly the expected 52 routes', () => {
    const actual = Object.entries(routes)
      .filter(([, route]) => Boolean(route.sidebar_group))
      .map(([path]) => path)
      .sort()

    expect(actual).toEqual([...EXPECTED_GROUPED_PATHS].sort())
    // 49 rendered rows + 3 detail references = 52; a future regroup that drops
    // a shipped page fails here rather than silently leaving the sidebar.
    expect(actual).toHaveLength(52)
    expect(EXPECTED_GROUPED_PATHS).toHaveLength(52)
  })

  it('pins exactly the Dashboard, and nothing else', () => {
    const pinned = Object.entries(routes)
      .filter(([, route]) => route.sidebar_pinned === true)
      .map(([path]) => path)

    expect(pinned).toEqual(['/'])
  })

  it('never declares a route as both pinned and grouped', () => {
    const both = Object.entries(routes)
      .filter(([, route]) => route.sidebar_pinned === true && Boolean(route.sidebar_group))
      .map(([path]) => path)

    expect(both).toEqual([])
  })

  it('keeps the two removed routes out of the nav entirely', () => {
    for (const path of EXPECTED_REMOVED_FROM_NAV) {
      const route = routes[path]
      expect(route, `${path} must still exist as a route`).toBeDefined()
      expect(route.sidebar_group ?? null, `${path} must carry no sidebar_group`).toBeNull()
      expect(route.sidebar_pinned ?? false, `${path} must not be pinned`).toBe(false)
    }
  })

  it('gives every declared group a labelKey that resolves in en-US', () => {
    // FAR-2605 deleted navigation.ts's groupLabelKeyMap: the manifest labelKey
    // is now the ONLY source of a translated group label, so an unset or
    // unresolvable key would render the raw key string in the sidebar.
    const locale = enUS as unknown as Record<string, unknown>
    const resolves = (key: string): boolean =>
      key
        .split('.')
        .reduce<unknown>((node, part) => (node as Record<string, unknown> | undefined)?.[part], locale) !== undefined

    const unresolved = Object.entries(groups)
      .filter(([, group]) => !group.labelKey || !resolves(group.labelKey))
      .map(([id, group]) => `${id} -> ${group.labelKey ?? '(none)'}`)

    expect(unresolved).toEqual([])
  })

  it('accounts for every nav row: 49 grouped + 1 pinned = 50 rows, next to 52 group-carrying routes', () => {
    const renderedGroupedRows = Object.values(EXPECTED_GROUPED_ROWS).reduce((sum, rows) => sum + rows.length, 0)
    const detailGroupRoutes = Object.values(EXPECTED_DETAIL_GROUP_ROUTES).reduce((sum, rows) => sum + rows.length, 0)
    const pinnedRows = Object.entries(routes).filter(([, route]) => route.sidebar_pinned === true).length

    // The ticket's 51 live rows before FAR-2605, minus the 2 removals, then
    // FAR-2597 folded the two eval rows into the single `/evals` row, then
    // FAR-2619 added the two system pages.
    expect(renderedGroupedRows + pinnedRows).toBe(50)
    // 49 rendered + 3 detail references = the 52 routes carrying a group.
    expect(renderedGroupedRows + detailGroupRoutes).toBe(52)
  })
})
