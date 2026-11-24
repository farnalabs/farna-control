<!--
  TableCard - a card whose primary content is a table.

  It is a DataCard (title/description header + optional header actions) with the
  table in its body. A caller whose table already owns its own scroll region (a
  DataTable component, or a raw table inside `.table-wrapper`) passes neither
  `heading` nor `tableLabel`: the table then renders untouched, so no second
  focusable scroll region is nested inside the first. Supplying `heading` or
  `tableLabel` opts into the edge-to-edge CardTable region (heading strip and a
  labelled keyboard-scrollable viewport) for a bare table.
-->
<template>
  <DataCard
    :as="as"
    :title="title"
    :description="description"
    :heading-level="headingLevel"
    :size="size"
    :variant="variant"
    :padding="effectivePadding"
    :elevation="elevation"
    :pressable="pressable"
    :unavailable="unavailable"
    :class="props.class"
    :data-testid="dataTestId"
  >
    <template v-if="$slots.headerActions" #headerActions>
      <slot name="headerActions" />
    </template>

    <CardTable
      v-if="heading || tableLabel"
      :heading="heading"
      :label="tableLabel"
    >
      <slot />
    </CardTable>
    <slot v-else />
  </DataCard>
</template>

<script setup lang="ts">
import { computed, useSlots } from 'vue'
import DataCard from './DataCard.vue'
import CardTable from './CardTable.vue'

type CardElement = 'div' | 'section' | 'form' | 'details' | 'article' | 'aside' | 'nav' | 'li'

const props = withDefaults(defineProps<{
  title?: string
  description?: string
  headingLevel?: 'h2' | 'h3' | 'h4'
  size?: 'md' | 'sm'
  variant?: 'default' | 'bordered' | 'muted' | 'warning' | 'context'
  padding?: 'none' | 'sm' | 'md' | 'lg' | 'xl'
  elevation?: 'none' | 'sm'
  pressable?: boolean
  unavailable?: boolean
  // Optional strip above the table region.
  heading?: string
  // Accessible name for the CardTable scroll region.
  tableLabel?: string
  as?: CardElement
  class?: string
  dataTestId?: string
}>(), {
  headingLevel: 'h3',
  size: 'md',
  variant: 'default',
  elevation: 'none',
  pressable: false,
  unavailable: false,
  as: 'div',
})

const slots = useSlots()

const hasHeader = computed(() => !!props.title || !!props.description || !!slots.headerActions)

// A titled card keeps the usual inset; a bare table card is edge-to-edge, which
// is what the CardTable region is designed for.
const effectivePadding = computed(() => {
  if (props.padding) return props.padding
  return hasHeader.value ? 'md' : 'none'
})
</script>
