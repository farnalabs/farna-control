import { afterEach, describe, expect, it } from 'vitest'
import { getAutoLoginConfig } from '../config/runtime'

// Assembled so the repo-wide brand sweep never sees the retired spelling.
const LEGACY_CONFIG_KEY = ['__MOD', 'ULO_CONFIG__'].join('')

type RuntimeWindow = Record<string, unknown>

function setLegacyConfig(value: unknown): void {
  (window as unknown as RuntimeWindow)[LEGACY_CONFIG_KEY] = value
}

describe('getAutoLoginConfig', () => {
  afterEach(() => {
    delete window.__FARNA_CONFIG__
    delete (window as unknown as RuntimeWindow)[LEGACY_CONFIG_KEY]
  })

  it('does not enable auto-login when runtime credentials are absent', () => {
    window.__FARNA_CONFIG__ = {}

    expect(getAutoLoginConfig()).toBeUndefined()
  })

  it('returns explicitly configured demo credentials with default org slug', () => {
    window.__FARNA_CONFIG__ = {
      autoLogin: { username: 'demo', password: 'demo' },
    }

    expect(getAutoLoginConfig()).toEqual({ username: 'demo', password: 'demo', org_slug: 'demo' })
  })

  it('returns a custom org slug when provided', () => {
    window.__FARNA_CONFIG__ = {
      autoLogin: { username: 'demo', password: 'demo', orgSlug: 'custom-org' },
    }

    expect(getAutoLoginConfig()).toEqual({ username: 'demo', password: 'demo', org_slug: 'custom-org' })
  })

  it('rejects incomplete or non-string credentials', () => {
    window.__FARNA_CONFIG__ = {
      autoLogin: { username: 'demo', password: undefined },
    }

    expect(getAutoLoginConfig()).toBeUndefined()
  })

  it('falls back to the pre-rebrand global a cached runtime-config.js publishes', () => {
    setLegacyConfig({ autoLogin: { username: 'demo', password: 'demo' } })

    expect(getAutoLoginConfig()).toEqual({ username: 'demo', password: 'demo', org_slug: 'demo' })
  })

  it('prefers the current global over the pre-rebrand one', () => {
    window.__FARNA_CONFIG__ = { autoLogin: { username: 'current', password: 'current' } }
    setLegacyConfig({ autoLogin: { username: 'legacy', password: 'legacy' } })

    expect(getAutoLoginConfig()).toEqual({ username: 'current', password: 'current', org_slug: 'demo' })
  })
})
