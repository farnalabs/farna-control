<template>
  <InputText
    v-bind="attrs"
    :id="resolvedId"
    :model-value="textValue"
    :type="type"
    :placeholder="placeholder"
    :disabled="disabled"
    :invalid="invalid"
    :autocomplete="autocomplete as any"
    :inputmode="inputmode"
    :min="min"
    :max="max"
    :step="step"
    :required="resolvedRequired"
    :aria-describedby="resolvedDescribedBy"
    :aria-invalid="resolvedAriaInvalid as any"
    @update:model-value="onUpdate"
  />
</template>

<script setup lang="ts">
import { computed, inject, useAttrs } from 'vue'
import InputText from 'primevue/inputtext'
import { FIELD_CONTEXT } from './Field.vue'

// Attributes and listeners (data-testid, maxlength, readonly, class, @blur,
// @input, @keydown, ...) are forwarded explicitly so the Field-owned id,
// description and invalid state can be merged in without fighting Vue's
// automatic attribute inheritance.
defineOptions({ inheritAttrs: false })

const props = defineProps<{
  modelValue?: string | number | null
  modelModifiers?: { number?: boolean }
  type?: string
  placeholder?: string
  disabled?: boolean
  invalid?: boolean
  autocomplete?: boolean | string
  inputmode?: 'none' | 'text' | 'search' | 'url' | 'email' | 'tel' | 'numeric' | 'decimal'
  min?: string | number
  max?: string | number
  step?: string | number
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: string | number | null): void
}>()

const attrs = useAttrs()
const field = inject(FIELD_CONTEXT, null)

// InputText's own type declares a string model value; a native input's value is
// a string too, so normalise a numeric model for display without changing what
// the parent receives.
const textValue = computed(() => (props.modelValue == null ? undefined : String(props.modelValue)))

const resolvedId = computed(() => (attrs.id as string | undefined) ?? field?.id.value)

const resolvedDescribedBy = computed(() => {
  const ids = [attrs['aria-describedby'] as string | undefined, field?.describedBy.value]
    .filter((value): value is string => Boolean(value))
  return ids.join(' ') || undefined
})

const resolvedAriaInvalid = computed<string | boolean | undefined>(() => {
  const explicit = attrs['aria-invalid']
  if (typeof explicit === 'string') return explicit
  if (typeof explicit === 'boolean') return explicit
  return field?.invalid.value ? 'true' : undefined
})

const resolvedRequired = computed(() => {
  if (attrs.required === false) return false
  if (attrs.required !== undefined) return true
  return field?.required.value ? true : undefined
})

// A native `<input type="number">` with `v-model` casts its value to a number
// (Vue's own v-model behaviour), and `v-model.number` does so explicitly. Match
// both so replacing a raw input with this wrapper cannot change the emitted
// type.
function onUpdate(value: string | undefined) {
  const next = value ?? ''
  const numeric = props.modelModifiers?.number || props.type === 'number'
  if (numeric && next !== '') {
    emit('update:modelValue', Number(next))
    return
  }
  emit('update:modelValue', next)
}
</script>
