<template>
  <BaseCard
    padding="lg"
    :class="'card-hover flex flex-col'"
    :data-testid="`library-item-${prim.id}`"
  >
    <div class="flex items-start justify-between mb-3">
      <div>
        <span :class="typeBadgeClass(prim.primitive_type)">
          {{ prim.primitive_type }}
        </span>
        <h3 class="mt-2 text-base font-medium text-foreground">{{ prim.name }}</h3>
      </div>
      <span
        v-if="badge === 'modulo' && prim.source === 'farna_control'"
        class="text-xs text-primary-text font-medium bg-primary/10 px-2 py-0.5 rounded"
      >
        {{ $t('views.LibraryView.modulo_badge') }}
      </span>
      <span
        v-else-if="badge === 'preview'"
        class="badge badge-context-amber text-xs"
      >
        {{ $t('views.LibraryView.preview_badge') }}
      </span>
      <span
        v-else-if="badge === 'collection'"
        class="badge badge-context-teal text-xs"
        data-testid="library-collection-badge"
      >
        {{ $t('views.LibraryView.collection_badge') }}
      </span>
    </div>

    <p v-if="prim.description" class="text-sm text-muted-foreground flex-1 mb-4 line-clamp-2">
      {{ prim.description }}
    </p>

    <div v-if="showTags" class="flex items-center gap-2 flex-wrap mb-4">
      <span
        v-for="tag in (prim.tags || []).slice(0, 3)"
        :key="tag"
        class="text-xs bg-muted text-muted-foreground px-2 py-0.5 rounded"
      >
        {{ tag }}
      </span>
      <span v-if="(prim.tags || []).length > 3" class="text-xs text-muted-foreground">
        +{{ prim.tags.length - 3 }}
      </span>
    </div>

    <div v-if="showAutoUpdate && prim.forked_from" class="flex items-center gap-2 mb-3">
      <span class="text-xs text-muted-foreground">{{ $t('views.LibraryView.auto_update') }}</span>
      <ToggleSwitch
        :checked="prim.auto_update"
        :disabled="!!toggleLoading[prim.id]"
        :toggling="!!toggleLoading[prim.id]"
        :label="$t('views.LibraryView.auto_update')"
        :data-testid="`auto-update-toggle-${prim.id}`"
        @toggle="$emit('toggle-auto-update', prim)"
      />
    </div>

    <div class="flex items-center gap-2 mt-auto">
      <button type="button"
        v-if="canCreatePipeline"
        class="flex-1 px-3 py-2 border border-primary/30 hover:border-primary/60"
        @click="$emit('create-pipeline', prim)"
        data-testid="library-create-pipeline"
      >
        {{ $t('views.LibraryView.create_pipeline') }}
      </button>

      <button type="button"
        class="flex-1 px-3 py-2 border border-border bg-background text-foreground text-sm font-medium rounded-lg hover:bg-accent transition-colors"
        @click="$emit('view-details', prim)"
        data-testid="library-view-details"
      >
        {{ $t('views.LibraryView.view_details') }}
      </button>
    </div>
  </BaseCard>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import BaseCard from '../ui/BaseCard.vue'
import ToggleSwitch from '../shared/ToggleSwitch.vue'

export interface LibraryPrimitive {
  id: string
  source: string
  primitive_type: string
  name: string
  description: string | null
  tags: string[]
  forked_from: string | null
  auto_update: boolean
  tier?: 'native' | 'preview' | 'in_dev'
  content_json?: Record<string, unknown>
}

const props = withDefaults(defineProps<{
  prim: LibraryPrimitive
  badge: 'modulo' | 'preview' | 'collection'
  showTags?: boolean
  showAutoUpdate?: boolean
  toggleLoading?: Record<string, boolean>
  adapting?: Record<string, boolean>
}>(), {
  showTags: true,
  showAutoUpdate: false,
  toggleLoading: () => ({}),
  adapting: () => ({}),
})

defineEmits<{
  'create-pipeline': [prim: LibraryPrimitive]

  'view-details': [prim: LibraryPrimitive]
  'toggle-auto-update': [prim: LibraryPrimitive]
}>()

/**
 * FAR-2870: a workflow primitive installs as a pipeline when it carries the
 * same graph the backend builder consumes (`graph_nodes` or the declarative
 * `nodes` shape, one unique id per node, and edges — when present — that
 * connect declared nodes). The Create Pipeline action must be hidden for a
 * workflow the endpoint would reject with a 400, so this mirrors the backend's
 * duplicate-id and dangling-edge checks rather than only the non-empty-id check.
 */
function workflowHasUsableGraph(prim: LibraryPrimitive): boolean {
  const content = prim.content_json
  if (!content || typeof content !== 'object') return false
  const raw = Object.prototype.hasOwnProperty.call(content, 'graph_nodes')
    ? content.graph_nodes
    : content.nodes
  if (!Array.isArray(raw) || raw.length === 0) return false
  const nodeIds = new Set<string>()
  for (const node of raw) {
    if (!node || typeof node !== 'object') return false
    const id = (node as Record<string, unknown>).id
    if (typeof id !== 'string' || id === '' || nodeIds.has(id)) return false
    nodeIds.add(id)
  }
  if (Object.prototype.hasOwnProperty.call(content, 'edges')) {
    const edges = content.edges
    if (!Array.isArray(edges)) return false
    for (const edge of edges) {
      if (!edge || typeof edge !== 'object') return false
      const record = edge as Record<string, unknown>
      const source = record.source ?? record.source_node_id
      const target = record.target ?? record.target_node_id
      if (typeof source !== 'string' || !nodeIds.has(source)) return false
      if (typeof target !== 'string' || !nodeIds.has(target)) return false
    }
  }
  return true
}

const canCreatePipeline = computed(() => {
  const type = props.prim.primitive_type
  if (type === 'pipeline_template' || type === 'composite') return true
  return type === 'workflow' && workflowHasUsableGraph(props.prim)
})

function typeBadgeClass(type: string): string {
  const map: Record<string, string> = {
    pipeline_template: 'badge badge-context-blue',
    workflow: 'badge badge-context-teal',
    agent: 'badge badge-context-purple',
    schema: 'badge badge-context-amber',
    integration: 'badge badge-context-cyan',
    test_fixture: 'badge badge-context-pink',
    composite: 'badge badge-context-green',
    lifecycle_map: 'badge badge-context-blue',
  }
  return map[type] ?? 'badge badge-context-slate'
}
</script>
