<template>
  <div
    v-bind="rootAttrs"
    role="group"
    :class="props.class"
    :aria-label="ariaLabel"
    :aria-labelledby="ariaLabelledBy"
  >
    <button
      v-for="option in options"
      :key="option.value"
      type="button"
      :class="[optionClass, isSelected(option) ? selectedClass : unselectedClass]"
      :aria-pressed="isSelected(option) ? 'true' : 'false'"
      :data-testid="option.testid"
      :disabled="disabled || option.disabled"
      @click="select(option)"
    >
      {{ option.label }}
    </button>
  </div>
</template>

<script setup lang="ts">
/*
 * The ONE shared single-select segmented control (FAR-2804).
 *
 * A set of mutually exclusive view switches that filters what the surrounding
 * surface shows (organisation vs instance scope) but is neither a tablist (the
 * content is the same page) nor a radio group (nothing is submitted as a form
 * value). The WAI-ARIA shape for that is a named `role="group"` whose options
 * are toggle buttons carrying `aria-pressed`, which is what this renders.
 *
 * Options are NATIVE `<button type="button">` elements, so Tab moves between
 * them, Enter and Space activate them, and the focused option is visible
 * through the site's own focus classes - no manual keydown handling to get
 * wrong.
 *
 * `optionClass` is the shared option layout; `selectedClass` and
 * `unselectedClass` are the two visual states, so an adopting site keeps its
 * existing classes exactly. Give the group an accessible name with `ariaLabel`
 * or `ariaLabelledBy`.
 */
import { computed, useAttrs } from 'vue'

export interface SegmentedOption<V extends string = string> {
  value: V
  label: string
  /** `data-testid` on the option button. */
  testid?: string
  disabled?: boolean
}

defineOptions({ name: 'UiSegmentedControl', inheritAttrs: false })

const props = withDefaults(defineProps<{
  modelValue: string
  options: SegmentedOption[]
  /** Accessible name of the group. */
  ariaLabel?: string
  /** Id of the element that names the group. */
  ariaLabelledBy?: string
  disabled?: boolean
  /** Classes shared by every option button. */
  optionClass?: string
  /** Classes for the pressed option. */
  selectedClass?: string
  /** Classes for the unpressed options. */
  unselectedClass?: string
  /** Root class. Declared so `class` lands on the group container. */
  class?: string
}>(), {
  disabled: false,
})

const emit = defineEmits<{ (e: 'update:modelValue', value: string): void }>()

const attrs = useAttrs()

// Every attribute the caller passes (`data-testid`, `id`, other aria-*) belongs
// on the group root, not lost because `inheritAttrs` is off.
const rootAttrs = computed(() => ({ ...attrs }))

function isSelected(option: SegmentedOption): boolean {
  return props.modelValue === option.value
}

function select(option: SegmentedOption): void {
  if (isSelected(option)) return
  emit('update:modelValue', option.value)
}
</script>
