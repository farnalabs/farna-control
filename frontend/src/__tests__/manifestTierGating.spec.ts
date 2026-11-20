/**
 * FAR-2604 — the manifest's `team`-tier routes must equal the reviewed Team
 * set.
 *
 * The pricing model (PRICING.md, 2026-10-02) makes Community a complete core
 * product; the Team bundle is SSO, SCIM, team RBAC, audit
 * aggregation/retention/chaining and spend controls. A small number of further
 * surfaces are deliberately withheld from Community because their capability
 * is Beta or operator-only (the error dashboard and Plugin API are Beta; the
 * error-forwarder, rate-limit and runtime-config controls are operator
 * controls). Any route that gains or loses the `team` tier must therefore be
 * an explicit, reviewed edit to the list below — otherwise a product-surface
 * regression ships silently. The motivating defect: Connectors and Model
 * Backends were `team`-tier, so a Community install could not reach either and
 * could never complete first run (`config/navigation.ts` hides a below-tier
 * route and the router redirects it to the dashboard).
 *
 * The manifest tier is only the CLIENT page gate. A tier change is coherent
 * only when the same capability is reachable end to end: the backend feature
 * flag (`require_feature`, 402) must be at or below the route's tier, and the
 * view's own `FeatureGate required-tier` must match. That is why the runners
 * and email routes stay Community together with their `environment_profiles`
 * and `email_config` flags — a self-hosted Community install cannot run
 * without them.
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

interface ManifestRoute {
  required_tier?: string | null
  [key: string]: unknown
}

interface Manifest {
  routes: Record<string, ManifestRoute>
}

function loadManifest(): Manifest {
  return yaml.load(readFileSync(MANIFEST_PATH, 'utf-8')) as Manifest
}

/**
 * The exact set of routes allowed to carry `required_tier: team`.
 *
 * The ratified Team bundle (SSO, teams, audit, spend controls, run retention)
 * plus the Beta/operator-only surfaces that are deliberately not part of the
 * Community product.
 */
const EXPECTED_TEAM_ROUTES = [
  // Ratified Team bundle.
  '/admin/audit',
  '/admin/costs',
  '/admin/costs/components',
  '/admin/costs/controls',
  '/admin/costs/limits',
  '/admin/run-retention',
  '/settings/sso',
  '/settings/teams',
  // Beta / operator-only surfaces withheld from Community.
  '/admin/errors',
  '/admin/errors/:id',
  '/admin/plugins',
  '/settings/error-forwarders',
  '/settings/rate-limits',
  '/settings/runtime-config',
] as const

const VALID_TIERS = new Set(['community', 'team'])

const EXPECTED_TEAM_SET = new Set<string>(EXPECTED_TEAM_ROUTES)

describe('manifest tier gating pins the Team set (FAR-2604)', () => {
  const routes = loadManifest().routes

  it('parses the real manifest routes (vacuity guard)', () => {
    expect(routes).toBeDefined()
    expect(Object.keys(routes).length).toBeGreaterThan(0)
  })

  it('every route declares a required_tier (the merged anchor always resolves one)', () => {
    const missing = Object.entries(routes)
      .filter(([, route]) => route.required_tier === undefined)
      .map(([path]) => path)
      .sort()
    expect(missing, `Routes missing required_tier: ${missing.join(', ')}`).toEqual([])
  })

  it('every route declares a KNOWN tier (community or team)', () => {
    // A typo'd tier id satisfies the presence check above but is neither a
    // real tier nor a route the router can gate, so it would silently slip
    // past every below-tier check in the app. Pin the vocabulary.
    const unknown = Object.entries(routes)
      .filter(([, route]) => !VALID_TIERS.has(String(route.required_tier)))
      .map(([path, route]) => `${path}=${String(route.required_tier)}`)
      .sort()
    expect(unknown, `Routes with an unknown required_tier: ${unknown.join(', ')}`).toEqual([])
  })

  it('the set of team-tier routes is exactly the reviewed Team set', () => {
    const actualTeam = new Set(
      Object.entries(routes)
        .filter(([, route]) => route.required_tier === 'team')
        .map(([path]) => path),
    )

    const gained = [...actualTeam].filter((path) => !EXPECTED_TEAM_SET.has(path)).sort()
    const lost = [...EXPECTED_TEAM_SET].filter((path) => !actualTeam.has(path)).sort()

    const problems = [
      ...gained.map((path) => `  + ${path} is now team-gated but is not in the reviewed Team set`),
      ...lost.map((path) => `  - ${path} is no longer team-gated but is in the reviewed Team set`),
    ]
    const message =
      problems.length > 0
        ? `Route tier gating drifted from the reviewed Team set:\n${problems.join('\n')}\n` +
          'Update the manifest to match the pricing model and the Beta/operator-only split, or add ' +
          'the route to EXPECTED_TEAM_ROUTES only after confirming its feature flag and view gate agree.'
        : 'team-tier route set matches the reviewed Team set'

    expect(problems, message).toEqual([])
  })

  it('first-run Community surfaces stay reachable on a Community install', () => {
    // Connectors and Model Backends are prerequisites for creating a model
    // backend and running any pipeline; gating them to team broke first run.
    for (const path of ['/admin/connectors', '/admin/model-backends']) {
      const route = routes[path]
      expect(route, `${path} must exist in the manifest`).toBeDefined()
      expect(route.required_tier, `${path} must be community-tier`).toBe('community')
    }
  })

  it('a self-hosted Community install keeps the runners and email surfaces', () => {
    // FAR-2604: these four runner routes plus the SMTP settings are required
    // for a Community install to dispatch runs and send HITL/readiness email.
    // They stay Community in the manifest AND in their capability gate
    // (`environment_profiles` / `email_config` are community-tier flags and
    // the views pass `required-tier="community"`).
    for (const path of [
      '/admin/runners/profiles',
      '/admin/runners/concurrency',
      '/admin/runners/profiles/new',
      '/admin/runners/profiles/:id/edit',
      '/settings/email',
    ]) {
      const route = routes[path]
      expect(route, `${path} must exist in the manifest`).toBeDefined()
      expect(route.required_tier, `${path} must be community-tier`).toBe('community')
    }
  })
})
