// FAR-2810: the canvas showed no edge badges because the view filled Vue Flow's
// `edge-default` slot while every edge was `type: 'smoothstep'`. Specs that stub
// VueFlow and call a slot by name cannot catch that, so this one mounts the view
// with the REAL @vue-flow/core and lets it choose each edge's renderer. Only the
// layout APIs jsdom lacks are faked, so that Vue Flow can measure the nodes.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'

const useApiFns = vi.hoisted(() => ({
  get: vi.fn(async () => ({ items: [] })),
  post: vi.fn(async () => ({})),
}))

const graph = vi.hoisted(() => {
  const node = (id: string, x: number, y: number) => ({
    id,
    node_type: 'agent',
    agent_id: null,
    label: id,
    description: '',
    position: { x, y },
  })
  const edge = (id: string, source: string, target: string, extra: Record<string, unknown>) => ({
    id,
    source_node_id: source,
    target_node_id: target,
    edge_type: 'normal',
    condition_expression: null,
    hitl_review_config: null,
    ...extra,
  })
  return {
    nodes: [node('a', 0, 0), node('b', 0, 200), node('c', 400, 200), node('d', 200, 400)],
    edges: [
      edge('gate', 'a', 'b', {
        hitl_review_config: {
          label: 'Gate',
          human_only: true,
          eval_condition: { eval_name: 'quality' },
        },
      }),
      edge('llm', 'a', 'c', { edge_type: 'llm', routing_label: 'retry' }),
      edge('loop', 'd', 'a', { edge_type: 'loop', max_iterations: 3 }),
      edge('plain', 'b', 'd', {}),
    ],
  }
})

vi.mock('../composables/useApi', () => ({
  useApi: () => useApiFns,
}))

vi.mock('../lib/api/client', () => {
  const get = (url: string) => {
    if (url.includes('/pipelines/{pipeline_id}/graph')) {
      return Promise.resolve({ data: structuredClone(graph), error: undefined })
    }
    if (url.includes('/api/v1/pipelines/{pipeline_id}')) {
      return Promise.resolve({ data: { id: 'test-pipeline-id', name: 'Test Pipeline' }, error: undefined })
    }
    return Promise.resolve({ data: { items: [] }, error: undefined })
  }
  const ok = () => Promise.resolve({ data: {}, error: undefined })
  return {
    api: { GET: vi.fn(get), POST: vi.fn(ok), PATCH: vi.fn(ok), PUT: vi.fn(ok), DELETE: vi.fn(ok) },
    getAccessToken: vi.fn().mockReturnValue('mock-token'),
  }
})

import PipelineEditorView from '../views/PipelineEditorView.vue'
import { usePlanStore } from '../stores/planStore'

const router = createRouter({
  history: createWebHistory(),
  routes: [{ path: '/pipelines/:id/editor', name: 'pipeline-editor', component: PipelineEditorView }],
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

async function mountCanvas() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = usePlanStore()
  store.currentTier = 'team'
  store.features = { pipeline_delete: true }
  router.push('/pipelines/test-pipeline-id/editor')
  await router.isReady()
  wrapper = mount(PipelineEditorView, {
    attachTo: document.body,
    global: { plugins: [pinia, router], stubs: { Background: true, Controls: true } },
  })
  const root = wrapper.element as HTMLElement
  await vi.waitFor(() => expect(root.querySelectorAll('.vue-flow__edge')).toHaveLength(graph.edges.length), {
    timeout: 5000,
  })
  return { wrapper, root }
}

const edgeElement = (root: HTMLElement, id: string) =>
  root.querySelector<SVGGElement>(`.vue-flow__edge[data-id="${id}"]`)!

/** The badge layer is separate from the edge's SVG group; PipelineEdge tags it. */
const badgeTexts = (root: HTMLElement, id: string) =>
  Array.from(
    root.querySelectorAll(`.vue-flow__edge-labels [data-edge-id="${id}"] [data-testid]`),
    (badge) => badge.textContent?.trim(),
  )

beforeEach(async () => {
  const { useRoute } = await import('vue-router')
  const route = (useRoute as unknown as () => {
    params: Record<string, string>
    query: Record<string, string>
  })()
  route.params = { id: 'test-pipeline-id' }
  route.query = {}
  fakeLayout()
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = undefined
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('PipelineEditorView — canvas edge rendering with the real Vue Flow (FAR-2810)', () => {
  it('shows the gate, LLM and loop badges on their own edges', async () => {
    const { root } = await mountCanvas()

    await vi.waitFor(() => expect(badgeTexts(root, 'gate')).toHaveLength(3), { timeout: 5000 })
    expect(badgeTexts(root, 'gate')).toEqual(['HITL', '· human-only', 'eval gate · quality'])
    expect(badgeTexts(root, 'llm')).toEqual(['LLM: retry'])
    expect(badgeTexts(root, 'loop')).toEqual(['Loop (3)'])
    expect(badgeTexts(root, 'plain')).toHaveLength(0)
  })

  it('still draws every edge: path, arrow marker, variant styling and hit area', async () => {
    const { root } = await mountCanvas()

    for (const { id } of graph.edges) {
      const path = edgeElement(root, id).querySelector('path.vue-flow__edge-path')!
      expect(path.getAttribute('d')).toMatch(/^M[\d.-]/)
      expect(path.getAttribute('marker-end')).toContain('type=arrowclosed')
      expect(edgeElement(root, id).querySelector('path.vue-flow__edge-interaction')).not.toBeNull()
    }
    const loopPath = edgeElement(root, 'loop').querySelector<SVGPathElement>('path.vue-flow__edge-path')!
    expect(loopPath.style.strokeDasharray.replaceAll(' ', '')).toBe('5,5')
    expect(edgeElement(root, 'loop').classList).toContain('animated')
    expect(edgeElement(root, 'plain').classList).not.toContain('animated')
  })

  it('still selects an edge and opens its properties on click', async () => {
    const { root } = await mountCanvas()

    edgeElement(root, 'gate').dispatchEvent(new MouseEvent('click', { bubbles: true }))

    await vi.waitFor(() => expect(edgeElement(root, 'gate').classList).toContain('selected'))
    await vi.waitFor(() =>
      expect(root.querySelector('[data-testid="pipeline-editor-edge-props-close"]')).not.toBeNull(),
    )
  })
})
