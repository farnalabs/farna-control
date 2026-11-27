// FAR-2855: with right-to-left attachment a backward edge's default path runs
// back along the handles' midline, through both cards, so PipelineEdge routes
// it underneath them.
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { BaseEdge, Position, SmoothStepEdge } from '@vue-flow/core'
import PipelineEdge from '../components/pipeline/edges/PipelineEdge.vue'

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
