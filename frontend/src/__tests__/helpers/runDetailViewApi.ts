/**
 * Module doubles for the RunDetailView specs.
 *
 * Vitest hoists `vi.mock` above the importing file's imports, so the factory
 * cannot close over anything declared in the spec. This module is therefore
 * dependency-free (it imports only `vitest`): every factory builds its own
 * state locally, and the API spies are plain module-level constants that the
 * spec and the factory share.
 *
 * Why the router stub is so wide: RunDetailView pulls in the Analyze action
 * (FAR-1235), whose assistant-store chain imports `@/router` — that module
 * calls `beforeEach`/`afterEach`/`onError` on the router it builds at import
 * time, so the stub must answer all of them.
 */
import { vi } from 'vitest'

/** The run the specs mount at. */
export function runDetailTestRoute() {
  return {
    params: { id: 'test-run-id' },
    fullPath: '/runs/test-run-id',
    path: '/runs/test-run-id',
    query: {},
    hash: '',
    matched: [],
    name: 'run-detail',
    redirectedFrom: undefined,
    meta: {},
  }
}

const routerStub = () => {
  const testRoute = runDetailTestRoute()
  return {
    install: vi.fn(),
    push: vi.fn().mockResolvedValue(undefined),
    replace: vi.fn(),
    resolve: vi.fn(),
    go: vi.fn(),
    back: vi.fn(),
    forward: vi.fn(),
    beforeEach: vi.fn(),
    afterEach: vi.fn(),
    onError: vi.fn(),
    currentRoute: { value: testRoute },
    getRoutes: vi.fn(() => []),
    addRoute: vi.fn(),
    removeRoute: vi.fn(),
    hasRoute: vi.fn(() => false),
    isReady: vi.fn().mockResolvedValue(undefined),
  }
}

/** `vi.mock('vue-router', () => vueRouterMock())`. */
export function vueRouterMock() {
  const testRoute = runDetailTestRoute()
  const router = routerStub()
  return {
    useRoute: vi.fn(() => testRoute),
    useRouter: vi.fn(() => router),
    createRouter: vi.fn(() => routerStub()),
    createWebHistory: vi.fn(() => ({})),
  }
}

export const getMock = vi.fn()
export const postMock = vi.fn()

/** `vi.mock('../lib/api/client', () => apiClientMock())`. */
export function apiClientMock() {
  return {
    api: { GET: getMock, POST: postMock },
    getAccessToken: vi.fn().mockReturnValue('mock-token'),
  }
}

/**
 * Deterministic operator session so `isOrgOperator` is true and the guardrail
 * override button always renders — this lets the override tests assert one
 * branch unconditionally instead of falling through a runtime `if (btn.exists())`.
 */
export function jwtMock() {
  return {
    decodeJwtPayload: () => ({ org_role: 'operator' }),
    principalSubject: () => null,
  }
}
