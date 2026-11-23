<template>
  <BaseCard
    :variant="variant"
    :padding="padding"
    :elevation="elevation"
    :data-testid="dataTestId"
    :class="props.class"
  >
    <CardHeader
      v-if="hasHeader"
      :title="title"
      :description="description"
      :heading-level="headingLevel"
      :size="size"
      :data-testid="dataTestId ? `${dataTestId}-header` : undefined"
    >
      <template v-if="$slots.headerActions" #actions>
        <slot name="headerActions" />
      </template>
    </CardHeader>

    <CardContent
      v-if="$slots.default"
      :data-testid="dataTestId ? `${dataTestId}-content` : undefined"
    >
      <slot />
    </CardContent>

    <div
      v-if="$slots.footer"
      class="card-footer mt-4 pt-4 border-t border-border"
      :data-testid="dataTestId ? `${dataTestId}-footer` : undefined"
    >
      <slot name="footer" />
    </div>
  </BaseCard>
</template>

<script setup lang="ts">
import BaseCard from './BaseCard.vue'
import CardHeader from './CardHeader.vue'
import CardContent from './CardContent.vue'
import { computed, useSlots } from 'vue'

const props = withDefaults(defineProps<{
  title?: string
  description?: string
  headingLevel?: 'h2' | 'h3' | 'h4'
  size?: 'md' | 'sm'
  variant?: 'default' | 'bordered' | 'muted' | 'warning' | 'context'
  padding?: 'none' | 'sm' | 'md' | 'lg' | 'xl'
  elevation?: 'none' | 'sm'
  class?: string
  dataTestId?: string
}>(), {
  headingLevel: 'h3',
  size: 'md',
  variant: 'default',
  padding: 'md',
  elevation: 'none',
})

const slots = useSlots()
const hasHeader = computed(() =>
  !!props.title || !!props.description || !!slots.headerActions
)
</script>
