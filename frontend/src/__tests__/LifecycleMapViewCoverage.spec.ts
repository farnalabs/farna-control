/**
 * Template/handler coverage for LifecycleMapView.vue (FAR-835).
 *
 * Covers the rendered surface (description, owner, counts, version badge,
 * loading state) and the DOM-driven handlers (stage clicks, export, delete
 * confirmation guard, import dialog, mount guard).
 *
 * Setup comes from ./helpers/lifecycleMapViewHarness (shared with
 * LifecycleMapView-branches.spec.ts, which owns the script-logic branches and
 * all early-return guards — the cases that used to be duplicated between the
 * two files now live there once).
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
import {
  lifecycleMapMountOptions,
  mapDetail,
  okJson,
  seedPlan,
} from './helpers/lifecycleMapViewHarness'

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(async () => {
  setActivePinia(createPinia())
  const { useRoute } = await import('vue-router')
  // Reset the route on every test: the guard tests install a no-`id` route and
  // `clearAllMocks()` does not undo a `mockReturnValue`.
  vi.mocked(useRoute).mockReturnValue(defaultRoute() as never)
  routerPushMock.mockClear()
  seedPlan()
  fetchMock = vi.fn((url: string) => {
    if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
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
  // Clean teleported dialog content from document.body
  document.body.innerHTML = ''
})

function mountView() {
  return mount(LifecycleMapView, lifecycleMapMountOptions(routerPushMock))
}

// ── Template content: description, owner, counts, version badge ──────────
describe('LifecycleMapView template content', () => {
  it('renders description when present', async () => {
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('A test lifecycle map')
  })
  it('renders owner when present', async () => {
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('alice')
  })
  it('shows correct stage counts', async () => {
    const wrapper = mountView()
    await flushPromises()
    const text = wrapper.text()
    expect(text).toContain('3 stages')
    expect(text).toContain('1 graduated')
    expect(text).toContain('1 manual')
  })
  it('renders version badge', async () => {
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('v2')
  })
  it('renders version select with multiple versions', async () => {
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.find('[data-testid="lifecycle-map-version-select"]').exists()).toBe(true)
  })
  it('hides version select with single version', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      return Promise.resolve(okJson({
        ...mapDetail, versions: [{ id: 'v1', version: 1, created_at: '2026-01-01T00:00:00Z', created_by: null }],
        current_version: 1,
      }))
    })
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.find('[data-testid="lifecycle-map-version-select"]').exists()).toBe(false)
  })
})

// ── Loading state ────────────────────────────────────────────────────────
describe('LifecycleMapView loading state', () => {
  it('shows spinner while loading', async () => {
    let resolveDetail!: (v: Response) => void
    fetchMock.mockImplementation((url: string) => {
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      return new Promise((resolve) => { resolveDetail = resolve })
    })
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.find('.animate-spin').exists()).toBe(true)
    resolveDetail(okJson(mapDetail))
    await flushPromises()
  })
})

// ── handleModuloStageClick ───────────────────────────────────────────────
describe('LifecycleMapView handleModuloStageClick', () => {
  it('navigates to pipeline when stage has pipeline_id', async () => {
    const wrapper = mountView()
    await flushPromises()
    const vm = wrapper.vm as unknown as { handleModuloStageClick: (s: { pipeline_id?: string | null }) => void }
    vm.handleModuloStageClick({ pipeline_id: 'pipe-1' })
    expect(routerPushMock).toHaveBeenCalledWith({ name: 'pipeline-editor', params: { id: 'pipe-1' } })
  })
  it('does nothing when no pipeline_id', async () => {
    const wrapper = mountView()
    await flushPromises()
    routerPushMock.mockClear()
    const vm = wrapper.vm as unknown as { handleModuloStageClick: (s: { pipeline_id?: string | null }) => void }
    vm.handleModuloStageClick({ pipeline_id: null })
    expect(routerPushMock).not.toHaveBeenCalled()
  })
})

// ── handleExternalStageClick ─────────────────────────────────────────────
describe('LifecycleMapView handleExternalStageClick', () => {
  it('opens URL when stage has external_url', async () => {
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null)
    const wrapper = mountView()
    await flushPromises()
    const vm = wrapper.vm as unknown as { handleExternalStageClick: (s: { external_url?: string | null }) => void }
    vm.handleExternalStageClick({ external_url: 'https://example.com' })
    expect(openSpy).toHaveBeenCalledWith('https://example.com', '_blank', 'noopener,noreferrer')
    openSpy.mockRestore()
  })
  it('does nothing when no external_url', async () => {
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null)
    const wrapper = mountView()
    await flushPromises()
    const vm = wrapper.vm as unknown as { handleExternalStageClick: (s: { external_url?: string | null }) => void }
    vm.handleExternalStageClick({ external_url: null })
    expect(openSpy).not.toHaveBeenCalled()
    openSpy.mockRestore()
  })
})

// ── handleExport error paths ─────────────────────────────────────────────
describe('LifecycleMapView handleExport', () => {
  it('shows error on export failure', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url.includes('/export')) return Promise.resolve({ ok: false, status: 500, json: async () => ({ detail: 'boom' }) })
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      return Promise.resolve(okJson(mapDetail))
    })
    const wrapper = mountView()
    await flushPromises()
    await wrapper.find('[data-testid="lifecycle-map-export"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="lifecycle-map-export-error"]').text()).toContain('boom')
  })
  it('handles clipboard failure gracefully', async () => {
    const urlCreate = vi.fn(() => 'blob:fake-url')
    const urlRevoke = vi.fn()
    vi.stubGlobal('URL', { ...URL, createObjectURL: urlCreate, revokeObjectURL: urlRevoke })
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
    vi.stubGlobal('navigator', { clipboard: { writeText: vi.fn(async () => { throw new Error('blocked') }) } })
    const wrapper = mountView()
    await flushPromises()
    await wrapper.find('[data-testid="lifecycle-map-export"]').trigger('click')
    await flushPromises()
    expect(urlCreate).toHaveBeenCalled()
    expect(clickSpy).toHaveBeenCalled()
    expect(wrapper.find('[data-testid="lifecycle-map-export-error"]').exists()).toBe(false)
    clickSpy.mockRestore()
  })
})

// ── handleDeleteConfirm ──────────────────────────────────────────────────
describe('LifecycleMapView handleDeleteConfirm', () => {
  it('does nothing when no mapId', async () => {
    const { useRoute } = await import('vue-router')
    vi.mocked(useRoute).mockReturnValue({ params: { id: '' }, query: {}, meta: {}, name: 'lifecycle-map-detail' } as never)
    const wrapper = mountView()
    await flushPromises()
    routerPushMock.mockClear()
    const vm = wrapper.vm as unknown as { handleDeleteConfirm: () => Promise<void> }
    await vm.handleDeleteConfirm()
    expect(routerPushMock).not.toHaveBeenCalled()
  })
})

// ── openImportDialog ─────────────────────────────────────────────────────
describe('LifecycleMapView openImportDialog', () => {
  it('opens dialog with empty state', async () => {
    const wrapper = mountView()
    await flushPromises()
    await wrapper.find('[data-testid="lifecycle-map-import"]').trigger('click')
    await flushPromises()
    const payload = document.body.querySelector('[data-testid="lifecycle-map-import-payload"]') as HTMLTextAreaElement
    expect(payload).not.toBeNull()
    expect(payload.value).toBe('')
  })
})

// ── onMounted guard ──────────────────────────────────────────────────────
describe('LifecycleMapView onMounted guard', () => {
  it('skips fetch when route has no id', async () => {
    const { useRoute } = await import('vue-router')
    vi.mocked(useRoute).mockReturnValue({ params: { id: '' }, query: {}, meta: {}, name: 'lifecycle-map-detail' } as never)
    mountView()
    await flushPromises()
    expect(fetchMock.mock.calls.every((c: unknown[]) => !String(c[0]).includes('/lifecycle-maps/map-1'))).toBe(true)
  })
})
