<template>
  <Textarea
    v-bind="attrs"
    :id="resolvedId"
    :model-value="textValue"
    :rows="rows"
    :placeholder="placeholder"
    :disabled="disabled"
    :invalid="invalid"
    :autocomplete="autocomplete as any"
    :required="resolvedRequired"
    :aria-describedby="resolvedDescribedBy"
    :aria-invalid="resolvedAriaInvalid as any"
    @update:model-value="onUpdate"
  />
</template>

<script setup lang="ts">
import Textarea from 'primevue/textarea'
import { useFieldControl } from '../../composables/useFieldControl'

// Same contract as Input: attributes/listeners (class, data-testid, readonly,
// @blur, @input, @keydown, ...) are forwarded, while id/description/invalid
// state come from the enclosing Field when one is present.
defineOptions({ name: 'FormTextarea', inheritAttrs: false })

const props = defineProps<{
  modelValue?: string | number | null
  rows?: string | number
  placeholder?: string
  disabled?: boolean
  invalid?: boolean
  autocomplete?: boolean | string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: string | number | null): void
}>()

const { attrs, textValue, resolvedId, resolvedDescribedBy, resolvedAriaInvalid, resolvedRequired } =
  useFieldControl(() => props.modelValue)

function onUpdate(value: string | undefined) {
  emit('update:modelValue', value ?? '')
}
</script>
