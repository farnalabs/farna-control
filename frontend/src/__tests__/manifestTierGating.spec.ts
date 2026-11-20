/**
 * FAR-2604 — the manifest's `team`-tier routes must equal the ratified Team
 * bundle.
 *
 * The pricing model (PRICING.md, 2026-10-02) makes Community a complete core
 * product; the Team bundle is SSO, SCIM, team RBAC, audit
 * aggregation/retention/chaining and spend controls. Any route that gains or
 * loses the `team` tier must therefore be an explicit, reviewed edit to the
 * list below — otherwise a product-surface regression ships silently. The
 * motivating defect: Connectors and Model Backends were `team`-tier, so a
 * Community install could not reach either and could never complete first run
 * (`config/navigation.ts` hides a below-tier route and the router redirects it
 * to the dashboard).
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
 * The exact set of routes allowed to carry `required_tier: team` — the
 * surfaces the ratified Team bundle names (audit, spend controls/costs, run
 * retention, SSO, teams). Everything else is Community.
 */
const EXPECTED_TEAM_ROUTES = [
  '/admin/audit',
  '/admin/costs',
  '/admin/costs/components',
  '/admin/costs/controls',
  '/admin/costs/limits',
  '/admin/run-retention',
  '/settings/sso',
  '/settings/teams',
] as const

const EXPECTED_TEAM_SET = new Set<string>(EXPECTED_TEAM_ROUTES)

describe('manifest tier gating pins the Team bundle (FAR-2604)', () => {
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

  it('the set of team-tier routes is exactly the ratified Team bundle', () => {
    const actualTeam = new Set(
      Object.entries(routes)
        .filter(([, route]) => route.required_tier === 'team')
        .map(([path]) => path),
    )

    const gained = [...actualTeam].filter((path) => !EXPECTED_TEAM_SET.has(path)).sort()
    const lost = [...EXPECTED_TEAM_SET].filter((path) => !actualTeam.has(path)).sort()

    const problems = [
      ...gained.map((path) => `  + ${path} is now team-gated but is not in the ratified Team bundle`),
      ...lost.map((path) => `  - ${path} is no longer team-gated but is in the ratified Team bundle`),
    ]
    const message =
      problems.length > 0
        ? `Route tier gating drifted from the Community/Team bundle:\n${problems.join('\n')}\n` +
          'Update the manifest to match the ratified pricing model, or add the route to ' +
          'EXPECTED_TEAM_ROUTES only if the Team bundle genuinely names it.'
        : 'team-tier route set matches the ratified Team bundle'

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
})
