// FAR-2815: "Arrange graph" overlapped node cards and loop-edge badges vanished
// behind them. Two independent causes, both pinned here against the REAL Vue
// Flow render path:
//
// 1. The backend graph carries no pixel size, so the layout ran on its 200x100
//    defaults while the cards size to their own (wider) content - the rank
//    spacing was then too small and the cards overlapped. `arrangeGraph` now
//    feeds Vue Flow's measured box into the layout.
// 2. Vue Flow paints the edge-label layer (which holds every edge badge) BEFORE
//    the nodes layer, with no z-index on either, so a badge over a card was
//    painted underneath it. The label layer is raised above the cards.
//
// Only the layout APIs jsdom lacks are faked (a measuring ResizeObserver plus
// element offset sizes), never Vue Flow itself: specs that stub VueFlow cannot
// see either defect.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'

/** The measured card box the canvas reports for every node. Wider than the
 *  layout's 200px default, which is what made the unmeasured layout overlap. */
const CARD_WIDTH = 320
const CARD_HEIGHT = 140

const useApiFns = vi.hoisted(() => ({
  get: vi.fn(async () => ({ items: [] })),
  post: vi.fn(async () => ({})),
}))

const graph = vi.hoisted(() => {
  // Spread-out starting positions: an all-zero graph is auto-laid-out at load,
  // which is a different path. Arrange is what this spec exercises.
  const node = (id: string, x: number, y: number) => ({
    id,
    node_type: 'agent',
    agent_id: null,
    label: id,
    description: '',
    position: { x, y },
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
  // A branching graph with an LLM edge and a loop back to the start, i.e. the
  // shape the ticket reports.
  return {
    nodes: [node('start', 10, 10), node('branch_a', 300, 10), node('branch_b', 300, 260), node('join', 600, 130)],
    edges: [
      edge('loop', 'join', 'start', { edge_type: 'loop', max_iterations: 3 }),
      edge('to_a', 'start', 'branch_a', { edge_type: 'llm', routing_label: 'retry' }),
      edge('to_b', 'start', 'branch_b'),
      edge('join_a', 'branch_a', 'join'),
      edge('join_b', 'branch_b', 'join'),
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

/** jsdom has no layout: report the real card box for everything and fire the
 *  resize callbacks Vue Flow uses to treat the nodes as measured. */
function fakeLayout() {
  class MeasuringResizeObserver {
    constructor(private readonly callback: ResizeObserverCallback) {}
    observe(target: Element): void {
      queueMicrotask(() => {
        const entry = {
          target,
          contentRect: { width: CARD_WIDTH, height: CARD_HEIGHT },
        } as unknown as ResizeObserverEntry
        this.callback([entry], this as unknown as ResizeObserver)
      })
    }
    unobserve(): void {}
    disconnect(): void {}
  }
  vi.stubGlobal('ResizeObserver', MeasuringResizeObserver)
  vi.stubGlobal('DOMMatrixReadOnly', class { m22 = 1 })
  vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(CARD_WIDTH)
  vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(CARD_HEIGHT)
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

interface Rect {
  id: string
  x: number
  y: number
}

/** Rendered node rectangles, read from the transform Vue Flow applies. */
function renderedNodes(root: HTMLElement): Rect[] {
  return Array.from(root.querySelectorAll<HTMLElement>('.vue-flow__node[data-id]'), (element) => {
    const translate = /translate\(\s*(-?[\d.]+)px\s*,\s*(-?[\d.]+)px\s*\)/.exec(
      element.style.transform,
    )
    expect(translate, `node ${element.dataset.id} carries a translate transform`).not.toBeNull()
    return {
      id: element.dataset.id!,
      x: Number(translate![1]),
      y: Number(translate![2]),
    }
  })
}

function overlaps(a: Rect, b: Rect): boolean {
  return (
    a.x < b.x + CARD_WIDTH &&
    b.x < a.x + CARD_WIDTH &&
    a.y < b.y + CARD_HEIGHT &&
    b.y < a.y + CARD_HEIGHT
  )
}

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

describe('PipelineEditorView - Arrange graph layout (FAR-2815)', () => {
  it('spaces cards by their measured size so a branching graph never overlaps', async () => {
    const { root } = await mountCanvas()

    root
      .querySelector<HTMLElement>('[data-testid="pipeline-editor-arrange-graph"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))

    // Two weights, so this fails on a 200px assumption instead of passing by
    // accident: the second rank sits at 200 + 40 = 240 with the defaults and at
    // 320 + 40 = 360 once the measured width is used.
    await vi.waitFor(() => {
      const xs = new Set(renderedNodes(root).map((node) => node.x))
      expect(xs.size).toBeGreaterThan(1)
    }, { timeout: 5000 })

    const nodes = renderedNodes(root)
    for (let i = 0; i < nodes.length; i += 1) {
      for (let j = i + 1; j < nodes.length; j += 1) {
        expect(
          overlaps(nodes[i], nodes[j]),
          `${nodes[i].id} overlaps ${nodes[j].id}`,
        ).toBe(false)
      }
    }
    // The join follows the two branch cards plus the column gap.
    expect(nodes.find((node) => node.id === 'join')!.x).toBe(CARD_WIDTH * 2 + 80)
  })

  it('raises the edge-label layer above the node cards so a loop badge stays visible', async () => {
    const { root } = await mountCanvas()

    const labels = root.querySelector<HTMLElement>('.vue-flow__edge-labels')
    const nodesLayer = root.querySelector<HTMLElement>('.vue-flow__nodes')
    expect(labels, 'Vue Flow renders an edge-label layer').not.toBeNull()
    expect(nodesLayer, 'Vue Flow renders a nodes layer').not.toBeNull()

    // Every edge badge teleports into this layer, so the loop edge's badge is
    // inside it.
    expect(labels!.querySelector('[data-edge-id="loop"]')).not.toBeNull()

    await vi.waitFor(() => expect(labels!.style.zIndex).not.toBe(''), { timeout: 5000 })
    // Above the plain node cards (no z-index => 0) and above Vue Flow's +1000
    // selection elevation, while the layer itself stays non-interactive.
    expect(Number(labels!.style.zIndex)).toBeGreaterThan(1000)
    expect(Number(nodesLayer!.style.zIndex || '0')).toBe(0)
  })
})
