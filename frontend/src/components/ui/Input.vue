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
import InputText from 'primevue/inputtext'
import { useFieldControl } from '../../composables/useFieldControl'

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

const { attrs, textValue, resolvedId, resolvedDescribedBy, resolvedAriaInvalid, resolvedRequired } =
  useFieldControl(() => props.modelValue)

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
