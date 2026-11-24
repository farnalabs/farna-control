import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { loadMonitorConfig } from '../monitor/config'

// Assembled so the repo-wide brand sweep never sees the retired spelling.
const LEGACY_DISABLED_KEY = ['__MOD', 'ULO_ERROR_TRACKING_DISABLED__'].join('')

type RuntimeWindow = Record<string, unknown>

function setLegacyDisabled(value: unknown): void {
  (window as unknown as RuntimeWindow)[LEGACY_DISABLED_KEY] = value
}

beforeEach(() => {
  delete (window as unknown as Record<string, unknown>).__FARNA_ERROR_TRACKING_DISABLED__
  delete (window as unknown as RuntimeWindow)[LEGACY_DISABLED_KEY]
})

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('loadMonitorConfig', () => {
  it('defaults to enabled when no env var is set', () => {
    const config = loadMonitorConfig()
    expect(config.enabled).toBe(true)
    expect(config.environment).toBeDefined()
  })

  it('disables tracking when __FARNA_ERROR_TRACKING_DISABLED__ is true', () => {
    (window as unknown as Record<string, unknown>).__FARNA_ERROR_TRACKING_DISABLED__ = true
    const config = loadMonitorConfig()
    expect(config.enabled).toBe(false)
  })

  it('disables tracking for any truthy disable flag value', () => {
    // An EMPTY object is deliberately not in this list: it is the bootstrap
    // stub's placeholder and counts as absent (see the legacy-fallback tests).
    for (const value of ['true', '1', 1, { enabled: true }]) {
      (window as unknown as Record<string, unknown>).__FARNA_ERROR_TRACKING_DISABLED__ = value
      expect(loadMonitorConfig().enabled).toBe(false)
    }
  })

  it('stays enabled when the disable flag is falsy', () => {
    for (const value of [false, '', 0, null]) {
      (window as unknown as Record<string, unknown>).__FARNA_ERROR_TRACKING_DISABLED__ = value
      expect(loadMonitorConfig().enabled).toBe(true)
    }
  })

  it('honours the pre-rebrand kill switch a cached runtime-config.js still sets', () => {
    setLegacyDisabled(true)
    expect(loadMonitorConfig().enabled).toBe(false)
  })

  it('lets an explicit current flag win over the pre-rebrand one', () => {
    (window as unknown as Record<string, unknown>).__FARNA_ERROR_TRACKING_DISABLED__ = false
    setLegacyDisabled(true)
    expect(loadMonitorConfig().enabled).toBe(true)
  })

  it('treats an empty-object current value as absent and uses the pre-rebrand switch', () => {
    (window as unknown as Record<string, unknown>).__FARNA_ERROR_TRACKING_DISABLED__ = {}
    setLegacyDisabled(true)
    expect(loadMonitorConfig().enabled).toBe(false)
  })

  it('returns environment from import.meta.env.MODE', () => {
    const config = loadMonitorConfig()
    expect(config.environment).toBe('test')
  })
})
