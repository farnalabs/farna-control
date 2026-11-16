<template>
  <div
    class="actions-row actions-nowrap flex flex-col items-stretch gap-3 sm:flex-row sm:items-start sm:justify-between sm:gap-4"
    data-testid="action-row"
  >
    <div class="min-w-0 flex-1 break-words" data-testid="action-row-content">
      <slot />
    </div>
    <div
      v-if="$slots.actions"
      class="flex w-full flex-wrap items-center gap-2 sm:w-auto sm:shrink-0 sm:flex-nowrap"
      data-testid="action-row-actions"
    >
      <slot name="actions" />
    </div>
  </div>
</template>
