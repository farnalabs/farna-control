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
import { computed, inject, useAttrs } from 'vue'
import Textarea from 'primevue/textarea'
import { FIELD_CONTEXT } from './Field.vue'

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

const attrs = useAttrs()
const field = inject(FIELD_CONTEXT, null)

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

function onUpdate(value: string | undefined) {
  emit('update:modelValue', value ?? '')
}
</script>
