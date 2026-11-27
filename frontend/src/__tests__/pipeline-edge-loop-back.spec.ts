// FAR-2855: with right-to-left attachment a backward edge's default path runs
// back along the handles' midline, through both cards, so PipelineEdge routes
// it underneath them.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { BaseEdge, Position, SmoothStepEdge } from '@vue-flow/core'
import PipelineEdge from '../components/pipeline/edges/PipelineEdge.vue'
import { PIPELINE_NODE_MAX_WIDTH_CLASS } from '../components/pipeline/nodes/NodeRegistry'
import { layoutNodes } from '../utils/graph-layout'

/** Every card on the canvas, as the edge reads them from the Vue Flow store. */
const canvas = vi.hoisted(() => ({ nodes: [] as unknown[] }))

vi.mock('@vue-flow/core', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@vue-flow/core')>()),
  useVueFlow: () => ({ nodes: { get value() { return canvas.nodes } } }),
}))

const card = (y: number, height: number) => ({ computedPosition: { y }, dimensions: { height } })

function mountEdge(sourceX: number, targetX: number, extra: Record<string, unknown> = {}) {
  return mount(PipelineEdge, {
    shallow: true,
    props: {
      id: 'e1',
      sourceX,
      sourceY: 240,
      targetX,
      targetY: 240,
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
      ...extra,
    },
  })
}

/** The largest y named by any command in an SVG path. */
function lowestY(path: string): number {
  return Math.max(...[...path.matchAll(/-?[\d.]+[ ,](-?[\d.]+)/g)].map((match) => Number(match[1])))
}

describe('PipelineEdge loop-back routing', () => {
  beforeEach(() => {
    canvas.nodes = []
  })

  it('also clears a taller card between the two ends, and ignores one outside them', () => {
    canvas.nodes = [
      { computedPosition: { x: 600, y: 200 }, dimensions: { width: 224, height: 170 } },
      { computedPosition: { x: 1500, y: 200 }, dimensions: { width: 224, height: 600 } },
    ]

    const wrapper = mountEdge(1200, 80, { sourceNode: card(200, 80), targetNode: card(200, 80) })

    // The card in the way ends at y=370; the one past the source does not count.
    expect(lowestY(wrapper.findComponent(BaseEdge).props('path') as string)).toBe(402)
  })

  it('keeps the default smooth step for a forward edge', () => {
    const wrapper = mountEdge(600, 980)

    expect(wrapper.findComponent(SmoothStepEdge).exists()).toBe(true)
    expect(wrapper.findComponent(BaseEdge).exists()).toBe(false)
  })

  it('routes a backward edge below the taller of the two cards', () => {
    const wrapper = mountEdge(1200, 80, { sourceNode: card(200, 80), targetNode: card(200, 150) })

    expect(wrapper.findComponent(SmoothStepEdge).exists()).toBe(false)
    // The taller card ends at y=350; the route clears it by 32.
    expect(lowestY(wrapper.findComponent(BaseEdge).props('path') as string)).toBe(382)
  })

  it('measures from the handles when the cards are not yet sized', () => {
    const wrapper = mountEdge(1200, 80)

    expect(lowestY(wrapper.findComponent(BaseEdge).props('path') as string)).toBe(272)
  })
})

describe('PipelineEdge at auto-layout spacing', () => {
  // Layout leaves 40px after the widest card in a column, and each handle
  // overhangs its card, so a forward edge's gap is under the 40px the default
  // smooth step needs.
  const CARD_WIDTH = Number(PIPELINE_NODE_MAX_WIDTH_CLASS.replace('max-w-', '')) * 4
  const HANDLE_OVERHANG = 3

  const edges = [
    { source: 'implement', target: 'risk' },
    { source: 'risk', target: 'open_pr' },
    { source: 'risk', target: 'review' },
    { source: 'review', target: 'open_pr' },
  ]
  const positions = new Map(
    layoutNodes(
      ['implement', 'risk', 'review', 'open_pr'].map((id) => ({ id, width: CARD_WIDTH, height: 100 })),
      edges,
    ).map((node) => [node.id, node.position]),
  )

  it.each(edges)('draws $source to $target as a forward step', ({ source, target }) => {
    const sourceX = positions.get(source)!.x + CARD_WIDTH + HANDLE_OVERHANG
    const targetX = positions.get(target)!.x - HANDLE_OVERHANG

    const wrapper = mountEdge(sourceX, targetX)

    expect(wrapper.findComponent(BaseEdge).exists()).toBe(false)
    const offset = wrapper.findComponent(SmoothStepEdge).props('offset') as number
    // Both offsets fit inside the gap, so the path never steps backwards.
    expect(offset * 2).toBeLessThan(targetX - sourceX)
  })

  it('leaves a gap narrower than the card spacing', () => {
    const gap = positions.get('risk')!.x - positions.get('implement')!.x - CARD_WIDTH

    expect(gap).toBe(40)
  })
})
