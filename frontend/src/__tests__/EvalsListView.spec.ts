import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import { setActivePinia, createPinia } from 'pinia'

// api.GET is the single call seam: every test drives the view by deciding what
// each path returns, exactly as the real openapi-fetch client does.
const { apiGet } = vi.hoisted(() => ({ apiGet: vi.fn() }))

vi.mock('../lib/api/client', () => ({
  api: { GET: apiGet },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

vi.mock('../lib/api/schema', () => ({}))

const routerMocks = vi.hoisted(() => ({
  push: vi.fn().mockResolvedValue(undefined),
  replace: vi.fn().mockResolvedValue(undefined),
}))

vi.mock('vue-router', () => ({
  useRoute: vi.fn(() => ({
    path: '/evals',
    fullPath: '/evals',
    params: {},
    query: {},
    hash: '',
    matched: [],
    name: 'evals-list',
    redirectedFrom: undefined,
    meta: {},
  })),
  useRouter: vi.fn(() => ({
    push: routerMocks.push,
    replace: routerMocks.replace,
    resolve: vi.fn(),
    go: vi.fn(),
    back: vi.fn(),
    forward: vi.fn(),
    beforeEach: vi.fn(),
    afterEach: vi.fn(),
    onError: vi.fn(),
    currentRoute: { value: {} },
    getRoutes: vi.fn(() => []),
    addRoute: vi.fn(),
    removeRoute: vi.fn(),
    hasRoute: vi.fn(() => false),
    isReady: vi.fn().mockResolvedValue(undefined),
    install: vi.fn(),
  })),
  createRouter: vi.fn(),
  createWebHistory: vi.fn(() => ({})),
}))

import EvalsListView from '../views/EvalsListView.vue'
import FilterBar from '../components/shared/FilterBar.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { DataTable } from '../components/ui/data-table'

const PIPELINES = [
  { id: 'p1', name: 'Ingest Pipeline' },
  { id: 'p2', name: 'Judge Pipeline' },
]

function evalItem(over: Record<string, unknown> = {}) {
  return {
    id: 'eval-1',
    pipeline_id: 'p1',
    node_id: null,
    name: 'Accuracy check',
    eval_type: 'regex',
    config_json: {},
    pass_threshold: 0.9,
    version: 3,
    created_by: 'user-1',
    judge: null,
    ...over,
  }
}

function mockApi(options: {
  evals?: { items: unknown[]; total: number }
  evalsError?: unknown
  pipelines?: unknown[]
} = {}) {
  apiGet.mockImplementation(async (path: string) => {
    if (path === '/api/v1/pipelines') {
      return { data: { items: options.pipelines ?? PIPELINES }, error: undefined }
    }
    if (path === '/api/v1/evals') {
      if (options.evalsError !== undefined) {
        return { data: undefined, error: options.evalsError }
      }
      return { data: options.evals ?? { items: [], total: 0 }, error: undefined }
    }
    return { data: { items: [], total: 0 }, error: undefined }
  })
}

function mountView() {
  return mount(EvalsListView, {
    global: {
      stubs: {
        FeatureGate: { template: '<div><slot /></div>' },
      },
    },
  })
}

async function flush() {
  await flushPromises()
  await nextTick()
}

/** The query object of the most recent GET /api/v1/evals call. */
function lastEvalsQuery(): Record<string, unknown> {
  const calls = apiGet.mock.calls.filter(call => call[0] === '/api/v1/evals')
  return (calls.at(-1)?.[1] as { params?: { query?: Record<string, unknown> } })?.params?.query ?? {}
}

function filterBar(wrapper: ReturnType<typeof mountView>) {
  return wrapper.findComponent(FilterBar)
}

describe('EvalsListView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    routerMocks.push.mockResolvedValue(undefined)
    routerMocks.replace.mockResolvedValue(undefined)
    mockApi()
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('renders every eval with its pipeline name joined from the pipeline list', async () => {
    mockApi({
      evals: {
        items: [
          evalItem(),
          evalItem({
            id: 'eval-2',
            pipeline_id: 'p2',
            name: 'Tone check',
            eval_type: 'model_judge',
            node_id: 'node-9',
            judge: { display: 'gpt-4o via OpenAI' },
          }),
        ],
        total: 2,
      },
    })
    const wrapper = mountView()
    await flush()

    const rows = wrapper.findAll('[data-testid="evals-list-row"]')
    expect(rows).toHaveLength(2)
    expect(rows[0].text()).toContain('Accuracy check')
    expect(rows[0].text()).toContain('Ingest Pipeline')
    expect(rows[0].text()).toContain('0.90')
    expect(rows[0].text()).toContain('3')
    expect(rows[1].text()).toContain('Judge Pipeline')
    expect(rows[1].text()).toContain('gpt-4o via OpenAI')
    wrapper.unmount()
  })

  it('requests the first page with no filters on mount', async () => {
    mountView()
    await flush()

    expect(lastEvalsQuery()).toEqual({ page: 1, page_size: 25 })
    // page_size 100, not the API default of 20: a truncated pipeline list would
    // silently hide pipelines 21+ from the filter and the editor deep link.
    expect(apiGet).toHaveBeenCalledWith('/api/v1/pipelines', { params: { query: { page_size: 100 } } })
  })

  it('sends the pipeline filter and returns to page one', async () => {
    mockApi({ evals: { items: [evalItem()], total: 80 } })
    const wrapper = mountView()
    await flush()
    // Move off page 1 so the filter's page reset is observable.
    await wrapper.find('[data-testid="evals-list-next-page"]').trigger('click')
    await flush()
    expect(lastEvalsQuery()).toEqual({ page: 2, page_size: 25 })

    filterBar(wrapper).vm.$emit('update:filter', 'pipeline_id', 'p2')
    await flush()

    expect(lastEvalsQuery()).toEqual({ page: 1, page_size: 25, pipeline_id: 'p2' })
    wrapper.unmount()
  })

  it('sends the eval-type filter', async () => {
    const wrapper = mountView()
    await flush()

    filterBar(wrapper).vm.$emit('update:filter', 'eval_type', 'model_judge')
    await flush()

    expect(lastEvalsQuery()).toEqual({ page: 1, page_size: 25, eval_type: 'model_judge' })
    wrapper.unmount()
  })

  it('explains the concept and offers a way in when there are no evals at all', async () => {
    const wrapper = mountView()
    await flush()

    expect(wrapper.find('[data-testid="evals-list-empty"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('No evals yet')
    expect(wrapper.text()).toContain('Evals score pipeline output')
    expect(wrapper.find('[data-testid="evals-list-empty-cta"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="evals-list-filtered-empty"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('offers a filter reset when filters match nothing, and resets through it', async () => {
    const wrapper = mountView()
    await flush()
    filterBar(wrapper).vm.$emit('update:filter', 'pipeline_id', 'p2')
    await flush()

    expect(wrapper.find('[data-testid="evals-list-filtered-empty"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('No evals match these filters')

    await wrapper.find('[data-testid="evals-list-reset"]').trigger('click')
    await flush()

    expect(wrapper.find('[data-testid="evals-list-empty"]').exists()).toBe(true)
    expect(lastEvalsQuery()).toEqual({ page: 1, page_size: 25 })
    wrapper.unmount()
  })

  it('paginates with previous/next and reports how many evals are on screen', async () => {
    mockApi({ evals: { items: [evalItem()], total: 80 } })
    const wrapper = mountView()
    await flush()

    expect(wrapper.find('[data-testid="evals-list-count"]').text()).toContain('Showing 1 of 80 evals')
    expect(wrapper.find('[data-testid="evals-list-page-indicator"]').text()).toContain('Page 1')
    expect(wrapper.find('[data-testid="evals-list-previous-page"]').attributes('disabled')).toBeDefined()

    await wrapper.find('[data-testid="evals-list-next-page"]').trigger('click')
    await flush()
    expect(lastEvalsQuery().page).toBe(2)
    expect(wrapper.find('[data-testid="evals-list-page-indicator"]').text()).toContain('Page 2')

    await wrapper.find('[data-testid="evals-list-previous-page"]').trigger('click')
    await flush()
    expect(lastEvalsQuery().page).toBe(1)
    wrapper.unmount()
  })

  it('disables next on the last page', async () => {
    mockApi({ evals: { items: [evalItem()], total: 25 } })
    const wrapper = mountView()
    await flush()

    expect(wrapper.find('[data-testid="evals-list-next-page"]').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it('opens the editor on the clicked eval, not just its pipeline', async () => {
    mockApi({ evals: { items: [evalItem({ id: 'eval-7', pipeline_id: 'p2' })], total: 1 } })
    const wrapper = mountView()
    await flush()

    await wrapper.find('[data-testid="evals-list-row"]').trigger('click')

    expect(routerMocks.push).toHaveBeenCalledWith({
      path: '/evals/editor',
      query: { pipeline_id: 'p2', eval_id: 'eval-7' },
    })
    wrapper.unmount()
  })

  it('labels its filters as whole sets so the reset option reads correctly', async () => {
    const wrapper = mountView()
    await flush()

    const filters = filterBar(wrapper).props('filters') as Array<{ key: string; label: string }>
    expect(filters.find(f => f.key === 'pipeline_id')?.label).toBe('All pipelines')
    expect(filters.find(f => f.key === 'eval_type')?.label).toBe('All types')
    wrapper.unmount()
  })

  it('announces the pending load as a busy status region', async () => {
    let release: (() => void) | undefined
    apiGet.mockImplementation(async (path: string) => {
      if (path === '/api/v1/pipelines') return { data: { items: PIPELINES }, error: undefined }
      if (path === '/api/v1/evals') {
        await new Promise<void>((resolve) => { release = resolve })
        return { data: { items: [], total: 0 }, error: undefined }
      }
      return { data: { items: [], total: 0 }, error: undefined }
    })
    const wrapper = mountView()
    await nextTick()

    const region = wrapper.find('[data-testid="evals-list-loading"]')
    expect(region.exists()).toBe(true)
    expect(region.attributes('role')).toBe('status')
    expect(region.attributes('aria-busy')).toBe('true')
    expect(region.attributes('aria-live')).toBe('polite')
    expect(region.text()).toContain('Loading evals')

    release?.()
    await flush()
    expect(wrapper.find('[data-testid="evals-list-loading"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('opens the editor from both New eval calls', async () => {
    const wrapper = mountView()
    await flush()

    await wrapper.find('[data-testid="evals-list-new"]').trigger('click')
    await wrapper.find('[data-testid="evals-list-empty-cta"]').trigger('click')

    expect(routerMocks.push).toHaveBeenNthCalledWith(1, { path: '/evals/editor' })
    expect(routerMocks.push).toHaveBeenNthCalledWith(2, { path: '/evals/editor' })
    wrapper.unmount()
  })

  it('surfaces a failed list request with a retry, then recovers', async () => {
    mockApi({ evalsError: { detail: 'boom' } })
    const wrapper = mountView()
    await flush()

    expect(wrapper.findComponent(ErrorAlert).exists()).toBe(true)
    expect(wrapper.findComponent(DataTable).exists()).toBe(false)

    mockApi({ evals: { items: [evalItem()], total: 1 } })
    await wrapper.find('[data-testid="error-alert-retry"]').trigger('click')
    await flush()

    expect(wrapper.findComponent(ErrorAlert).exists()).toBe(false)
    expect(wrapper.findAll('[data-testid="evals-list-row"]')).toHaveLength(1)
    wrapper.unmount()
  })

  it('keeps the list usable when the pipeline join fails (short ids, logged)', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    apiGet.mockImplementation(async (path: string) => {
      if (path === '/api/v1/pipelines') throw new Error('pipelines unavailable')
      return { data: { items: [evalItem()], total: 1 }, error: undefined }
    })
    const wrapper = mountView()
    await flush()

    expect(wrapper.find('[data-testid="evals-list-row"]').text()).toContain('p1')
    expect(warn).toHaveBeenCalled()
    wrapper.unmount()
    warn.mockRestore()
  })
})
