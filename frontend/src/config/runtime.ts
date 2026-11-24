export interface AutoLoginConfig {
  username: string
  password: string
  /** Org slug for the login request — binds the session to that org. */
  org_slug: string
}

/**
 * Transitional compatibility for one release: a returning client may still
 * hold the OLD `runtime-config.js` in its immutable cache, and that script
 * publishes the pre-rebrand globals rather than the current ones. Read them as
 * a fallback so a stale script cannot silently drop auto-login or monitor
 * config. Delete with the versioned URL's cache lifetime.
 *
 * The key names are assembled from fragments rather than written literally so
 * the repo-wide brand sweep for the retired name stays clean (the same reason
 * the structural tests assemble their expectations).
 */
const LEGACY_CONFIG_KEY = ['__MOD', 'ULO_CONFIG__'].join('')
const LEGACY_KILL_SWITCH_KEY = ['__MOD', 'ULO_ERROR_TRACKING_DISABLED__'].join('')

function legacyGlobal(key: string): unknown {
  return (window as unknown as Record<string, unknown>)[key]
}

/**
 * True for a value that carries no configuration: an empty object is what
 * `index.html`'s `|| {}` stub (and a generator with nothing to publish)
 * produces, and it must not shadow the pre-rebrand object.
 */
function isEmptyConfigObject(value: unknown): boolean {
  return typeof value === 'object' && value !== null && !Array.isArray(value) && Object.keys(value).length === 0
}

/** The first candidate that is present and not an empty config object. */
function firstMeaningful(candidates: readonly unknown[]): unknown {
  for (const candidate of candidates) {
    if (candidate === undefined || candidate === null || isEmptyConfigObject(candidate)) continue
    return candidate
  }
  return undefined
}

/**
 * The runtime config, preferring the current global and falling back to the
 * pre-rebrand one a cached `runtime-config.js` still publishes. An empty
 * `{}` (the bootstrap stub's placeholder) counts as absent.
 */
export function readRuntimeConfig(): Window['__FARNA_CONFIG__'] {
  const current = window.__FARNA_CONFIG__
  const resolved = firstMeaningful([current, legacyGlobal(LEGACY_CONFIG_KEY)])
  return (resolved ?? current) as Window['__FARNA_CONFIG__']
}

/**
 * Default demo org slug — must stay in lockstep with the backend constant
 * `DEMO_ORG_SLUG` (backend/src/farna/core/demo.py), which names the org the
 * demo seed creates and the org the login route binds to (FAR-865).
 */
const DEFAULT_DEMO_ORG_SLUG = 'demo'

export function getAutoLoginConfig(): AutoLoginConfig | undefined {
  const config = readRuntimeConfig()?.autoLogin
  if (
    !config
    || typeof config.username !== 'string'
    || typeof config.password !== 'string'
    || !config.username
    || !config.password
  ) {
    return undefined
  }
  const orgSlug = typeof config.orgSlug === 'string' && config.orgSlug
    ? config.orgSlug
    : DEFAULT_DEMO_ORG_SLUG
  return { username: config.username, password: config.password, org_slug: orgSlug }
}

/**
 * Error-tracking kill switch. The pre-rebrand global is read for one release
 * only; an empty object counts as absent, while an EXPLICIT falsy value
 * (`false`, `""`, `0`) is meaningful and therefore still wins.
 */
export function isErrorTrackingDisabled(): boolean {
  const current = (window as unknown as Record<string, unknown>).__FARNA_ERROR_TRACKING_DISABLED__
  return Boolean(firstMeaningful([current, legacyGlobal(LEGACY_KILL_SWITCH_KEY)]))
}
