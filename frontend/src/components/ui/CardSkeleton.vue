<template>
  <BaseCard
    :data-testid="dataTestId"
    aria-hidden="true"
    class="animate-pulse"
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

withDefaults(defineProps<{
  lines?: number
  heading?: boolean
  dataTestId?: string
}>(), {
  lines: 2,
  heading: true,
})
</script>
