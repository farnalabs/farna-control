// FAR-2816: the canvas set `:edges-focusable="true"` and styled
// `.vue-flow__edge:focus-visible`, yet keyboard focus never landed on an edge.
//
// Confirmed mechanism: Vue Flow 1.48.2 renders the focusable edge <g> with the
// attribute key "tabIndex" (capital I). SVG attribute names are
// case-sensitive, so the rendered attribute is `tabIndex`, `tabindex` does not
// exist, and the browser never puts the edge in the tab order - `focus()` is a
// no-op. The canvas now adds the correctly-cased `tabindex` through
// `defaultEdgeOptions.domAttributes`.
//
// This spec mounts the view with the REAL @vue-flow/core and exercises the real
// render path: a stubbed VueFlow would never produce the attribute at all.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const useApiFns = vi.hoisted(() => ({
  get: vi.fn(async () => ({ items: [] })),
  post: vi.fn(async () => ({})),
}))

const graph = vi.hoisted(() => {
  const node = (id: string, y: number) => ({
    id,
    node_type: 'agent',
    agent_id: null,
    label: id,
    description: '',
    position: { x: 0, y },
  })
  const edge = (id: string, source: string, target: string, extra: Record<string, unknown> = {}) => ({
    id,
    source_node_id: source,
    target_node_id: target,
    edge_type: 'normal',
    condition_expression: null,
    hitl_review_config: null,
    ...extra,
  })
  return {
    nodes: [node('a', 0), node('b', 200), node('c', 400)],
    edges: [
      edge('gate', 'a', 'b', {
        hitl_review_config: { label: 'Gate', human_only: true, eval_condition: { eval_name: 'quality' } },
      }),
      edge('llm', 'b', 'c', { edge_type: 'llm', routing_label: 'retry' }),
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

const edgePropsOpen = (root: HTMLElement) =>
  root.querySelector('[data-testid="pipeline-editor-edge-props-close"]')

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

describe('PipelineEditorView - keyboard reachable edges (FAR-2816)', () => {
  it('puts every rendered edge in the tab order with a lowercase tabindex', async () => {
    const { root } = await mountCanvas()

    for (const { id } of graph.edges) {
      const edge = edgeElement(root, id)
      // Browsers read SVG attributes case-sensitively: the lowercase name is
      // the only one that makes the <g> focusable.
      expect(edge.getAttribute('tabindex'), `edge ${id} is in the tab order`).toBe('0')
      expect(edge.getAttribute('role')).toBe('group')
      expect(edge.getAttribute('aria-describedby')).not.toBeNull()
    }
  })

  it('moves focus onto an edge and opens it from the keyboard', async () => {
    const { root } = await mountCanvas()
    const edge = edgeElement(root, 'gate')

    // Before the fix this is a no-op: the mis-cased attribute leaves
    // document.activeElement on <body>.
    edge.focus()
    expect(document.activeElement).toBe(edge)

    // Enter is the canvas's activation key for a focused edge. The properties
    // sheet that opens takes focus (its narrow-viewport sheet moves focus to its
    // first control), which is what a keyboard user needs to edit the edge.
    edge.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))

    await vi.waitFor(() => expect(edgePropsOpen(root)).not.toBeNull(), { timeout: 5000 })
    const panel = edgePropsOpen(root)!.closest('aside')
    expect(panel, 'the edge properties sheet is the open aside').not.toBeNull()
    expect(panel!.contains(document.activeElement)).toBe(true)
  })

  it('also opens a focused edge with Space', async () => {
    const { root } = await mountCanvas()
    const edge = edgeElement(root, 'llm')

    edge.focus()
    expect(document.activeElement).toBe(edge)
    edge.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true }))

    await vi.waitFor(() => expect(edgePropsOpen(root)).not.toBeNull(), { timeout: 5000 })
  })

  // Coverage of the activation guard's early returns (FAR-2816 follow-up): the
  // first two tests only walk the happy path, so the two `return false` arms
  // were the branch-coverage gap on this branch's changed lines.

  it('ignores a key event whose target is not an element', async () => {
    const { root } = await mountCanvas()
    const canvas = root.querySelector<HTMLElement>('[data-testid="pipeline-editor-canvas"]')!
    // `event.target` is an EventTarget, not necessarily an Element (a text node
    // is the reachable case). The guard must fall through quietly instead of
    // dereferencing it.
    const textTarget = document.createTextNode('edge badge')
    canvas.appendChild(textTarget)
    try {
      textTarget.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
      await nextTick()
      expect(edgePropsOpen(root)).toBeNull()
    } finally {
      textTarget.remove()
    }
  })

  it('ignores Enter on a canvas edge with no backend counterpart', async () => {
    const { wrapper, root } = await mountCanvas()
    const vm = wrapper.vm as any
    // A freshly drawn connection exists only on the canvas until it is saved, so
    // it has no backend edge to open.
    vm.flowEdges = [...vm.flowEdges, { id: 'flow-only', source: 'a', target: 'b', type: 'smoothstep' }]
    await vi.waitFor(() => expect(edgeElement(root, 'flow-only')).toBeTruthy(), { timeout: 5000 })

    const edge = edgeElement(root, 'flow-only')
    edge.focus()
    expect(document.activeElement).toBe(edge)
    edge.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    await nextTick()

    expect(edgePropsOpen(root)).toBeNull()
    // The interaction is inert, not destructive: the edge is still selectable.
    expect(vm.selectedEdgeData).toBeNull()
  })
})
