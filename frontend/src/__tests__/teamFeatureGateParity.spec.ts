/**
 * Static structural parity test: every team-tier feature with a backend
 * `require_feature` gate should ALSO have a frontend `FeatureGate` /
 * `featureEnabled` reference, and vice-versa.
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

/**
 * Known acceptable exceptions — backend-only features with no UI surface:
 *   - scim: SCIM provisioning is API-only (no UI)
 *   - system_maps: the discovery/draft-generation API is backend-only in this
 *     change; the internal System Maps review UI (a separate change) is the
 *     first frontend surface and will add the featureEnabled reference.
 *
 * ``external_secrets`` is NOT an exception: the flag was retired, so external
 * secret backends are available on every tier and no gate (backend or
 * frontend) references it any more.
 */
const BACKEND_ONLY_EXCEPTIONS = new Set(['scim', 'system_maps'])

/**
 * Known acceptable exceptions — frontend-gated team features with no backend
 * route gate yet. Empty since FAR-2663: ``pipeline_delete`` was the last entry
 * and the flag is retired (deletion is guarded by the ``pipeline.delete`` RBAC
 * permission, and neither the UI nor a route references the flag any more).
 */
const FRONTEND_ONLY_EXCEPTIONS = new Set<string>()

/**
 * Backend-gated features whose frontend gate uses the manifest route-guard
 * mechanism (required_tier / required_permissions in manifest.yaml) rather
 * than a FeatureGate component or planStore.featureEnabled() call:
 *   - analytics_page: the /analytics route is gated by
 *     required_permissions: [analytics.query] in frontend/src/manifest.yaml.
 *   - system_maps_read: the published System Maps read model is gated by the
 *     /system-maps route entries' required_permissions: [system_maps.read] in
 *     frontend/src/manifest.yaml, not by a FeatureGate component.
 */
const MANIFEST_GATED_EXCEPTIONS = new Set(['analytics_page', 'system_maps_read'])

/**
 * Deliberate VIEW-TIER exceptions: a view whose own `FeatureGate required-tier`
 * (or a Governance Stack level's `requiredTier`) is BROADER — more permissive —
 * than the tier the registry declares for the flag it gates.
 *
 * The intended steady state is equality between the view prop and the flag's
 * `feature_flags.py` tier; this map records the places where they legitimately
 * diverge so a future "reconcile the props with the registry" sweep does not
 * re-lock a page that must stay open. Each entry is asserted to still be a
 * genuine divergence, so it fails loudly (and must be deleted) once the flag
 * narrows.
 *
 *   - plugin_management: the registry declares `team`, but the flag is broader
 *     than the pages it gates. Connectors and Node Categories are first-session
 *     surfaces — FAR-2604 deliberately made /admin/connectors and
 *     /admin/node-categories Community routes because connecting a repository
 *     is the first thing a self-hoster does, and their APIs carry no
 *     `require_feature` gate. The Governance Stack "Tools" level is the
 *     Connectors surface (its only link is /admin/connectors), not the Plugins
 *     page. Locking these would strand a Community installer at first run while
 *     the API still served them; a lock is worse than an unused gate.
 *     AdminPluginsView.vue stays at `team` — that IS the Plugin API surface.
 *     FAR-2665 narrows `plugin_management`, which retires this exception.
 */
const VIEW_TIER_EXCEPTIONS: Record<string, { feature: string; tier: string }> = {
  'views/AdminConnectorsView.vue': { feature: 'plugin_management', tier: 'community' },
  'views/AdminNodeCategoriesView.vue': { feature: 'plugin_management', tier: 'community' },
  'views/GovernanceStackView.vue': { feature: 'plugin_management', tier: 'community' },
}

function collectTeamFeatureNames(): Set<string> {
  const names = new Set<string>()
  const text = readFileSync(BACKEND_FEATURE_FLAGS, 'utf-8')
  // Split on each FeatureFlag declaration so name + tier resolve within the
  // same flag (a lazy cross-flag regex would misattribute a later tier="team").
  for (const chunk of text.split('FeatureFlag(')) {
    const nameMatch = chunk.match(/name="([a-z_]+)"/)
    if (nameMatch && /tier="team"/.test(chunk)) {
      names.add(nameMatch[1])
    }
  }
  return names
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

/**
 * The registry tier of every declared feature flag, keyed by name. Split on
 * each FeatureFlag declaration so a tier resolves within its own flag (the
 * same technique as collectTeamFeatureNames).
 */
function collectFeatureTiers(): Map<string, string> {
  const tiers = new Map<string, string>()
  const text = readFileSync(BACKEND_FEATURE_FLAGS, 'utf-8')
  for (const chunk of text.split('FeatureFlag(')) {
    const nameMatch = chunk.match(/name="([a-z_]+)"/)
    const tierMatch = chunk.match(/tier="([a-z]+)"/)
    if (nameMatch && tierMatch) tiers.set(nameMatch[1], tierMatch[1])
  }
  return tiers
}

/**
 * The tier a view declares for a feature, in either supported shape:
 *   - `<FeatureGate feature-name="..." required-tier="...">`
 *   - a Governance Stack `LevelDef` carrying `featureName: '...'` and, within
 *     the same object literal, `requiredTier: '...'`.
 * Returns null when the view does not gate that feature.
 */
function readViewTier(file: string, feature: string): string | null {
  const text = readFileSync(join(FRONTEND_SRC, file), 'utf-8')
  for (const tag of text.match(/<FeatureGate\b[^>]*>/g) ?? []) {
    if (!tag.includes(`feature-name="${feature}"`)) continue
    const tier = tag.match(/required-tier="([a-z]+)"/)
    return tier ? tier[1] : null
  }
  const level = text.match(
    new RegExp(`featureName:\\s*'${feature}'[\\s\\S]{0,400}?requiredTier:\\s*'([a-z]+)'`),
  )
  return level ? level[1] : null
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

describe('team feature backend <-> frontend parity', () => {
  const backendFeatures = collectBackendTeamFeatures()
  const frontendRefs = collectFrontendFeatureRefs()
  const teamFeatures = collectTeamFeatureNames()

  it('parsed feature sets are non-empty (vacuity guard)', () => {
    expect(teamFeatures.size).toBeGreaterThan(0)
    expect(backendFeatures.size).toBeGreaterThan(0)
    expect(frontendRefs.size).toBeGreaterThan(0)
  })

  it('frontend-only exceptions are tracked as gaps or frontend-enforced', () => {
    const backendTestPath = join(REPO_ROOT, 'backend', 'tests', 'unit', 'test_team_feature_parity.py')
    const text = readFileSync(backendTestPath, 'utf-8')
    // The backend declaration is either a populated set literal
    // (``= {"flag_a", "flag_b"}``) or the empty-set call (``= set()``); the
    // latter has no ``{...}`` body to capture, so treat it as an empty gap set.
    const match = text.match(/KNOWN_UNENFORCED_TEAM_FLAGS[^=]*=\s*(?:\{([^}]*)\}|set\(\))/)
    expect(match).not.toBeNull()
    const backendKnown = new Set(
      ((match?.[1] ?? '').match(/"([a-z_]+)"/g) ?? []).map((s) => s.replaceAll('"', '')),
    )
    for (const f of FRONTEND_ONLY_EXCEPTIONS) {
      // A frontend-only exception must either be tracked in the backend's
      // KNOWN_UNENFORCED_TEAM_FLAGS (a genuine gap in both layers) or be a
      // real featureEnabled UI gate in this codebase (a gate that simply
      // lacks a backend route gate yet — e.g. pipeline_delete). A flag that
      // is NEITHER is a stale exception that must be cleaned up.
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

  it('every team-tier frontend-gated feature is enforced on the backend', () => {
    const teamFrontendGated = [...frontendRefs].filter((f) => teamFeatures.has(f))
    const missing = teamFrontendGated.filter((f) => !backendFeatures.has(f) && !FRONTEND_ONLY_EXCEPTIONS.has(f))
    expect(missing).toEqual([])
  })
})

describe('recorded view-tier exceptions stay live and retire themselves', () => {
  const registryTiers = collectFeatureTiers()

  it('each exception view still declares the recorded broader tier', () => {
    for (const [file, { feature, tier }] of Object.entries(VIEW_TIER_EXCEPTIONS)) {
      expect(
        readViewTier(file, feature),
        `${file} must gate "${feature}" at required-tier="${tier}"`,
      ).toBe(tier)
    }
  })

  it('each exception is still a genuine divergence from the registry tier', () => {
    for (const [file, { feature, tier }] of Object.entries(VIEW_TIER_EXCEPTIONS)) {
      const registryTier = registryTiers.get(feature)
      expect(registryTier, `"${feature}" is not declared in feature_flags.py`).toBeDefined()
      expect(
        registryTier,
        `${file} no longer diverges from the registry ("${feature}" is "${registryTier}") — ` +
          'remove its VIEW_TIER_EXCEPTIONS entry',
      ).not.toBe(tier)
    }
  })

  it('the Plugin API surface (AdminPluginsView) stays Team-gated until FAR-2665 lands', () => {
    // Unlike Connectors and Node Categories, this page IS the Plugin API
    // surface, so it agrees with the registry's team tier.
    expect(readViewTier('views/AdminPluginsView.vue', 'plugin_management')).toBe('team')
  })
})
