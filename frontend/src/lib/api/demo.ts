import { decodeJwtPayload } from '../jwt'
import {
  clearAccessToken,
  getAccessToken,
  isValidToken,
  refreshSession,
  setAccessToken,
  setDemoSession,
} from './auth'
import { setMustChangePassword } from '../mustChangePassword'

// The hand-off fetch runs pre-mount (router guard) and main.ts awaits
// router.isReady() BEFORE App mounts, so a hung request would block first
// paint indefinitely. Abort after a generous fixed window instead.
const DEMO_HANDOFF_TIMEOUT_MS = 15_000
// Also pre-mount, so also bounded. Shorter: a timeout only means "no verdict".
const SESSION_PROBE_TIMEOUT_MS = 5_000
// A cheap read that checks the token's signature and expiry AND that its
// account, organisation and membership still exist.
const SESSION_PROBE_URL = '/api/v1/me/settings'

function isPastExpiry(token: string): boolean {
  const exp = decodeJwtPayload(token)?.exp
  return typeof exp === 'number' && exp * 1000 <= Date.now()
}

// Whether the stored access token no longer authenticates. The server decides,
// so a skewed client clock cannot discard a live session, and a token that is
// unexpired but dead is still caught. Only when the server gives no verdict
// (network error, timeout, 429, 5xx) is the token's own expiry claim used.
// Deliberately a bare fetch: the API client would react to a 401 by tearing the
// session down.
async function isAccessTokenDead(token: string): Promise<boolean> {
  const controller = new AbortController()
  const timeoutId = setTimeout(() => controller.abort(), SESSION_PROBE_TIMEOUT_MS)
  try {
    const res = await fetch(SESSION_PROBE_URL, {
      headers: { Authorization: `Bearer ${token}` },
      signal: controller.signal,
    })
    if (res.status === 401) return true
    if (res.status !== 429 && res.status < 500) return false
  } catch {
    console.warn('[demo] could not check the stored session')
  } finally {
    clearTimeout(timeoutId)
  }
  return isPastExpiry(token)
}

// Whether the browser holds a session worth keeping. A dead access token is
// refreshed where possible. The session is given up only when the refresh is
// refused: a refresh that could not be completed keeps it, so a transient
// failure never swaps a real session for a demo one.
async function keepStoredSession(token: string): Promise<boolean> {
  if (!(await isAccessTokenDead(token))) return true
  if ((await refreshSession()) !== 'rejected') return true
  // A sign-in elsewhere (e.g. another tab) may have replaced the token while
  // we were waiting; never tear that one down.
  const current = getAccessToken()
  return current !== null && current !== token
}

// FAR-535 demo auto-login hand-off.
//
// One-shot: tears down any stored session exactly the way logout does (a stale
// session from a previous account must never survive the demo hand-off), then
// exchanges NOTHING for a short-lived read-only demo session — the server reads
// the demo credentials from env itself; no credential is ever sent, stored, or
// put in a URL. On success the demo marker is set (drives the demo-mode banner
// and the auto-login recovery gate). Returns whether a demo session was
// established; callers redirect to the dashboard on true and to /login on
// false — a failure must never surface an error that reveals demo internals.
export async function runDemoHandOff(): Promise<boolean> {
  clearAccessToken()
  const controller = new AbortController()
  const timeoutId = setTimeout(() => controller.abort(), DEMO_HANDOFF_TIMEOUT_MS)
  try {
    const res = await fetch('/api/v1/auth/demo', {
      method: 'POST',
      signal: controller.signal,
    })
    if (!res.ok) {
      console.warn(`[demo] demo hand-off failed with status ${res.status}`)
      return false
    }
    const data = (await res.json()) as { access_token?: string }
    if (!isValidToken(data.access_token)) {
      console.warn('[demo] demo hand-off returned an unexpected response shape')
      return false
    }
    setAccessToken(data.access_token)
    // Order matters: setAccessToken resets demo state (marker + tombstone), so
    // the marker must be set AFTER storing the token — the default is "any new
    // token is not a demo session unless the demo hand-off says so".
    setDemoSession(true)
    // The demo account is seeded with must_change_password=false; clear any
    // stale gate left by a previously logged-in account.
    setMustChangePassword(false)
    return true
  } catch (err) {
    // Visitor-facing silence: log a category only — never internals.
    const timedOut = err instanceof DOMException && err.name === 'AbortError'
    console.warn(timedOut ? '[demo] demo hand-off timed out' : '[demo] demo hand-off request failed')
    return false
  } finally {
    clearTimeout(timeoutId)
  }
}

export type DemoEntryRoute = 'dashboard' | 'login'

// qa iter 2: the single /demo decision point, shared by the router guard and
// DemoView's defensive fallback so the two paths cannot drift. A browser that
// already holds a LIVE session (demo or real) keeps it and goes to the
// dashboard — a mere visit to /demo never tears a live session down or re-mints
// a token (Back/Forward must not burn the mint budget). A stored token alone is
// not proof of a live session (FAR-2857): a dead one whose refresh is refused
// counts as no session. A browser without a session runs the one-shot hand-off
// exactly once, landing on the dashboard on success or plain login on failure —
// never an error that reveals demo internals.
export async function resolveDemoEntry(): Promise<DemoEntryRoute> {
  const token = getAccessToken()
  if (token && (await keepStoredSession(token))) {
    return 'dashboard'
  }
  const ok = await runDemoHandOff()
  return ok ? 'dashboard' : 'login'
}
