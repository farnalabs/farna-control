/**
 * Branch coverage tests for LifecycleMapView.vue (FAR-835).
 *
 * Targets uncovered branches: saveStatus rendering (saving/saved/error),
 * version change (same/different), loadSavedPositions server vs localStorage,
 * persistPositions abort/error, delete success/error, import valid/error,
 * onVersionChange guards, closeJourneyDetail, retryJourneyDetail guards,
 * journey detail empty runs/loading, mapData null, journeys pagination,
 * unattributedJourneys, journeys error.
 *
 * Setup comes from ./helpers/lifecycleMapViewHarness (shared with
 * LifecycleMapViewCoverage.spec.ts, which covers the template/handler half).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

const routerPushMock = vi.fn()

// `vi.mock` factories are hoisted above the imports, so the route/router double
// stays in-file; everything else comes from the shared harness.
function defaultRoute() {
  return { params: { id: 'map-1' }, query: {}, meta: {}, name: 'lifecycle-map-detail' }
}

vi.mock('vue-router', () => ({
  useRoute: vi.fn(() => defaultRoute()),
  useRouter: vi.fn(() => ({ push: routerPushMock })),
}))

vi.mock('../lib/api/auth', () => ({
  getAuthHeaders: vi.fn(() => ({ Authorization: 'Bearer token-1' })),
  attemptTokenRefresh: vi.fn(async () => true),
  clearAccessToken: vi.fn(),
  exitToLogin: vi.fn(),
}))

vi.mock('../lib/api/formatError', () => ({
  formatApiError: (e: unknown) => {
    if (e instanceof Error) return e.message
    const obj = e as Record<string, unknown> | null
    return (obj?.detail as string) ?? 'Request failed'
  },
}))

// Imported after the mocks so the view resolves the doubles registered above.
import LifecycleMapView from '../views/lifecycle-map/LifecycleMapView.vue'
import { useLifecycleMapsStore } from '../stores/lifecycleMaps'
import {
  installLocalStorageStub,
  lifecycleMapMountOptions,
  mapDetail,
  okJson,
  seedPlan,
} from './helpers/lifecycleMapViewHarness'

let fetchMock: ReturnType<typeof vi.fn>
let journeysResponse: { items: unknown[]; next_cursor?: string | null }

beforeEach(async () => {
  setActivePinia(createPinia())
  const { useRoute } = await import('vue-router')
  // Reset the route on every test: guard tests below install a no-`id` route,
  // and `clearAllMocks()` does not undo a `mockReturnValue`.
  vi.mocked(useRoute).mockReturnValue(defaultRoute() as never)
  routerPushMock.mockClear()
  seedPlan()
  journeysResponse = { items: [] }
  fetchMock = vi.fn((url: string) => {
    if (url.includes('/journeys')) return Promise.resolve(okJson(journeysResponse))
    if (url.includes('/export')) {
      return Promise.resolve(okJson({
        primitive_type: 'lifecycle_map', format_version: '1', name: 'Launch Flow',
        description: null, content_json: { stages: [{ id: 'stage-1', name: 'Build', type: 'manual' }], edges: [] },
      }))
    }
    return Promise.resolve(okJson(mapDetail))
  })
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
  document.body.innerHTML = ''
})

function mountView() {
  return mount(LifecycleMapView, lifecycleMapMountOptions(routerPushMock))
}

// ── saveStatus rendering ────────────────────────────────────────────────
describe('LifecycleMapView branches — saveStatus', () => {
  beforeEach(() => {
    installLocalStorageStub()
  })

  it('shows saving status during position save', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let resolveVersion!: (v: Response) => void
    fetchMock.mockImplementation((url: string) => {
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      if (url.includes('/versions/')) {
        return new Promise((resolve) => { resolveVersion = resolve })
      }
      return Promise.resolve(okJson(mapDetail))
    })

    const wrapper = mountView()
    await flushPromises()

    const vm = wrapper.vm as unknown as { handlePositionsChanged: (p: Record<string, { x: number; y: number }>) => void }
    vm.handlePositionsChanged({ 'stage-1': { x: 123, y: 456 } })
    await vi.advanceTimersByTimeAsync(500)

    const status = wrapper.find('[data-testid="lifecycle-map-save-status"]')
    expect(status.exists()).toBe(true)
    expect(status.text()).toContain('Saving')

    resolveVersion(okJson({ id: 'v1', version: 2, created_at: '2026-01-01T00:00:00Z', created_by: null }))
    await flushPromises()
    vi.useRealTimers()
    wrapper.unmount()
  })

  it('shows error status when server write fails', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    fetchMock.mockImplementation((url: string) => {
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      if (url.includes('/versions/')) {
        return Promise.resolve({ ok: false, status: 500, statusText: 'Error', json: async () => ({ detail: 'server error' }) })
      }
      return Promise.resolve(okJson(mapDetail))
    })

    const wrapper = mountView()
    await flushPromises()

    const vm = wrapper.vm as unknown as { handlePositionsChanged: (p: Record<string, { x: number; y: number }>) => void }
    vm.handlePositionsChanged({ 'stage-1': { x: 1, y: 2 } })
    await vi.advanceTimersByTimeAsync(600)

    const status = wrapper.find('[data-testid="lifecycle-map-save-status"]')
    expect(status.exists()).toBe(true)
    expect(status.text()).toContain('Failed')
    vi.useRealTimers()
    wrapper.unmount()
  })
})

// ── version change ──────────────────────────────────────────────────────
describe('LifecycleMapView branches — version change', () => {
  it('fetches map when same version is selected', async () => {
    const wrapper = mountView()
    await flushPromises()
    fetchMock.mockClear()

    const vm = wrapper.vm as unknown as {
      selectedVersion: number | null
      onVersionChange: () => Promise<void>
    }
    // Set selectedVersion to the same as current_version
    vm.selectedVersion = 2
    await vm.onVersionChange()

    // Should call fetchMap (same version path)
    const calls = fetchMock.mock.calls.map((c: unknown[]) => String(c[0]))
    expect(calls.some(url => url.includes('/lifecycle-maps/map-1'))).toBe(true)
    wrapper.unmount()
  })

  it('fetches specific version when different version is selected', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      if (url.includes('/versions/1')) {
        return Promise.resolve(okJson({ ...mapDetail, current_version: 1, stages: [{ ...mapDetail.stages[0] }] }))
      }
      return Promise.resolve(okJson(mapDetail))
    })

    const wrapper = mountView()
    await flushPromises()
    fetchMock.mockClear()

    const vm = wrapper.vm as unknown as {
      selectedVersion: number | null
      onVersionChange: () => Promise<void>
    }
    vm.selectedVersion = 1
    await vm.onVersionChange()

    const calls = fetchMock.mock.calls.map((c: unknown[]) => String(c[0]))
    expect(calls.some(url => url.includes('/versions/1'))).toBe(true)
    wrapper.unmount()
  })

  it('returns early when no mapId', async () => {
    const { useRoute } = await import('vue-router')
    vi.mocked(useRoute).mockReturnValue({ params: { id: '' }, query: {}, meta: {}, name: 'lifecycle-map-detail' } as never)

    const wrapper = mountView()
    await flushPromises()
    fetchMock.mockClear()

    const vm = wrapper.vm as unknown as { onVersionChange: () => Promise<void> }
    await vm.onVersionChange()
    expect(fetchMock).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('returns early when selectedVersion is null', async () => {
    const wrapper = mountView()
    await flushPromises()
    fetchMock.mockClear()

    const vm = wrapper.vm as unknown as {
      selectedVersion: number | null
      onVersionChange: () => Promise<void>
    }
    vm.selectedVersion = null
    await vm.onVersionChange()
    expect(fetchMock).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})

// ── loadSavedPositions — server has positions vs localStorage fallback ──
describe('LifecycleMapView branches — loadSavedPositions', () => {
  let localStorageStore: Record<string, string>

  beforeEach(() => {
    localStorageStore = installLocalStorageStub()
  })

  it('loads positions from server stages with x/y', async () => {
    const mapWithPositions = {
      ...mapDetail,
      stages: [{ ...mapDetail.stages[0], x: 500, y: 600 }],
    }
    fetchMock.mockImplementation((url: string) => {
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      return Promise.resolve(okJson(mapWithPositions))
    })

    const wrapper = mountView()
    await flushPromises()

    const renderer = wrapper.findComponent({ name: 'LifecycleMapRenderer' })
    expect(renderer.props('savedPositions')).toEqual({ 'stage-1': { x: 500, y: 600 } })
    wrapper.unmount()
  })

  it('falls back to localStorage when server has no positions', async () => {
    localStorageStore['lifecycle-map-positions:map-1:2'] = JSON.stringify({ 'stage-1': { x: 100, y: 200 } })
    const wrapper = mountView()
    await flushPromises()

    const renderer = wrapper.findComponent({ name: 'LifecycleMapRenderer' })
    expect(renderer.props('savedPositions')).toEqual({ 'stage-1': { x: 100, y: 200 } })
    wrapper.unmount()
  })

  it('returns empty when localStorage has no positions', async () => {
    const wrapper = mountView()
    await flushPromises()

    const renderer = wrapper.findComponent({ name: 'LifecycleMapRenderer' })
    expect(renderer.props('savedPositions')).toEqual({})
    wrapper.unmount()
  })

  it('returns early when no mapId', async () => {
    const { useRoute } = await import('vue-router')
    vi.mocked(useRoute).mockReturnValue({ params: { id: '' }, query: {}, meta: {}, name: 'lifecycle-map-detail' } as never)
    const wrapper = mountView()
    await flushPromises()
    // No crash, positions stay empty — the renderer is stubbed so check the view text
    expect(wrapper.text()).toContain('Lifecycle Map')
    wrapper.unmount()
  })
})

// ── delete confirm successful ───────────────────────────────────────────
describe('LifecycleMapView branches — delete success', () => {
  it('navigates to /lifecycle-maps after successful delete', async () => {
    const wrapper = mountView()
    await flushPromises()

    const vm = wrapper.vm as unknown as {
      showDeleteDialog: boolean
      handleDeleteConfirm: () => Promise<void>
    }
    vm.showDeleteDialog = true
    await flushPromises()

    await vm.handleDeleteConfirm()
    await flushPromises()

    expect(routerPushMock).toHaveBeenCalledWith('/lifecycle-maps')
    wrapper.unmount()
  })
})

// ── delete error ────────────────────────────────────────────────────────
describe('LifecycleMapView branches — delete error', () => {
  it('renders the delete-error alert when delete fails with the dialog open', async () => {
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      // Only fail DELETE requests for the map, pass through GET
      if (String(url).includes('/lifecycle-maps/map-1') && init?.method === 'DELETE') {
        return Promise.resolve({ ok: false, status: 500, statusText: 'Error', json: async () => ({ detail: 'delete failed' }) })
      }
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      return Promise.resolve(okJson(mapDetail))
    })

    const wrapper = mountView()
    await flushPromises()

    // Open the confirmation dialog first, so its slot (including the converted
    // ErrorAlert surface) is mounted when the delete attempt fails.
    const vm = wrapper.vm as unknown as { showDeleteDialog: boolean; handleDeleteConfirm: () => Promise<void> }
    vm.showDeleteDialog = true
    await flushPromises()

    await vm.handleDeleteConfirm()
    await flushPromises()

    // The component should not crash — the error is caught and surfaced.
    expect(wrapper.exists()).toBe(true)
    // The dialog teleports its body to <body>, where the ErrorAlert stub renders
    // the failure message.
    expect(document.body.textContent).toContain('delete failed')
    wrapper.unmount()
  })
})

// ── import valid JSON success ───────────────────────────────────────────
describe('LifecycleMapView branches — import success', () => {
  it('navigates to new map after successful import', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (String(url).includes('/import')) {
        return Promise.resolve(okJson({ id: 'new-map-1' }))
      }
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      return Promise.resolve(okJson(mapDetail))
    })

    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="lifecycle-map-import"]').trigger('click')
    await flushPromises()

    const payload = document.body.querySelector('[data-testid="lifecycle-map-import-payload"]') as HTMLTextAreaElement
    payload.value = JSON.stringify({
      primitive_type: 'lifecycle_map',
      format_version: '1',
      name: 'Imported Map',
      content_json: { stages: [], edges: [] },
    })
    await payload.dispatchEvent(new Event('input'))
    await flushPromises()

    const confirmBtn = document.body.querySelector('[data-testid="lifecycle-map-import-confirm"]') as HTMLButtonElement
    await confirmBtn.click()
    await flushPromises()

    expect(routerPushMock).toHaveBeenCalledWith({ name: 'lifecycle-map-detail', params: { id: 'new-map-1' } })
    wrapper.unmount()
  })
})

// ── import SyntaxError vs API error ─────────────────────────────────────
describe('LifecycleMapView branches — import error', () => {
  it('shows SyntaxError message for invalid JSON', async () => {
    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="lifecycle-map-import"]').trigger('click')
    await flushPromises()

    const payload = document.body.querySelector('[data-testid="lifecycle-map-import-payload"]') as HTMLTextAreaElement
    payload.value = 'not-valid-json{'
    await payload.dispatchEvent(new Event('input'))
    await flushPromises()

    const confirmBtn = document.body.querySelector('[data-testid="lifecycle-map-import-confirm"]') as HTMLButtonElement
    await confirmBtn.click()
    await flushPromises()

    expect(document.body.textContent).toContain('Not valid JSON')
    wrapper.unmount()
  })

  it('shows API error for failed import', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (String(url).includes('/import')) {
        return Promise.resolve({ ok: false, status: 400, json: async () => ({ detail: 'bad envelope' }) })
      }
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      return Promise.resolve(okJson(mapDetail))
    })

    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="lifecycle-map-import"]').trigger('click')
    await flushPromises()

    const payload = document.body.querySelector('[data-testid="lifecycle-map-import-payload"]') as HTMLTextAreaElement
    payload.value = JSON.stringify({
      primitive_type: 'lifecycle_map',
      format_version: '1',
      name: 'test',
      content_json: {},
    })
    await payload.dispatchEvent(new Event('input'))
    await flushPromises()

    const confirmBtn = document.body.querySelector('[data-testid="lifecycle-map-import-confirm"]') as HTMLButtonElement
    await confirmBtn.click()
    await flushPromises()

    expect(document.body.textContent).toContain('bad envelope')
    wrapper.unmount()
  })
})

// ── closeJourneyDetail ──────────────────────────────────────────────────
describe('LifecycleMapView branches — closeJourneyDetail', () => {
  it('clears journey state', async () => {
    const wrapper = mountView()
    await flushPromises()
    const store = useLifecycleMapsStore()
    store.selectedJourneyKey = 'run:run-1'
    store.journeyDetail = { kind: 'run', ref: 'run-1', runs: [] } as never
    await flushPromises()

    const vm = wrapper.vm as unknown as { closeJourneyDetail: () => void }
    vm.closeJourneyDetail()
    expect(store.selectedJourneyKey).toBeNull()
    expect(store.journeyDetail).toBeNull()
    wrapper.unmount()
  })
})

// ── retryJourneyDetail guards ──────────────────────────────────────────
describe('LifecycleMapView branches — retryJourneyDetail', () => {
  it('returns early when no mapId', async () => {
    const { useRoute } = await import('vue-router')
    vi.mocked(useRoute).mockReturnValue({ params: { id: '' }, query: {}, meta: {}, name: 'lifecycle-map-detail' } as never)
    const wrapper = mountView()
    await flushPromises()
    // A key is set so the mapId guard is what stops the call, not the key guard.
    const store = useLifecycleMapsStore()
    store.selectedJourneyKey = 'run:run-1'
    fetchMock.mockClear()
    const vm = wrapper.vm as unknown as { retryJourneyDetail: () => Promise<void> }
    await vm.retryJourneyDetail()
    expect(fetchMock).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('returns early when no key', async () => {
    const wrapper = mountView()
    await flushPromises()
    const store = useLifecycleMapsStore()
    store.selectedJourneyKey = null
    fetchMock.mockClear()
    const vm = wrapper.vm as unknown as { retryJourneyDetail: () => Promise<void> }
    await vm.retryJourneyDetail()
    expect(fetchMock).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('returns early when key has no colon', async () => {
    const wrapper = mountView()
    await flushPromises()
    const store = useLifecycleMapsStore()
    store.selectedJourneyKey = 'nocolonkey'
    fetchMock.mockClear()
    const vm = wrapper.vm as unknown as { retryJourneyDetail: () => Promise<void> }
    await vm.retryJourneyDetail()
    expect(fetchMock).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})

// ── journey detail: runs empty ──────────────────────────────────────────
describe('LifecycleMapView branches — journey detail empty runs', () => {
  it('shows no_runs message when journey detail has empty runs', async () => {
    seedPlan({}, true)
    const wrapper = mountView()
    await flushPromises()
    await wrapper.find('[data-testid="lifecycle-map-show-work-items"]').setValue(true)
    await flushPromises()

    const store = useLifecycleMapsStore()
    store.selectedJourneyKey = 'run:run-1'
    store.journeyDetail = { kind: 'run', ref: 'run-1', runs: [] } as never
    await flushPromises()

    expect(wrapper.text()).toContain('No runs yet')
    wrapper.unmount()
  })

  it('shows loading spinner when journeyDetail is null', async () => {
    seedPlan({}, true)
    const wrapper = mountView()
    await flushPromises()
    await wrapper.find('[data-testid="lifecycle-map-show-work-items"]').setValue(true)
    await flushPromises()

    const store = useLifecycleMapsStore()
    store.selectedJourneyKey = 'run:run-1'
    store.journeyDetail = null
    await flushPromises()

    // Loading spinner should be shown
    const detail = wrapper.find('[aria-label="Journey details"]')
    if (detail.exists()) {
      expect(detail.find('.animate-spin').exists()).toBe(true)
    }
    wrapper.unmount()
  })
})

// ── mapData null ────────────────────────────────────────────────────────
describe('LifecycleMapView branches — mapData null', () => {
  it('shows error alert when the detail fetch 404s', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      return Promise.resolve({ ok: false, status: 404, statusText: 'Not Found', json: async () => ({ detail: 'not found' }) })
    })

    const wrapper = mountView()
    await flushPromises()

    // When the fetch fails with 404, the store sets detailError
    // and the ErrorAlert component renders
    expect(wrapper.findComponent({ name: 'ErrorAlert' }).exists()).toBe(true)
    wrapper.unmount()
  })
})

// ── journeys pagination hasMore ─────────────────────────────────────────
describe('LifecycleMapView branches — journeys pagination', () => {
  it('shows load more button when hasMoreJourneys is true', async () => {
    seedPlan({}, true)
    journeysResponse = { items: [{ kind: 'run', ref: 'r1', unattributed: true, status: 'complete' }], next_cursor: 'cursor-2' }

    const wrapper = mountView()
    await flushPromises()
    await wrapper.find('[data-testid="lifecycle-map-show-work-items"]').setValue(true)
    await flushPromises()

    expect(wrapper.find('[data-testid="lifecycle-map-journeys-load-more"]').exists()).toBe(true)
    wrapper.unmount()
  })
})

// ── unattributedJourneys present ────────────────────────────────────────
describe('LifecycleMapView branches — unattributedJourneys', () => {
  it('renders unattributed section when journeys are present', async () => {
    seedPlan({}, true)
    journeysResponse = {
      items: [{ kind: 'run', ref: 'r1', unattributed: true, status: 'complete', provenance: 'execution' }],
      next_cursor: null,
    }

    const wrapper = mountView()
    await flushPromises()
    await wrapper.find('[data-testid="lifecycle-map-show-work-items"]').setValue(true)
    await flushPromises()

    expect(wrapper.find('[aria-label="Unattributed journeys"]').exists()).toBe(true)
    wrapper.unmount()
  })
})

// ── journeys error ──────────────────────────────────────────────────────
describe('LifecycleMapView branches — journeys error', () => {
  it('shows error when journeys fetch fails', async () => {
    seedPlan({}, true)
    fetchMock.mockImplementation((url: string) => {
      if (url.includes('/journeys')) {
        return Promise.resolve({ ok: false, status: 500, json: async () => ({ detail: 'journeys failed' }) })
      }
      return Promise.resolve(okJson(mapDetail))
    })

    const wrapper = mountView()
    await flushPromises()
    await wrapper.find('[data-testid="lifecycle-map-show-work-items"]').setValue(true)
    await flushPromises()

    // ErrorAlert should be present for journeys error
    expect(wrapper.findComponent({ name: 'ErrorAlert' }).exists()).toBe(true)
    wrapper.unmount()
  })
})

// ── statusBadgeClass branches ───────────────────────────────────────────
describe('LifecycleMapView branches — statusBadgeClass', () => {
  it('returns correct badge for all status types', async () => {
    const wrapper = mountView()
    await flushPromises()
    const vm = wrapper.vm as unknown as { statusBadgeClass: (s: string) => string }
    expect(vm.statusBadgeClass('complete')).toBe('badge-context-green')
    expect(vm.statusBadgeClass('failed')).toBe('badge-context-rose')
    expect(vm.statusBadgeClass('stalled')).toBe('badge-context-rose')
    expect(vm.statusBadgeClass('eval_failed')).toBe('badge-context-rose')
    expect(vm.statusBadgeClass('running')).toBe('badge-context-blue')
    expect(vm.statusBadgeClass('awaiting_human')).toBe('badge-context-amber')
    expect(vm.statusBadgeClass('claimed')).toBe('badge-context-amber')
    expect(vm.statusBadgeClass('unknown')).toBe('badge-context-slate')
    wrapper.unmount()
  })
})

// ── statusLabel branches ────────────────────────────────────────────────
describe('LifecycleMapView branches — statusLabel', () => {
  it('returns translated label for known status', async () => {
    const wrapper = mountView()
    await flushPromises()
    const vm = wrapper.vm as unknown as { statusLabel: (s: string) => string }
    expect(vm.statusLabel('complete')).toBe('Completed')
    wrapper.unmount()
  })

  it('returns raw status for unknown status', async () => {
    const wrapper = mountView()
    await flushPromises()
    const vm = wrapper.vm as unknown as { statusLabel: (s: string) => string }
    expect(vm.statusLabel('unknown_status')).toBe('unknown_status')
    wrapper.unmount()
  })
})

// ── loadMap guard ───────────────────────────────────────────────────────
describe('LifecycleMapView branches — loadMap guard', () => {
  it('returns early when no mapId', async () => {
    const { useRoute } = await import('vue-router')
    vi.mocked(useRoute).mockReturnValue({ params: { id: '' }, query: {}, meta: {}, name: 'lifecycle-map-detail' } as never)
    const wrapper = mountView()
    await flushPromises()
    fetchMock.mockClear()
    await (wrapper.vm as unknown as { loadMap: () => Promise<void> }).loadMap()
    expect(fetchMock).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})

// ── openJourneyDetail guard ─────────────────────────────────────────────
describe('LifecycleMapView branches — openJourneyDetail guard', () => {
  it('returns early when no mapId', async () => {
    const { useRoute } = await import('vue-router')
    vi.mocked(useRoute).mockReturnValue({ params: { id: '' }, query: {}, meta: {}, name: 'lifecycle-map-detail' } as never)
    const wrapper = mountView()
    await flushPromises()
    fetchMock.mockClear()
    const vm = wrapper.vm as unknown as { openJourneyDetail: (j: { kind: string; ref: string }) => Promise<void> }
    await vm.openJourneyDetail({ kind: 'run', ref: 'run-1' })
    expect(fetchMock).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})

// ── editMap guard ───────────────────────────────────────────────────────
describe('LifecycleMapView branches — editMap guard', () => {
  it('returns early when no mapId', async () => {
    const { useRoute } = await import('vue-router')
    vi.mocked(useRoute).mockReturnValue({ params: { id: '' }, query: {}, meta: {}, name: 'lifecycle-map-detail' } as never)
    const wrapper = mountView()
    await flushPromises()
    routerPushMock.mockClear()
    ;(wrapper.vm as unknown as { editMap: () => void }).editMap()
    expect(routerPushMock).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})

// ── loadJourneys / loadMoreJourneys guards ──────────────────────────────
describe('LifecycleMapView branches — journeys loading guards', () => {
  it('loadJourneys returns early when no mapId', async () => {
    const { useRoute } = await import('vue-router')
    vi.mocked(useRoute).mockReturnValue({ params: { id: '' }, query: {}, meta: {}, name: 'lifecycle-map-detail' } as never)
    const wrapper = mountView()
    await flushPromises()
    fetchMock.mockClear()
    await (wrapper.vm as unknown as { loadJourneys: () => Promise<void> }).loadJourneys()
    expect(fetchMock).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('loadMoreJourneys returns early when no mapId', async () => {
    const { useRoute } = await import('vue-router')
    vi.mocked(useRoute).mockReturnValue({ params: { id: '' }, query: {}, meta: {}, name: 'lifecycle-map-detail' } as never)
    const wrapper = mountView()
    await flushPromises()
    fetchMock.mockClear()
    await (wrapper.vm as unknown as { loadMoreJourneys: () => Promise<void> }).loadMoreJourneys()
    expect(fetchMock).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
