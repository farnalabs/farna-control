/**
 * Regression test: the manifest's YAML merge-key (`<<: *admin` /
 * `<<: *community`) anchors MUST expand when frontend/src/manifest.yaml is
 * parsed with the project's js-yaml.
 *
 * js-yaml v5 dropped merge-key expansion. Under v5 every merged route keeps a
 * literal `'<<'` key and `required_entitlement` / `required_roles` /
 * `required_permissions` are undefined, which silently disables ALL sidebar
 * (config/navigation.ts) and router (hydrateManifestMeta →
 * enforceRoleTierVisibility) entitlement/role gating. This test parses the REAL
 * manifest from disk with the INSTALLED js-yaml so a future major bump fails
 * here instead of shipping silently.
 *
 * FAR-2665 replaced the commercial `required_tier` with `required_entitlement`
 * (an entry in the backend entitlement registry); a route no longer declares a
 * tier at all.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import yaml from 'js-yaml'
import { hydrateManifestMeta } from '../router'
import {
  getNavGroups,
  isNavItemVisible,
  type NavItem,
  type NavVisibilityContext,
} from '../config/navigation'

const MANIFEST_PATH = resolve(__dirname, '../manifest.yaml')

interface ManifestRoute {
  name?: string | null
  visibility?: string | null
  required_entitlement?: string | null
  required_roles?: string[] | null
  required_permissions?: string[] | null
  [key: string]: unknown
}

interface Manifest {
  routes: Record<string, ManifestRoute>
}

function loadManifest(): Manifest {
  const raw = readFileSync(MANIFEST_PATH, 'utf-8')
  return yaml.load(raw) as Manifest
}

describe('manifest merge-key gating (js-yaml regression guard)', () => {
  const manifest = loadManifest()
  const routes = manifest.routes

  it('parses a routes map from the real manifest (vacuity guard)', () => {
    expect(routes).toBeDefined()
    const routeCount = Object.keys(routes).length
    expect(routeCount).toBeGreaterThan(0)
  })

  it('no route object carries a literal "<<" key (js-yaml v5 regression signature)', () => {
    const routesWithMergeKey = Object.entries(routes)
      .filter(([, route]) => Object.prototype.hasOwnProperty.call(route, '<<'))
      .map(([path]) => path)
    expect(routesWithMergeKey.length).toBe(0)
  })

  it('/settings/sso requires the Identity entitlement with the admin role', () => {
    const ssoRoute = routes['/settings/sso']
    expect(ssoRoute).toBeDefined()
    expect(ssoRoute.required_entitlement).toBe('identity')
    expect(ssoRoute.required_roles).toContain('admin')
  })

  it('/admin/org carries no entitlement with the admin role (FAR-1269)', () => {
    // The /admin/org route itself must NOT gate a commercial capability on the
    // org HITL review window endpoint (GET/PUT
    // /api/v1/admin/org/hitl-review-window), which is not plan-gated —
    // otherwise the card outside the in-page FeatureGate is unreachable no
    // matter where it sits in the template. required_roles stays [admin] so
    // navigation is still admin-only.
    const orgRoute = routes['/admin/org']
    expect(orgRoute).toBeDefined()
    expect(orgRoute.required_entitlement == null).toBe(true)
    expect(orgRoute.required_roles).toContain('admin')
  })

  it('/admin/errors and /admin/errors/:id admit viewers as well as admins with no entitlement (FAR-2356/FAR-2665)', () => {
    // FAR-2356 relaxes the error dashboard from `<<: *admin` (admin-only) to
    // `<<: *admin_viewer` so a demo-org viewer can reach it. FAR-2665 lowered
    // `error_tracking` to Community (operational capability), so the routes
    // carry NO entitlement — a route gate here would contradict the free
    // backend surface. Pin the gate on the REAL manifest: the role list must
    // admit `viewer` WITHOUT dropping `admin` (the router role gate is ANY-of,
    // so a regression that replaces the list with [viewer] would lock admins
    // out). An access-control relaxation without a pinning test can silently
    // regress.
    for (const path of ['/admin/errors', '/admin/errors/:id']) {
      const route = routes[path]
      expect(route, `${path} must exist`).toBeDefined()
      expect(route.required_entitlement == null, `${path} required_entitlement`).toBe(true)
      expect(route.required_roles, `${path} required_roles`).toContain('viewer')
      expect(route.required_roles, `${path} required_roles`).toContain('admin')
    }
  })

  it('/system-maps published reads are authenticated viewer routes, not anonymous public routes (FAR-2363)', () => {
    for (const path of ['/system-maps', '/system-maps/:id']) {
      const route = routes[path]
      expect(route, `${path} must exist`).toBeDefined()
      expect(route.required_entitlement == null, `${path} required_entitlement`).toBe(true)
      expect(route.required_roles, `${path} required_roles`).toEqual(['viewer', 'operator', 'admin'])
      expect(route.required_permissions, `${path} required_permissions`).toEqual(['system_maps.read'])
      expect(route.visibility, `${path} visibility`).toBe('public')
    }
  })


  it('at least 8 routes carry a required_entitlement (non-vacuity guard)', () => {
    const gatedRoutes = Object.values(routes).filter((route) => route.required_entitlement != null)
    expect(gatedRoutes.length).toBeGreaterThanOrEqual(8)
  })

  it('no route declares a required_tier any more (FAR-2665)', () => {
    for (const [path, route] of Object.entries(routes)) {
      expect((route as Record<string, unknown>).required_tier, `${path} must not declare required_tier`).toBeUndefined()
    }
  })

  it('route-level system-admin gates carry system_admin_only: true (manifest is the single source)', () => {
    // These three routes have a blank sidebar_group, so the sidebar-group
    // derivation alone can never gate them — the route entry must carry the
    // signal itself. Regression guard for an inline-meta-only gate.
    for (const path of ['/admin/system/orgs', '/admin/system/config', '/admin/product-analytics']) {
      const route = routes[path]
      expect(route, `${path} must exist`).toBeDefined()
      expect(route.system_admin_only).toBe(true)
    }
  })

  it('/admin/product-analytics is system-admin gated and drops the accidental admin org-role gate (FAR-1795/FAR-2604)', () => {
    // `<<: *admin` injects `required_roles: [admin]`.
    // A system_admin_only route is an INSTANCE-level gate; the org-ROLE
    // requirement does not apply and wrongly redirected a system admin whose
    // org_role != 'admin'. The explicit `required_roles:` after `<<:` must
    // override the merged value to null. FAR-2665: the route also carries no
    // entitlement (product analytics is not in the ratified commercial groups),
    // so it is gated by system_admin_only alone.
    const route = routes['/admin/product-analytics']
    expect(route).toBeDefined()
    expect(route.system_admin_only).toBe(true)
    expect(route.required_entitlement == null).toBe(true)
    expect(route.required_roles == null).toBe(true)
  })

  it('drives the REAL derivation: /admin/product-analytics is system-admin gated with no org-role requirement (FAR-1795)', () => {
    // Exercise the actual router hydration (vite yaml plugin → hydrateManifestMeta),
    // not a source-text scrape: requiresSystemAdmin must still be set and the
    // accidental org-role gate must be gone.
    const routeName = routes['/admin/product-analytics'].name as string
    expect(typeof routeName).toBe('string')
    expect(routeName.length).toBeGreaterThan(0)
    const route = { name: routeName, meta: {} as Record<string, unknown> }
    hydrateManifestMeta(route as never)
    expect(route.meta.requiresSystemAdmin).toBeTruthy()
    expect(route.meta.requiredRoles).toBeFalsy()
  })
})

describe('Beta surfaces stay out of the alpha nav (FAR-2615)', () => {
  // Risk Assessment and Compliance Evidence Index were ratified as BETA
  // surfaces, so neither may appear in an alpha instance's nav. Both are
  // gated the same way the other preview routes are: `visibility:
  // private_preview`, which the sidebar (isNavItemVisible) and the router
  // (enforceRoleTierVisibility) both read — hidden for every customer and the
  // read-only demo, reachable only with developer mode on. They are promoted
  // to `public` when Beta enables them.
  //
  // This spec drives the REAL derivation: the routes come from the manifest
  // parsed off disk by js-yaml, and the visibility decision comes from the
  // shipping navigation code — not a source-text scrape and not a mock.
  const routes = loadManifest().routes
  const BETA_PREVIEW_ROUTES = ['/risk-assessment', '/admin/compliance/evidence-index'] as const

  function navItemFor(path: string): NavItem | undefined {
    for (const group of getNavGroups()) {
      const item = group.items.find((candidate) => candidate.to === path)
      if (item) return item
    }
    return undefined
  }

  function visibilityContext(overrides: Partial<NavVisibilityContext> = {}): NavVisibilityContext {
    return {
      isSystemAdmin: true,
      userRole: 'admin',
      // The most permissive context that still exercises the devMode gate:
      // every role/tier/permission requirement these routes could carry is
      // satisfied, so only `visibility` can hide them.
      userPermissions: ['compliance.evidence_index.read'],
      devMode: false,
      tierInfoLoaded: true,
      planLoaded: true,
      isEntitlementGranted: () => true,
      isDemoSession: false,
      isFeatureEnabled: () => true,
      ...overrides,
    }
  }

  for (const path of BETA_PREVIEW_ROUTES) {
    it(`${path} is declared private_preview in the manifest`, () => {
      const route = routes[path]
      expect(route, `${path} must exist in the manifest`).toBeDefined()
      expect(
        route.visibility,
        `${path} must be private_preview so it stays out of the alpha nav (FAR-2615)`,
      ).toBe('private_preview')
    })

    it(`${path} is hidden from the sidebar while developer mode is off`, () => {
      const item = navItemFor(path)
      // Vacuity guard: a route that is not a sidebar item at all would pass
      // the hidden assertion for the wrong reason.
      if (!item) throw new Error(`${path} must be a sidebar item (with dev mode on)`)
      expect(
        isNavItemVisible(item, visibilityContext()),
        `${path} must be hidden from the sidebar when developer mode is off (FAR-2615)`,
      ).toBe(false)
      expect(
        isNavItemVisible(item, visibilityContext({ devMode: true })),
        `${path} must be reachable with developer mode on (FAR-2615)`,
      ).toBe(true)
    })

    it(`${path} hydrates the router's visibility gate as private_preview`, () => {
      const routeName = routes[path].name
      expect(typeof routeName, `${path} must carry a route name`).toBe('string')
      const route = { name: routeName, meta: {} as Record<string, unknown> }
      hydrateManifestMeta(route as never)
      expect(
        route.meta.visibility,
        `${path} must bounce to the dashboard while developer mode is off (FAR-2615)`,
      ).toBe('private_preview')
    })
  }
})
