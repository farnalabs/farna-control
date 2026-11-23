import { beforeEach, describe, expect, it, vi } from 'vitest'

// Use the real router implementation so the permission assertions below cover
// the navigation guard rather than only the extracted helper.
vi.mock('vue-router', async () => {
  const actual = await vi.importActual<typeof import('vue-router')>('vue-router')
  return actual
})

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import yaml from 'js-yaml'

// Unit-test the extracted router guard helpers (FAR: S3776 decomposition) in
// isolation by mocking their two external dependencies. This deterministically
// covers every branch of the new functions — navigation-based tests cannot
// reach the "allowed" paths without mounting the target Vue components, which
// is slow and flaky under jsdom.
const { decodeJwtPayload, usePlanStore } = vi.hoisted(() => ({
  decodeJwtPayload: vi.fn(),
  usePlanStore: vi.fn(),
}))

vi.mock('../lib/jwt', () => ({ decodeJwtPayload }))
vi.mock('../stores/planStore', () => ({ usePlanStore }))

import router, {
  documentTitleFor,
  enforceRoleTierVisibility,
  focusRouteContent,
  hydrateManifestMeta,
  lockedRouteFor,
} from '../router'
import { clearAccessToken, setAccessToken } from '../lib/api/auth'

it('registers the risk assessment route in the real router', () => {
  expect(router.getRoutes().some((route) => route.name === 'risk-assessment' && route.path === '/risk-assessment')).toBe(true)
})

describe('document title', () => {
  it('prefixes the manifest title with the product name', () => {
    expect(documentTitleFor({ title: 'Pipelines' })).toBe('Pipelines | Farna Control')
  })

  it('uses the manifest breadcrumb when the route carries no title', () => {
    expect(documentTitleFor({ breadcrumb: 'Runs' })).toBe('Runs | Farna Control')
  })

  it('falls back to the bare product name rather than "Farna Control | Farna Control"', () => {
    // A bare route (e.g. /assistant) has neither meta.title nor a manifest
    // breadcrumb; repeating the product name in that case would be redundant.
    expect(documentTitleFor({})).toBe('Farna Control')
  })
})

describe('route content focus', () => {
  it('does nothing when the entered route renders neither a heading nor the shell landmark', () => {
    // A route whose content is still resolving has neither `[data-page-heading]`
    // nor the shell `<main data-main-landmark>`; there is no target to move
    // focus to, and guessing one would steal it from wherever it belongs.
    const focusSpy = vi.spyOn(HTMLElement.prototype, 'focus')
    focusRouteContent()
    expect(focusSpy).not.toHaveBeenCalled()
    focusSpy.mockRestore()
  })

  it('does not re-focus a heading that already holds focus', () => {
    const heading = document.createElement('h1')
    heading.tabIndex = -1
    heading.dataset.pageHeading = ''
    document.body.append(heading)
    heading.focus()
    const focusSpy = vi.spyOn(heading, 'focus')

    focusRouteContent()

    expect(focusSpy).not.toHaveBeenCalled()
    heading.remove()
    focusSpy.mockRestore()
  })

  it('leaves focus alone when it is already inside the page heading container', () => {
    // A composite page header renders its own focusable controls; a user who
    // has tabbed into one of them must not be yanked back to the container.
    const header = document.createElement('div')
    header.dataset.pageHeading = ''
    const control = document.createElement('a')
    control.href = '#'
    header.append(control)
    document.body.append(header)
    control.focus()
    const focusSpy = vi.spyOn(header, 'focus')

    focusRouteContent()

    expect(focusSpy).not.toHaveBeenCalled()
    header.remove()
    focusSpy.mockRestore()
  })
})

it('keeps organisation email settings reachable for a community org admin', async () => {
  // FAR-2604: `/settings/email` is the organisation SMTP configuration that
  // HITL email alerts and readiness alerts send through, so a self-hosted
  // Community install cannot work without it. BOTH levers are Community: this
  // route tier, and the view's own `FeatureGate required-tier="community"` on
  // the `email_config` capability (which is what would otherwise show the page
  // and then a "Team Feature" lock, with 402s on every call).
  decodeJwtPayload.mockReturnValue({ org_role: 'admin' })
  const resolved = router.resolve('/settings/email')
  const route = {
    name: resolved.name,
    matched: resolved.matched,
    path: resolved.path,
    meta: {} as Record<string, unknown>,
  }

  hydrateManifestMeta(route as never)

  expect(route.name).toBe('settings-email')
  expect(route.path).toBe('/settings/email')
  expect(route.meta.requiresSystemAdmin).toBeUndefined()
  expect(route.meta.requiredRoles).toEqual(['admin'])
  expect(route.meta.requiredTier).toBe('community')
  await expect(enforceRoleTierVisibility(route as never, adminToken())).resolves.toBe(true)
})

function makeRoute(name: string | undefined, meta: Record<string, unknown> = {}): any {
  return { name, meta }
}

const planStore = {
  loaded: true,
  features: {} as Record<string, unknown>,
  devMode: false,
  userPermissions: [] as string[],
  isAtMinimumTier: vi.fn(() => true),
  featureEnabled: vi.fn(() => false),
  fetchPlan: vi.fn(async () => {}),
  fetchPermissions: vi.fn(async () => {}),
}

beforeEach(() => {
  vi.clearAllMocks()
  planStore.loaded = true
  planStore.features = {}
  planStore.devMode = false
  planStore.userPermissions = []
  planStore.isAtMinimumTier.mockReturnValue(true)
  planStore.featureEnabled.mockReturnValue(false)
  planStore.fetchPlan.mockResolvedValue(undefined)
  planStore.fetchPermissions.mockResolvedValue(undefined)
  clearAccessToken()
  usePlanStore.mockReturnValue(planStore)
  decodeJwtPayload.mockReturnValue(null)
})

function adminToken(): string {
  const b64url = (obj: Record<string, unknown>) =>
    btoa(JSON.stringify(obj)).replaceAll('+', '-').replaceAll('/', '_').replaceAll('=', '')
  return `${b64url({ alg: 'none', typ: 'JWT' })}.${b64url({ org_role: 'admin' })}.test-signature`
}

describe('router permission navigation guard', () => {
  it('allows the compliance evidence index route when its permission exists', async () => {
    setAccessToken(adminToken())
    // The route is a private_preview surface (FAR-2615): developer mode must be on
    // so the only thing under test here is the permission check, not the gate.
    planStore.devMode = true
    planStore.userPermissions = ['compliance.evidence_index.read']

    await router.push({ name: 'admin-compliance-evidence-index' })

    expect(router.currentRoute.value.name).toBe('admin-compliance-evidence-index')
    expect(planStore.fetchPermissions).toHaveBeenCalled()
  })

  it('maps both evidence-index URLs to the same permission and view', () => {
    const canonical = router.getRoutes().find((route) => route.name === 'admin-compliance-evidence-index')
    const legacy = router.getRoutes().find((route) => route.name === 'admin-evidence-index')

    expect(canonical?.path).toBe('/admin/compliance/evidence-index')
    expect(legacy?.path).toBe('/admin/evidence')
    expect(canonical?.components?.default).toBeTruthy()
    expect(legacy?.components?.default).toBeTruthy()
    expect(canonical?.meta).toBeDefined()
    expect(legacy?.meta).toBeDefined()

    const manifest = yaml.load(
      readFileSync(resolve(__dirname, '../manifest.yaml'), 'utf-8'),
    ) as {
      routes: Record<string, { deprecated?: boolean }>
      features: Record<string, { behaviours?: string[] }>
    }
    expect(manifest.routes['/admin/evidence'].deprecated).toBe(true)

    const evidenceBehaviour = manifest.features['feat-evidence'].behaviours?.find(
      (behaviour) => behaviour.includes('deprecated /admin/evidence route'),
    )
    expect(evidenceBehaviour).toContain('ComplianceEvidenceIndexView.vue')
    expect(evidenceBehaviour).toContain('compatibility alias')
  })

  it('redirects the compliance evidence index route when its permission is missing', async () => {
    setAccessToken(adminToken())
    // Developer mode on so private_preview gating (FAR-2615) cannot be the reason
    // for the bounce: the refusal must be attributable to the missing permission.
    planStore.devMode = true
    planStore.userPermissions = []
    await router.push({ name: 'dashboard' })

    await router.push({ name: 'admin-compliance-evidence-index' })

    expect(router.currentRoute.value.name).toBe('dashboard')
  })
})

describe('/evals/proposals permission gate (FAR-2621)', () => {
  // The proposal queue reads with `feedback.list` (viewer floor); publishing and
  // dismissing are operator-floor and hidden per-permission inside the view.
  // Drive the guard with the REAL manifest entry so a future edit that drops or
  // widens the read gate fails here.
  function hydratedProposalsRoute(): any {
    const resolved = router.resolve('/evals/proposals')
    const route = {
      name: resolved.name,
      matched: resolved.matched,
      path: resolved.path,
      meta: {} as Record<string, unknown>,
    }
    hydrateManifestMeta(route as never)
    return route
  }

  it('declares exactly the read permission the list endpoint enforces', () => {
    const manifest = yaml.load(
      readFileSync(resolve(__dirname, '../manifest.yaml'), 'utf-8'),
    ) as { routes: Record<string, { required_permissions?: string[] | null }> }
    const entry = manifest.routes['/evals/proposals']
    expect(entry).toBeDefined()
    // `GET /api/v1/feedback/proposals` is require_permission("feedback.list").
    expect(entry.required_permissions).toEqual(['feedback.list'])
  })

  it('hydrates the manifest read gate onto the resolved route', () => {
    const route = hydratedProposalsRoute()

    expect(route.name).toBe('eval-proposals-queue')
    expect(route.meta.requiredPermissions).toEqual(['feedback.list'])
  })

  it('admits a viewer whose resolved permissions include feedback.list', async () => {
    planStore.userPermissions = ['feedback.list']

    await expect(
      enforceRoleTierVisibility(hydratedProposalsRoute(), adminToken()),
    ).resolves.toBe(true)
  })

  it('redirects a caller who does not hold feedback.list', async () => {
    planStore.userPermissions = ['feedback.review', 'gate.create', 'feedback.update']

    await expect(
      enforceRoleTierVisibility(hydratedProposalsRoute(), adminToken()),
    ).resolves.toEqual({ name: 'dashboard' })
  })
})

describe('enforceRoleTierVisibility', () => {
  it('sends a requiresSystemAdmin route to the locked page when the token is not a system admin', async () => {
    decodeJwtPayload.mockReturnValue({ is_system_admin: false })
    const result = await enforceRoleTierVisibility(
      makeRoute('admin-system-orgs', { requiresSystemAdmin: true }),
      'tok',
    )
    // FAR-2688: this previously pinned `{ name: 'dashboard' }` — the silent
    // bounce, i.e. the defect. It now asserts the locked destination and the
    // system_admin reason, with the real manifest's capability and tier.
    expect(result).toEqual({
      path: '/locked',
      query: {
        from: '/',
        capability: 'Organisations',
        tier: 'community',
        reason: 'system_admin',
      },
    })
  })

  it('allows a requiresSystemAdmin route when the token is a system admin', async () => {
    decodeJwtPayload.mockReturnValue({ is_system_admin: true })
    const result = await enforceRoleTierVisibility(
      makeRoute('admin-system-orgs', { requiresSystemAdmin: true }),
      'tok',
    )
    expect(result).toBe(true)
  })

  it('redirects when requiredRoles is set and the org role is missing', async () => {
    decodeJwtPayload.mockReturnValue({})
    const result = await enforceRoleTierVisibility(
      makeRoute('admin-org', { requiredRoles: ['admin'] }),
      'tok',
    )
    expect(result).toEqual({ name: 'dashboard' })
  })

  it('redirects when requiredRoles is set and the org role does not match', async () => {
    decodeJwtPayload.mockReturnValue({ org_role: 'member' })
    const result = await enforceRoleTierVisibility(
      makeRoute('admin-org', { requiredRoles: ['admin'] }),
      'tok',
    )
    expect(result).toEqual({ name: 'dashboard' })
  })

  it('allows when requiredRoles is set and the org role matches', async () => {
    decodeJwtPayload.mockReturnValue({ org_role: 'admin' })
    const result = await enforceRoleTierVisibility(
      makeRoute('admin-org', { requiredRoles: ['admin'] }),
      'tok',
    )
    expect(result).toBe(true)
  })

  it('sends a below-tier route to the locked page, not the Dashboard (FAR-2664)', async () => {
    decodeJwtPayload.mockReturnValue({})
    planStore.features = { something: 1 }
    planStore.isAtMinimumTier.mockReturnValue(false)
    const result = await enforceRoleTierVisibility(
      makeRoute('some-tier-route', { requiredTier: 'team' }),
      'tok',
    )
    expect(result).toEqual({
      path: '/locked',
      query: { from: '/', tier: 'team', reason: 'tier' },
    })
  })

  it('names a real Team route and the licence it needs on the locked page (FAR-2664)', async () => {
    // Pin the acceptance criterion against a REAL Team capability: a community
    // admin deep-linking /settings/sso must be told it needs the Team licence,
    // not silently returned to the Dashboard.
    decodeJwtPayload.mockReturnValue({ org_role: 'admin' })
    const resolved = router.resolve('/settings/sso')
    const route = {
      name: resolved.name,
      matched: resolved.matched,
      path: resolved.path,
      fullPath: resolved.fullPath,
      meta: {} as Record<string, unknown>,
    }
    hydrateManifestMeta(route as never)

    planStore.loaded = true
    planStore.isAtMinimumTier.mockReturnValue(false)

    const result = await enforceRoleTierVisibility(route as never, adminToken())

    expect(result).toEqual({
      path: '/locked',
      query: {
        from: '/settings/sso',
        capability: 'SSO',
        tier: 'team',
        reason: 'tier',
      },
    })
  })

  it('omits the tier and capability when the route is not in the manifest (FAR-2664)', () => {
    // Defensive path: a below-tier refusal for a route the manifest does not
    // describe still lands on the locked page, just without a capability name
    // or licence to quote.
    const target = lockedRouteFor({
      path: '/not-in-manifest',
      fullPath: '/not-in-manifest',
      matched: [],
      meta: {},
    } as never)

    expect(target).toEqual({
      path: '/locked',
      query: { from: '/not-in-manifest', reason: 'tier' },
    })
  })

  it('allows on requiredTier when at the minimum tier', async () => {
    decodeJwtPayload.mockReturnValue({})
    planStore.isAtMinimumTier.mockReturnValue(true)
    const result = await enforceRoleTierVisibility(
      makeRoute('some-tier-route', { requiredTier: 'team' }),
      'tok',
    )
    expect(result).toBe(true)
  })

  it('fetches the plan when not yet loaded before evaluating tier', async () => {
    decodeJwtPayload.mockReturnValue({})
    planStore.loaded = false
    const result = await enforceRoleTierVisibility(
      makeRoute('some-tier-route', { requiredTier: 'team' }),
      'tok',
    )
    expect(planStore.fetchPlan).toHaveBeenCalled()
    expect(result).toBe(true)
  })

  it('applies the tier check to a KNOWN-EMPTY flag map — empty ≠ unknown (FAR-1809)', async () => {
    // A payload applied with zero flags still means the TIER is known
    // (`loaded` is written together with `currentTier`). The old guard read
    // `Object.keys(features).length > 0`, so a resolved community org sailed
    // through a team route whenever the flag map came back empty.
    decodeJwtPayload.mockReturnValue({})
    planStore.loaded = true
    planStore.features = {} // known-empty, not unknown
    planStore.isAtMinimumTier.mockReturnValue(false)
    const result = await enforceRoleTierVisibility(
      makeRoute('some-tier-route', { requiredTier: 'team' }),
      'tok',
    )
    expect(result).toEqual({
      path: '/locked',
      query: { from: '/', tier: 'team', reason: 'tier' },
    })
  })

  it('skips the tier check while the tier is unknown — no payload ever applied (FAR-1809)', async () => {
    // `loaded` false after the guard's own fetch = the plan bootstrap settled
    // unreadable ('unavailable') or produced nothing, so `currentTier` is the
    // 'community' bootstrap DEFAULT, not observed truth. Tier-checking that
    // default would bounce a licensed user to the dashboard on a transient
    // plan failure; the backend 402 still enforces the real licence.
    decodeJwtPayload.mockReturnValue({})
    planStore.loaded = false
    planStore.features = {}
    planStore.isAtMinimumTier.mockReturnValue(false) // would redirect if consulted
    const result = await enforceRoleTierVisibility(
      makeRoute('some-tier-route', { requiredTier: 'team' }),
      'tok',
    )
    expect(planStore.fetchPlan).toHaveBeenCalled()
    expect(result).toBe(true)
  })

  it('redirects a private_preview route when devMode is off', async () => {
    decodeJwtPayload.mockReturnValue({})
    planStore.devMode = false
    const result = await enforceRoleTierVisibility(
      makeRoute('settings-assistant', { visibility: 'private_preview' }),
      'tok',
    )
    expect(result).toEqual({ name: 'dashboard' })
  })

  it('allows a private_preview route when devMode is on', async () => {
    decodeJwtPayload.mockReturnValue({})
    planStore.devMode = true
    const result = await enforceRoleTierVisibility(
      makeRoute('settings-assistant', { visibility: 'private_preview' }),
      'tok',
    )
    expect(result).toBe(true)
  })

  it('redirects an in_dev route when devMode is off', async () => {
    decodeJwtPayload.mockReturnValue({})
    planStore.devMode = false
    const result = await enforceRoleTierVisibility(
      makeRoute('some-dev-route', { visibility: 'in_dev' }),
      'tok',
    )
    expect(result).toEqual({ name: 'dashboard' })
  })

  it('lets a community-tier admin through /admin/org with the REAL manifest entry (FAR-1269)', async () => {
    // Drive the guard with /admin/org's actual required_tier/required_roles
    // from frontend/src/manifest.yaml, in the exact plan state that used to
    // redirect community admins to the dashboard (a RESOLVED plan — the
    // guard's fail-open applies only while the tier is UNKNOWN, `loaded`
    // false, which is not the path a real community session takes). The org HITL review window endpoint is
    // not plan-gated, so this route must admit the community admin.
    const manifest = yaml.load(
      readFileSync(resolve(__dirname, '../manifest.yaml'), 'utf-8'),
    ) as { routes: Record<string, { required_tier?: string | null; required_roles?: string[] | null }> }
    const entry = manifest.routes['/admin/org']
    expect(entry).toBeDefined()
    expect(entry.required_tier).toBe('community')

    decodeJwtPayload.mockReturnValue({ org_role: 'admin' })
    planStore.features = { hitl_reviews: true }
    // A community org is at the minimum tier only when the route's required
    // tier is itself 'community'.
    planStore.isAtMinimumTier.mockReturnValue(entry.required_tier === 'community')

    const result = await enforceRoleTierVisibility(
      makeRoute('admin-org', {
        requiredRoles: entry.required_roles ?? undefined,
        requiredTier: entry.required_tier ?? undefined,
      }),
      'tok',
    )
    expect(result).toBe(true)
  })
})

describe('/runs/diff private_preview gate (FAR-2248)', () => {
  function hydratedRunsDiff(): any {
    const resolved = router.resolve('/runs/diff')
    const route = {
      name: resolved.name,
      matched: resolved.matched,
      path: resolved.path,
      meta: {} as Record<string, unknown>,
    }
    hydrateManifestMeta(route as never)
    return route
  }

  it('hydrates the runs-diff manifest entry as private_preview, not the run-detail entry', () => {
    const route = hydratedRunsDiff()

    expect(route.name).toBe('runs-diff')
    expect(route.meta.visibility).toBe('private_preview')
  })

  it('bounces to the dashboard and names the cause in the console when dev mode is off', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    planStore.devMode = false

    const result = await enforceRoleTierVisibility(hydratedRunsDiff(), adminToken())

    expect(result).toEqual({ name: 'dashboard' })
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('runs-diff is a private_preview surface'))
    warn.mockRestore()
  })

  it('lets an admin through when dev mode is on', async () => {
    planStore.devMode = true

    const result = await enforceRoleTierVisibility(hydratedRunsDiff(), adminToken())

    expect(result).toBe(true)
  })
})

describe('/evals/editor write gate (FAR-2609)', () => {
  function hydratedEvalEditor(): any {
    const resolved = router.resolve('/evals/editor')
    const route = {
      name: resolved.name,
      matched: resolved.matched,
      path: resolved.path,
      meta: {} as Record<string, unknown>,
    }
    hydrateManifestMeta(route as never)
    return route
  }

  // The registry resolves `eval.list` to viewer while the editor's save
  // endpoints require `eval.definition.create` / `eval.definition.update`
  // (operator). Listing `eval.list` in an any-of permission gate is what let a
  // viewer open the editor and 403 on save.
  it('declares only the eval-definition writer permissions, never the viewer read', () => {
    const manifest = yaml.load(
      readFileSync(resolve(__dirname, '../manifest.yaml'), 'utf-8'),
    ) as { routes: Record<string, { required_permissions?: string[] | null }> }

    expect(manifest.routes['/evals/editor'].required_permissions).toEqual([
      'eval.definition.create',
      'eval.definition.update',
    ])
    // The all-evals list stays readable at the viewer permission.
    expect(manifest.routes['/evals'].required_permissions).toEqual(['eval.list'])
  })

  it('bounces a viewer who can only list evals', async () => {
    planStore.userPermissions = ['eval.list']

    const result = await enforceRoleTierVisibility(hydratedEvalEditor(), adminToken())

    expect(result).toEqual({ name: 'dashboard' })
  })

  it('admits an operator who can create and update eval definitions', async () => {
    planStore.userPermissions = ['eval.list', 'eval.definition.create', 'eval.definition.update']

    const result = await enforceRoleTierVisibility(hydratedEvalEditor(), adminToken())

    expect(result).toBe(true)
  })

  it('admits a scoped caller holding only the update permission', async () => {
    // Fine-grained API-key grants can carry one writer permission without the
    // other, so the gate is any-of rather than requiring both.
    planStore.userPermissions = ['eval.definition.update']

    const result = await enforceRoleTierVisibility(hydratedEvalEditor(), adminToken())

    expect(result).toBe(true)
  })
})
