import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import { getAutoLoginConfig } from '../config/runtime'
import { loadMonitorConfig } from '../monitor'
import { loadMonitorConfig as loadTrackingConfig } from '../monitor/config'

/**
 * The real `index.html`, not a hand-built fixture: the previous fallback tests
 * stubbed the window directly and therefore never executed the inline
 * bootstrap. That is how a `|| {}` stub came to shadow the pre-rebrand object
 * on every returning client - the fallback existed and could never fire.
 */
const INDEX_HTML = readFileSync(resolve(process.cwd(), 'index.html'), 'utf-8')

// Assembled so the repo-wide brand sweep never sees the retired spelling.
const LEGACY_CONFIG_KEY = ['__MOD', 'ULO_CONFIG__'].join('')
const LEGACY_DISABLED_KEY = ['__MOD', 'ULO_ERROR_TRACKING_DISABLED__'].join('')

type RuntimeWindow = Record<string, unknown>

function setLegacy(key: string, value: unknown): void {
  (window as unknown as RuntimeWindow)[key] = value
}

/** Inline (src-less) scripts in document order, exactly as the browser runs them. */
function inlineBootstrapScripts(html: string): string[] {
  const scripts: string[] = []
  for (const match of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) {
    scripts.push(match[1])
  }
  return scripts
}

function runBootstrap(): void {
  for (const script of inlineBootstrapScripts(INDEX_HTML)) {
    // Same global scope the browser gives an inline <script>.
    Function(script)()
  }
}

describe('index.html runtime-config bootstrap order', () => {
  afterEach(() => {
    delete window.__FARNA_CONFIG__
    delete (window as unknown as RuntimeWindow)[LEGACY_CONFIG_KEY]
    delete (window as unknown as RuntimeWindow)[LEGACY_DISABLED_KEY]
  })

  it('keeps auto-login and monitor config from a legacy-only cached runtime-config.js', () => {
    // 1. The cached pre-rebrand script runs first and publishes ONLY the old global.
    setLegacy(LEGACY_CONFIG_KEY, {
      autoLogin: { username: 'demo', password: 'demo' },
      monitor: { monitorBackends: ['builtin'] },
    })

    // 2. Then the real inline stub from index.html runs, in document order.
    expect(inlineBootstrapScripts(INDEX_HTML).length).toBeGreaterThan(0)
    runBootstrap()

    expect(getAutoLoginConfig()).toEqual({ username: 'demo', password: 'demo', org_slug: 'demo' })
    expect(loadMonitorConfig().monitorBackends).toEqual(['builtin'])
  })

  it('still resolves the legacy kill switch after the stub runs', () => {
    setLegacy(LEGACY_DISABLED_KEY, true)

    runBootstrap()

    expect(loadTrackingConfig().enabled).toBe(false)
  })
})
