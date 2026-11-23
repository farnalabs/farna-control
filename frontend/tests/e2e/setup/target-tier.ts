import { readFileSync } from 'node:fs'
import path from 'node:path'
import yaml from 'js-yaml'
import type { Page } from '@playwright/test'
import type { TestEnv } from './env'
import { apiBaseFor, apiFetch, apiLogin } from './realstack-api'

/**
 * Entitlement-aware navigation guard for the real-stack targets.
 *
 * The staging deployment is the CI-only E2E environment and deliberately runs
 * without a licence (the Community grant; see the staging Fly secret set and
 * the FAR-2091 startup contract). Every manifest route carrying a commercial
 * `required_entitlement` is therefore redirected to the locked page by the
 * router guard — correct product behaviour, but a page spec that asserts that
 * page's heading fails on a redirect it can never satisfy.
 *
 * FAR-2665 replaced the commercial `required_tier` with `required_entitlement`,
 * so this guard reads the manifest's entitlement gate (not a tier string) and
 * the target's GRANTED entitlements from the plan payload. (It previously read
 * `required_tier: team`, which no longer exists, leaving the guard silently
 * inert and every entitlement-gated spec failing on staging.)
 *
 * Rather than duplicating a `test.skip(...)` precondition in every commercial
 * spec (and missing the next one), the `page` fixture installs one guard that
 * skips a test the moment it tries to navigate to a route whose entitlement the
 * target's licence does not grant. The guard applies ONLY to the staging
 * target, and is further inert when:
 *
 *  - staging's real `GET /api/v1/plan` grants the route's entitlement (a
 *    licence was configured there), and
 *  - a spec registers its own `/api/v1/plan` handler (it has explicitly taken
 *    control of the grant the router sees, so the guard must not second-guess
 *    it).
 *
 * The `app` (production) target is deliberately NOT guarded: production is
 * meant to be licensed, so a missing entitlement there must fail loudly instead
 * of silently skipping the spec.
 *
 * Skipping is the established contract for licence-gated surfaces on a real
 * target (`journeys/teams-journey.spec.ts` does the same for team RBAC), and
 * the spec starts running again automatically once the target carries a licence
 * granting the capability — no test is deleted or weakened.
 */

interface ManifestRoute {
  required_entitlement?: string | null
}

interface Manifest {
  routes?: Record<string, ManifestRoute>
}

export interface EntitledRoute {
  /** The static route prefix (`/admin/audit`, `/admin/costs`). */
  path: string
  /** The commercial capability the route requires. */
  entitlement: string
}

/**
 * Manifest routes gated on a commercial entitlement, longest path first so a
 * nested route (`/admin/costs/limits`) is matched before its parent.
 *
 * Dynamic suffixes (`/admin/errors/:id`) are reduced to their static prefix;
 * a real navigation can never contain the literal `:id` segment anyway.
 */
function loadEntitlementGatedRoutes(): EntitledRoute[] {
  // Same resolution pattern as mobile-layout.spec.ts: this file lives in
  // frontend/tests/e2e/setup, so three levels up is frontend/.
  const manifestPath = path.join(__dirname, '../../../src/manifest.yaml')
  const manifest = yaml.load(readFileSync(manifestPath, 'utf-8')) as Manifest | undefined
  return Object.entries(manifest?.routes ?? {})
    .filter(([, route]) => typeof route?.required_entitlement === 'string' && route.required_entitlement.length > 0)
    .map(([routePath, route]) => ({
      path: routePath.replace(/\/:[^/]+/g, ''),
      entitlement: route.required_entitlement as string,
    }))
    .sort((a, b) => b.path.length - a.path.length)
}

const ENTITLEMENT_GATED_ROUTES = loadEntitlementGatedRoutes()

/**
 * Legacy paths that the router redirects onto an entitled manifest route.
 *
 * The manifest lists the CURRENT paths only; `src/router/index.ts` keeps
 * one-release aliases alive. A spec that navigates to an alias is gated exactly
 * as its target is, so the alias has to be matched here too.
 *
 * Every current alias target is free (FAR-2604), so both lists are empty today:
 * `/admin` and `/admin/remy` redirect to `/admin/assistant`,
 * `/admin/sandbox-concurrency` to `/admin/runners/concurrency`,
 * `/admin/environments` and `/environment-profiles/**` to
 * `/admin/runners/profiles/**`, and `/settings/remy` to `/settings/assistant`.
 * Keep these lists in step with the router's redirects to an entitled path.
 */
const LEGACY_ENTITLED_EXACT: ReadonlyMap<string, string> = new Map()
const LEGACY_ENTITLED_PREFIXES: readonly (EntitledRoute & { prefix: string })[] = []

/** Return the entitled manifest route *pathname* falls under, if any. */
export function matchEntitlementGatedPath(pathname: string): EntitledRoute | undefined {
  const gated = ENTITLEMENT_GATED_ROUTES.find(
    (route) => pathname === route.path || pathname.startsWith(`${route.path}/`),
  )
  if (gated) return gated
  const exact = LEGACY_ENTITLED_EXACT.get(pathname)
  if (exact) return { path: pathname, entitlement: exact }
  const prefixMatch = LEGACY_ENTITLED_PREFIXES.find(
    (route) => pathname === route.prefix || pathname.startsWith(`${route.prefix}/`),
  )
  return prefixMatch ? { path: prefixMatch.path, entitlement: prefixMatch.entitlement } : undefined
}

function pathnameFor(url: string): string | undefined {
  try {
    return new URL(url, 'http://e2e.invalid').pathname
  } catch {
    return undefined
  }
}

/**
 * Resolve the real GRANTED entitlements of a target once per worker.
 *
 * `/api/v1/plan` is the caller-scoped plan bootstrap the router itself reads
 * (FAR-1809), so it reports exactly the grant that will gate the navigation.
 */
const grantedCache = new Map<string, Promise<ReadonlySet<string>>>()

async function fetchTargetGrantedEntitlements(env: TestEnv): Promise<ReadonlySet<string>> {
  const apiBase = apiBaseFor(env)
  const token = await apiLogin(env)
  const res = await apiFetch<{ entitlements?: { key?: unknown; granted?: unknown }[] }>(
    apiBase,
    token,
    'GET',
    '/api/v1/plan',
  )
  if (res.status !== 200) {
    throw new Error(
      `[target-entitlement] GET /api/v1/plan failed on ${env.name}: ${res.status} ${res.text.slice(0, 200)}`,
    )
  }
  const entries = Array.isArray(res.body?.entitlements) ? res.body.entitlements : []
  return new Set(
    entries
      .filter((entry) => entry?.granted === true && typeof entry.key === 'string')
      .map((entry) => entry.key as string),
  )
}

export function resolveTargetGrantedEntitlements(env: TestEnv): Promise<ReadonlySet<string>> {
  let resolved = grantedCache.get(env.name)
  if (!resolved) {
    resolved = fetchTargetGrantedEntitlements(env)
    grantedCache.set(env.name, resolved)
  }
  return resolved
}

function patternIncludesPlan(pattern: string | RegExp): boolean {
  const source = typeof pattern === 'string' ? pattern : pattern.source
  return source.includes('/api/v1/plan')
}

/**
 * Install the entitlement navigation guard on *page*.
 *
 * Only the staging target is guarded (see the module docstring). `skip` is the
 * caller's `testInfo.skip` (injected to avoid importing the fixture module
 * back into this one). It is only invoked for a navigation to a manifest route
 * whose required entitlement the target does not grant, and never when the spec
 * owns the `/api/v1/plan` response.
 */
export function installEntitlementNavigationGuard(
  page: Page,
  env: TestEnv,
  skip: (reason: string) => void,
  resolveGranted: (env: TestEnv) => Promise<ReadonlySet<string>> = resolveTargetGrantedEntitlements,
): void {
  if (env.name !== 'staging') return

  let planControlledBySpec = false
  const originalRoute = page.route.bind(page)
  page.route = ((pattern: string | RegExp, handler: Parameters<Page['route']>[1]) => {
    if (patternIncludesPlan(pattern)) planControlledBySpec = true
    return originalRoute(pattern, handler)
  }) as Page['route']

  const originalGoto = page.goto.bind(page)
  page.goto = (async (url: string, options?: Parameters<Page['goto']>[1]) => {
    const pathname = pathnameFor(url)
    const gated = pathname ? matchEntitlementGatedPath(pathname) : undefined
    if (gated && !planControlledBySpec) {
      const granted = await resolveGranted(env)
      if (!granted.has(gated.entitlement)) {
        skip(
          `Route ${gated.path} requires the "${gated.entitlement}" entitlement, which the "${env.name}" target's licence does not grant; ` +
            'the router redirects it to the locked page. Set a licence granting that capability to run this spec.',
        )
      }
    }
    return originalGoto(url, options)
  }) as Page['goto']
}
