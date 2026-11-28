import { getAuthHeaders } from './auth'

const DEMO_ENTRY_PATH = /^\/demo\/?$/

/**
 * Whether a request's unrecoverable 401 may clear the session and exit to
 * /login. `sentAuthorization` is the Authorization header the request carried.
 *
 * False in two cases (FAR-2857):
 * - on /demo, where resolveDemoEntry owns the session decision. Requests sent
 *   at app startup with a stale token would otherwise redirect away before the
 *   new demo session is established.
 * - when a different token has since been stored: the 401 is about the old
 *   credential, and clearing would destroy the new session.
 */
export function mayTearDownSession(sentAuthorization: string | undefined): boolean {
  if (DEMO_ENTRY_PATH.test(window.location.pathname)) return false
  const current = getAuthHeaders()?.Authorization
  return !current || current === sentAuthorization
}
