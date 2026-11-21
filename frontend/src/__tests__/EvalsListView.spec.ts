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
  graphs?: Record<string, { nodes?: unknown[] } | 'error'>
} = {}) {
  apiGet.mockImplementation(async (path: string, request?: { params?: { path?: { pipeline_id?: string } } }) => {
    if (path === '/api/v1/pipelines') {
      return { data: { items: options.pipelines ?? PIPELINES }, error: undefined }
    }
    if (path === '/api/v1/evals') {
      if (options.evalsError !== undefined) {
        return { data: undefined, error: options.evalsError }
      }
      return { data: options.evals ?? { items: [], total: 0 }, error: undefined }
    }
    if (path === '/api/v1/pipelines/{pipeline_id}/graph') {
      const pipelineId = request?.params?.path?.pipeline_id ?? ''
      const graph = options.graphs?.[pipelineId]
      if (graph === 'error') {
        return { data: undefined, error: { detail: 'graph unavailable' } }
      }
      return { data: graph ?? { nodes: [] }, error: undefined }
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

/** The pipeline-graph reads the view issued for node labels. */
function graphCalls() {
  return apiGet.mock.calls.filter(call => call[0] === '/api/v1/pipelines/{pipeline_id}/graph')
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

  it('falls back for rows missing optional fields and for unknown eval types', async () => {
    mockApi({
      evals: {
        items: [
          // A null threshold and a type outside the known set.
          evalItem({ id: 'eval-null', pass_threshold: null, eval_type: 'custom_type' }),
          // An undefined threshold and a missing version.
          evalItem({ id: 'eval-undef', pass_threshold: undefined, version: undefined }),
        ],
        total: 2,
      },
    })
    const wrapper = mountView()
    await flush()

    const rows = wrapper.findAll('[data-testid="evals-list-row"]')
    expect(rows).toHaveLength(2)
    // Both null and undefined render the em-dash placeholder.
    expect(rows[0].text()).toContain('—')
    expect(rows[1].text()).toContain('—')
    // An unknown type shows its raw value instead of being dropped.
    expect(rows[0].text()).toContain('custom_type')
    // The missing version falls back to 1.
    expect(rows[1].text()).toContain('1')
    wrapper.unmount()
  })

  it('copes with list responses that omit items and total', async () => {
    apiGet.mockImplementation(async (path: string) => {
      if (path === '/api/v1/pipelines') return { data: {}, error: undefined }
      if (path === '/api/v1/evals') return { data: {}, error: undefined }
      return { data: {}, error: undefined }
    })
    const wrapper = mountView()
    await flush()

    expect(wrapper.find('[data-testid="evals-list-empty"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('guards pagination at the edges, ignores unknown filters, and opens a pipeline-only editor', async () => {
    const wrapper = mountView()
    await flush()
    const vm = wrapper.vm as unknown as {
      prevPage: () => void
      nextPage: () => void
      openEditor: (pipelineId?: string, evalId?: string) => void
    }
    const evalCalls = () => apiGet.mock.calls.filter(call => call[0] === '/api/v1/evals').length

    // Page one has no previous page: the guard returns before re-querying.
    const callsBefore = evalCalls()
    vm.prevPage()
    await flush()
    expect(evalCalls()).toBe(callsBefore)

    // A single (empty) page has no next page either.
    vm.nextPage()
    await flush()
    expect(evalCalls()).toBe(callsBefore)

    // An unknown filter key is ignored rather than treated as a filter change.
    filterBar(wrapper).vm.$emit('update:filter', 'search', 'anything')
    await flush()
    expect(lastEvalsQuery()).toEqual({ page: 1, page_size: 25 })

    // A pipeline without a selected eval opens the editor with the filter only.
    vm.openEditor('p2')
    expect(routerMocks.push).toHaveBeenCalledWith({ path: '/evals/editor', query: { pipeline_id: 'p2' } })
    wrapper.unmount()
  })

  it('fetches each distinct pipeline graph once and renders node labels with fallbacks', async () => {
    mockApi({
      evals: {
        items: [
          evalItem({ id: 'eval-1', pipeline_id: 'p1', node_id: 'node-9' }),
          evalItem({ id: 'eval-2', pipeline_id: 'p1', node_id: 'node-10' }),
          evalItem({ id: 'eval-3', pipeline_id: 'p2', node_id: 'node-7' }),
        ],
        total: 3,
      },
      graphs: {
        p1: {
          nodes: [
            { id: 'node-9', node_type: 'agent', label: 'Tone reviewer' },
            // No label on this node: the node_type is the fallback.
            { id: 'node-10', node_type: 'sandbox_agent' },
          ],
        },
        p2: { nodes: [{ id: 'node-7', node_type: 'manual', label: 'Approval gate' }] },
      },
    })
    const wrapper = mountView()
    await flush()

    // Two evals on p1 share ONE graph read; p2 gets its own — never per row.
    const calls = graphCalls()
    expect(calls).toHaveLength(2)
    const requested = calls
      .map(call => (call[1] as { params: { path: { pipeline_id: string } } }).params.path.pipeline_id)
      .sort()
    expect(requested).toEqual(['p1', 'p2'])

    const rows = wrapper.findAll('[data-testid="evals-list-row"]')
    expect(rows[0].text()).toContain('Tone reviewer')
    expect(rows[1].text()).toContain('sandbox_agent')
    expect(rows[2].text()).toContain('Approval gate')
    // A resolved label is not a short id, so it carries no title fallback.
    expect(rows[0].find('span[title]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('degrades to the short node id with a title when the graph read fails', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    mockApi({
      evals: { items: [evalItem({ id: 'eval-1', pipeline_id: 'p1', node_id: 'node-9' })], total: 1 },
      graphs: { p1: 'error' },
    })
    const wrapper = mountView()
    await flush()

    // The list itself is unaffected: the row renders on its short id.
    expect(wrapper.findComponent(ErrorAlert).exists()).toBe(false)
    const row = wrapper.find('[data-testid="evals-list-row"]')
    expect(row.text()).toContain('#node-9')
    expect(row.find('span[title]').attributes('title')).toBe('node-9')
    expect(warn).toHaveBeenCalled()
    wrapper.unmount()
    warn.mockRestore()
  })

  it('leaves the all-outputs row unchanged and reads no graph for it', async () => {
    mockApi({
      evals: { items: [evalItem({ id: 'eval-1', pipeline_id: 'p1', node_id: null })], total: 1 },
    })
    const wrapper = mountView()
    await flush()

    expect(wrapper.find('[data-testid="evals-list-row"]').text()).toContain('All outputs')
    expect(graphCalls()).toHaveLength(0)
    wrapper.unmount()
  })
})
