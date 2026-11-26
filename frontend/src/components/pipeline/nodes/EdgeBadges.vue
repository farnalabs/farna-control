<template>
  <!-- A stack sized to its content; PipelineEdge centres it on the edge. -->
  <div class="flex w-max max-w-56 flex-col items-center gap-1">
    <!-- The warning and eval tints are translucent, so each of those chips sits
         on an opaque card backing: the edge line must not strike through it. -->
    <div v-if="hasHitl" class="flex flex-wrap justify-center gap-1">
      <div class="rounded bg-card">
        <span class="block rounded bg-warning/20 px-1.5 py-0.5 text-[10px] font-medium text-warning-text" data-testid="edge-badge-hitl">
          {{ t('views.PipelineEditorView.node_hitl_badge') }}
        </span>
      </div>
      <!-- FAR-2462: the gate's real mode is additive to the kind badge, so the
           human-only marker and the eval condition render only when declared. -->
      <div v-if="humanOnly" class="rounded bg-card">
        <span class="block rounded bg-warning/20 px-1.5 py-0.5 text-[10px] font-medium text-warning-text" data-testid="pipeline-edge-semantic-hitl">
          · {{ t('views.PipelineEditorView.semantic_badge_human_only') }}
        </span>
      </div>
      <div v-if="evalName" class="rounded bg-card">
        <span class="block rounded bg-eval-marker/10 px-1.5 py-0.5 text-[10px] font-medium text-eval-marker-text" data-testid="pipeline-edge-semantic-eval">
          {{ t('views.PipelineEditorView.semantic_badge_eval') }} · {{ evalName }}
        </span>
      </div>
    </div>
    <span v-if="isLoop" class="rounded bg-blue-100 px-1.5 py-0.5 text-[10px] font-medium text-blue-600 dark:bg-blue-900 dark:text-blue-300" data-testid="edge-badge-loop">
      {{ t('views.PipelineEditorView.loop') }}{{ maxIterations ? ` (${maxIterations})` : '' }}
    </span>
    <span v-if="isLlm" class="rounded bg-edge-llm px-1.5 py-0.5 text-[10px] font-medium text-edge-llm-text" data-testid="edge-badge-llm">
      {{ t('views.PipelineEditorView.llm_badge') }}{{ routingLabel ? `: ${routingLabel}` : '' }}
    </span>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

interface EdgeReviewConfig {
  description?: string
  human_only?: boolean
  eval_condition?: { eval_name?: string } | null
}

interface EdgeData {
  hitl_review_config?: EdgeReviewConfig | null
  edge_type?: string
  max_iterations?: number
  routing_label?: string
}

const props = defineProps<{ data?: EdgeData | null }>()
const { t } = useI18n()
const data = computed(() => props.data ?? {})
const hasHitl = computed(() => Boolean(data.value.hitl_review_config))
const humanOnly = computed(() => data.value.hitl_review_config?.human_only === true)
const evalName = computed(() => data.value.hitl_review_config?.eval_condition?.eval_name ?? '')
const isLoop = computed(() => data.value.edge_type === 'loop')
const isLlm = computed(() => data.value.edge_type === 'llm')
const maxIterations = computed(() => data.value.max_iterations ?? 0)
const routingLabel = computed(() => data.value.routing_label ?? '')
</script>
