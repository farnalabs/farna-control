<!--
  CardTable - an edge-to-edge table region inside a card.

  The card root must use `overflow-hidden` so the table's corners follow the
  card's bottom border-radius (BaseCard/DataCard with `class="overflow-hidden"`).
  The scroll container is focusable and, when `label` is supplied, carries an
  accessible name so keyboard users can scroll it.

  Extra attributes (`class`, `role`, `data-*`) are forwarded to the SCROLL
  CONTAINER, not this component's root, because the scroll container IS the
  table region a caller styles: a caller that already owns the shell passes its
  shell class here (e.g. `class="table-wrapper ..."`), and `describedby` keeps
  an sr-only instruction paragraph attached to the same focusable element.
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
      v-bind="$attrs"
      class="overflow-x-auto"
      tabindex="0"
      :aria-label="label"
      :aria-describedby="describedby"
    >
      <slot />
    </div>
  </div>
</template>

<script setup lang="ts">
defineOptions({ inheritAttrs: false })

defineProps<{
  heading?: string
  label?: string
  /** id(s) of an always-rendered description of this scroll region. */
  describedby?: string
}>()
</script>
