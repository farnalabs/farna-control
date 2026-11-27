// FAR-2855: two defects seen on the read-only demo, pinned on the mounted view
// with the REAL @vue-flow/core:
//
// 1. No pipeline card rendered a Handle, so Vue Flow attached every edge to the
//    bottom of its source and the top of its target.
// 2. The write controls were gated on the historic view only, so a viewer was
//    offered Run, Save, Delete and the rest.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { VueFlow } from '@vue-flow/core'

const useApiFns = vi.hoisted(() => ({
  get: vi.fn(async () => []),
  post: vi.fn(async () => ({})),
}))

const state = vi.hoisted(() => ({
  role: 'viewer',
  empty: false,
  pipeline: {} as Record<string, unknown>,
  /** URLs the mock API refuses with a 403. */
  refused: [] as string[],
}))

const graph = vi.hoisted(() => {
  const node = (id: string, x: number, y: number) => ({
    id,
    node_type: id === 'review' ? 'hitl' : 'agent',
    agent_id: null,
    label: id,
    description: '',
    position: { x, y },
  })
  const edge = (id: string, source: string, target: string, edgeType = 'normal') => ({
    id,
    source_node_id: source,
    target_node_id: target,
    edge_type: edgeType,
    condition_expression: null,
    hitl_review_config: null,
  })
  return {
    nodes: [node('risk', 380, 200), node('review', 680, 60), node('open_pr', 980, 200)],
    edges: [
      // Gated, so an editor's panel for it offers Save Edge.
      { ...edge('risk-review', 'risk', 'review'), hitl_review_config: { label: 'Gate', human_only: true } },
      edge('review-open', 'review', 'open_pr'),
    ],
  }
})

vi.mock('../composables/useApi', () => ({
  useApi: () => useApiFns,
}))

vi.mock('../lib/api/client', () => {
  const get = (url: string) => {
    if (state.refused.includes(url)) {
      return Promise.resolve({ data: undefined, error: { status: 403, detail: 'refused-by-server' } })
    }
    if (url.includes('/pipelines/{pipeline_id}/graph')) {
      const data = state.empty ? { nodes: [], edges: [] } : structuredClone(graph)
      return Promise.resolve({ data, error: undefined })
    }
    if (url === '/api/v1/pipelines/{pipeline_id}') {
      return Promise.resolve({
        data: { id: 'test-pipeline-id', name: 'Test Pipeline', run_enabled: true, ...state.pipeline },
        error: undefined,
      })
    }
    return Promise.resolve({ data: { items: [] }, error: undefined })
  }
  const ok = () => Promise.resolve({ data: { run_id: 'run-1' }, error: undefined })
  return {
    api: { GET: vi.fn(get), POST: vi.fn(ok), PATCH: vi.fn(ok), PUT: vi.fn(ok), DELETE: vi.fn(ok) },
    getAccessToken: vi.fn(() => `h.${btoa(JSON.stringify({ org_role: state.role }))}.s`),
  }
})

import PipelineEditorView from '../views/PipelineEditorView.vue'
import { api } from '../lib/api/client'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/pipelines/:id/editor', name: 'pipeline-editor', component: PipelineEditorView },
    { path: '/runs/:id', name: 'run-detail', component: { template: '<div />' } },
  ],
})

/** jsdom has no layout: report a size for everything and fire resize callbacks,
 *  which is what makes Vue Flow treat the nodes as measured. */
function fakeLayout() {
  class MeasuringResizeObserver {
    constructor(private readonly callback: ResizeObserverCallback) {}
    observe(target: Element): void {
      queueMicrotask(() => {
        const entry = { target, contentRect: { width: 200, height: 80 } } as unknown as ResizeObserverEntry
        this.callback([entry], this as unknown as ResizeObserver)
      })
    }
    unobserve(): void {}
    disconnect(): void {}
  }
  vi.stubGlobal('ResizeObserver', MeasuringResizeObserver)
  vi.stubGlobal('DOMMatrixReadOnly', class { m22 = 1 })
  vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(200)
  vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(80)
}

let wrapper: VueWrapper | undefined

async function mountEditor(role: string) {
  state.role = role
  const pinia = createPinia()
  setActivePinia(pinia)
  router.push('/pipelines/test-pipeline-id/editor')
  await router.isReady()
  wrapper = mount(PipelineEditorView, {
    attachTo: document.body,
    global: { plugins: [pinia, router], stubs: { Background: true, Controls: true } },
  })
  const ready = state.empty ? '[data-testid="pipeline-editor-canvas"]' : '.vue-flow__edge'
  await vi.waitFor(() => expect(wrapper!.element.querySelector?.(ready)).toBeTruthy(), { timeout: 5000 })
  await flushPromises()
  // Read after the editor renders: the loading placeholder is a different root.
  return { wrapper, root: wrapper.element as HTMLElement }
}

const byTestId = (id: string) => wrapper!.find(`[data-testid="pipeline-editor-${id}"]`)
const present = (ids: string[]) => ids.filter((id) => byTestId(id).exists())
const flowProps = () => wrapper!.findComponent(VueFlow).props() as Record<string, unknown>

const EDIT_CONTROLS = ['save', 'node-type-select', 'add-node', 'arrange-graph']
const SETTINGS_CONTROLS = ['rename', 'archive', 'pause', 'delete']
/** Settings every role reads; a role without `pipeline.update` gets them disabled. */
const SETTING_FIELDS = ['max-duration', 'circuit-breaker-threshold', 'hitl-review-window', 'queue-timeout']
const SETTING_SELECTS = ['max-autonomy', 'owner-team']
const GOVERNANCE_SURFACES = [
  'toolbar-group-settings',
  ...SETTING_FIELDS,
  ...SETTING_SELECTS,
  'autonomy-panel-toggle',
  'retry-policy-toggle',
]
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const WRITE_CONTROLS = [...EDIT_CONTROLS, ...SETTINGS_CONTROLS, 'save-as-template', 'run']
const READ_CONTROLS = ['version-timeline', 'fit-view', 'canvas']

beforeEach(async () => {
  const { useRoute } = await import('vue-router')
  const route = (useRoute as unknown as () => {
    params: Record<string, string>
    query: Record<string, string>
  })()
  route.params = { id: 'test-pipeline-id' }
  route.query = {}
  state.empty = false
  state.pipeline = {}
  state.refused = []
  vi.mocked(api.PATCH).mockClear()
  vi.mocked(api.DELETE).mockClear()
  vi.mocked(api.PUT).mockClear()
  vi.mocked(api.POST).mockClear()
  fakeLayout()
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = undefined
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('PipelineEditorView - edge attachment (FAR-2855)', () => {
  it('gives every card a left target handle and a right source handle', async () => {
    const { root } = await mountEditor('operator')

    const cards = Array.from(root.querySelectorAll('.vue-flow__node'))
    expect(cards).toHaveLength(graph.nodes.length)
    for (const card of cards) {
      const handles = Array.from(card.querySelectorAll('.vue-flow__handle')).map(
        (handle) => `${handle.classList.contains('target') ? 'target' : 'source'}:${handle.getAttribute('data-handlepos')}`,
      )
      expect(handles).toStrictEqual(['target:left', 'source:right'])
    }
  })

  it('turns a drag connection into an edge whose properties open', async () => {
    const { wrapper } = await mountEditor('operator')

    wrapper.findComponent(VueFlow).vm.$emit('connect', { source: 'risk', target: 'open_pr' })
    await nextTick()

    const vm = wrapper.vm as any
    const added = vm.rawEdges.at(-1)
    expect([added.source_node_id, added.target_node_id]).toStrictEqual(['risk', 'open_pr'])
    // One canvas edge, sharing the backend edge's id: without that the click
    // cannot find the edge to inspect.
    expect(vm.flowEdges.map((edge: any) => edge.id)).toStrictEqual(['risk-review', 'review-open', added.id])
    vm.onEdgeClick({ edge: { id: added.id } })
    await nextTick()
    expect(byTestId('edge-props-close').exists()).toBe(true)
  })
})

describe('PipelineEditorView - role gates (FAR-2855)', () => {
  it('offers a viewer the read controls and no write control', async () => {
    await mountEditor('viewer')

    expect(present(WRITE_CONTROLS)).toStrictEqual([])
    expect(present(READ_CONTROLS)).toStrictEqual(READ_CONTROLS)
    expect(flowProps()).toMatchObject({
      nodesDraggable: false,
      nodesConnectable: false,
      edgesUpdatable: false,
      deleteKeyCode: null,
    })
  })

  it('offers a runner Run and nothing that edits the pipeline', async () => {
    await mountEditor('runner')

    expect(present(WRITE_CONTROLS)).toStrictEqual(['run'])
    expect(flowProps()).toMatchObject({ nodesDraggable: false, nodesConnectable: false, deleteKeyCode: null })
  })

  it('offers an operator every write control', async () => {
    await mountEditor('operator')

    expect(present(WRITE_CONTROLS)).toStrictEqual(WRITE_CONTROLS)
    expect(flowProps()).toMatchObject({ nodesDraggable: true, nodesConnectable: true })
  })

  it("starts a runner's run without saving the graph", async () => {
    const { wrapper } = await mountEditor('runner')

    const vm = wrapper.vm as any
    vm.runPrompt = 'go'
    await vm.triggerRun()

    expect(api.PUT).not.toHaveBeenCalled()
    expect(vi.mocked(api.POST).mock.calls.map((call) => String(call[0]))).toContain('/api/v1/runs')
  })

  it("saves the graph before an operator's run", async () => {
    const { wrapper } = await mountEditor('operator')

    const vm = wrapper.vm as any
    vm.runPrompt = 'go'
    await vm.triggerRun()

    expect(vi.mocked(api.PUT).mock.calls.map((call) => String(call[0]))).toContain(
      '/api/v1/pipelines/{pipeline_id}/graph',
    )
  })

  it('ignores the add and connect shortcuts and offers only read commands to a viewer', async () => {
    const { wrapper } = await mountEditor('viewer')
    const canvas = byTestId('canvas')

    await canvas.trigger('keydown', { key: 'a' })
    await canvas.trigger('keydown', { key: 'e' })
    await canvas.trigger('keydown', { key: 'k', ctrlKey: true })
    await nextTick()

    const vm = wrapper.vm as any
    expect(vm.rawNodes).toHaveLength(graph.nodes.length)
    expect(vm.keyboardConnectSourceId).toBeNull()
    const offered = wrapper
      .findAll('[data-testid^="pipeline-editor-command-option-"]')
      .map((option) => option.attributes('data-testid')!.replace('pipeline-editor-command-option-', ''))
    expect(offered).toStrictEqual(['fit', 'timeline'])
  })

  it('opens node and edge details read-only for a viewer', async () => {
    const { wrapper } = await mountEditor('viewer')
    const vm = wrapper.vm as any

    vm.onNodeClick({ node: { id: 'risk' } })
    await nextTick()
    expect(byTestId('node-label').attributes('readonly')).toBeDefined()
    expect(byTestId('node-description').attributes('readonly')).toBeDefined()
    expect(byTestId('convert-to-agent').exists()).toBe(false)
    expect(byTestId('revert-to-manual').exists()).toBe(false)

    vm.onEdgeClick({ edge: { id: 'risk-review' } })
    await nextTick()
    expect(byTestId('edge-props-close').exists()).toBe(true)
    expect(byTestId('save-edge').exists()).toBe(false)
  })

  it('offers an operator Save Edge on the same gated edge', async () => {
    const { wrapper } = await mountEditor('operator')

    ;(wrapper.vm as any).onEdgeClick({ edge: { id: 'risk-review' } })
    await nextTick()

    expect(byTestId('save-edge').exists()).toBe(true)
  })

  it('hides every action on an empty pipeline from a viewer', async () => {
    state.empty = true
    await mountEditor('viewer')

    const actions = ['empty-rename', 'empty-archive', 'empty-delete', 'empty-add-node', 'empty-show-me', 'empty-template']
    expect(present(actions)).toStrictEqual([])
  })

  it('keeps every action on an empty pipeline for an operator', async () => {
    state.empty = true
    await mountEditor('operator')

    const actions = ['empty-rename', 'empty-archive', 'empty-delete', 'empty-add-node', 'empty-show-me', 'empty-template']
    expect(present(actions)).toStrictEqual(actions)
  })
})

describe('PipelineEditorView - client-generated ids (FAR-2855)', () => {
  // The API types node and edge ids as uuid.UUID: any other shape is refused,
  // and with it every later save and every run that saves first.
  it('sends UUIDs for an added node and for edges connected to it', async () => {
    const { wrapper } = await mountEditor('operator')
    const vm = wrapper.vm as any

    vm.addNode('agent')
    const addedNode = vm.rawNodes.at(-1).id
    wrapper.findComponent(VueFlow).vm.$emit('connect', { source: 'review', target: addedNode })
    vm.connectNodes('risk', 'open_pr')
    await vm.saveGraph()

    const [url, options] = vi.mocked(api.PUT).mock.calls.at(-1) as unknown as [string, { body: any }]
    expect(url).toBe('/api/v1/pipelines/{pipeline_id}/graph')
    const newNodes = options.body.nodes.slice(graph.nodes.length)
    const newEdges = options.body.edges.slice(graph.edges.length)
    expect(newNodes).toHaveLength(1)
    expect(newEdges).toHaveLength(2)
    for (const { id } of [...newNodes, ...newEdges]) expect(id).toMatch(UUID)
    expect(newEdges[0].target_node_id).toBe(addedNode)
  })
})

describe('PipelineEditorView - governance stays readable (FAR-2855)', () => {
  it.each(['viewer', 'runner'])('shows a %s every setting with its control disabled', async (role) => {
    state.pipeline = { max_duration_seconds: 900 }
    await mountEditor(role)

    expect(present(GOVERNANCE_SURFACES)).toStrictEqual(GOVERNANCE_SURFACES)
    for (const id of SETTING_FIELDS) expect(byTestId(id).attributes('disabled'), id).toBeDefined()
    expect((byTestId('max-duration').element as HTMLInputElement).value).toBe('900')
    for (const id of SETTING_SELECTS) expect(byTestId(id).classes(), id).toContain('p-disabled')
  })

  it('leaves the same controls enabled for an operator', async () => {
    await mountEditor('operator')

    for (const id of SETTING_FIELDS) expect(byTestId(id).attributes('disabled'), id).toBeUndefined()
    for (const id of SETTING_SELECTS) expect(byTestId(id).classes(), id).not.toContain('p-disabled')
  })

  it('shows a viewer the retry policy with no way to change or save it', async () => {
    const { wrapper } = await mountEditor('viewer')

    await byTestId('retry-policy-toggle').trigger('click')
    await flushPromises()

    const panel = byTestId('retry-policy-panel')
    const inputs = panel.findAll('input')
    expect(inputs.length).toBeGreaterThanOrEqual(5)
    for (const input of inputs) expect(input.attributes('disabled')).toBeDefined()
    expect(byTestId('retry-policy-save').exists()).toBe(false)
    expect(byTestId('retry-policy-close').exists()).toBe(true)
    wrapper.unmount()
  })

  it('shows a viewer the autonomy levels without demote or promote', async () => {
    await mountEditor('viewer')

    await byTestId('autonomy-panel-toggle').trigger('click')
    await flushPromises()

    const panel = wrapper!.find('[data-testid="progressive-autonomy-panel"]')
    for (const level of ['default', 'ceiling', 'earned']) {
      expect(panel.find(`[data-testid="progressive-autonomy-${level}"]`).exists()).toBe(true)
    }
    for (const action of ['target', 'reason', 'demote', 'promote']) {
      expect(panel.find(`[data-testid="progressive-autonomy-${action}"]`).exists()).toBe(false)
    }
  })

  it('shows a viewer a tripped circuit breaker without the reset', async () => {
    state.pipeline = { circuit_breaker_tripped: true }
    await mountEditor('viewer')

    expect(byTestId('circuit-breaker-tripped').exists()).toBe(true)
    expect(byTestId('circuit-breaker-reset').exists()).toBe(false)
  })

  it('fires no write from any settings handler for a viewer', async () => {
    const { wrapper } = await mountEditor('viewer')
    const vm = wrapper.vm as any

    for (const id of SETTING_FIELDS) {
      await byTestId(id).trigger('change')
      await byTestId(id).trigger('blur')
    }
    vm.maxDurationInput = 60
    vm.hitlReviewWindowInput = 120
    vm.queueTimeoutInput = 120
    vm.circuitBreakerInput = '5'
    vm.renameName = 'renamed'
    for (const handler of ['updateMaxDuration', 'updateCircuitBreakerThreshold', 'updateHitlReviewWindow', 'updateQueueTimeout', 'saveRetryPolicy', 'handleRename', 'handleArchive', 'handleUnarchive', 'handlePause', 'handleResume', 'handleDelete', 'resetCircuitBreaker']) {
      await vm[handler]()
    }
    await vm.updateMaxAutonomyLevel('fully_autonomous')
    await vm.updateOwnerTeam('team-1')
    await vm.updateEnvironmentProfile('profile-1')
    await vm.saveGraph()
    await flushPromises()

    expect(api.PATCH).not.toHaveBeenCalled()
    expect(api.PUT).not.toHaveBeenCalled()
    expect(api.POST).not.toHaveBeenCalled()
    expect(api.DELETE).not.toHaveBeenCalled()
  })

  it('does not report a refused team list to a viewer', async () => {
    state.refused = ['/api/v1/teams']
    await mountEditor('viewer')

    expect(byTestId('owner-team-error').exists()).toBe(false)
  })

  it('still reports a refused team list to someone who could change the team', async () => {
    state.refused = ['/api/v1/teams']
    await mountEditor('operator')

    expect(byTestId('owner-team-error').text()).toContain('refused-by-server')
  })
})
