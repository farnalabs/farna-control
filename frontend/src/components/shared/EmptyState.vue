<template>
  <div :class="rootClass">
    <slot name="icon">
      <svg
        width="48"
        height="48"
        viewBox="0 0 48 48"
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
        class="mx-auto mb-4 text-muted-foreground/40"
        aria-hidden="true"
        focusable="false"
      >
        <path d="M16,16 L22.9,20 L22.9,28 L16,32 L9.1,28 L9.1,20 Z" stroke="currentColor" stroke-width="1.5" />
        <path d="M32,16 L38.9,20 L38.9,28 L32,32 L25.1,28 L25.1,20 Z" stroke="currentColor" stroke-width="1.5" />
        <line x1="22.9" y1="24" x2="25.1" y2="24" stroke="currentColor" stroke-width="1.5" />
      </svg>
    </slot>
    <p class="text-base font-semibold">{{ title }}</p>
    <p v-if="description" class="mt-1 text-sm text-muted-foreground">
      {{ description }}
    </p>
    <div v-if="$slots.default" class="mt-6">
      <slot />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{
  title: string
  description?: string
  /**
   * `card` (default) owns the card shell — byte-identical to the original
   * component. `bare` drops the `card p-8` root so the empty state can sit
   * inside an existing card/panel without nesting a card in a card; the inner
   * anatomy (icon, title, description, actions, centring) is unchanged.
   */
  variant?: 'card' | 'bare'
}>(), { variant: 'card' })

const rootClass = computed(() =>
  props.variant === 'bare' ? 'text-center' : 'card p-8 text-center',
)
</script>
