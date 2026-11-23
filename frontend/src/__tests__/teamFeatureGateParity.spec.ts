/**
 * Static structural parity test between the backend feature/entitlement
 * registries and the frontend gates.
 *
 * FAR-2665: a feature flag is BEHAVIOUR ONLY and declares no tier. The
 * commercial capability lives in `backend/src/farna/core/entitlements.py`, and
 * a team-gated route names that entitlement. "Team feature" below therefore
 * means "a flag covered by an entitlement".
 *
 * Reads the backend route files and frontend view files as plain text (no
 * module import / no component mount) and compares the feature-name sets.
 *
 * Known acceptable exceptions — backend-only features with no UI surface:
 *   - scim: SCIM provisioning is API-only (no UI)
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'

const REPO_ROOT = resolve(__dirname, '../../../')
const BACKEND_ROUTES = join(REPO_ROOT, 'backend', 'src', 'farna', 'api', 'routes')
const FRONTEND_SRC = resolve(__dirname, '../')
const BACKEND_FEATURE_FLAGS = join(REPO_ROOT, 'backend', 'src', 'farna', 'core', 'feature_flags.py')
const BACKEND_ENTITLEMENTS = join(REPO_ROOT, 'backend', 'src', 'farna', 'core', 'entitlements.py')

/**
 * Known acceptable exceptions — backend-only features with no UI surface:
 *   - scim: SCIM provisioning is API-only (no UI)
 *   - system_maps: the discovery/draft-generation API is backend-only in this
 *     change; the internal System Maps review UI (a separate change) is the
 *     first frontend surface and will add the featureEnabled reference.
 */
const BACKEND_ONLY_EXCEPTIONS = new Set(['scim', 'system_maps'])

/**
 * Known acceptable exceptions — frontend-gated features with no backend route
 * gate yet. Empty since FAR-2663 (``pipeline_delete`` retired).
 */
const FRONTEND_ONLY_EXCEPTIONS = new Set<string>()

/**
 * Backend-gated features whose frontend gate uses the manifest route-guard
 * mechanism rather than a FeatureGate component or planStore.featureEnabled():
 *   - analytics_page: the /analytics route is gated by
 *     required_permissions: [analytics.query] in frontend/src/manifest.yaml.
 *   - system_maps_read: the published System Maps read model is gated by the
 *     /system-maps route entries' required_permissions: [system_maps.read].
 */
const MANIFEST_GATED_EXCEPTIONS = new Set(['analytics_page', 'system_maps_read'])

interface EntitlementDef {
  key: string
  flags: string[]
}

/**
 * The declared entitlements and the flags each covers, parsed from
 * `core/entitlements.py` (the commercial single source of truth). Splitting on
 * `Entitlement(` keeps a flag list attached to its own key.
 */
function collectEntitlements(): EntitlementDef[] {
  const text = readFileSync(BACKEND_ENTITLEMENTS, 'utf-8')
  const defs: EntitlementDef[] = []
  for (const chunk of text.split('Entitlement(').slice(1)) {
    const key = chunk.match(/key="([a-z_]+)"/)?.[1]
    const flagsBlock = chunk.match(/flags=\(([^)]*)\)/)?.[1] ?? ''
    const flags = [...flagsBlock.matchAll(/"([a-z_]+)"/g)].map((m) => m[1])
    if (key) defs.push({ key, flags })
  }
  return defs
}

function collectEntitledFlags(): Set<string> {
  return new Set(collectEntitlements().flatMap((e) => e.flags))
}

/** The flag tiers the backend PRESENTS: entitled => team, everything else community. */
function collectFeatureTiers(): Map<string, string> {
  const entitled = collectEntitledFlags()
  const tiers = new Map<string, string>()
  const text = readFileSync(BACKEND_FEATURE_FLAGS, 'utf-8')
  for (const chunk of text.split('FeatureFlag(')) {
    const nameMatch = chunk.match(/name="([a-z_]+)"/)
    if (nameMatch) tiers.set(nameMatch[1], entitled.has(nameMatch[1]) ? 'team' : 'community')
  }
  return tiers
}

function collectBackendTeamFeatures(): Set<string> {
  const features = new Set<string>()
  if (!statSync(BACKEND_ROUTES, { throwIfNoEntry: false })) return features
  for (const file of readdirSync(BACKEND_ROUTES)) {
    if (!file.endsWith('.py')) continue
    const text = readFileSync(join(BACKEND_ROUTES, file), 'utf-8')
    for (const match of text.matchAll(/require_feature\("([a-z_]+)"\)/g)) {
      features.add(match[1])
    }
  }
  return features
}

/** Entitlement keys a backend route gate names. */
function collectBackendEntitlementGates(): Set<string> {
  const keys = new Set<string>()
  if (!statSync(BACKEND_ROUTES, { throwIfNoEntry: false })) return keys
  for (const file of readdirSync(BACKEND_ROUTES)) {
    if (!file.endsWith('.py')) continue
    const text = readFileSync(join(BACKEND_ROUTES, file), 'utf-8')
    for (const match of text.matchAll(/require_entitlement\("([a-z_]+)"\)/g)) keys.add(match[1])
  }
  return keys
}

function collectFrontendFeatureRefs(): Set<string> {
  const refs = new Set<string>()
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir)) {
      const full = join(dir, entry)
      if (statSync(full).isDirectory()) {
        walk(full)
      } else if (entry.endsWith('.vue') || entry.endsWith('.ts')) {
        const text = readFileSync(full, 'utf-8')
        for (const match of text.matchAll(/feature-name="([a-z_]+)"/g)) refs.add(match[1])
        for (const match of text.matchAll(/featureEnabled\('([a-z_]+)'\)/g)) refs.add(match[1])
        for (const match of text.matchAll(/featureEnabled\("([a-z_]+)"\)/g)) refs.add(match[1])
      }
    }
  }
  walk(FRONTEND_SRC)
  return refs
}

/** Every view/component that still carries a static `required-tier` prop. */
function collectStaticTierProps(): string[] {
  const offenders: string[] = []
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir)) {
      const full = join(dir, entry)
      if (statSync(full).isDirectory()) {
        walk(full)
      } else if (entry.endsWith('.vue')) {
        const text = readFileSync(full, 'utf-8')
        if (/<FeatureGate[\s\S]*?required-tier=/.test(text)) offenders.push(full)
      }
    }
  }
  walk(FRONTEND_SRC)
  return offenders
}

describe('entitlement / feature backend <-> frontend parity', () => {
  const backendFeatures = collectBackendTeamFeatures()
  const frontendRefs = collectFrontendFeatureRefs()
  const entitledFlags = collectEntitledFlags()
  const entitlements = collectEntitlements()

  it('parsed sets are non-empty (vacuity guard)', () => {
    expect(entitlements.length).toBeGreaterThan(0)
    expect(entitledFlags.size).toBeGreaterThan(0)
    expect(backendFeatures.size).toBeGreaterThan(0)
    expect(frontendRefs.size).toBeGreaterThan(0)
  })

  it('frontend-only exceptions are tracked as gaps or frontend-enforced', () => {
    const backendTestPath = join(REPO_ROOT, 'backend', 'tests', 'unit', 'test_team_feature_parity.py')
    const text = readFileSync(backendTestPath, 'utf-8')
    const match = text.match(/KNOWN_UNENFORCED_TEAM_FLAGS[^=]*=\s*(?:\{([^}]*)\}|set\(\))/)
    expect(match).not.toBeNull()
    const backendKnown = new Set(
      ((match?.[1] ?? '').match(/"([a-z_]+)"/g) ?? []).map((s) => s.replaceAll('"', '')),
    )
    for (const f of FRONTEND_ONLY_EXCEPTIONS) {
      expect(
        backendKnown.has(f) || frontendRefs.has(f),
        `${f} is neither in backend KNOWN_UNENFORCED_TEAM_FLAGS nor featureEnabled-gated in the frontend — remove it from FRONTEND_ONLY_EXCEPTIONS`,
      ).toBe(true)
    }
  })

  it('every backend-gated feature has a frontend FeatureGate reference', () => {
    const missing = [...backendFeatures].filter(
      (f) => !frontendRefs.has(f) && !BACKEND_ONLY_EXCEPTIONS.has(f) && !MANIFEST_GATED_EXCEPTIONS.has(f),
    )
    expect(missing).toEqual([])
  })

  it('every entitled frontend-gated feature is enforced on the backend', () => {
    // FAR-2665: a route may enforce the entitlement instead of naming the flag,
    // so a gate counts when either the flag or its covering entitlement is named.
    const gates = collectBackendEntitlementGates()
    const entitlementForFlag = new Map<string, string>()
    for (const def of entitlements) {
      for (const flag of def.flags) entitlementForFlag.set(flag, def.key)
    }
    const entitledFrontendGated = [...frontendRefs].filter((f) => entitledFlags.has(f))
    const missing = entitledFrontendGated.filter((f) => {
      if (FRONTEND_ONLY_EXCEPTIONS.has(f)) return false
      if (backendFeatures.has(f)) return false
      const key = entitlementForFlag.get(f)
      return key === undefined || !gates.has(key)
    })
    expect(missing).toEqual([])
  })

  it('no view carries a static required-tier prop any more (self-retiring FAR-2665)', () => {
    // This replaces the FAR-2663 VIEW_TIER_EXCEPTIONS map: the exception only
    // existed to reconcile a per-view tier prop with the registry, and the prop
    // is gone. A reappearing prop is the drift this test exists to stop.
    expect(collectStaticTierProps()).toEqual([])
  })

  it('every entitled flag is presented as team tier by the backend projection', () => {
    const tiers = collectFeatureTiers()
    for (const flag of entitledFlags) {
      expect(tiers.get(flag), `${flag} must be declared in feature_flags.py`).toBe('team')
    }
  })
})
