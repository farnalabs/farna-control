<template>
  <component
    :is="as"
    :class="[
      'card',
      'card-base',
      variantClass,
      paddingClass,
      elevationClass,
      stateClass,
      props.class,
    ]"
    :data-testid="dataTestId"
  >
    <slot name="header" />
    <slot />
    <slot name="footer" />
  </component>
</template>

<script setup lang="ts">
import { computed } from 'vue'

type CardElement = 'div' | 'section' | 'form' | 'details' | 'article' | 'aside' | 'nav' | 'li'

const props = withDefaults(defineProps<{
  variant?: 'default' | 'bordered' | 'muted' | 'warning' | 'context'
  padding?: 'none' | 'sm' | 'md' | 'lg' | 'xl'
  elevation?: 'none' | 'sm'
  pressable?: boolean
  unavailable?: boolean
  // The card surface is a visual primitive, not an element choice: a caller
  // that needs section/form/details semantics keeps it while still getting the
  // shared surface.
  as?: CardElement
  class?: string
  dataTestId?: string
}>(), {
  variant: 'default',
  padding: 'md',
  elevation: 'none',
  pressable: false,
  unavailable: false,
  as: 'div',
})

const variantClass = computed(() => {
  switch (props.variant) {
    // 'bordered' is a deprecated alias of 'default' - the `.card` base class
    // already supplies the border, so the two render identically.
    case 'bordered':
      return 'bg-card'
    case 'muted':
      return 'bg-muted/50'
    case 'warning':
      return 'bg-card border-warning/30'
    case 'context':
      return 'bg-primary/5 border border-primary/20'
    case 'default':
    default:
      return 'bg-card'
  }
})

const paddingClass = computed(() => {
  switch (props.padding) {
    case 'none':
      return 'p-0'
    case 'sm':
      return 'p-3'
    case 'md':
      return 'p-4'
    case 'lg':
      return 'p-6'
    case 'xl':
      return 'p-8'
    default:
      return 'p-4'
  }
})

const elevationClass = computed(() => {
  switch (props.elevation) {
    case 'sm':
      return 'shadow-sm'
    case 'none':
    default:
      return ''
  }
})

const stateClass = computed(() => ({
  'card-hover cursor-pointer transition-colors hover:bg-accent/30': props.pressable,
  'border-dashed': props.unavailable,
}))
</script>

<style scoped>
.card-base {
  border-radius: var(--radius-lg);
  color: hsl(var(--card-foreground));
}
</style>
