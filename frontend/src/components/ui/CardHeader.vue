<template>
  <div
    v-if="hasHeaderContent"
    class="card-header flex flex-col items-stretch gap-3 sm:flex-row sm:items-start sm:justify-between sm:gap-4 mb-4"
    :class="divider && 'border-b border-border pb-3'"
    :data-testid="dataTestId"
  >
    <div class="flex min-w-0 flex-1 items-start gap-3">
      <div v-if="$slots.leading" class="shrink-0 pt-0.5">
        <slot name="leading" />
      </div>
      <div class="min-w-0 flex-1">
        <component
          :is="headingLevel"
          v-if="title"
          :class="titleClass"
        >
          {{ title }}
        </component>
        <p
          v-if="description"
          :class="descriptionClass"
        >
          {{ description }}
        </p>
      </div>
    </div>
    <div v-if="$slots.actions" :data-testid="`card-header-actions-${headerUid}`" class="actions-nowrap flex w-full flex-wrap items-center gap-2 sm:w-auto sm:shrink-0 sm:flex-nowrap">
      <slot name="actions" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, useId, useSlots } from 'vue'

const props = withDefaults(defineProps<{
  title?: string
  description?: string
  headingLevel?: 'h2' | 'h3' | 'h4'
  size?: 'md' | 'sm'
  divider?: boolean
  dataTestId?: string
}>(), {
  headingLevel: 'h3',
  size: 'md',
  divider: false,
})

const slots = useSlots()
// FAR-2714: a page can render more than one CardHeader with actions, so the
// actions wrapper needs a per-instance id for Playwright strict-mode getByTestId.
const headerUid = useId()
const hasHeaderContent = computed(() =>
  !!props.title || !!props.description || !!slots.actions || !!slots.leading
)

const titleClass = computed(() =>
  props.size === 'sm' ? 'text-sm font-medium' : 'text-base font-semibold'
)

const descriptionClass = computed(() =>
  props.size === 'sm'
    ? 'mt-1 text-xs text-muted-foreground'
    : 'mt-1 text-sm text-muted-foreground'
)
</script>
