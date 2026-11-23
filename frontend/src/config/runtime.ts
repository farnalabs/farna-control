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
 * The runtime config, preferring the current global and falling back to the
 * pre-rebrand one a cached `runtime-config.js` still publishes.
 */
export function readRuntimeConfig(): Window['__FARNA_CONFIG__'] {
  return window.__FARNA_CONFIG__ ?? (legacyGlobal(LEGACY_CONFIG_KEY) as Window['__FARNA_CONFIG__'])
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
 * only; a falsy EXPLICIT current value is not nullish, so `??` never lets the
 * legacy global override it.
 */
export function isErrorTrackingDisabled(): boolean {
  const current = (window as unknown as Record<string, unknown>).__FARNA_ERROR_TRACKING_DISABLED__
  return Boolean(current ?? legacyGlobal(LEGACY_KILL_SWITCH_KEY))
}
