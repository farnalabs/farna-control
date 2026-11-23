<!--
  CollapsibleCard - a native `<details>` card whose whole header row is the
  toggle. The card surface lives on the `<details>` element; the `<summary>` is
  the title (with an optional description) and the default slot is the body, so
  expanding and collapsing stays a real disclosure widget with no JS state.
-->
<template>
  <BaseCard
    as="details"
    :padding="padding"
    :class="props.class"
    :data-testid="dataTestId"
    :open="open"
  >
    <summary
      class="cursor-pointer list-none font-medium [&::-webkit-details-marker]:hidden"
    >
      <span class="inline-flex items-center gap-2">
        <span aria-hidden="true">▸</span>
        {{ title }}
      </span>
      <span
        v-if="description"
        class="ml-2 text-xs font-normal text-muted-foreground"
      >{{ description }}</span>
    </summary>
    <slot />
  </BaseCard>
</template>

<script setup lang="ts">
import BaseCard from './BaseCard.vue'

const props = withDefaults(defineProps<{
  title: string
  description?: string
  open?: boolean
  padding?: 'none' | 'sm' | 'md' | 'lg' | 'xl'
  class?: string
  dataTestId?: string
}>(), {
  padding: 'md',
})
</script>
