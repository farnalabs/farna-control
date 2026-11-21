import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('vue-i18n', () => ({
  useI18n: () => ({ t: (key: string) => key }),
}))

const hoisted = vi.hoisted(() => ({
  route: {
    path: '/',
    fullPath: '/',
    params: {} as Record<string, string>,
    query: {} as Record<string, string>,
    hash: '',
    matched: [] as unknown[],
    name: null,
    redirectedFrom: undefined,
  },
}))

vi.mock('vue-router', () => ({
  useRoute: () => hoisted.route,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  createRouter: vi.fn(() => ({ push: vi.fn() })),
  createWebHistory: vi.fn(() => ({})),
  createMemoryHistory: vi.fn(() => ({})),
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups') return Promise.resolve({ data: [], error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    }),
    POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import { api } from '../lib/api/client'
import VariantCompareView from '../views/VariantCompareView.vue'
import VariantGroupBuilder from '../components/variants/VariantGroupBuilder.vue'
import PageTabs from '../components/PageTabs.vue'
import FilterBar from '../components/shared/FilterBar.vue'

const groupWithVariants = {
  id: 'g1',
  name: 'Group 1',
  pipeline_id: 'p1',
  variants: [
    { name: 'var-a', weight: 0.5 },
    { name: 'var-b', weight: 0.5 },
  ],
  run_count: 0,
  selection_strategy: 'weighted',
}

describe('VariantCompareView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    window.history.replaceState(null, '')
    hoisted.route.params = {}
    hoisted.route.query = {}
  })

  it('renders without crashing', async () => {
    const wrapper = mount(VariantCompareView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await nextTick()
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.find('[role="tablist"]').exists()).toBe(false)
  })

  it('does not render the redundant page-level tab strip (FAR-1236)', async () => {
    const wrapper = mount(VariantCompareView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="variant-compare-filters"]').exists()).toBe(true)
    })
    expect(wrapper.findComponent(PageTabs).exists()).toBe(false)
  })

  it('renders the pure agent return in the diff viewers with no telemetry keys', async () => {
    vi.useFakeTimers()

    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups') {
        return Promise.resolve({ data: [groupWithVariants], error: undefined })
      }
      if (url === '/api/v1/variant-groups/{group_id}') {
        return Promise.resolve({ data: groupWithVariants, error: undefined })
      }
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({
          data: { run_id: 'r1', status: 'complete', total_cost_usd: 0, token_consumption: null },
          error: undefined,
        })
      }
      if (url === '/api/v1/runs/{run_id}/io') {
        return Promise.resolve({
          data: { outputs_json: { 'node-a': { summary: 'x', result: 'ok' } } },
          error: undefined,
        })
      }
      if (url === '/api/v1/runs/{run_id}/evals') {
        return Promise.resolve({ data: { items: [] }, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    let runCalls = 0
    vi.mocked(api.POST as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups/{group_id}/run') {
        runCalls += 1
        const variantName = runCalls === 1 ? 'var-a' : 'var-b'
        return Promise.resolve({ data: { run_id: `r${runCalls}`, variant_name: variantName }, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const wrapper = mount(VariantCompareView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await nextTick()
    await nextTick()
    await vi.advanceTimersByTimeAsync(0)

    const runBtn = wrapper.find('[data-testid="variant-compare-run-variants"]')
    expect(runBtn.exists()).toBe(true)

    await runBtn.trigger('click')
    await vi.advanceTimersByTimeAsync(2000)
    await nextTick()

    await runBtn.trigger('click')
    await vi.advanceTimersByTimeAsync(2000)
    await nextTick()
    await nextTick()

    const viewers = wrapper.findAll('[data-testid="json-viewer"]')
    expect(viewers.length).toBeGreaterThanOrEqual(2)
    const text = viewers.map(v => v.text()).join(' ')
    expect(text).toContain('summary')
    expect(text).toContain('x')
    expect(text).toContain('result')
    expect(text).toContain('ok')
    expect(text).not.toContain('agent_stdout')
    expect(text).not.toContain('agent_stderr')
    expect(text).not.toContain('exit_code')
    expect(text).not.toContain('stall_reason')
    vi.useRealTimers()
  })

  it('renders per-node pass/fail/partial eval badges and scores side by side', async () => {
    vi.useFakeTimers()

    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups') {
        return Promise.resolve({ data: [groupWithVariants], error: undefined })
      }
      if (url === '/api/v1/variant-groups/{group_id}') {
        return Promise.resolve({ data: groupWithVariants, error: undefined })
      }
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({
          data: { run_id: 'r1', status: 'complete', total_cost_usd: 0, token_consumption: null },
          error: undefined,
        })
      }
      if (url === '/api/v1/runs/{run_id}/io') {
        return Promise.resolve({
          data: {
            outputs_json: {
              'node-a': { ok: true },
              'node-b': { ok: false },
              'node-c': { ok: 'mixed' },
            },
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/runs/{run_id}/evals') {
        return Promise.resolve({
          data: {
            items: [
              { eval_id: 'e1', node_id: 'node-a', passed: true, score: 0.95 },
              { eval_id: 'e2', node_id: 'node-b', passed: false, score: 0.3 },
              { eval_id: 'e3', node_id: 'node-c', passed: true, score: 0.8 },
              { eval_id: 'e4', node_id: 'node-c', passed: false, score: 0.2 },
            ],
          },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    vi.mocked(api.POST as unknown as (url: string) => Promise<unknown>).mockResolvedValue({
      data: { run_id: 'r1', variant_name: 'var-a' },
      error: undefined,
    })

    const wrapper = mount(VariantCompareView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await nextTick()
    await nextTick()
    await vi.advanceTimersByTimeAsync(0)

    const runBtn = wrapper.find('[data-testid="variant-compare-run-variants"]')
    await runBtn.trigger('click')
    await vi.advanceTimersByTimeAsync(2000)
    await nextTick()

    const text = wrapper.text()
    expect(text).toContain('views.variantCompare.statusPass')
    expect(text).toContain('views.variantCompare.statusFail')
    expect(text).toContain('views.variantCompare.statusPartial')
    expect(text).toContain('0.95')
    expect(text).toContain('0.30')
    expect(text).toContain('0.80')
    expect(text).toContain('0.20')
    vi.useRealTimers()
  })

  it('shows per-variant cost and token totals in the summary footer', async () => {
    vi.useFakeTimers()

    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups') {
        return Promise.resolve({ data: [groupWithVariants], error: undefined })
      }
      if (url === '/api/v1/variant-groups/{group_id}') {
        return Promise.resolve({ data: groupWithVariants, error: undefined })
      }
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({
          data: {
            run_id: 'r1',
            status: 'complete',
            total_cost_usd: 1.23,
            token_consumption: { total_tokens: 4500 },
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/runs/{run_id}/io') {
        return Promise.resolve({ data: { outputs_json: { 'node-a': { summary: 'x' } } }, error: undefined })
      }
      if (url === '/api/v1/runs/{run_id}/evals') {
        return Promise.resolve({
          data: { items: [{ eval_id: 'e1', node_id: 'node-a', passed: true, score: 0.99 }] },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    vi.mocked(api.POST as unknown as (url: string) => Promise<unknown>).mockResolvedValue({
      data: { run_id: 'r1', variant_name: 'var-a' },
      error: undefined,
    })

    const tWithParams = (key: string, params?: Record<string, unknown>) =>
      params && Object.keys(params).length > 0 ? `${key}:${JSON.stringify(params)}` : key

    const wrapper = mount(VariantCompareView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: tWithParams },
      },
    })
    await nextTick()
    await nextTick()
    await vi.advanceTimersByTimeAsync(0)

    const runBtn = wrapper.find('[data-testid="variant-compare-run-variants"]')
    await runBtn.trigger('click')
    await vi.advanceTimersByTimeAsync(2000)
    await nextTick()

    const text = wrapper.text()
    // interpolated $t output carries the token total; pass rate renders raw as a percentage
    expect(text).toContain('"count":"4,500"')
    expect(text).toContain('100%')
    expect(text).toContain('"cost":"$1.230000"')
    // var-b has no run data — its pass-rate cell shows an em dash
    expect(text).toContain('—')
    vi.useRealTimers()
  })

  it('seeds fired batch runs passed via navigation state and polls them to completion', async () => {
    vi.useFakeTimers()

    window.history.replaceState(
      {
        firedRuns: [
          { run_id: 'fr1', variant_name: 'var-a' },
          { run_id: 'fr2', variant_name: 'var-b' },
        ],
      },
      '',
    )
    hoisted.route.params.batchId = 'g1'

    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups') return Promise.resolve({ data: [groupWithVariants], error: undefined })
      if (url === '/api/v1/variant-groups/{group_id}') return Promise.resolve({ data: groupWithVariants, error: undefined })
      if (url.startsWith('/api/v1/runs/') && url.endsWith('/io')) {
        return Promise.resolve({ data: { outputs_json: { 'node-a': { summary: 'fired' } } }, error: undefined })
      }
      if (url.startsWith('/api/v1/runs/') && url.endsWith('/evals')) {
        return Promise.resolve({
          data: { items: [{ eval_id: 'e1', node_id: 'node-a', passed: true, score: 0.9 }] },
          error: undefined,
        })
      }
      if (url.startsWith('/api/v1/runs/')) {
        return Promise.resolve({
          data: { run_id: 'fr1', status: 'complete', total_cost_usd: 0, token_consumption: null },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const wrapper = mount(VariantCompareView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await nextTick()
    await nextTick()
    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(2000)
    await nextTick()
    await nextTick()

    const vm = wrapper.vm as unknown as { runEntries: Map<string, unknown> }
    expect(vm.runEntries.size).toBe(2)
    const text = wrapper.text()
    expect(text).toContain('var-a')
    expect(text).toContain('var-b')
    expect(text).toContain('fired')
    vi.useRealTimers()
  })

  it('preselects the group named by the :batchId route param on a deep link', async () => {
    // groups are ordered newest-first; the requested batch is the OLDER one (not first)
    const newGroup = { ...groupWithVariants, id: 'g-new', name: 'Group New' }
    const olderGroup = { ...groupWithVariants, id: 'g-older', name: 'Group Older' }
    hoisted.route.params.batchId = 'g-older'

    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups') {
        return Promise.resolve({ data: [newGroup, olderGroup], error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    try {
      const wrapper = mount(VariantCompareView, {
        global: {
          stubs: { FeatureGate: { template: '<div><slot /></div>' } },
          mocks: { $t: (key: string) => key },
        },
      })
      await nextTick()
      await nextTick()
      await new Promise(r => setTimeout(r, 0))
      await nextTick()
      const vm = wrapper.vm as unknown as { selectedGroupId: string | null }
      expect(vm.selectedGroupId).toBe('g-older')
    } finally {
      vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation(
        (url: string) =>
          Promise.resolve(
            url === '/api/v1/variant-groups'
              ? { data: [], error: undefined }
              : { data: null, error: undefined },
          ),
      )
    }
  })

  it('preselects the pipeline group carried by the pipeline_id query param on a deep link', async () => {
    // PipelineListView's "Run as variant" sends ?pipeline_id=... — the view must
    // consume it so the deep-linked pipeline selection is not silently dropped.
    const otherPipeline = { ...groupWithVariants, id: 'g-other', name: 'Other', pipeline_id: 'p-other' }
    const wantedPipeline = { ...groupWithVariants, id: 'g-wanted', name: 'Wanted', pipeline_id: 'p-wanted' }
    hoisted.route.params.batchId = ''
    hoisted.route.query.pipeline_id = 'p-wanted'

    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups') {
        return Promise.resolve({ data: [otherPipeline, wantedPipeline], error: undefined })
      }
      if (url === '/api/v1/variant-groups/{group_id}') {
        return Promise.resolve({ data: wantedPipeline, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    try {
      const wrapper = mount(VariantCompareView, {
        global: {
          stubs: { FeatureGate: { template: '<div><slot /></div>' } },
          mocks: { $t: (key: string) => key },
        },
      })
      await nextTick()
      await nextTick()
      await new Promise(r => setTimeout(r, 0))
      await nextTick()
      const vm = wrapper.vm as unknown as { selectedGroupId: string | null }
      expect(vm.selectedGroupId).toBe('g-wanted')
    } finally {
      hoisted.route.params.batchId = ''
      hoisted.route.query.pipeline_id = ''
    }
  })

  it('opens the variant-group builder for the pipeline_id deep-link', async () => {
    hoisted.route.query = { pipeline_id: 'p-deep' }

    const wrapper = mount(VariantCompareView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await nextTick()
    await nextTick()
    await new Promise(r => setTimeout(r, 0))
    await nextTick()

    const builder = wrapper.findComponent(VariantGroupBuilder)
    expect(wrapper.find('[data-testid="variant-group-builder"]').exists()).toBe(true)
    expect(builder.props('initialPipelineId')).toBe('p-deep')
  })

  it('opens the variant-group builder from the New Comparison button', async () => {
    const wrapper = mount(VariantCompareView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await nextTick()
    await nextTick()
    await new Promise(r => setTimeout(r, 0))
    await nextTick()

    expect(wrapper.find('[data-testid="variant-group-builder"]').exists()).toBe(false)
    await wrapper.find('[data-testid="variant-compare-new-group"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="variant-group-builder"]').exists()).toBe(true)
  })

  it('offers Retry on a failed variant-groups load and re-fetches on click (FAR-1655)', async () => {
    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups') {
        return Promise.reject(new Error('groups backend down'))
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const wrapper = mount(VariantCompareView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('groups backend down')
    })

    const retry = wrapper.find('[data-testid="error-alert-retry"]')
    expect(retry.exists()).toBe(true)

    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups') {
        return Promise.resolve({ data: [groupWithVariants], error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    await retry.trigger('click')

    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="error-alert-retry"]').exists()).toBe(false)
    })
    expect(wrapper.find('[data-testid="variant-compare-groups-table"]').exists()).toBe(true)
  })

  it('renders the selected-group summary with the run-count and strategy fallbacks', async () => {
    // A selected group whose run_count and selection_strategy are absent exercises
    // BOTH defensive fallbacks in the summary span: `run_count ?? 0` and
    // `selection_strategy || '—'` (the changed-lines coverage gate requires the
    // nullish/empty arms, not just the populated ones).
    const sparseGroup = {
      ...groupWithVariants,
      id: 'g-sparse',
      name: 'Sparse Group',
      run_count: null,
      selection_strategy: null,
    }

    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups') {
        return Promise.resolve({ data: [sparseGroup], error: undefined })
      }
      if (url === '/api/v1/variant-groups/{group_id}') {
        return Promise.resolve({ data: sparseGroup, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const tWithParams = (key: string, params?: Record<string, unknown>) =>
      params && Object.keys(params).length > 0 ? `${key}:${JSON.stringify(params)}` : key

    const wrapper = mount(VariantCompareView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: tWithParams },
      },
    })

    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('views.variantCompare.runs')
    })

    const text = wrapper.text()
    // run_count null falls back to 0 (the `??` arm)
    expect(text).toContain('"count":0')
    // selection_strategy null falls back to an em dash (the `||` arm)
    expect(text).toContain('—')
  })

  it('skips a poll with no data and reports a failed terminal status', async () => {
    vi.useFakeTimers()
    let runCalls = 0

    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') {
        runCalls += 1
        if (runCalls === 1) return Promise.resolve({ data: null, error: undefined })
        return Promise.resolve({
          data: { run_id: 'r1', status: 'failed', total_cost_usd: null, token_consumption: null },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const wrapper = mount(VariantCompareView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await nextTick()

    const vm = wrapper.vm as any
    vm.runEntries.set('var-a', {
      runId: 'r1',
      variantName: 'var-a',
      runStatus: 'pending',
      totalCostUsd: null,
      tokenConsumption: null,
      nodeOutputs: null,
      evalResults: [],
    })
    vm.runningVariants.add('var-a')

    const poll = vm.pollRunStatus('r1', 'var-a')
    await vi.advanceTimersByTimeAsync(2000)
    await vi.advanceTimersByTimeAsync(2000)
    await poll

    expect(runCalls).toBe(2)
    expect(vm.error).toContain('views.variantCompare.runFailedStatus')
    expect(vm.runningVariants.has('var-a')).toBe(false)
    vi.useRealTimers()
  })

  it('does not create an entry when polling a run with no local entry', async () => {
    vi.useFakeTimers()

    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') {
        return Promise.resolve({
          data: { run_id: 'r1', status: 'complete', total_cost_usd: 0, token_consumption: null },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const wrapper = mount(VariantCompareView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await nextTick()

    const vm = wrapper.vm as any
    expect(vm.runEntries.has('ghost')).toBe(false)

    const poll = vm.pollRunStatus('r1', 'ghost')
    await vi.advanceTimersByTimeAsync(2000)
    await poll

    expect(vm.runEntries.has('ghost')).toBe(false)
    expect(vm.runningVariants.has('ghost')).toBe(false)
    vi.useRealTimers()
  })

  it('keeps polling while the run is not terminal', async () => {
    vi.useFakeTimers()
    let runCalls = 0

    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/runs/{run_id}') {
        runCalls += 1
        if (runCalls === 1) {
          return Promise.resolve({
            data: { run_id: 'r1', status: 'running', total_cost_usd: null, token_consumption: null },
            error: undefined,
          })
        }
        return Promise.resolve({
          data: { run_id: 'r1', status: 'complete', total_cost_usd: 0, token_consumption: null },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const wrapper = mount(VariantCompareView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await nextTick()

    const vm = wrapper.vm as any
    vm.runEntries.set('var-a', {
      runId: 'r1',
      variantName: 'var-a',
      runStatus: 'pending',
      totalCostUsd: null,
      tokenConsumption: null,
      nodeOutputs: null,
      evalResults: [],
    })

    const poll = vm.pollRunStatus('r1', 'var-a')
    await vi.advanceTimersByTimeAsync(2000)
    await vi.advanceTimersByTimeAsync(2000)
    await poll

    expect(runCalls).toBe(2)
    expect(vm.runEntries.get('var-a').runStatus).toBe('complete')
    vi.useRealTimers()
  })

  // FAR-2598: the comparison-groups table replaces the unlabelled dropdown.
  const twoGroups = [
    {
      ...groupWithVariants,
      id: 'g1',
      name: 'Group 1',
      pipeline_id: 'p1',
      run_count: 4,
      variants: [
        { name: 'var-a', weight: 0.5, eval_definition_ids: ['e1', 'e2'] },
        { name: 'var-b', weight: 0.5, eval_definition_ids: ['e2', 'e3'] },
      ],
      degraded_evals: true,
    },
    {
      ...groupWithVariants,
      id: 'g2',
      name: 'Group 2',
      pipeline_id: 'p2',
      run_count: 0,
      variants: [
        { name: 'var-a', weight: 1, eval_definition_ids: ['e1'] },
      ],
      degraded_evals: false,
    },
  ]

  function mockGroupsAndPipelines() {
    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups') {
        return Promise.resolve({ data: twoGroups, error: undefined })
      }
      if (url === '/api/v1/pipelines') {
        return Promise.resolve({
          data: {
            items: [
              { id: 'p1', name: 'Alpha pipeline' },
              { id: 'p2', name: 'Beta pipeline' },
            ],
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/variant-groups/{group_id}') {
        return Promise.resolve({ data: twoGroups[0], error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
  }

  it('renders each comparison as a table row with its pipeline, counts and degraded badge', async () => {
    mockGroupsAndPipelines()

    const wrapper = mount(VariantCompareView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })

    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('Alpha pipeline')
    })

    const table = wrapper.find('[data-testid="variant-compare-groups-table"]')
    expect(table.exists()).toBe(true)
    const text = table.text()
    expect(text).toContain('Group 1')
    expect(text).toContain('Beta pipeline')
    expect(text).toContain('views.variantCompare.columnPipeline')
    expect(text).toContain('views.variantCompare.columnDeclaredEvals')
    expect(text).toContain('views.variantCompare.degradedBadge')

    const rows = (wrapper.vm as any).groupRows as Array<Record<string, unknown>>
    // Declared evals counts DISTINCT declared eval ids (e1, e2, e3). It is
    // explicitly not a score count: the tooltip says so on every cell.
    expect(rows[0].declared_evals_count).toBe(3)
    expect(wrapper.find('[title="views.variantCompare.declaredEvalsTooltip"]').exists()).toBe(true)
    expect(rows[0].variants_count).toBe(2)
    expect(rows[0].pipeline).toBe('Alpha pipeline')
    expect(rows[1].degraded).toBe(false)
  })

  it('filters the comparison table by pipeline and by name, keeping a valid selection', async () => {
    mockGroupsAndPipelines()

    const wrapper = mount(VariantCompareView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('Alpha pipeline')
    })

    const vm = wrapper.vm as any
    vm.filterPipelineId = 'p2'
    await nextTick()
    expect((vm.groupRows as Array<Record<string, unknown>>).map(row => row.name)).toEqual(['Group 2'])
    expect(vm.selectedGroupId).toBe('g2')

    vm.filterSearch = 'no such comparison'
    await nextTick()
    expect(vm.groupRows).toHaveLength(0)
    expect(wrapper.text()).toContain('views.variantCompare.noMatchingComparisons')
    // The detail panel must not keep rendering a comparison the table excluded.
    expect(vm.selectedGroup).toBeNull()
    expect(wrapper.text()).toContain('views.variantCompare.selectGroupFirst')
  })

  it('selects a comparison in place from its row action without navigating', async () => {
    mockGroupsAndPipelines()

    const wrapper = mount(VariantCompareView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="variant-compare-select-g2"]').exists()).toBe(true)
    })

    await wrapper.find('[data-testid="variant-compare-select-g2"]').trigger('click')
    await nextTick()

    const vm = wrapper.vm as any
    expect(vm.selectedGroupId).toBe('g2')
    // Still on the list page: selection is in place, not a batch-detail route.
    expect(hoisted.route.params).toEqual({})
    // Each row action names its own comparison for assistive tech.
    expect(wrapper.find('[data-testid="variant-compare-select-g2"]').attributes('aria-label'))
      .toBe('views.variantCompare.viewingComparison')
    expect(wrapper.find('[data-testid="variant-compare-select-g1"]').attributes('aria-label'))
      .toBe('views.variantCompare.viewComparison')
  })

  it('asks for the 100-row page ceiling and reports the loaded count truthfully', async () => {
    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups') {
        return Promise.resolve({ data: twoGroups, error: undefined })
      }
      if (url === '/api/v1/pipelines') {
        return Promise.resolve({ data: { items: [{ id: 'p1', name: 'Alpha pipeline' }] }, error: undefined })
      }
      if (url === '/api/v1/variant-groups/{group_id}') {
        return Promise.resolve({ data: twoGroups[0], error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const wrapper = mount(VariantCompareView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="variant-compare-groups-table"]').exists()).toBe(true)
    })

    // Both list reads ask for the endpoint's maximum page — the server default
    // of 20 silently truncated the comparison inventory.
    expect(api.GET).toHaveBeenCalledWith('/api/v1/variant-groups', { params: { query: { page_size: 100 } } })
    expect(api.GET).toHaveBeenCalledWith('/api/v1/pipelines', { params: { query: { page_size: 100 } } })

    const count = wrapper.find('[data-testid="variant-compare-count"]')
    expect(count.exists()).toBe(true)
    expect(count.text()).toBe('views.variantCompare.showingComparisons')
    // Fewer than the page ceiling: no capped-list warning.
    expect(wrapper.text()).not.toContain('views.variantCompare.cappedComparisons')
  })

  it('warns that the list may be capped when a full page comes back', async () => {
    const fullPage = Array.from({ length: 100 }, (_, index) => ({
      ...groupWithVariants,
      id: `g-${index}`,
      name: `Comparison ${index}`,
      pipeline_id: 'p1',
      run_count: 0,
    }))
    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups') {
        return Promise.resolve({ data: fullPage, error: undefined })
      }
      if (url === '/api/v1/pipelines') {
        return Promise.resolve({ data: { items: [{ id: 'p1', name: 'Alpha pipeline' }] }, error: undefined })
      }
      if (url === '/api/v1/variant-groups/{group_id}') {
        return Promise.resolve({ data: fullPage[0], error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const wrapper = mount(VariantCompareView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('views.variantCompare.cappedComparisons')
    })
  })

  it('surfaces a pipeline-name read failure instead of silently showing ids', async () => {
    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups') {
        return Promise.resolve({ data: twoGroups, error: undefined })
      }
      if (url === '/api/v1/pipelines') {
        return Promise.reject(new Error('pipelines backend down'))
      }
      if (url === '/api/v1/variant-groups/{group_id}') {
        return Promise.resolve({ data: twoGroups[0], error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const wrapper = mount(VariantCompareView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })

    await vi.waitFor(() => {
      expect(wrapper.find('[role="alert"]').text()).toContain('views.variantCompare.pipelinesLoadFailed')
    })
    // The table still renders, with ids as the degraded fallback.
    expect(wrapper.find('[data-testid="variant-compare-groups-table"]').exists()).toBe(true)
  })

  it('offers the New comparison CTA in the empty state and opens the builder', async () => {
    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups') {
        return Promise.resolve({ data: [], error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const wrapper = mount(VariantCompareView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })

    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="variant-compare-empty-create"]').exists()).toBe(true)
    })
    expect(wrapper.text()).toContain('views.variantCompare.emptyGroupsTitle')
    expect(wrapper.text()).toContain('views.variantCompare.explainerEvalsLink')

    await wrapper.find('[data-testid="variant-compare-empty-create"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="variant-group-builder"]').exists()).toBe(true)
  })

  it('tolerates a bare-array pipelines response and sparse group/variant payloads', async () => {
    const sparseGroups = [
      {
        id: 'g1',
        name: null,
        pipeline_id: 'p1',
        variants: null,
        run_count: null,
        selection_strategy: null,
      },
      {
        id: 'g2',
        name: 'Group 2',
        pipeline_id: 'p-missing',
        variants: [
          { name: 'var-a', weight: 1, eval_definition_ids: ['e1', '', 123] },
        ],
        run_count: 3,
      },
    ]
    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups') {
        return Promise.resolve({ data: sparseGroups, error: undefined })
      }
      if (url === '/api/v1/pipelines') {
        // A bare array (not the documented { items } envelope) must still work,
        // and a pipeline without a name falls back to its id.
        return Promise.resolve({
          data: [
            { id: 'p1', name: null },
            { id: '', name: 'No id pipeline' },
            { id: 'p2', name: 'Beta pipeline' },
          ],
          error: undefined,
        })
      }
      if (url === '/api/v1/variant-groups/{group_id}') {
        return Promise.resolve({ data: sparseGroups[1], error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const wrapper = mount(VariantCompareView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="variant-compare-groups-table"]').exists()).toBe(true)
    })

    const rows = (wrapper.vm as any).groupRows as Array<Record<string, unknown>>
    const byId = Object.fromEntries(rows.map(row => [row.id, row]))
    // A null variants list counts as zero variants and zero declared evals.
    expect(byId.g1.variants_count).toBe(0)
    expect(byId.g1.declared_evals_count).toBe(0)
    // Only non-empty string eval ids count: the empty string and the number are ignored.
    expect(byId.g2.declared_evals_count).toBe(1)
    // Missing pipeline name falls back to the id; an unknown pipeline keeps its id.
    expect(byId.g1.pipeline).toBe('p1')
    expect(byId.g2.pipeline).toBe('p-missing')

    // A search term combined with a null group name must not throw.
    ;(wrapper.vm as any).filterSearch = 'group'
    await nextTick()
    const filtered = (wrapper.vm as any).groupRows as Array<Record<string, unknown>>
    expect(filtered.map(row => row.id)).toEqual(['g2'])
  })

  it('ignores an empty or non-string row id when selecting and auto-selecting', async () => {
    const numericIdGroups = [
      {
        id: 123,
        name: 'Numeric comparison',
        pipeline_id: 'p1',
        variants: [],
        run_count: 0,
      },
      {
        id: 'g2',
        name: 'String comparison',
        pipeline_id: 'p1',
        variants: [],
        run_count: 0,
      },
    ]
    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups') {
        return Promise.resolve({ data: numericIdGroups, error: undefined })
      }
      if (url === '/api/v1/pipelines') {
        return Promise.resolve({ data: { items: [{ id: 'p1', name: 'Alpha pipeline' }] }, error: undefined })
      }
      if (url === '/api/v1/variant-groups/{group_id}') {
        return Promise.resolve({ data: numericIdGroups[0], error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const wrapper = mount(VariantCompareView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="variant-compare-groups-table"]').exists()).toBe(true)
    })

    const vm = wrapper.vm as any
    // The row action only accepts a non-empty string id; a number or empty id is ignored.
    vm.selectGroupRow({ id: 'g2' })
    expect(vm.selectedGroupId).toBe('g2')
    vm.selectGroupRow({ id: 123 })
    vm.selectGroupRow({ id: '' })
    expect(vm.selectedGroupId).toBe('g2')

    // The auto-select watcher must not adopt a non-string first row id.
    vm.selectedGroupId = null
    vm.filterSearch = 'numeric'
    await nextTick()
    expect(vm.selectedGroupId).toBeNull()
  })

  it('applies pipeline filter updates from the filter bar and ignores unknown keys', async () => {
    mockGroupsAndPipelines()

    const wrapper = mount(VariantCompareView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('Alpha pipeline')
    })

    const vm = wrapper.vm as any
    const filterBar = wrapper.findComponent(FilterBar)
    expect(filterBar.exists()).toBe(true)

    filterBar.vm.$emit('update:filter', 'pipeline_id', 'p2')
    await nextTick()
    expect(vm.filterPipelineId).toBe('p2')

    filterBar.vm.$emit('update:filter', 'unrelated', 'x')
    await nextTick()
    expect(vm.filterPipelineId).toBe('p2')
  })
})
