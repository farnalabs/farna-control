<script lang="ts">
/**
 * One radio option. `value` is the persisted setting value; the rest is
 * presentation the primitive renders unless a site needs richer content.
 */
export interface RadioOption<V extends string | number = string> {
  value: V
  label: string
  description?: string
  /** Small inline badge next to the label (e.g. "Beta"). */
  badge?: string
  /** Explicit control id, so a site can keep an existing `#id` association. */
  id?: string
  disabled?: boolean
  /** `data-testid` on the native radio input. */
  testid?: string
}

/**
 * Module-scoped so the generated name is unique across EVERY instance on the
 * page. A counter inside `<script setup>` would run once per instance and give
 * every group the same name, so two groups would share one native radio name
 * and selecting in one would clear the other. Mirrors `Field.vue`'s
 * `nextFieldId` (FAR-2792 review).
 */
let radioGroupCounter = 0

export function nextRadioGroupName(): string {
  radioGroupCounter += 1
  return `radio-group-${radioGroupCounter}`
}
</script>

<script setup lang="ts" generic="V extends string | number">
/*
 * The ONE shared mutually-exclusive choice control (FAR-2792).
 *
 * Native `<input type="radio">` elements sharing a generated `name`: these are
 * persisted mutually-exclusive settings (a graduation target, a discovery
 * mode), not tabs, so the browser's own radio group, roving focus and arrow-key
 * behaviour are what the app wants. `role="radiogroup"` on the container adds
 * the group's accessible name.
 *
 * Accessibility: give the group a name with `ariaLabel` or `ariaLabelledBy`.
 * Inside a `Field`, the id, `aria-describedby` and `aria-invalid` come from the
 * Field's context. A group wrapped in a native `<fieldset><legend>` keeps that
 * legend as its name.
 *
 * `optionClass` is the option `<label>`'s layout; `selectedClass` is appended
 * to the checked option so a card-style option can show its selected border.
 * `option-after` renders BELOW the option label and OUTSIDE the `<label>` (a
 * follow-up control such as a select that must not toggle the radio), which
 * turns each option into a block-level wrapper. Without it the options are
 * plain labels in the group's own layout.
 */
import { computed, inject, useAttrs, useSlots } from 'vue'
import { FIELD_CONTEXT } from './Field.vue'
import type { AriaInvalidValue } from '../../composables/useFieldControl'

defineOptions({ inheritAttrs: false })

const props = defineProps<{
  modelValue?: V | null
  options: RadioOption<V>[]
  /** Shared native `name`. Generated when omitted. */
  name?: string
  /** Accessible name for the group itself. */
  ariaLabel?: string
  /** Id of the element that names the group. */
  ariaLabelledBy?: string
  /** Disables every option (a native disabled `<fieldset>` also does this). */
  disabled?: boolean
  /** Option `<label>` layout. */
  optionClass?: string
  /** Appended to the option `<label>` when it is checked. */
  selectedClass?: string
  /** Native control classes (size, margin, accent colour). */
  controlClass?: string
  /** Classes for the option label span. */
  labelClass?: string
  /** Classes for the option description span. */
  descriptionClass?: string
  /** Classes for the option badge span. */
  badgeClass?: string
  /** Root class. Declared so `class` lands on the radiogroup container. */
  class?: string
}>()

const emit = defineEmits<{ (e: 'update:modelValue', value: V): void }>()

const attrs = useAttrs()
const slots = useSlots()
const field = inject(FIELD_CONTEXT, null)

const groupName = nextRadioGroupName()
const resolvedName = computed(() => props.name || groupName)

// Passthrough attributes (`data-testid`, `id`, other aria-*) must reach the
// group root, not vanish because `inheritAttrs` is off. The two we resolve
// ourselves are excluded so the explicit bindings below stay authoritative.
const rootAttrs = computed(() => {
  const rest = { ...attrs }
  delete rest['aria-describedby']
  delete rest['aria-invalid']
  return rest
})
const wrapsOptions = computed(() => Boolean(slots['option-after']))

const resolvedDescribedBy = computed(() => {
  const ids = [attrs['aria-describedby'] as string | undefined, field?.describedBy.value]
    .filter((value): value is string => Boolean(value))
  return ids.join(' ') || undefined
})

const resolvedAriaInvalid = computed<AriaInvalidValue | undefined>(() => {
  const explicit = attrs['aria-invalid']
  if (typeof explicit === 'string') return explicit as AriaInvalidValue
  if (typeof explicit === 'boolean') return explicit
  return field?.invalid.value ? 'true' : undefined
})

function isChecked(option: RadioOption<V>): boolean {
  return props.modelValue === option.value
}

function optionClasses(option: RadioOption<V>): (string | undefined)[] {
  return [props.optionClass, isChecked(option) ? props.selectedClass : undefined]
}

function select(option: RadioOption<V>): void {
  emit('update:modelValue', option.value)
}
</script>

<template>
  <div
    role="radiogroup"
    v-bind="rootAttrs"
    :class="props.class"
    :aria-label="ariaLabel"
    :aria-labelledby="ariaLabelledBy"
    :aria-describedby="resolvedDescribedBy"
    :aria-invalid="resolvedAriaInvalid"
  >
    <template v-for="option in options" :key="option.value">
      <div v-if="wrapsOptions">
        <label :class="optionClasses(option)">
          <input
            :id="option.id"
            type="radio"
            :name="resolvedName"
            :value="option.value"
            :checked="isChecked(option)"
            :disabled="disabled || option.disabled"
            :data-testid="option.testid"
            :class="controlClass"
            @change="select(option)"
          />
          <span :class="labelClass">{{ option.label }}</span>
          <span v-if="option.badge" :class="badgeClass">{{ option.badge }}</span>
          <span v-if="option.description" :class="descriptionClass">{{ option.description }}</span>
        </label>
        <slot name="option-after" :option="option" :checked="isChecked(option)" />
      </div>
      <label v-else :class="optionClasses(option)">
        <input
          :id="option.id"
          type="radio"
          :name="resolvedName"
          :value="option.value"
          :checked="isChecked(option)"
          :disabled="disabled || option.disabled"
          :data-testid="option.testid"
          :class="controlClass"
          @change="select(option)"
        />
        <span :class="labelClass">{{ option.label }}</span>
        <span v-if="option.badge" :class="badgeClass">{{ option.badge }}</span>
        <span v-if="option.description" :class="descriptionClass">{{ option.description }}</span>
      </label>
    </template>
  </div>
</template>
