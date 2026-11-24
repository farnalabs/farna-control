<template>
  <div class="flex items-center gap-2">
    <button
      type="button"
      :disabled="!hasPrev"
      :data-testid="prevTestid"
      :class="buttonClass"
      @click="$emit('prev')"
    >
      {{ resolvedPrevLabel }}
    </button>
    <span
      v-if="hasPageLabel"
      :data-testid="statusTestid"
      aria-live="polite"
      class="text-sm text-muted-foreground"
    >{{ pageLabel }}</span>
    <button
      type="button"
      :disabled="!hasNext"
      :data-testid="nextTestid"
      :class="buttonClass"
      @click="$emit('next')"
    >
      {{ resolvedNextLabel }}
    </button>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

/**
 * Shared pagination control (FAR-2709).
 *
 * The component owns presentation only: the page owns the slice, the cursor
 * stack or the offset, and passes the two end-of-list booleans. `pageLabel` is
 * already-localised text (for example "Page 3"); pass `null`/omit it when the
 * page has no positional label. Both controls keep a stable `data-testid` so
 * adopting pages can preserve the ids their e2e/BDD tests and the product map
 * already use.
 */
const props = withDefaults(defineProps<{
  hasPrev: boolean
  hasNext: boolean
  pageLabel?: string | null
  prevLabel?: string
  nextLabel?: string
  prevTestid?: string
  nextTestid?: string
  statusTestid?: string
}>(), {
  pageLabel: null,
  prevLabel: '',
  nextLabel: '',
  prevTestid: 'pagination-prev',
  nextTestid: 'pagination-next',
  statusTestid: 'pagination-status',
})

defineEmits<{ prev: []; next: [] }>()

// The file keeps its contract name (`Pagination.vue`) while the runtime
// component name satisfies vue/multi-word-component-names, matching how the
// other single-word shared components (e.g. Sparkline.vue) do it.
defineOptions({ name: 'PaginationControls' })

const { t } = useI18n()

// Single source for the canonical secondary-outlined treatment shared by both
// controls, so the two can never drift apart.
const buttonClass =
  'rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent ' +
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ' +
  'disabled:opacity-30 disabled:cursor-not-allowed'

const hasPageLabel = computed(() => props.pageLabel !== null && props.pageLabel !== '')
const resolvedPrevLabel = computed(() => props.prevLabel || t('common.previous'))
const resolvedNextLabel = computed(() => props.nextLabel || t('common.next'))
</script>
