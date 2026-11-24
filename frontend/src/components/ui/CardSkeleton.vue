<template>
  <BaseCard
    :data-testid="dataTestId"
    :class="`animate-pulse ${props.class ?? ''}`"
    aria-hidden="true"
  >
    <div v-if="heading" class="mb-3 h-5 w-1/3 rounded bg-muted" />
    <div class="space-y-2">
      <div
        v-for="line in lines"
        :key="line"
        class="h-4 rounded bg-muted"
        :class="line === lines && lines > 1 ? 'w-2/3' : 'w-full'"
      />
    </div>
  </BaseCard>
</template>

<script setup lang="ts">
import BaseCard from './BaseCard.vue'

const props = withDefaults(defineProps<{
  lines?: number
  heading?: boolean
  // Grid cells and other layouts style the skeleton container, so the caller
  // can pass layout classes (height, flex) without wrapping it.
  class?: string
  dataTestId?: string
}>(), {
  lines: 2,
  heading: true,
})
</script>
