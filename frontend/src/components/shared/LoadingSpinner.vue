<script setup lang="ts">
import { computed } from 'vue'

/**
 * Page-level spinner, with an inline variant for panels.
 *
 * The defaults reproduce the original markup byte-for-byte (no props = the
 * centred `h-8 w-8` ring inside a `py-16` wrapper). Panels that previously
 * hand-rolled a smaller spinner use `size="sm"` with `:padded="false"` and
 * supply their own vertical spacing, so the primitive never imposes
 * page-level padding on an inline surface.
 */
const props = withDefaults(
  defineProps<{
    size?: 'sm' | 'md' | 'lg'
    padded?: boolean
  }>(),
  { size: 'md', padded: true },
)

/** Whole class strings per size: the `md` entry must stay byte-identical. */
const RING_CLASSES: Record<'sm' | 'md' | 'lg', string> = {
  sm: 'h-5 w-5 animate-spin rounded-full border-2 border-primary border-t-transparent',
  md: 'h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent',
  lg: 'h-12 w-12 animate-spin rounded-full border-4 border-primary border-t-transparent',
}

const ringClass = computed(() => RING_CLASSES[props.size])
</script>

<template>
  <div class="flex items-center justify-center" :class="{ 'py-16': padded }">
    <div :class="ringClass" />
  </div>
</template>
