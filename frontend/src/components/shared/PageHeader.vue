<template>
  <header v-bind="$attrs" :data-testid="dataTestId || $attrs['data-testid']" class="page-header page-header--display flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
    <div class="flex min-w-0 flex-1 flex-col">
      <button type="button" v-if="backLink" class="mb-2 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground transition-colors" @click="$router.push(backLink)">
        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true" focusable="false"><polyline points="15 18 9 12 15 6"/></svg>
        Back
      </button>
      <!-- FAR-2723: a header rendered above a nested page that has its own
           title is a context label, not the document's heading. `titleAs="p"`
           keeps the exact same visual treatment but stops the element being a
           second <h1> (and the focus target) on those routes. -->
      <component
        :is="titleAs"
        class="text-2xl font-semibold tracking-tight focus:outline-none"
        :tabindex="titleAs === 'h1' ? -1 : undefined"
        :data-page-heading="titleAs === 'h1' ? '' : undefined"
      >{{ title }}</component>
      <p v-if="subtitle" class="mt-1 text-sm text-muted-foreground">{{ subtitle }}</p>
      <div v-if="$slots.default" class="mt-4">
        <slot />
      </div>
    </div>
    <div v-if="$slots.right" data-testid="page-header-right" class="actions-nowrap flex w-full flex-wrap items-center gap-2 sm:w-auto sm:shrink-0 sm:flex-nowrap">
      <slot name="right" />
    </div>
  </header>
  <PageTabs v-if="tabs?.length" :tabs="tabs" />
</template>

<script setup lang="ts">
import PageTabs, { type Tab } from '../PageTabs.vue'

withDefaults(
  defineProps<{
    title: string
    subtitle?: string
    backLink?: string
    dataTestId?: string
    tabs?: Tab[]
    /**
     * The element the title renders as. Defaults to `h1`: the page's one
     * heading, and the route-change focus target (`data-page-heading`).
     * A header shown above a nested page that renders its own title passes
     * `p` so that nested route keeps exactly one `<h1>` (FAR-2723).
     */
    titleAs?: 'h1' | 'p'
  }>(),
  { titleAs: 'h1' },
)
</script>

<style scoped>
.page-header {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

@media (min-width: 640px) {
  .page-header {
    flex-direction: row;
    align-items: flex-start;
    justify-content: space-between;
  }
}
</style>
