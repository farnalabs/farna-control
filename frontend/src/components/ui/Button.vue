<template>
  <PrimeButton
    v-bind="forwardedAttrs"
    :severity="severity"
    :outlined="outlined"
    :text="text"
    :size="primeSize"
    :label="label"
    :icon="icon"
    :loading="loading"
    :disabled="disabled"
    @click="onClick"
  >
    <slot />
  </PrimeButton>
</template>

<script setup lang="ts">
/*
 * The ONE shared action control.
 *
 * A thin wrapper over PrimeVue `Button` so the preset stays the single source of
 * visual truth - adoption changes no rendered markup beyond the flags each
 * variant sets on the same PrimeVue component. `variant` and `size` are the
 * public API; the underlying `severity` / `outlined` / `text` / `size` mapping
 * lives here and nowhere else.
 *
 * Variant map (PrimeVue flags):
 *   primary   -> default severity, solid
 *   secondary -> severity="secondary" + outlined
 *   ghost     -> text
 *   danger    -> severity="danger", solid
 *
 * Two legacy flag combinations the four variants do not express are reachable
 * through the `outlined` override: a solid secondary button
 * (`variant="secondary" :outlined="false"`) and an outlined danger button
 * (`variant="danger" :outlined="true"`). Pass it only to preserve an existing
 * site's exact rendering; new call sites use the four variants unchanged.
 *
 * Accessible naming: the name comes from `label`, the default slot, or - for an
 * icon-only button - the explicit `ariaLabel` prop. The primitive never infers
 * a name from an icon. An icon-only button with no name is a REVIEW FINDING;
 * this primitive warns in development so the gap is visible rather than silent,
 * and still renders (the call site remains responsible for the name).
 *
 * Attribute fall-through: `class` (layout: width, borders), `data-testid`,
 * `aria-pressed`, `as`/`href`/`target`/`rel` for link buttons, and any other
 * attribute land on the PrimeVue root, exactly as they did before adoption.
 */
import { computed, onBeforeMount, useAttrs, useSlots } from 'vue'
import PrimeButton from 'primevue/button'

type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'
type ButtonSize = 'sm' | 'md'

const props = withDefaults(defineProps<{
  variant?: ButtonVariant
  size?: ButtonSize
  label?: string
  icon?: string
  loading?: boolean
  disabled?: boolean
  // Deliberately undefined by default: PrimeVue already renders
  // `type="button"` for a <button> and no type at all for `as="a"` link
  // buttons, so passing undefined keeps the pre-adoption markup identical.
  type?: 'button' | 'submit' | 'reset'
  // Explicit accessible name for the icon-only form.
  ariaLabel?: string
  // Tri-state override for the two legacy severity/outlined combinations the
  // four variants do not cover. `null` means "use the variant's default".
  outlined?: boolean | null
}>(), {
  variant: 'primary',
  size: 'md',
  loading: false,
  disabled: false,
  outlined: null,
})

const emit = defineEmits<{ (e: 'click', event: MouseEvent): void }>()

// Attrs are forwarded explicitly (below) rather than by implicit root
// fall-through, so the variant flags can never be silently overridden by a
// stray `severity`/`outlined` attribute passed by a caller.
defineOptions({ inheritAttrs: false })

const slots = useSlots()
const attrs = useAttrs()

const VARIANT_FLAGS: Record<ButtonVariant, { severity?: 'secondary' | 'danger'; outlined: boolean; text: boolean }> = {
  primary: { severity: undefined, outlined: false, text: false },
  secondary: { severity: 'secondary', outlined: true, text: false },
  ghost: { severity: undefined, outlined: false, text: true },
  danger: { severity: 'danger', outlined: false, text: false },
}

const variantFlags = computed(() => VARIANT_FLAGS[props.variant])
const severity = computed(() => variantFlags.value.severity)
const outlined = computed(() => props.outlined ?? variantFlags.value.outlined)
const text = computed(() => variantFlags.value.text)
const primeSize = computed(() => (props.size === 'sm' ? 'small' : undefined))

const hasDefaultSlot = computed(() => !!slots.default)
const isIconOnly = computed(() => !!props.icon && !props.label && !hasDefaultSlot.value)

// A caller may pass `aria-label` directly (several pre-adoption sites do); it
// must survive adoption, so resolve it here rather than overwriting it with an
// undefined prop.
const resolvedAriaLabel = computed(() => {
  if (props.ariaLabel) return props.ariaLabel
  const passthrough = attrs['aria-label']
  return typeof passthrough === 'string' ? passthrough : undefined
})

// Bind `type` and `aria-label` only when they have a value. Passing an explicit
// `undefined` to PrimeVue would overwrite the defaults it renders itself
// (`type="button"` for a <button>, and `aria-label` derived from `label`).
const defaultedAttrs = computed(() => {
  const result: Record<string, string> = {}
  if (props.type) result.type = props.type
  if (resolvedAriaLabel.value) result['aria-label'] = resolvedAriaLabel.value
  return result
})

// Everything the caller passes (`class`, `data-testid`, `as`/`href`, listeners,
// ...) is forwarded to the PrimeVue root, with `type`/`aria-label` taking the
// resolved values above.
const forwardedAttrs = computed(() => ({ ...attrs, ...defaultedAttrs.value }))

onBeforeMount(() => {
  if (!import.meta.env.DEV) return
  if (!isIconOnly.value) return
  if (resolvedAriaLabel.value) return
  console.warn(
    '[Button] icon-only button has no accessible name. Pass `ariaLabel` (or a default slot / label) - '
      + 'an icon-only button without a name is an accessibility review finding.',
  )
})

function onClick(event: MouseEvent): void {
  emit('click', event)
}
</script>
