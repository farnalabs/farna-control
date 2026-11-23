/**
 * FAR-2665 — the manifest's commercial gates must equal the reviewed
 * entitlement set.
 *
 * A runtime feature flag is behaviour only; the commercial capability is an
 * ENTITLEMENT declared in `backend/src/farna/core/entitlements.py`. Three named
 * Team groups replace the flat "Team tier":
 *
 *   identity        — sso, scim, team_rbac
 *   audit_evidence  — audit_viewer, admin_run_retention
 *   cost_governance — admin_spend_limits, admin_cost_controls, admin_cost_breakdown
 *
 * The manifest names the entitlement a route requires (`required_entitlement`)
 * and the router reads the grant from `/api/v1/plan`; a route no longer
 * declares a tier. Any route that gains or loses a commercial gate must
 * therefore be an explicit, reviewed edit to the list below — otherwise a
 * product-surface regression ships silently. The motivating defect (FAR-2604):
 * Connectors and Model Backends were paid-gated, so a Community install could
 * never complete first run.
 *
 * Reads the REAL manifest from disk with the installed js-yaml — the same
 * pattern as `manifestGating.spec.ts` — so the YAML anchors/merge keys are
 * expanded exactly as the app's vite plugin expands them.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import yaml from 'js-yaml'

const MANIFEST_PATH = resolve(__dirname, '../manifest.yaml')
const ENTITLEMENTS_PATH = resolve(__dirname, '../../../backend/src/farna/core/entitlements.py')

interface ManifestRoute {
  required_entitlement?: string | null
  required_roles?: string[] | null
  required_permissions?: string[] | null
  [key: string]: unknown
}

interface Manifest {
  routes: Record<string, ManifestRoute>
}

function loadManifest(): Manifest {
  return yaml.load(readFileSync(MANIFEST_PATH, 'utf-8')) as Manifest
}

/** The entitlement keys the backend registry declares. */
function declaredEntitlementKeys(): Set<string> {
  const text = readFileSync(ENTITLEMENTS_PATH, 'utf-8')
  return new Set([...text.matchAll(/key="([a-z_]+)"/g)].map((m) => m[1]))
}

/**
 * The exact set of routes allowed to carry a `required_entitlement`.
 *
 * The ratified commercial groups only. The Beta/operator-only surfaces that
 * used to be paid (error dashboard, Plugin API, error forwarders, rate limits,
 * runtime config) are Community as of FAR-2665 — operational and integration
 * capability, not organisation-scale governance.
 */
const EXPECTED_ENTITLEMENT_ROUTES: Record<string, string> = {
  '/settings/teams': 'identity',
  '/settings/sso': 'identity',
  '/admin/audit': 'audit_evidence',
  '/admin/run-retention': 'audit_evidence',
  '/admin/costs': 'cost_governance',
  '/admin/costs/components': 'cost_governance',
  '/admin/costs/controls': 'cost_governance',
  '/admin/costs/limits': 'cost_governance',
}

describe('manifest entitlement gating pins the commercial set (FAR-2665)', () => {
  const routes = loadManifest().routes

  it('parses the real manifest routes (vacuity guard)', () => {
    expect(routes).toBeDefined()
    expect(Object.keys(routes).length).toBeGreaterThan(0)
  })

  it('every declared required_entitlement names a registry entry', () => {
    const declared = declaredEntitlementKeys()
    expect(declared.size).toBeGreaterThan(0)
    const unknown = Object.entries(routes)
      .filter(([, route]) => route.required_entitlement != null && !declared.has(String(route.required_entitlement)))
      .map(([path, route]) => `${path}=${String(route.required_entitlement)}`)
      .sort()
    expect(unknown, `Routes with an unknown required_entitlement: ${unknown.join(', ')}`).toEqual([])
  })

  it('every route with an entitlement also carries a role or permission gate', () => {
    // FAR-2665 invariant: an entitlement is ALWAYS additive to RBAC. A route
    // that asked for a capability but no role/permission would let a lower
    // role inside a paid capability the moment it was granted.
    for (const [path, route] of Object.entries(routes)) {
      if (route.required_entitlement == null) continue
      const hasRole = Array.isArray(route.required_roles) && route.required_roles.length > 0
      const hasPermission = Array.isArray(route.required_permissions) && route.required_permissions.length > 0
      expect(hasRole || hasPermission, `${path} must keep a role/permission gate alongside its entitlement`).toBe(true)
    }
  })

  it('the set of commercially gated routes is exactly the reviewed set', () => {
    const actual = new Map(
      Object.entries(routes)
        .filter(([, route]) => route.required_entitlement != null)
        .map(([path, route]) => [path, String(route.required_entitlement)]),
    )

    const problems: string[] = []
    for (const [path, entitlement] of Object.entries(EXPECTED_ENTITLEMENT_ROUTES)) {
      if (!actual.has(path)) {
        problems.push(`  - ${path} lost its '${entitlement}' gate`)
      } else if (actual.get(path) !== entitlement) {
        problems.push(`  ~ ${path} names '${String(actual.get(path))}', expected '${entitlement}'`)
      }
    }
    for (const [path, entitlement] of actual) {
      if (!(path in EXPECTED_ENTITLEMENT_ROUTES)) {
        problems.push(`  + ${path} is gated on '${entitlement}' but is not in the reviewed commercial set`)
      }
    }

    const message =
      problems.length > 0
        ? `Route entitlement gating drifted from the reviewed commercial set:\n${problems.join('\n')}\n` +
          'Update the manifest to match the ratified groups, or add the route here only after ' +
          'confirming its backend gate and view gate agree.'
        : 'entitlement-gated route set matches the reviewed set'

    expect(problems, message).toEqual([])
  })

  it('first-run Community surfaces stay reachable on a Community install', () => {
    // Connectors and Model Backends are prerequisites for creating a model
    // backend and running any pipeline; gating them broke first run.
    for (const path of ['/admin/connectors', '/admin/model-backends']) {
      const route = routes[path]
      expect(route, `${path} must exist in the manifest`).toBeDefined()
      expect(route.required_entitlement == null, `${path} must carry no entitlement`).toBe(true)
    }
  })

  it('a self-hosted Community install keeps the runners, email and operational controls', () => {
    // FAR-2604 + FAR-2665: these surfaces are required to dispatch runs, send
    // HITL/readiness email, and operate the install. They carry no entitlement
    // in the manifest AND their capability flags are Community.
    for (const path of [
      '/admin/runners/profiles',
      '/admin/runners/concurrency',
      '/admin/runners/profiles/new',
      '/admin/runners/profiles/:id/edit',
      '/settings/email',
      '/settings/runtime-config',
      '/settings/rate-limits',
      '/settings/error-forwarders',
      '/admin/plugins',
      '/admin/errors',
      '/admin/errors/:id',
    ]) {
      const route = routes[path]
      expect(route, `${path} must exist in the manifest`).toBeDefined()
      expect(route.required_entitlement == null, `${path} must carry no entitlement`).toBe(true)
    }
  })
})
