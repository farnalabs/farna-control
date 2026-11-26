<!--
  The pipeline editor's canvas edge (FAR-2810). It stands in for Vue Flow's
  built-in `smoothstep` renderer, so it must still draw the edge itself: every
  prop Vue Flow hands the edge is forwarded to SmoothStepEdge (path, arrow
  marker, stroke style, interaction hit area). Selection, click, keyboard focus
  and the animated class live on Vue Flow's wrapper around this component.
  The badges are HTML, so they go through EdgeLabelRenderer and are centred on
  the point SmoothStepEdge itself would put a label.
-->
<template>
  <SmoothStepEdge v-bind="{ ...$attrs, ...geometry }" />
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
import { EdgeLabelRenderer, SmoothStepEdge, getSmoothStepPath, type Position } from '@vue-flow/core'
import EdgeBadges from '../nodes/EdgeBadges.vue'
import type { CanvasEdgeData } from '../../../types/pipeline-graph-projection'

defineOptions({ inheritAttrs: false })

const props = defineProps<{
  id: string
  sourceX: number
  sourceY: number
  targetX: number
  targetY: number
  sourcePosition: Position
  targetPosition: Position
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

const labelPosition = computed(() => {
  const [, x, y] = getSmoothStepPath(geometry.value)
  return { x, y }
})
</script>
