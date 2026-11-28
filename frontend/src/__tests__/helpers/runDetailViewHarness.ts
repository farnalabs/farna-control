/**
 * Shared harness for the RunDetailView specs.
 *
 * `RunDetailViewBranch.spec.ts` and `RunDetailViewCoverage.spec.ts` (and six
 * sibling RunDetail specs) each carried the same 137-line setup: the router /
 * API-client / jwt doubles, the run fixture base, the mount host and the
 * mount-with-run helper. The doubles live in ./runDetailViewApi (they must be
 * importable from a hoisted `vi.mock` factory); the rest is here.
 *
 * Bodies of the two specs are otherwise untouched — they assert different
 * things and are not redundant with one another.
 */
import { vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import type { Stubs } from '@vue/test-utils'
import { nextTick } from 'vue'
import { createPinia } from 'pinia'

import RunDetailView from '../../views/RunDetailView.vue'
import { getMock, postMock } from './runDetailViewApi'

export { getMock, postMock }

export function baseRun(overrides: Record<string, unknown> = {}) {
  return {
    run_id: 'test-run-id',
    pipeline_id: 'test-pipeline',
    status: 'complete',
    total_cost_usd: 1.23,
    node_token_usage: null,
    trace_id: null,
    ...overrides,
  }
}

export function mountView(options: { stubs?: Stubs; attachTo?: boolean } = {}) {
  return mount(RunDetailView, {
    attachTo: options.attachTo ? document.body : undefined,
    // The Analyze action (FAR-1235) reads plan/assistant stores, so the
    // view's test host needs an active Pinia.
    global: { plugins: [createPinia()], ...(options.stubs ? { stubs: options.stubs } : {}) },
  })
}

/** PrimeVue's Dialog teleports to <body>; inline the slots so the wrapper can
 *  find the override form. Mirrors the stub used in RunDetailGuardrail.spec.ts. */
export const overrideDialogStub = {
  template: '<div class="p-dialog"><slot name="header" /><slot /><slot name="footer" /></div>',
}

/** Set up the mock for run + IO and mount the component. */
export async function mountWith(run: Record<string, unknown>, io: Record<string, unknown> = {}) {
  getMock.mockImplementation((url: string) => {
    if (url === '/api/v1/runs/{run_id}') return Promise.resolve({ data: run, error: undefined })
    if (url === '/api/v1/runs/{run_id}/io') return Promise.resolve({ data: io, error: undefined })
    if (url === '/api/v1/runs/{run_id}/hitl/pending') return Promise.resolve({ data: { reviews: [] }, error: undefined })
    if (url === '/api/v1/pipelines/{pipeline_id}/graph') return Promise.resolve({ data: { nodes: [], edges: [] }, error: undefined })
    return Promise.resolve({ data: null, error: undefined })
  })
  const wrapper = mountView()
  await flushPromises()
  await nextTick()
  return wrapper
}

/** Shared `beforeEach` body (timers faked, one pending rerun response queued). */
export function beforeEachRunDetail() {
  vi.clearAllMocks()
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] })
  postMock.mockResolvedValue({ data: { run_id: 'test-run-id', status: 'pending' }, error: undefined })
}

/** Shared `afterEach` body. */
export function afterEachRunDetail() {
  vi.useRealTimers()
  vi.unstubAllGlobals()
}
