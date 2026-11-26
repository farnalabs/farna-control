<template>
  <button
    v-bind="attrs"
    type="button"
    :class="props.class"
    :aria-controls="resolvedControls"
    :aria-expanded="open ? 'true' : 'false'"
    :disabled="disabled"
    @click="onClick"
  >
    <template v-if="$slots.trigger">
      <slot
        name="trigger"
        :open="open"
        :chevron-class="chevronClass"
        :rotation-class="open ? rotationClass : ''"
      />
    </template>
    <template v-else>
      <component
        :is="chevronIcon"
        :class="[chevronClass, open ? rotationClass : '']"
        aria-hidden="true"
      />
      <slot name="label">{{ label }}</slot>
    </template>
  </button>
  <div
    v-if="$slots.default && open"
    :id="resolvedControls"
    :class="regionClass"
  >
    <slot />
  </div>
</template>

<script lang="ts">
/**
 * Module-scoped so every instance on the page gets a unique region id. A
 * counter inside `<script setup>` runs once per instance and would repeat the
 * id, making one trigger's `aria-controls` resolve to another disclosure's
 * region (mirrors `Field.vue` / `RadioGroup.vue`).
 */
let disclosureCounter = 0

export function nextDisclosureId(): string {
  disclosureCounter += 1
  return `disclosure-region-${disclosureCounter}`
}
</script>

<script setup lang="ts">
/*
 * The ONE shared disclosure (FAR-2804).
 *
 * A controlled disclosure: the caller owns the open state (`open` in,
 * `update:open` out) so sites keep their existing expand/collapse handlers and
 * any side effect (loading the expanded payload, clearing a selection).
 *
 * The trigger is a real `<button type="button">` carrying `aria-expanded` and
 * `aria-controls`, so keyboard activation is the browser's own and the state is
 * announced without a manual keydown handler. The primitive owns the chevron
 * and its rotation (right rotates 90 degrees open, down rotates 180); pass
 * `chevron="down"` for the sites whose resting icon points down.
 *
 * Two ways to render the region:
 *   - the default slot is the region; it renders (only while open) with the id
 *     `aria-controls` points at, so the wiring can never drift;
 *   - no default slot: the region lives elsewhere in the caller's layout (a
 *     sibling table row, a card body). Pass `controls` with that region's id and
 *     the trigger still advertises it.
 *
 * A row-shaped trigger (a whole card header rather than just a chevron and a
 * label) uses the `trigger` slot. It is scoped with `open`, `chevron-class` and
 * `rotation-class` so the slot can keep the site's own icon classes and still
 * have the primitive drive the rotation.
 *
 * Attribute fall-through: `class` lays out the trigger button (the visible
 * control), and every other attribute (`data-testid`, `aria-label`, `id`,
 * listeners) lands on it too. Set `stopPropagation` when the trigger sits inside
 * a clickable row, so toggling does not also fire the row handler.
 */
import { computed, onBeforeMount, useAttrs, useSlots } from 'vue'
import { ChevronDown, ChevronRight } from '@lucide/vue'

defineOptions({ name: 'UiDisclosure', inheritAttrs: false })

const props = withDefaults(defineProps<{
  /** Controlled open state. */
  open?: boolean
  /**
   * Explicit id of the region the trigger controls, when the region is rendered
   * outside the default slot.
   */
  controls?: string
  /** Visible label rendered beside the chevron (ignored by the trigger slot). */
  label?: string
  /** Resting chevron: right (rotates 90 on open) or down (rotates 180). */
  chevron?: 'right' | 'down'
  /** Classes for the built-in chevron (size, colour). */
  chevronClass?: string
  /** Classes for the default-slot region wrapper. */
  regionClass?: string
  disabled?: boolean
  /** Stop the trigger's click from bubbling (a trigger inside a clickable row). */
  stopPropagation?: boolean
  /** Trigger layout classes. Declared so `class` lands on the button. */
  class?: string
}>(), {
  open: false,
  chevron: 'right',
  chevronClass: 'h-4 w-4 transition-transform',
  stopPropagation: false,
})

const emit = defineEmits<{ (e: 'update:open', value: boolean): void }>()

const attrs = useAttrs()
const slots = useSlots()

const generatedControls = nextDisclosureId()
const resolvedControls = computed(() => props.controls || generatedControls)

const chevronIcon = computed(() => (props.chevron === 'down' ? ChevronDown : ChevronRight))
const rotationClass = computed(() => (props.chevron === 'down' ? 'rotate-180' : 'rotate-90'))

onBeforeMount(() => {
  if (!import.meta.env.DEV) return
  if (slots.default) return
  if (props.controls) return
  console.warn(
    '[Disclosure] no default-slot region and no `controls` id: `aria-controls` cannot resolve. '
      + 'Render the region in the default slot or pass the region id as `controls`.',
  )
})

function onClick(event: MouseEvent): void {
  if (props.stopPropagation) event.stopPropagation()
  emit('update:open', !props.open)
}
</script>
