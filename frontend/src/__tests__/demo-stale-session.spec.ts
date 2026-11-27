// This helper MUST be imported first so globalThis.Request and globalThis.fetch
// are stubbed before lib/api/client's createClient() captures them.
import './helpers/resolve-request-origin'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

// FAR-2857: /demo with a STORED but dead session. The real router guard, auth
// store and API client run against a fake backend that, like the real one,
// rejects only the dead token.

vi.mock('vue-router', async () => {
  const actual = await vi.importActual<typeof import('vue-router')>('vue-router')
  return actual
})

import router from '../router'
import { resolveDemoEntry } from '../lib/api/demo'
import { api, shouldReRunAutoLogin } from '../lib/api/client'
import {
  getAccessToken,
  isDemoSession,
  setAccessToken,
  setDemoSession,
  wasDemoSessionEnded,
} from '../lib/api/auth'
import { usePlanStore } from '../stores/planStore'
import { fetchMock as sharedFetchMock } from './helpers/resolve-request-origin'

// The shared spy is declared without parameters; this suite reads the request.
const fetchMock = sharedFetchMock as unknown as Mock<typeof fetch>

const PROBE = '/api/v1/me/settings'
const REFRESH = '/api/v1/auth/refresh'
const MINT = '/api/v1/auth/demo'

// A number is an HTTP status; 'offline' is a network failure.
type Outcome = number | 'offline'

interface Backend {
  deadTokens: string[]
  probe?: Outcome
  refresh: Outcome
  mint: Outcome
}

function jwt(label: string, expiresInSeconds: number): string {
  const encode = (value: object) => Buffer.from(JSON.stringify(value)).toString('base64url')
  const exp = Math.floor(Date.now() / 1000) + expiresInSeconds
  return `${encode({ alg: 'HS256' })}.${encode({ sub: label, exp })}.signature`
}

const expiredToken = jwt('stored', -60)
const unexpiredToken = jwt('stored', 3600)
const refreshedToken = jwt('refreshed', 3600)
const mintedToken = jwt('minted', 3600)

let calls: string[] = []

function reply(outcome: Outcome, body: object = {}): Response {
  if (outcome === 'offline') throw new TypeError('Failed to fetch')
  return new Response(JSON.stringify(outcome === 200 ? body : {}), { status: outcome })
}

function useBackend(backend: Partial<Backend>): void {
  const { deadTokens, probe, refresh, mint }: Backend = {
    deadTokens: [expiredToken],
    refresh: 401,
    mint: 200,
    ...backend,
  }
  fetchMock.mockImplementation((async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = input instanceof Request ? input.url : String(input)
    const path = new URL(url, 'http://localhost/').pathname
    calls.push(path)
    if (path === MINT) return reply(mint, { access_token: mintedToken })
    if (path === REFRESH) return reply(refresh, { access_token: refreshedToken })
    if (path === PROBE && probe !== undefined) return reply(probe)
    const headers = input instanceof Request ? input.headers : new Headers(init?.headers)
    const bearer = headers.get('Authorization')?.replace('Bearer ', '') ?? ''
    return reply(deadTokens.includes(bearer) ? 401 : 200)
  }))
}

function storeSession(token: string, kind: 'demo' | 'real'): void {
  setAccessToken(token)
  setDemoSession(kind === 'demo')
}

function expectNewDemoSession(): void {
  expect(getAccessToken()).toBe(mintedToken)
  expect(isDemoSession()).toBe(true)
  expect(wasDemoSessionEnded()).toBe(false)
}

function expectKeptSession(token: string, kind: 'demo' | 'real'): void {
  expect(getAccessToken()).toBe(token)
  expect(isDemoSession()).toBe(kind === 'demo')
  expect(calls).not.toContain(MINT)
}

beforeEach(() => {
  calls = []
  localStorage.clear()
  fetchMock.mockReset()
  setActivePinia(createPinia())
  usePlanStore().loaded = true
  vi.spyOn(console, 'warn').mockImplementation(() => {})
})

afterEach(() => {
  window.history.replaceState(null, '', '/')
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('/demo with a dead stored session', () => {
  it('starts a new demo session through the router for an expired demo token', async () => {
    storeSession(expiredToken, 'demo')
    useBackend({})

    await router.push('/demo')

    expect(router.currentRoute.value.name).toBe('dashboard')
    expectNewDemoSession()
    expect(calls).not.toContain(REFRESH)
  }, 60_000)

  it.each([401, 403])(
    'starts a demo session when an expired real session is refused a refresh with %i',
    async (status) => {
      storeSession(expiredToken, 'real')
      useBackend({ refresh: status })

      expect(await resolveDemoEntry()).toBe('dashboard')

      expectNewDemoSession()
      expect(calls).toStrictEqual([PROBE, REFRESH, MINT])
    },
  )

  it.each(['demo', 'real'] as const)(
    'replaces an unexpired %s token that the server rejects',
    async (kind) => {
      storeSession(unexpiredToken, kind)
      useBackend({ deadTokens: [unexpiredToken] })

      expect(await resolveDemoEntry()).toBe('dashboard')

      expectNewDemoSession()
    },
  )

  it.each(['offline', 500, 429] as const)(
    'replaces an expired demo token when the probe gives no verdict (%s)',
    async (probe) => {
      storeSession(expiredToken, 'demo')
      useBackend({ probe })

      expect(await resolveDemoEntry()).toBe('dashboard')

      expectNewDemoSession()
    },
  )

  it('lands on /login and keeps auto-login blocked when no new demo session can be minted', async () => {
    storeSession(expiredToken, 'demo')
    useBackend({ mint: 429 })

    expect(await resolveDemoEntry()).toBe('login')

    expect(getAccessToken()).toBeNull()
    expect(wasDemoSessionEnded()).toBe(true)
    expect(shouldReRunAutoLogin(true, false, true)).toBe(false)
  })
})

describe('/demo with a session worth keeping', () => {
  it.each(['demo', 'real'] as const)(
    'keeps a live %s session across repeated visits without minting or refreshing',
    async (kind) => {
      storeSession(unexpiredToken, kind)
      useBackend({})

      expect(await resolveDemoEntry()).toBe('dashboard')
      expect(await resolveDemoEntry()).toBe('dashboard')

      expectKeptSession(unexpiredToken, kind)
      expect(calls).toStrictEqual([PROBE, PROBE])
    },
  )

  it('keeps a token the server accepts even when the local clock says it expired', async () => {
    storeSession(expiredToken, 'demo')
    useBackend({ deadTokens: [] })

    expect(await resolveDemoEntry()).toBe('dashboard')

    expectKeptSession(expiredToken, 'demo')
  })

  it.each([403, 404])('does not treat a %i from the probe as a dead session', async (probe) => {
    storeSession(unexpiredToken, 'real')
    useBackend({ probe })

    expect(await resolveDemoEntry()).toBe('dashboard')

    expectKeptSession(unexpiredToken, 'real')
    expect(calls).toStrictEqual([PROBE])
  })

  it.each(['offline', 503, 429] as const)(
    'keeps an unexpired session when the probe gives no verdict (%s)',
    async (probe) => {
      storeSession(unexpiredToken, 'real')
      useBackend({ probe })

      expect(await resolveDemoEntry()).toBe('dashboard')

      expectKeptSession(unexpiredToken, 'real')
      expect(calls).toStrictEqual([PROBE])
    },
  )

  it('refreshes an expired real session and keeps it', async () => {
    storeSession(expiredToken, 'real')
    useBackend({ refresh: 200 })

    expect(await resolveDemoEntry()).toBe('dashboard')

    expectKeptSession(refreshedToken, 'real')
  })

  it.each(['offline', 500, 429] as const)(
    'never swaps a real session for a demo one when the refresh cannot complete (%s)',
    async (refresh) => {
      storeSession(expiredToken, 'real')
      useBackend({ refresh })

      expect(await resolveDemoEntry()).toBe('dashboard')

      expectKeptSession(expiredToken, 'real')
    },
  )

  it('keeps a token that another sign-in stored while the old one was being refreshed', async () => {
    storeSession(expiredToken, 'real')
    useBackend({})
    const backend = fetchMock.getMockImplementation()!
    fetchMock.mockImplementation((async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === REFRESH) setAccessToken(refreshedToken)
      return backend(input, init)
    }))

    expect(await resolveDemoEntry()).toBe('dashboard')

    expectKeptSession(refreshedToken, 'real')
  })

  it('bounds a hung probe and falls back to the token expiry', async () => {
    vi.useFakeTimers()
    storeSession(unexpiredToken, 'real')
    fetchMock.mockImplementation(((_input: unknown, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () =>
          reject(new DOMException('The operation was aborted.', 'AbortError')),
        )
      })))

    const pending = resolveDemoEntry()
    await vi.advanceTimersByTimeAsync(5_000)

    expect(await pending).toBe('dashboard')
    expect(getAccessToken()).toBe(unexpiredToken)
  })
})

// main.ts sends authenticated requests (the locale preference) right after
// installing the router, so they carry the stale token while the /demo guard is
// still running.
describe('startup request racing the /demo hand-off', () => {
  it.each(['demo', 'real'] as const)(
    'does not undo the new demo session when a stale %s request fails alongside it',
    async (kind) => {
      window.history.replaceState(null, '', '/demo')
      storeSession(expiredToken, kind)
      useBackend({})

      const startupRequest = api.GET('/api/v1/me/settings')
      const entry = resolveDemoEntry()

      expect(await entry).toBe('dashboard')
      await startupRequest
      expectNewDemoSession()
    },
  )

  it('keeps the new demo session when the stale 401 arrives after the hand-off', async () => {
    storeSession(expiredToken, 'demo')
    useBackend({})
    const backend = fetchMock.getMockImplementation()!
    let releaseStale: () => void = () => {}
    const staleHeld = new Promise<void>((resolve) => {
      releaseStale = resolve
    })
    fetchMock.mockImplementationOnce((async (input: RequestInfo | URL, init?: RequestInit) => {
      await staleHeld
      return backend(input, init)
    }))

    const startupRequest = api.GET('/api/v1/me/settings')
    expect(await resolveDemoEntry()).toBe('dashboard')
    releaseStale()
    await startupRequest

    expectNewDemoSession()
  })
})
