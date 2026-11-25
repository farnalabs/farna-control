import { computed, inject, useAttrs } from 'vue'
import { FIELD_CONTEXT } from '../components/ui/Field.vue'

/**
 * Shared Field wiring for the form-control wrappers (`Input`, `Textarea`).
 *
 * An explicit caller attribute always wins; otherwise the value comes from the
 * enclosing Field's context. Keeping this in one place means both wrappers
 * resolve the id, aria description, invalid state and required flag identically
 * (FAR-2779).
 */
export function useFieldControl(modelValue: () => string | number | null | undefined) {
  const attrs = useAttrs()
  const field = inject(FIELD_CONTEXT, null)

  // A native control's value is always a string, so normalise a numeric model
  // for display without changing what the parent receives.
  const textValue = computed(() => (modelValue() == null ? undefined : String(modelValue())))

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

  return {
    attrs,
    textValue,
    resolvedId,
    resolvedDescribedBy,
    resolvedAriaInvalid,
    resolvedRequired,
  }
}
