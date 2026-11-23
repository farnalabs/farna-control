<!--
  CardTable - an edge-to-edge table region inside a card.

  The card root must use `overflow-hidden` so the table's corners follow the
  card's bottom border-radius (BaseCard/DataCard with `class="overflow-hidden"`).
  The scroll container is focusable and, when `label` is supplied, carries an
  accessible name so keyboard users can scroll it.
-->
<template>
  <div>
    <div
      v-if="heading"
      class="border-b bg-muted/30 px-4 py-2 text-xs font-medium text-muted-foreground"
    >
      {{ heading }}
    </div>
    <div
      class="overflow-x-auto"
      tabindex="0"
      :aria-label="label"
    >
      <slot />
    </div>
  </div>
</template>

<script setup lang="ts">
defineProps<{
  heading?: string
  label?: string
}>()
</script>
