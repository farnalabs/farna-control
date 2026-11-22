import manifest from '@/manifest.yaml'

export interface NavItem {
  to: string
  icon: string
  label: string
  labelKey: string
  exact?: boolean
  visibility?: 'public' | 'public_preview' | 'private_preview' | 'in_dev'
  requiredRoles?: string[] | null
  requiredTier?: string | null
  requiredPermissions?: string[] | null
  requiredFeatureFlag?: string | null
  // FAR-1796: route-entry (or inherited group) system-admin gate, mirroring the
  // router's `requiresSystemAdmin` derivation. A visible group is not enough —
  // an item gated only by this flag must stay hidden from non-system-admins.
  systemAdminOnly?: boolean
}

export interface NavGroup {
  id: string
  label: string
  /** i18n key for the group label; absent when the manifest declares none. */
  labelKey?: string
  items: NavItem[]
  defaultCollapsed: boolean
  systemAdminOnly?: boolean
}

const routeConfigMap: Record<string, { icon: string; labelKey: string }> = {
  dashboard: { icon: 'LayoutDashboard', labelKey: 'components.SidebarNav.item_dashboard' },
  'governance-stack': { icon: 'LayoutTemplate', labelKey: 'components.SidebarNav.item_governance_stack' },
  analytics: { icon: 'BarChart', labelKey: 'components.SidebarNav.item_analytics' },
  evidence: { icon: 'FileText', labelKey: 'components.SidebarNav.item_evidence' },
  'risk-assessment': { icon: 'AlertTriangle', labelKey: 'components.SidebarNav.item_risk_assessment' },
  'admin-compliance-evidence-index': { icon: 'FileText', labelKey: 'components.SidebarNav.item_compliance_evidence_index' },
  notifications: { icon: 'Bell', labelKey: 'components.SidebarNav.item_notifications' },
  library: { icon: 'BookOpen', labelKey: 'components.SidebarNav.item_library' },
  'system-maps': { icon: 'Map', labelKey: 'components.SidebarNav.item_system_maps' },
  determination: { icon: 'FlaskConical', labelKey: 'components.SidebarNav.item_determination' },
  'pipeline-list': { icon: 'GitFork', labelKey: 'components.SidebarNav.item_my_pipelines' },

  'pipeline-copy': { icon: 'Copy', labelKey: 'components.SidebarNav.item_copy_pipeline' },
  'runs-list': { icon: 'CirclePlay', labelKey: 'components.SidebarNav.item_runs_list' },
  'runs-diff': { icon: 'GitCommit', labelKey: 'components.SidebarNav.item_output_diff' },
  'evals-list': { icon: 'CheckSquare', labelKey: 'components.SidebarNav.item_evals' },
  'eval-editor': { icon: 'FileText', labelKey: 'components.SidebarNav.item_eval_editor' },
  'eval-proposals-queue': { icon: 'Clipboard', labelKey: 'components.SidebarNav.item_eval_proposals' },
  'variant-compare': { icon: 'GitFork', labelKey: 'components.SidebarNav.item_variants' },
  schemas: { icon: 'Database', labelKey: 'components.SidebarNav.item_schemas' },
  'schema-editor': { icon: 'FileText', labelKey: 'components.SidebarNav.item_editor' },
  'schema-infer': { icon: 'Search', labelKey: 'components.SidebarNav.item_infer' },
  'settings-assistant': { icon: 'Bot', labelKey: 'components.SidebarNav.item_my_skills' },
  'admin-assistant': { icon: 'Settings', labelKey: 'components.SidebarNav.item_admin_config' },
  'settings-teams': { icon: 'Users', labelKey: 'components.SidebarNav.item_teams' },
  'settings-sso': { icon: 'Shield', labelKey: 'components.SidebarNav.item_sso' },
  'settings-license': { icon: 'KeyRound', labelKey: 'components.SidebarNav.item_license' },
  'settings-mcp': { icon: 'Cable', labelKey: 'components.SidebarNav.item_mcp' },
  'settings-triggers': { icon: 'Zap', labelKey: 'components.SidebarNav.item_triggers' },
  'settings-runtime-config': { icon: 'Settings', labelKey: 'components.SidebarNav.item_runtime_config' },
  'settings-rate-limits': { icon: 'Gauge', labelKey: 'components.SidebarNav.item_rate_limits' },
  'settings-monitoring': { icon: 'Eye', labelKey: 'components.SidebarNav.item_browser_monitoring' },
  'settings-hitl-review': { icon: 'ShieldQuestion', labelKey: 'components.SidebarNav.item_hitl_review' },
  'settings-observability': { icon: 'Eye', labelKey: 'components.SidebarNav.item_observability' },
  'settings-email': { icon: 'Mail', labelKey: 'components.SidebarNav.item_email_settings' },
  'settings-notifications': { icon: 'Bell', labelKey: 'components.SidebarNav.item_notification_preferences' },
  'settings-error-forwarders': { icon: 'AlertTriangle', labelKey: 'components.SidebarNav.item_error_forwarders' },
  'settings-guardrails': { icon: 'ShieldAlert', labelKey: 'components.SidebarNav.item_settings_guardrails' },
  'admin-users': { icon: 'UserCircle', labelKey: 'components.SidebarNav.item_users' },
  'admin-org': { icon: 'Building', labelKey: 'components.SidebarNav.item_org_settings' },
  'admin-audit': { icon: 'FileText', labelKey: 'components.SidebarNav.item_audit_log' },
  'admin-trigger-events': { icon: 'History', labelKey: 'components.SidebarNav.item_trigger_event_log' },
  'admin-costs': { icon: 'DollarSign', labelKey: 'components.SidebarNav.item_costs' },
  'admin-costs-limits': { icon: 'CreditCard', labelKey: 'components.SidebarNav.item_spend_limits' },
  'admin-costs-controls': { icon: 'SlidersHorizontal', labelKey: 'components.SidebarNav.item_cost_controls' },
  'admin-connectors': { icon: 'Plug', labelKey: 'components.SidebarNav.item_connectors' },
  'admin-model-backends': { icon: 'Cpu', labelKey: 'components.SidebarNav.item_model_backends' },
  'admin-node-categories': { icon: 'Tag', labelKey: 'components.SidebarNav.item_node_categories' },
  'admin-evidence-redaction': { icon: 'Shield', labelKey: 'components.SidebarNav.item_evidence_redaction' },
  'admin-feature-flags': { icon: 'Flag', labelKey: 'components.SidebarNav.item_feature_flags' },
  'admin-run-retention': { icon: 'Clock', labelKey: 'components.SidebarNav.item_run_retention' },
  // FAR-591 D5: the Runners page replaced the Environment Profiles +
  // Sandbox Concurrency surfaces (net -1 sidebar item). Only the profiles
  // tab carries the sidebar_group; the concurrency tab is a child route.
  'admin-runners-profiles': { icon: 'Container', labelKey: 'components.SidebarNav.item_runners' },
  'admin-errors': { icon: 'AlertTriangle', labelKey: 'components.SidebarNav.item_error_dashboard' },
  'admin-error-detail': { icon: 'AlertTriangle', labelKey: 'components.SidebarNav.item_error_dashboard' },
  'admin-notification-delivery': { icon: 'Bell', labelKey: 'components.SidebarNav.item_notification_log' },
  'admin-plugins': { icon: 'Puzzle', labelKey: 'components.SidebarNav.item_plugins' },
  'feedback-inbox': { icon: 'MessageSquare', labelKey: 'components.SidebarNav.item_feedback_inbox' },
  'admin-housekeeping': { icon: 'Broom', labelKey: 'components.SidebarNav.item_housekeeping' },
  'admin-parameter-schemas': { icon: 'FileText', labelKey: 'components.SidebarNav.item_parameter_schemas' },
  'admin-system-config': { icon: 'Settings', labelKey: 'components.SidebarNav.item_system_config' },
  'admin-system-orgs': { icon: 'Building', labelKey: 'components.SidebarNav.item_organisations' },
}

// FAR-2605: the per-group fallback key map is gone. Every declared group in
// the real manifest carries a `labelKey`, so the map only ever shadowed a
// group id, and a stale entry outlived its group as an unreferenced locale key.
// A group with no `labelKey` now falls back to its manifest `label` (the
// pre-i18n English string), never to a synthetic unresolved key.

interface ManifestRoute {
  name: string
  breadcrumb: string
  // FAR-2583: the manifest ancestry chain. A sidebar item owns every route
  // whose chain reaches it, which is what keeps the owning item active on
  // child/detail pages that share no path prefix with it.
  parent?: string | null
  sidebar_group?: string | null
  sidebar_order?: number | null
  type?: string
  exact?: boolean
  visibility?: 'public' | 'public_preview' | 'private_preview' | 'in_dev'
  required_tier?: string | null
  required_roles?: string[] | null
  required_permissions?: string[] | null
  feature_flag?: string | null
  // FAR-1796: route-entry system-admin gate (blank sidebar_group routes carry
  // their gate here, not on a sidebar group).
  system_admin_only?: boolean
  // FAR-2605: a pinned route renders as a single row ABOVE the groups (the
  // Dashboard), never inside one — so it must carry no `sidebar_group`.
  sidebar_pinned?: boolean | null
}

interface ManifestSidebarGroup {
  label: string
  order: number
  default_expanded: boolean
  labelKey?: string
  system_admin_only?: boolean
}

interface Manifest {
  routes: Record<string, ManifestRoute>
  sidebar_groups: Record<string, ManifestSidebarGroup>
}

function isManifestRoute(
  route: ManifestRoute,
): route is ManifestRoute & { sidebar_group: string; sidebar_order: number } {
  return typeof route.sidebar_group === 'string' && typeof route.sidebar_order === 'number'
}

function isManifest(obj: unknown): obj is Manifest {
  if (typeof obj !== 'object' || obj === null) return false
  const m = obj as Record<string, unknown>
  return (
    typeof m.routes === 'object' && m.routes !== null
    && typeof m.sidebar_groups === 'object' && m.sidebar_groups !== null
  )
}

/**
 * Runtime check for one route entry. The manifest is YAML, so a null or scalar
 * entry survives `isManifest` (which only shapes the top-level object) and any
 * dereference of it throws — at module init for the route-name map, which took
 * down every consumer of this module. Every per-entry loop skips a non-entry
 * the same way `buildSidebarGroups` skips a route with no sidebar group.
 */
function isManifestEntry(entry: unknown): entry is ManifestRoute {
  return (
    typeof entry === 'object'
    && entry !== null
    && typeof (entry as ManifestRoute).name === 'string'
  )
}

function resolveSystemAdminOnly(
  route: ManifestRoute,
  group: ManifestSidebarGroup,
): true | undefined {
  if (route.system_admin_only || group.system_admin_only) return true
  return undefined
}

/**
 * The minimal shape of a resolved route needed to place it in the manifest
 * ancestry. Structurally satisfied by vue-router's `RouteLocationNormalized`,
 * and by the partial route objects unit tests drive the components with.
 */
export interface NavActiveRoute {
  path: string
  name?: string | symbol | null
  matched?: Array<{ name?: string | symbol | null; path: string }>
}

interface CompiledManifestPattern {
  path: string
  regex: RegExp
  // A pattern whose final segment is `:param` also matches the path without
  // that segment — vue-router declares such params optional on the routes that
  // allow it (e.g. `/schemas/editor/:id?`), and the manifest key omits the `?`.
  optionalTailRegex?: RegExp
  literalSegments: number
}

const manifestRouteTable: Record<string, ManifestRoute> = isManifest(manifest)
  ? (manifest as Manifest).routes
  : {}

const manifestPathByName = new Map<string, string>(
  Object.entries(manifestRouteTable)
    .filter(([, entry]) => isManifestEntry(entry))
    .map(([path, entry]): [string, string] => [entry.name, path]),
)

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

// Compile each manifest key (static routes and `:param` patterns) once, so a
// concrete URL that carries no manifest route name can still be placed in the
// ancestry. Specificity is the number of literal segments, so the most
// specific pattern wins (`/admin/runners/profiles/new` over a `:id` sibling).
function compileManifestPattern(path: string): CompiledManifestPattern {
  let literalSegments = 0
  const segments = path.split('/')
  const translated = segments.map((segment) => {
    if (segment.startsWith(':')) return '[^/]+'
    if (segment) literalSegments += 1
    return escapeRegExp(segment)
  })
  // A key whose final segment is a `:param` also owns its collection path
  // (`/schemas/editor/:id` owns `/schemas/editor`); the manifest omits
  // vue-router's optional marker, so the alias drops the trailing param.
  const lastSegment = path.slice(path.lastIndexOf('/') + 1)
  const optionalTailRegex = lastSegment.startsWith(':')
    ? new RegExp(`^${translated.slice(0, -1).join('/')}$`)
    : undefined
  return { path, regex: new RegExp(`^${translated.join('/')}$`), optionalTailRegex, literalSegments }
}

const manifestPathPatterns = Object.keys(manifestRouteTable)
  .map(compileManifestPattern)
  .sort((a, b) => b.literalSegments - a.literalSegments)

function matchesPattern(pattern: CompiledManifestPattern, path: string): boolean {
  return pattern.regex.test(path) || Boolean(pattern.optionalTailRegex?.test(path))
}

function resolvePatternPath(path: string): string | undefined {
  return manifestPathPatterns.find((pattern) => matchesPattern(pattern, path))?.path
}

function manifestPathForName(name: string | symbol | null | undefined): string | undefined {
  if (typeof name !== 'string') return undefined
  return manifestPathByName.get(name)
}

function resolveMatchedPath(matched: NavActiveRoute['matched']): string | undefined {
  const records = matched ?? []
  // vue-router orders `matched` parent-first, so walk from the deepest record
  // back — the most specific manifest route must win.
  for (let index = records.length - 1; index >= 0; index -= 1) {
    const record = records[index]
    const byName = manifestPathForName(record.name)
    if (byName !== undefined) return byName
    if (Object.prototype.hasOwnProperty.call(manifestRouteTable, record.path)) return record.path
  }
  return undefined
}

function resolveManifestPath(route: NavActiveRoute): string | undefined {
  const byName = manifestPathForName(route.name)
  if (byName !== undefined) return byName
  const fromMatched = resolveMatchedPath(route.matched)
  if (fromMatched !== undefined) return fromMatched
  if (Object.prototype.hasOwnProperty.call(manifestRouteTable, route.path)) return route.path
  return resolvePatternPath(route.path)
}

function isManifestNavItem(entry: ManifestRoute): boolean {
  // Mirrors the filter in buildSidebarGroups exactly: a route becomes a
  // sidebar item only when it carries both a group and an order, and is not a
  // detail page. Keeping the two in step means the resolved owner is always a
  // path that an actual item renders.
  return isManifestRoute(entry) && entry.type !== 'detail_page'
}

/**
 * The sidebar item that owns a manifest route, following the `parent` chain.
 *
 * The Dashboard entry (`/`) owns only itself, so a chain that merely passes
 * through the root yields no owner. A chain that ends before reaching a
 * sidebar item (a form page, a bare detail route) likewise has no owner.
 * Parents are authored to always name a real route; a unit test asserts every
 * parent exists and no chain cycles, so this walk needs no defensive branch.
 */
function resolveOwnerForPath(path: string): string | undefined {
  let current: string | undefined = path
  while (current !== undefined) {
    if (current === '/') return path === '/' ? '/' : undefined
    const entry: ManifestRoute | null | undefined = manifestRouteTable[current]
    if (!isManifestEntry(entry)) return undefined
    if (isManifestNavItem(entry)) return current
    current = entry.parent ?? undefined
  }
  return undefined
}

const manifestOwnerPath = new Map<string, string>()
for (const path of Object.keys(manifestRouteTable)) {
  const owner = resolveOwnerForPath(path)
  if (owner !== undefined) manifestOwnerPath.set(path, owner)
}

/**
 * FAR-2583: the manifest path of the sidebar item that owns `route`.
 *
 * Walks the current route's manifest ancestry (the same `parent` chain the
 * router hydrates into `meta.parent`) and returns the nearest ancestor that is
 * a sidebar item. A detail page such as `/runs/:id` therefore reports `/runs`,
 * and a child surface such as `/schemas/infer` reports `/schemas` — even
 * though neither shares a path prefix with the item.
 *
 * The Dashboard entry owns only `/` itself: a chain that merely passes through
 * the root must never activate it. When the route cannot be placed in the
 * manifest (`/dev/metrics`, a redirect target, a unit-test fixture route) this
 * returns `undefined` and callers fall back to the historic prefix rules.
 */
export function resolveActiveNavPath(route: NavActiveRoute): string | undefined {
  const manifestPath = resolveManifestPath(route)
  if (manifestPath === undefined) return undefined
  return manifestOwnerPath.get(manifestPath)
}

/**
 * Whether a sidebar item is the active one for `route`.
 *
 * Exactly one item can be active per route because activation is an equality
 * check against the resolved owner path. Routes the manifest does not describe
 * keep the historic path/prefix semantics (the root item is always exact-only).
 */
export function isNavItemActive(
  item: Pick<NavItem, 'to' | 'exact'>,
  route: NavActiveRoute,
): boolean {
  const ownerPath = resolveActiveNavPath(route)
  if (ownerPath !== undefined) return item.to === ownerPath
  return (
    route.path === item.to ||
    (!item.exact && item.to !== '/' && route.path.startsWith(item.to + '/'))
  )
}

/** Map one manifest route entry onto the NavItem the sidebar renders. */
function toNavItem(
  path: string,
  route: ManifestRoute,
  systemAdminOnly: true | undefined,
): NavItem {
  return {
    to: path,
    icon: routeConfigMap[route.name]?.icon ?? 'File',
    label: route.breadcrumb ?? route.name,
    labelKey: routeConfigMap[route.name]?.labelKey ?? `nav.${route.name}`,
    exact: route.exact ?? undefined,
    visibility: (route.visibility as 'public_preview' | 'private_preview' | 'in_dev') ?? undefined,
    requiredRoles: route.required_roles ?? null,
    requiredTier: route.required_tier ?? null,
    requiredPermissions: route.required_permissions ?? null,
    requiredFeatureFlag: route.feature_flag ?? null,
    systemAdminOnly,
  }
}

/**
 * FAR-2605: the single row pinned above the groups (`sidebar_pinned: true`).
 * Pinned routes belong to no group, so they are never part of
 * `buildSidebarGroups()`; the manifest validator rejects a route that declares
 * both. More than one pinned route is not supported — the first by
 * `sidebar_order` wins, and the nav-membership guard asserts exactly one.
 */
function buildPinnedNavItem(): NavItem | null {
  if (!isManifest(manifest)) return null
  const pinned = Object.entries(manifest.routes)
    .filter(([, route]) => isManifestEntry(route) && route.sidebar_pinned === true)
    .sort(([, a], [, b]) => (a.sidebar_order ?? 0) - (b.sidebar_order ?? 0))
  const first = pinned[0]
  if (!first) return null
  const [path, route] = first
  // A pinned route has no group to inherit a gate from, so only its own
  // route-level `system_admin_only` can hide it.
  return toNavItem(path, route, route.system_admin_only ? true : undefined)
}

function buildSidebarGroups(): NavGroup[] {
  if (!isManifest(manifest)) {
    console.error('[navigation] manifest is invalid — missing routes or sidebar_groups')
    return []
  }

  const m: Manifest = manifest
  const itemsByGroup: Record<string, NavItem[]> = {}

  for (const [path, route] of Object.entries(m.routes)) {
    if (!isManifestEntry(route)) continue
    if (!route.sidebar_group) continue
    if (route.type === 'detail_page') continue
    if (!isManifestRoute(route)) continue

    const group = m.sidebar_groups[route.sidebar_group]
    if (!group) {
      console.warn(`[navigation] Route "${path}" references non-existent sidebar_group "${route.sidebar_group}"`)
      continue
    }

    let groupItems = itemsByGroup[route.sidebar_group]
    if (groupItems === undefined) {
      groupItems = []
      itemsByGroup[route.sidebar_group] = groupItems
    }
    // FAR-1796: the route entry ORs with its group's flag, mirroring the
    // router's hydrateManifestMeta derivation. A true on either signal gates
    // the item; a false route flag must not shadow a true group flag.
    groupItems.push(toNavItem(path, route, resolveSystemAdminOnly(route, group)))
  }

  for (const groupId of Object.keys(itemsByGroup)) {
    itemsByGroup[groupId].sort((a, b) => {
      const routeA = m.routes[a.to]
      const routeB = m.routes[b.to]
      return (routeA?.sidebar_order ?? 0) - (routeB?.sidebar_order ?? 0)
    })
  }

  return Object.entries(m.sidebar_groups)
    .sort(([, a], [, b]) => a.order - b.order)
    .map(([id, sg]) => ({
      id,
      label: sg.label,
      labelKey: sg.labelKey,
      items: itemsByGroup[id] ?? [],
      defaultCollapsed: !sg.default_expanded,
      systemAdminOnly: sg.system_admin_only ?? undefined,
    }))
    .filter((g) => g.items.length > 0)
}

/** Claims a link-decision reads from the caller's access token. */
export interface RouteGateUser {
  /** False when the token is opaque/absent — no claims could be read. */
  hasReadableToken: boolean
  isSystemAdmin: boolean
  orgRole: string | null
}

/** A refusal no licence upgrade can fix — only a role or instance admin can. */
export type NonTierRefusal = 'role' | 'system_admin'

/**
 * The non-tier reason `item` is refused for `user`, or null when it is not.
 *
 * Tier gates are deliberately out of scope: a below-tier caller still follows
 * the link, and the router guard lands them on the locked page with the
 * upgrade path. Only a role / system-admin refusal is decided up front, so a
 * view can point the caller at the refusal that a purchase cannot fix.
 *
 * An unreadable token yields null — nothing can be known, so the guard stays
 * the authority. A READABLE token with an empty/absent `org_role` is a
 * refusal: the role is known and it is not one of the required ones.
 */
export function nonTierRefusal(item: NavItem, user: RouteGateUser): NonTierRefusal | null {
  if (!user.hasReadableToken) return null
  if (item.systemAdminOnly && !user.isSystemAdmin) return 'system_admin'
  if (item.requiredRoles?.length && !item.requiredRoles.includes(user.orgRole ?? '')) return 'role'
  return null
}

export function canSeeItem(
  item: NavItem,
  user: { role: string; permissions?: string[] },
  plan: { isAtMinimumTier: (tier: string) => boolean },
): boolean {
  if (item.requiredRoles != null) {
    if (item.requiredRoles.length === 0 || !item.requiredRoles.includes(user.role)) return false
  }
  if (item.requiredTier && !plan.isAtMinimumTier(item.requiredTier)) return false
  if (item.requiredPermissions != null) {
    if (item.requiredPermissions.length === 0) return false
    const permissions = user.permissions
    if (!permissions || !item.requiredPermissions.some((p) => permissions.includes(p))) return false
  }
  return true
}

export interface NavVisibilityContext {
  isSystemAdmin: boolean
  userRole: string | null
  userPermissions: string[]
  devMode: boolean
  tierInfoLoaded: boolean
  isAtMinimumTier: (tier: string) => boolean
  // FAR-535: computed ONCE per context construction by the caller (never read
  // from localStorage inside the per-item function) — private_preview surfaces
  // are hidden for demo sessions even when dev mode is enabled.
  isDemoSession: boolean
  // FAR-656: resolves manifest-declared route flags (planStore.featureEnabled)
  // so flag-gated sidebar items stay consistent with the router guard.
  isFeatureEnabled: (flag: string) => boolean
}

export function isNavItemVisible(item: NavItem, ctx: NavVisibilityContext): boolean {
  // FAR-1796: an item-level system-admin gate hides the item from non-system-
  // admins even inside a visible group. Checked FIRST so the early-true return
  // below (no role/tier/permission/flag restrictions) cannot bypass it.
  if (item.systemAdminOnly && !ctx.isSystemAdmin) return false
  if ((item.visibility === 'private_preview' || item.visibility === 'in_dev') && !ctx.devMode) return false
  // FAR-535: demo sessions never see private_preview surfaces — even when the
  // instance runs with dev mode enabled. Server-side mutation denial still
  // holds; this keeps the nav consistent with the demo spec (read-only, no
  // preview tools). The demo state is sourced from the context, not from
  // localStorage, so every consumer gates identically without per-item reads.
  if (item.visibility === 'private_preview' && ctx.isDemoSession) return false
  if (
    !item.requiredRoles
    && !item.requiredTier
    && !item.requiredPermissions
    && !item.requiredFeatureFlag
  )
    return true
  // FAR-656: flag-gated items stay hidden until the plan fetch resolves, and
  // are filtered out entirely while their flag is disabled — the router guard
  // enforces the same flag on the route itself.
  if (item.requiredFeatureFlag && !ctx.tierInfoLoaded) return false
  if (item.requiredFeatureFlag && !ctx.isFeatureEnabled(item.requiredFeatureFlag)) return false
  if (item.requiredTier && !ctx.tierInfoLoaded) return false
  return canSeeItem(
    item,
    { role: ctx.userRole || '', permissions: ctx.userPermissions },
    { isAtMinimumTier: ctx.isAtMinimumTier },
  )
}

export function getVisibleNavGroups(ctx: NavVisibilityContext): NavGroup[] {
  return getNavGroups()
    .filter((g) => !g.systemAdminOnly || ctx.isSystemAdmin)
    .map((group) => ({
      ...group,
      items: group.items.filter((item) => isNavItemVisible(item, ctx)),
    }))
    .filter((g) => g.items.length > 0)
}

let _cachedGroups: NavGroup[] | null = null

export function getNavGroups(): NavGroup[] {
  _cachedGroups ??= buildSidebarGroups()
  return _cachedGroups.map((g) => ({ ...g, items: [...g.items] }))
}

let _cachedPinnedItem: NavItem | null | undefined

/** FAR-2605: the manifest's pinned nav row (the Dashboard), or null. */
export function getPinnedNavItem(): NavItem | null {
  if (_cachedPinnedItem === undefined) _cachedPinnedItem = buildPinnedNavItem()
  return _cachedPinnedItem === null ? null : { ..._cachedPinnedItem }
}

/**
 * FAR-2605: the pinned row after the SAME visibility filtering the grouped
 * items get, so a pinned route cannot leak past its tier/role/flag gate. The
 * Dashboard is community/pinned with no gate today; this keeps that true if a
 * gate is ever added.
 */
export function getVisiblePinnedNavItem(ctx: NavVisibilityContext): NavItem | null {
  const item = getPinnedNavItem()
  return item && isNavItemVisible(item, ctx) ? item : null
}
