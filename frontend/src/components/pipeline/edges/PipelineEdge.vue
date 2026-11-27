<!--
  The pipeline editor's canvas edge (FAR-2810). It stands in for Vue Flow's
  built-in `smoothstep` renderer, so it must still draw the edge itself: every
  prop Vue Flow hands the edge is forwarded to SmoothStepEdge (path, arrow
  marker, stroke style, interaction hit area). Selection, click, keyboard focus
  and the animated class live on Vue Flow's wrapper around this component.
  A backward (loop-back) edge is the one exception (FAR-2855): its default
  path runs straight back through both cards, so it is routed underneath them.
  The badges are HTML, so they go through EdgeLabelRenderer and are centred on
  the point SmoothStepEdge itself would put a label.
-->
<template>
  <BaseEdge v-if="loopBack" v-bind="{ ...$attrs, path: loopBack[0] }" />
  <SmoothStepEdge v-else v-bind="{ ...$attrs, ...geometry }" />
  <EdgeLabelRenderer>
    <div
      class="pointer-events-none absolute"
      :style="{ transform: `translate(-50%, -50%) translate(${labelPosition.x}px, ${labelPosition.y}px)` }"
      :data-edge-id="id"
    >
      <EdgeBadges :data="data" />
    </div>
  </EdgeLabelRenderer>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { BaseEdge, EdgeLabelRenderer, SmoothStepEdge, getSmoothStepPath, type Position } from '@vue-flow/core'
import EdgeBadges from '../nodes/EdgeBadges.vue'
import type { CanvasEdgeData } from '../../../types/pipeline-graph-projection'

defineOptions({ inheritAttrs: false })

/** Vue Flow's default smooth-step handle offset, applied at both ends. */
const SMOOTH_STEP_OFFSET = 20
/** How far below the lower card a backward edge runs. */
const LOOP_BACK_CLEARANCE = 32

/** The part of Vue Flow's node object the loop-back route reads. */
interface EdgeEndNode {
  computedPosition?: { y: number }
  dimensions?: { height: number }
}

const props = defineProps<{
  id: string
  sourceX: number
  sourceY: number
  targetX: number
  targetY: number
  sourcePosition: Position
  targetPosition: Position
  sourceNode?: EdgeEndNode
  targetNode?: EdgeEndNode
  /** Absent on an edge Vue Flow created itself (a fresh connection). */
  data?: Partial<CanvasEdgeData> | null
}>()

const geometry = computed(() => ({
  sourceX: props.sourceX,
  sourceY: props.sourceY,
  targetX: props.targetX,
  targetY: props.targetY,
  sourcePosition: props.sourcePosition,
  targetPosition: props.targetPosition,
}))

function cardBottom(node: EdgeEndNode | undefined, fallback: number): number {
  const top = node?.computedPosition?.y
  const height = node?.dimensions?.height
  return top === undefined || !height ? fallback : top + height
}

/**
 * The `[path, labelX, labelY]` of a backward edge, or null for a forward one.
 *
 * An edge leaves the right side and enters the left side, so when the target
 * handle is not clear of the source handle the default path can double back
 * along the handles' midline - through both cards when they share a row.
 */
const loopBack = computed(() => {
  if (props.targetX - props.sourceX > SMOOTH_STEP_OFFSET * 2) return null
  const lowest = Math.max(
    cardBottom(props.sourceNode, props.sourceY),
    cardBottom(props.targetNode, props.targetY),
  )
  return getSmoothStepPath({ ...geometry.value, centerY: lowest + LOOP_BACK_CLEARANCE })
})

const labelPosition = computed(() => {
  const [, x, y] = loopBack.value ?? getSmoothStepPath(geometry.value)
  return { x, y }
})
</script>
