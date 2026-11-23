<template>
  <div class="card-content" :class="paddingClass">
    <slot />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{
  // The card root already insets its contents, so the body region adds no
  // padding of its own by default.
  padding?: 'none' | 'sm' | 'md'
}>(), {
  padding: 'none',
})

const paddingClass = computed(() => {
  switch (props.padding) {
    case 'sm':
      return 'p-3'
    case 'md':
      return 'p-4'
    case 'none':
    default:
      return 'p-0'
  }
})
</script>
