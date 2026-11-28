<!--
  Badge - the one semantic status pill.

  `variant` maps onto the semantic colour tokens through the existing
  `.badge` + `.badge-status-*` utility classes in `style.css`, so a migrated
  caller renders the exact pill it rendered before: same padding, radius,
  type scale and line height. Only the colour pair differs per variant.

  Identity / context chips are a DIFFERENT concept and do not use this
  primitive: node types, eval markers, dispatch origin and the
  `badge-context-*` family encode WHAT a thing is (a stable category), not
  whether it is healthy. Their colour must not be collapsed into a semantic
  state.

  The root is a single span, so `class` (e.g. `capitalize`) and
  `data-testid` fall through unchanged - Vue merges a parent's class onto the
  root and passes any other attribute straight through.
-->
<template>
  <span class="badge" :class="variantClass"><slot /></span>
</template>

<script lang="ts">
export type BadgeVariant = 'neutral' | 'success' | 'warning' | 'danger' | 'info'
</script>

<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{
  variant?: BadgeVariant
}>(), {
  variant: 'neutral',
})

// One variant, one pre-existing token class. `info` is the brand-primary tint
// (the only informational tint the palette defines); `--pending` and
// `--preview` are separate states, not a generic info colour.
const VARIANT_CLASS: Record<BadgeVariant, string> = {
  neutral: 'badge-status-muted',
  success: 'badge-status-success',
  warning: 'badge-status-warning',
  danger: 'badge-status-destructive',
  info: 'badge-status-primary',
}

const variantClass = computed(() => VARIANT_CLASS[props.variant])
</script>
