<template>
  <component
    :is="to ? 'router-link' : 'div'"
    :to="to"
    class="block"
    :data-testid="dataTestId"
  >
    <BaseCard
      variant="bordered"
      padding="md"
      :class="`${to ? 'card-hover' : ''} ${props.class ?? ''}`"
    >
      <div class="flex items-center gap-3">
        <div
          v-if="$slots.icon"
          class="flex h-9 w-9 items-center justify-center rounded-lg"
          :class="iconBgClass"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            aria-hidden="true"
          >
            <slot name="icon" />
          </svg>
        </div>
        <div class="min-w-0 flex-1">
          <p class="text-sm font-medium text-muted-foreground">{{ label }}</p>
          <div v-if="hasValue" class="flex items-baseline gap-2">
            <p class="text-2xl font-semibold tabular-nums" :class="valueClass">{{ value }}</p>
            <span
              v-if="objectDeltaPct != null"
              class="inline-flex items-center gap-0.5 text-xs font-medium"
              :class="deltaClass"
            >
              <span aria-hidden="true">{{ deltaArrow }}</span>{{ deltaAbsText }} ({{ deltaPctText }})
            </span>
            <span
              v-else-if="objectDeltaAbs != null && noBaselineLabel"
              class="inline-flex items-center gap-0.5 text-xs font-medium"
              :class="deltaAbsClass"
              data-testid="stat-no-baseline"
              :title="noBaselineLabel"
            >
              <span aria-hidden="true">{{ deltaAbsArrow }}</span>{{ deltaAbsText }}
              <span class="sr-only">{{ noBaselineLabel }}</span>
            </span>
          </div>
        </div>
      </div>
      <!-- Metric-tile delta row: an explicit absolute delta and/or signed
           percentage supplied as scalar props. -->
      <div v-if="explicitHasDelta" class="mt-1 flex items-center gap-1.5">
        <span
          class="inline-flex items-center gap-0.5 text-xs font-medium"
          :class="explicitDeltaClass"
        >
          <span aria-hidden="true">{{ explicitDeltaArrow }}</span>
          <template v-if="explicitDeltaAbsText && explicitDeltaPctText">{{ explicitDeltaAbsText }} ({{ explicitDeltaPctText }})</template>
          <template v-else-if="explicitDeltaAbsText">{{ explicitDeltaAbsText }}</template>
          <template v-else>{{ explicitDeltaPctText }}</template>
        </span>
      </div>
      <div v-else-if="progress && !objectHasRenderedDelta" class="mt-1">
        <!-- Progress bar, shown only when there is no delta to display. -->
        <div class="h-2 w-full overflow-hidden rounded-full bg-muted">
          <div
            class="h-full rounded-full transition-all duration-500"
            :class="progressBarClass"
            :style="{ width: `${progressPercent}%` }"
          />
        </div>
      </div>
      <!-- Rich metric body (a chart, a secondary breakdown) belongs below the
           headline value. A plain stat leaves it empty. -->
      <slot name="content" />
    </BaseCard>
  </component>
</template>

<script setup lang="ts">
import BaseCard from './BaseCard.vue'
import { computed } from 'vue'
import {
  formatDeltaAbs,
  formatDeltaPct,
  useDeltaPctText,
  useDeltaArrow,
  useDeltaClass,
} from '../../composables/useDeltaFormatting'

export interface StatDelta {
  current?: number | null
  previous?: number | null
  delta_pct?: number | null
}

export interface StatProgress {
  value: number
  max: number
  color?: 'primary' | 'success' | 'warning' | 'destructive'
}

const props = defineProps<{
  label: string
  value?: number | string
  color?: 'primary' | 'success' | 'warning' | 'destructive' | 'muted'
  to?: string
  /**
   * A structured period comparison (current/previous/delta_pct), or a plain
   * absolute delta. The structured form drives the inline headline delta; the
   * scalar form drives the metric-tile delta row.
   */
  delta?: StatDelta | number | null
  /** Explicit signed percentage delta (metric tile). */
  deltaPct?: number | null
  /** Progress bar shown when there is no delta to display (metric tile). */
  progress?: StatProgress | null
  noBaselineLabel?: string
  inverted?: boolean
  neutralTrend?: boolean
  class?: string
  dataTestId?: string
}>()

const hasValue = computed(() => props.value !== undefined && props.value !== null && props.value !== '')

const iconBgClass = computed(() => {
  const map: Record<string, string> = {
    primary: 'bg-primary/10 text-primary-text',
    success: 'bg-success/10 text-success-text',
    warning: 'bg-warning/10 text-warning-text',
    destructive: 'bg-destructive/10 text-destructive-text',
    muted: 'bg-muted text-muted-foreground',
  }
  return map[props.color ?? 'primary']
})

const valueClass = computed(() => {
  const map: Record<string, string> = {
    success: 'text-success-text',
    warning: 'text-warning-text',
    destructive: 'text-destructive',
    muted: 'text-muted-foreground',
  }
  return map[props.color ?? 'primary'] ?? ''
})

// Scalar (metric-tile) delta. A plain numeric `delta` is the absolute change;
// an explicit `deltaPct` is the signed percentage. These keep the metric-tile
// formatting contract: signed percentages and an abs-only delta row.
const numericDelta = computed(() =>
  typeof props.delta === 'number' && Number.isFinite(props.delta) ? props.delta : null,
)

const explicitDeltaPct = computed(() => {
  const d = props.deltaPct
  return typeof d === 'number' && Number.isFinite(d) ? d : null
})

const explicitHasDelta = computed(() => explicitDeltaPct.value != null || numericDelta.value != null)

const explicitDeltaAbsText = computed(() => formatDeltaAbs(numericDelta.value))
const explicitDeltaPctText = computed(() => formatDeltaPct(explicitDeltaPct.value, true))
const explicitDeltaArrow = useDeltaArrow(computed(() => explicitDeltaPct.value ?? numericDelta.value))
const explicitDeltaClass = useDeltaClass(computed(() => explicitDeltaPct.value ?? numericDelta.value))

// Structured (headline-stat) delta.
const objectDelta = computed<StatDelta | null>(() =>
  props.delta != null && typeof props.delta === 'object' ? props.delta : null,
)

const objectDeltaPct = computed(() => {
  const d = objectDelta.value?.delta_pct
  return typeof d === 'number' && Number.isFinite(d) ? d : null
})

const objectDeltaAbs = computed(() => {
  const { current, previous } = objectDelta.value ?? {}
  if (typeof current !== 'number' || typeof previous !== 'number') return null
  if (!Number.isFinite(current) || !Number.isFinite(previous)) return null
  return current - previous
})

const objectHasRenderedDelta = computed(
  () => objectDeltaPct.value != null || (objectDeltaAbs.value != null && Boolean(props.noBaselineLabel)),
)

const deltaAbsText = computed(() => formatDeltaAbs(objectDeltaAbs.value))
const deltaPctText = useDeltaPctText(objectDeltaPct)
const deltaArrow = useDeltaArrow(objectDeltaPct)
const deltaAbsArrow = useDeltaArrow(objectDeltaAbs)
const inverted = computed(() => Boolean(props.inverted))
const neutralTrend = computed(() => Boolean(props.neutralTrend))
const deltaClass = useDeltaClass(objectDeltaPct, inverted, neutralTrend)
const deltaAbsClass = useDeltaClass(objectDeltaAbs, inverted, neutralTrend)

const progressPercent = computed(() => {
  const progress = props.progress
  if (!progress || !Number.isFinite(progress.max) || progress.max <= 0) return 0
  if (!Number.isFinite(progress.value)) return 0
  return Math.min(Math.max(progress.value, 0), progress.max) / progress.max * 100
})

const progressBarClass = computed(() => {
  if (!props.progress) return 'bg-primary'

  // An explicit progress colour is always respected. When no colour is
  // requested, thresholds provide the default severity treatment.
  const color = props.progress.color
  if (color) {
    switch (color) {
      case 'success': return 'bg-success'
      case 'warning': return 'bg-warning'
      case 'destructive': return 'bg-destructive'
      case 'primary': return 'bg-primary'
    }
  }

  const pct = progressPercent.value
  if (pct >= 100) return 'bg-destructive'
  if (pct >= 75) return 'bg-warning'
  return 'bg-primary'
})
</script>

<style scoped>
.stagger-item:nth-child(1) { animation-delay: 0ms; }
.stagger-item:nth-child(2) { animation-delay: 40ms; }
.stagger-item:nth-child(3) { animation-delay: 80ms; }
.stagger-item:nth-child(4) { animation-delay: 120ms; }
.stagger-item:nth-child(5) { animation-delay: 160ms; }
.stagger-item:nth-child(6) { animation-delay: 200ms; }
</style>
