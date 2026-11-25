<script lang="ts">
import type { ComputedRef, InjectionKey } from 'vue'

/**
 * The label/help/error contract a Field owns for the control it wraps.
 * `Input` and `Textarea` inject this when they are rendered inside a Field,
 * so the id, `aria-describedby`, `aria-invalid` and `required` wiring is the
 * Field's responsibility rather than each form's (FAR-2779).
 */
export interface FieldContext {
  id: ComputedRef<string>
  describedBy: ComputedRef<string | undefined>
  invalid: ComputedRef<boolean>
  required: ComputedRef<boolean>
}

export const FIELD_CONTEXT: InjectionKey<FieldContext> = Symbol('modulo-field')

let fieldCounter = 0

export function nextFieldId(): string {
  fieldCounter += 1
  return `field-${fieldCounter}`
}
</script>

<script setup lang="ts">
import { computed, provide } from 'vue'

defineOptions({ name: 'FormField' })

const props = withDefaults(defineProps<{
  /** Explicit control id. Generated when omitted so `for` is always wired. */
  id?: string
  /** Visible label. Omit for a control whose name comes from `aria-label`. */
  label?: string
  /** Persistent help text. Associated with the control but never announced. */
  help?: string
  /** Validation message. Announced with aria-live="polite". */
  error?: string
  /** Marks the field required both visually (marker) and semantically. */
  required?: boolean
  /** Extra ids a caller already wires into aria-describedby (e.g. a hint). */
  describedBy?: string
  /** Explicit ids, so a site can keep an existing association. */
  helpId?: string
  errorId?: string
  labelClass?: string
  helpClass?: string
  errorClass?: string
  class?: string
}>(), {
  labelClass: 'mb-1 block text-sm font-medium',
  helpClass: 'mt-1 text-xs text-muted-foreground',
  errorClass: 'mt-1 text-xs text-destructive',
})

const generatedId = nextFieldId()
const fieldId = computed(() => props.id || generatedId)
const helpId = computed(() => props.helpId || `${fieldId.value}-help`)
const errorId = computed(() => props.errorId || `${fieldId.value}-error`)
const isInvalid = computed(() => Boolean(props.error))
const isRequired = computed(() => Boolean(props.required))

const describedBy = computed(() => {
  const ids = [props.describedBy]
  // Mirror the hand-rolled forms this replaces: an error supersedes the help
  // hint in the description so a screen reader reads the actionable message,
  // while the help stays visible whenever there is no error.
  if (isInvalid.value) ids.push(errorId.value)
  else if (props.help) ids.push(helpId.value)
  return ids.filter((value): value is string => Boolean(value)).join(' ') || undefined
})

provide(FIELD_CONTEXT, {
  id: fieldId,
  describedBy,
  invalid: isInvalid,
  required: isRequired,
})
</script>

<template>
  <div :class="props.class">
    <label v-if="label" :for="fieldId" :class="labelClass">
      {{ label }}<span v-if="isRequired" class="text-destructive" aria-hidden="true"> *</span>
    </label>
    <slot
      :id="fieldId"
      :described-by="describedBy"
      :invalid="isInvalid"
      :required="isRequired"
      :help-id="helpId"
      :error-id="errorId"
    />
    <p v-if="help && !isInvalid" :id="helpId" :class="helpClass">{{ help }}</p>
    <p v-if="isInvalid" :id="errorId" :class="errorClass" aria-live="polite">{{ error }}</p>
  </div>
</template>
