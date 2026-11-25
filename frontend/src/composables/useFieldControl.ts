import { computed, inject, useAttrs } from 'vue'
import { FIELD_CONTEXT } from '../components/ui/Field.vue'

/**
 * The values Vue's own `aria-invalid` attribute accepts. Typing the resolved
 * value this way lets the wrappers bind it without casting to `any`.
 */
export type AriaInvalidValue = boolean | 'true' | 'false' | 'grammar' | 'spelling'

/**
 * The values Vue's own `aria-required` attribute accepts. Like `aria-invalid`,
 * a caller-supplied string is passed through with a narrowed cast (never
 * `any`), so the wrappers bind the attribute without a type escape hatch.
 */
export type AriaRequiredValue = boolean | 'true' | 'false'

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

  const resolvedAriaInvalid = computed<AriaInvalidValue | undefined>(() => {
    const explicit = attrs['aria-invalid']
    // A caller-supplied value is passed through verbatim (a narrowed cast, not
    // `any`): callers express validity states the boolean shorthand cannot.
    if (typeof explicit === 'string') return explicit as AriaInvalidValue
    if (typeof explicit === 'boolean') return explicit
    return field?.invalid.value ? 'true' : undefined
  })

  // `aria-required` is the semantic flag the Field owns; native validation is
  // separate and opt-in, because a browser-blocked submit would stop the form's
  // own handler (and its validation message) from ever running (FAR-2779).
  const resolvedAriaRequired = computed<AriaRequiredValue | undefined>(() => {
    const explicit = attrs['aria-required']
    if (typeof explicit === 'string') return explicit as AriaRequiredValue
    if (typeof explicit === 'boolean') return explicit
    return field?.required.value ? 'true' : undefined
  })

  // Only renders the native attribute when the caller asked for it explicitly
  // (an explicit `required` on the control, or `native-required` on the Field).
  const resolvedNativeRequired = computed(() => {
    if (attrs.required === false) return false
    if (attrs.required !== undefined) return true
    return field?.nativeRequired.value ? true : undefined
  })

  return {
    attrs,
    textValue,
    resolvedId,
    resolvedDescribedBy,
    resolvedAriaInvalid,
    resolvedAriaRequired,
    resolvedNativeRequired,
  }
}

/**
 * Normalise the wrapper's `autocomplete` model to the string Vue's
 * `autocomplete` attribute accepts, so the wrappers bind it without a cast.
 * The legacy boolean form means on/off.
 */
export function resolveAutocomplete(autocomplete: boolean | string | undefined) {
  if (typeof autocomplete === 'boolean') return autocomplete ? 'on' : 'off'
  return autocomplete
}
