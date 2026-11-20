<template>
  <div class="table-wrapper">
    <!-- The live region announces only the loading status text; the loading
         placeholder table is layout, not phrasing content, so it is a sibling
         rather than a child of <output> (invalid content model). `aria-busy`
         must NOT sit on the live region's ancestor: AT holds the announcement
         until the busy wrapper clears (and this wrapper unmounts instead), so
         it belongs on the placeholder table it actually describes. -->
    <output class="sr-only" aria-live="polite">{{ ariaLabel }}</output>
    <table class="w-full text-left text-sm" aria-busy="true">
      <thead>
        <tr>
          <th
            v-for="col in columns"
            :key="col"
            class="table-header"
            :class="col === columns ? 'text-right' : 'text-left'"
          >
            <span
              class="inline-block h-3 w-24 animate-pulse rounded bg-muted"
              :class="col === columns ? 'ml-auto' : ''"
            />
          </th>
        </tr>
      </thead>
      <tbody class="divide-y divide-border">
        <tr v-for="row in rows" :key="row">
          <td
            v-for="col in columns"
            :key="col"
            class="table-cell"
            :class="col === columns ? 'text-right' : 'text-left'"
          >
            <span
              class="inline-block h-4 w-full animate-pulse rounded bg-muted"
              :class="col === columns ? 'ml-auto' : ''"
            />
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup lang="ts">
// Shared table loading skeleton (ux-conformance STATE-3 + VIS-3). The `columns`
// prop is the single source of truth for the placeholder column count so a
// caller can derive it from the same array that renders its real <th> row and
// the placeholder can never silently desync from the real layout.
withDefaults(
  defineProps<{
    columns: number
    rows?: number
    ariaLabel?: string
  }>(),
  {
    rows: 5,
    ariaLabel: undefined,
  },
)
</script>
