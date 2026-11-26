<template>
  <div class="relative" :class="rootClass">
    <svg
      class="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      aria-hidden="true"
      focusable="false"
    >
      <circle cx="11" cy="11" r="8" />
      <path d="m21 21-4.3-4.3" />
    </svg>
    <Input
      v-bind="controlAttrs"
      :model-value="modelValue"
      type="text"
      inputmode="search"
      :class="[
        'w-full rounded-lg border border-input bg-background py-2 pl-9! text-sm focus:outline-none focus:ring-2 focus:ring-ring',
        clearable && hasValue ? 'pr-9!' : 'pr-3',
        inputClass,
      ]"
      @update:model-value="onUpdate"
    />
    <button
      v-if="clearable && hasValue"
      type="button"
      :aria-label="clearLabel"
      class="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      @click="onClear"
    >
      <svg
        class="h-4 w-4"
        xmlns="http://www.w3.org/2000/svg"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        aria-hidden="true"
        focusable="false"
      >
        <path d="M18 6 6 18" />
        <path d="m6 6 12 12" />
      </svg>
    </button>
  </div>
</template>

<script setup lang="ts">
import { computed, useAttrs } from 'vue'
import Input from './Input.vue'

/**
 * The single search-field shape (FAR-2784), composed on `Input`.
 *
 * It owns only presentation and the clear affordance: the caller keeps its own
 * timing, so no value is debounced here. Placed inside a `Field` it inherits
 * that Field's id, `aria-describedby`, `aria-invalid` and `required` wiring via
 * `Input`; stand-alone it takes its accessible name from `aria-label`.
 *
 * `clearable` is opt-in and adds a clear button that empties the value and
 * emits `clear`. `clearLabel` is the button's accessible name and is required
 * whenever `clearable` is set (there is deliberately no hard-coded default, so
 * every caller supplies a translated string).
 *
 * The padding that clears the icon (`pl-9!`) and the clear button (`pr-9!`)
 * must stay `!important`: PrimeVue injects its `.p-inputtext` padding as
 * unlayered CSS, which beats ordinary Tailwind utilities (they live in
 * `@layer utilities`), leaving the icon drawn over the text (FAR-2809).
 *
 * `class` styles the wrapper (the icon and button are positioned against it);
 * `inputClass` adds control-level classes such as a width or font.
 */
defineOptions({ name: 'SearchInput', inheritAttrs: false })

const props = withDefaults(
  defineProps<{
    modelValue?: string | number | null
    /** Show a clear button while the field has a value. */
    clearable?: boolean
    /** Accessible name for the clear button. Required when `clearable` is set. */
    clearLabel?: string
    /** Extra classes for the control itself (width, font, ...). */
    inputClass?: string
  }>(),
  {
    modelValue: '',
    clearable: false,
    clearLabel: undefined,
    inputClass: undefined,
  },
)

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void
  (e: 'clear'): void
}>()

const attrs = useAttrs()

// `class` belongs to the wrapper; every other attribute (data-testid,
// aria-label, placeholder, disabled, listeners, ...) goes to the control.
const rootClass = computed(() => attrs.class)
const controlAttrs = computed(() => {
  const { class: _class, ...rest } = attrs
  return rest
})

const hasValue = computed(() => String(props.modelValue ?? '') !== '')

function onUpdate(value: string | number | null): void {
  emit('update:modelValue', value == null ? '' : String(value))
}

function onClear(): void {
  emit('update:modelValue', '')
  emit('clear')
}
</script>
