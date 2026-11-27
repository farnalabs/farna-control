/**
 * Shared harness for the LifecycleMapView specs.
 *
 * `LifecycleMapView-branches.spec.ts` and `LifecycleMapViewCoverage.spec.ts`
 * each carried their own byte-identical copy of the map fixture, the i18n
 * catalogue, the `fetch` double's helpers, the mount options and the
 * localStorage double. Only the *setup* lives here; each spec keeps its own
 * `vi.mock` registrations (Vitest hoists those per test file, so they cannot
 * move into a helper) and all of its own cases.
 */
import { vi } from 'vitest'
import { createI18n } from 'vue-i18n'
import { usePlanStore } from '../../stores/planStore'

export const MAP_ID = 'map-1'

// The `vue-router` double itself stays in each spec: `vi.mock` factories are
// hoisted above the imports, so they cannot call into this module.

/**
 * Detail payload returned for `GET /lifecycle-maps/:id`.
 *
 * Superset of both copies this replaces: it keeps the third `manual` stage and
 * the second transition that only the coverage spec asserted on, so the
 * stage-count expectations (`3 stages`, `1 graduated`, `1 manual`) still hold
 * while every `stages[0]`-based test is unaffected.
 */
export const mapDetail = {
  id: MAP_ID,
  name: 'Launch Flow',
  description: 'A test lifecycle map',
  owner: 'alice',
  owner_team_id: 'team-1',
  stages: [
    { id: 'stage-1', name: 'Build', description: null, type: 'farna_control', owner_badge: null, graduated: false, pipeline_id: 'pipe-1', external_url: null },
    { id: 'stage-2', name: 'Deploy', description: 'Deploy stage', type: 'external', owner_badge: null, graduated: true, pipeline_id: null, external_url: 'https://deploy.example.com' },
    { id: 'stage-3', name: 'Manual', description: null, type: 'manual', owner_badge: null, graduated: false, pipeline_id: null, external_url: null },
  ],
  transitions: [
    { id: 'e1', source_stage_id: 'stage-1', target_stage_id: 'stage-2', trigger_type: 'auto', description: null },
    { id: 'e2', source_stage_id: 'stage-2', target_stage_id: 'stage-3', trigger_type: 'manual', description: null, condition_expression: null, estimated_frequency: null, trigger_link: null },
  ],
  versions: [
    { id: 'v-uuid-1', version: 1, created_at: '2026-01-01T00:00:00Z', created_by: 'alice' },
    { id: 'v-uuid-2', version: 2, created_at: '2026-02-01T00:00:00Z', created_by: 'bob' },
  ],
  current_version: 2,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-02-01T00:00:00Z',
}

export const i18n = createI18n({
  legacy: false,
  locale: 'en-US',
  messages: {
    'en-US': {
      views: {
        LifecycleMapView: {
          edit: 'Edit', delete: 'Delete', delete_confirm_title: 'Delete Lifecycle Map',
          delete_confirm_body: 'Delete {name}?', version_label: 'Version:', version_placeholder: 'Select a version',
          export_map: 'Export', exporting: 'Exporting...', import_map: 'Import',
          import_dialog_title: 'Import Lifecycle Map', import_paste_hint: 'Paste JSON.',
          import_placeholder: 'Paste JSON', import_payload_label: 'JSON',
          import_invalid_json: 'Not valid JSON.', importing: 'Importing...', cancel: 'Cancel',
          show_work_items: 'Show work items', save_status_saving: 'Saving...',
          save_status_saved: 'Saved', save_status_failed: 'Failed',
          journey: {
            detail_title: 'Journey: {journey}', close: 'Close', loading: 'Loading...',
            loading_more: 'Loading more...', load_more: 'Load more', no_runs: 'No runs yet',
            run_count: '{count} run | {count} runs', open: 'Open {label}', unattributed: 'Unattributed',
            unattributed_hint: '{count} unattributed run | {count} unattributed runs',
            unattributed_desc: 'Desc', filter_period_label: 'Period', filter_status_label: 'Status',
            filter_period_24h: '24h', filter_period_3d: '3d', filter_period_7d: '7d',
            filter_period_30d: '30d', filter_period_all: 'All', filter_status_all: 'All',
            provenance: { derived: 'Derived', reported: 'Reported' },
            status: {
              complete: 'Completed', failed: 'Failed', stalled: 'Stalled', running: 'Running',
              pending: 'Pending', awaiting_human: 'Awaiting Human', cancelled: 'Cancelled',
              eval_failed: 'Eval Failed', claimed: 'Claimed',
            },
          },
        },
      },
    },
  },
})

export function okJson(data: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => data,
  } as unknown as Response
}

/**
 * Message-rendering stub: the default `ErrorAlert: true` stub drops the
 * message prop, which hides converted error boxes from text assertions.
 */
export const ErrorAlertStub = {
  name: 'ErrorAlert',
  template: '<div data-testid="error-alert-stub">{{ message }}</div>',
  props: ['message', 'onRetry', 'retryable'],
}

/** Seed the plan store so feature-gated journey UI renders deterministically. */
export function seedPlan(flags: Record<string, boolean> = {}, devMode = false) {
  const planStore = usePlanStore()
  planStore.features = flags
  planStore.devMode = devMode
  planStore.loaded = true
}

/** Mount options shared by both LifecycleMapView specs. */
export function lifecycleMapMountOptions(routerPush: (...args: unknown[]) => unknown) {
  return {
    global: {
      plugins: [i18n],
      mocks: { $router: { push: routerPush } },
      stubs: {
        RouterLink: { template: '<a><slot /></a>' },
        ErrorAlert: ErrorAlertStub,
        LifecycleMapRenderer: true,
        JourneyCard: true,
        ProvenanceBadge: true,
      },
    },
  }
}

/**
 * Install a functional localStorage double and return its backing object so a
 * test can pre-seed entries. Returns the same reference the stubbed
 * `getItem`/`setItem` read and write.
 */
export function installLocalStorageStub() {
  const store: Record<string, string> = {}
  vi.stubGlobal('localStorage', {
    getItem: vi.fn((key: string) => store[key] ?? null),
    setItem: vi.fn((key: string, value: string) => { store[key] = value }),
    removeItem: vi.fn((key: string) => { delete store[key] }),
  })
  return store
}
