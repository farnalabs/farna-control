<template>
  <input
    v-if="bare && !help"
    ref="inputEl"
    v-bind="inputAttrs"
    :id="resolvedId"
    type="checkbox"
    :class="bareControlClass"
    :checked="isChecked"
    :disabled="disabled"
    :indeterminate="indeterminate"
    :aria-label="ariaLabel"
    :aria-describedby="resolvedDescribedBy"
    :aria-invalid="resolvedAriaInvalid"
    @change="onChange"
  />
  <template v-else-if="bare">
    <input
      ref="inputEl"
      v-bind="inputAttrs"
      :id="resolvedId"
      type="checkbox"
      :class="bareControlClass"
      :checked="isChecked"
      :disabled="disabled"
      :indeterminate="indeterminate"
      :aria-label="ariaLabel"
      :aria-describedby="resolvedDescribedBy"
      :aria-invalid="resolvedAriaInvalid"
      @change="onChange"
    />
    <p :id="helpId" :class="helpClass">{{ help }}</p>
  </template>
  <template v-else>
    <label :for="resolvedId" :class="[rootClass, wrapperClass]">
      <input
        ref="inputEl"
        v-bind="inputAttrs"
        :id="resolvedId"
        type="checkbox"
        :class="controlClass"
        :checked="isChecked"
        :disabled="disabled"
        :indeterminate="indeterminate"
        :aria-label="ariaLabel"
        :aria-describedby="resolvedDescribedBy"
        :aria-invalid="resolvedAriaInvalid"
        @change="onChange"
      />
      <span v-if="label && !$slots.default" :class="labelClass">{{ label }}</span>
      <slot />
    </label>
    <p v-if="help" :id="helpId" :class="helpClass">{{ help }}</p>
  </template>
</template>

<script setup lang="ts">
/*
 * The ONE shared checkbox (FAR-2792).
 *
 * A thin wrapper over a NATIVE `<input type="checkbox">`, so the semantics the
 * app already relies on - `setValue`, `:checked`, indeterminate presentation and
 * native keyboard behaviour - are unchanged. The primitive owns what every hand
 * rolled checkbox repeated: the label association, the accessible name, the
 * optional help text, the controlled DOM sync and the Field wiring.
 *
 * Two shapes:
 *   - labelled: `label` (or a default slot for rich label content) renders a
 *     `<label for>` wrapping the control, with optional `help` below it;
 *   - bare: `bare` renders the control with no `<label>` wrapper, for a
 *     checkbox whose name comes from `aria-label` (a table cell, a card header
 *     action). The caller's `class` lands on the control, and optional `help`
 *     text still renders below it (so its `aria-describedby` reference is real).
 *
 * The control styling is the caller's `controlClass` so a migration keeps the
 * site's existing token classes exactly; `wrapperClass` is the label's layout.
 *
 * Controlled DOM sync: `:checked` only patches the DOM when the bound value
 * CHANGES, so a site that rejects a user flip (a failed save) must be able to
 * re-assert the committed state. The native control is exposed as `inputEl`
 * for exactly that (`AdminProductAnalyticsView` keeps its existing
 * `syncToggleDom`). The primitive does not re-assert on every render: that
 * would flick the control back while a save is still in flight.
 *
 * Field integration: inside a `Field`, the id, `aria-describedby` and
 * `aria-invalid` come from the Field's context unless the caller sets them.
 */
import { computed, inject, ref, useAttrs } from 'vue'
import { FIELD_CONTEXT, nextFieldId } from './Field.vue'
import type { AriaInvalidValue } from '../../composables/useFieldControl'

defineOptions({ name: 'FormCheckbox', inheritAttrs: false })

const props = defineProps<{
  modelValue?: boolean
  label?: string
  help?: string
  disabled?: boolean
  indeterminate?: boolean
  /** Explicit control id. Generated when omitted so `for` is always wired. */
  id?: string
  /** Explicit accessible name for the bare / no-visible-label form. */
  ariaLabel?: string
  /** Classes for the native control (size, radius, accent colour). */
  controlClass?: string
  /** Classes for the visible label span. */
  labelClass?: string
  /** Classes for the help text. */
  helpClass?: string
  /** Classes for the `<label>` wrapper (layout). */
  wrapperClass?: string
  /** Render the control with no `<label>` wrapper (name comes from aria-label). */
  bare?: boolean
  /** Root class. Declared so `class` lands on the wrapper, not the control. */
  class?: string
}>()

const emit = defineEmits<{ (e: 'update:modelValue', value: boolean): void }>()

const attrs = useAttrs()
const field = inject(FIELD_CONTEXT, null)

const generatedId = nextFieldId()
const resolvedId = computed(() => props.id || (attrs.id as string | undefined) || field?.id.value || generatedId)
const helpId = computed(() => `${resolvedId.value}-help`)

const rootClass = computed(() => props.class)

// Bare mode renders no wrapper, so the caller's root `class` must land on the
// control itself or it would be silently dropped. Labelled mode keeps `class`
// on the `<label>` wrapper (rootClass below).
const bareControlClass = computed(() => [props.controlClass, props.class])

// Every attribute except `class` belongs to the control: a caller keeps its
// `data-testid`, `value`, `name` and aria-* exactly as before adoption.
const inputAttrs = computed(() => {
  const { class: _class, ...rest } = attrs
  return rest
})

const isChecked = computed(() => Boolean(props.modelValue))

const resolvedDescribedBy = computed(() => {
  const ids = [
    attrs['aria-describedby'] as string | undefined,
    field?.describedBy.value,
    props.help ? helpId.value : undefined,
  ].filter((value): value is string => Boolean(value))
  return ids.join(' ') || undefined
})

const resolvedAriaInvalid = computed<AriaInvalidValue | undefined>(() => {
  const explicit = attrs['aria-invalid']
  if (typeof explicit === 'string') return explicit as AriaInvalidValue
  if (typeof explicit === 'boolean') return explicit
  return field?.invalid.value ? 'true' : undefined
})

const inputEl = ref<HTMLInputElement | null>(null)

function onChange(event: Event): void {
  const target = event.target as HTMLInputElement
  emit('update:modelValue', target.checked)
}

defineExpose({ inputEl })
</script>
