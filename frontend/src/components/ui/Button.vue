<template>
  <PrimeButton
    ref="primeButton"
    v-bind="forwardedAttrs"
    :class="[variantClass, sizeClass]"
    :severity="severity"
    :outlined="outlined"
    :text="text"
    :link="link"
    :size="primeSize"
    :label="label"
    :icon="icon"
    :loading="loading"
    :disabled="disabled || loading"
    @click="onClick"
  >
    <!-- Forwarded only when the caller supplies it: an always-present icon slot
         would make PrimeVue treat every slot-content button as icon-only (its
         test is `hasIcon && !label && !badge`, so it does not look at the
         default slot) and hide the label. -->
    <template v-if="slots.icon" #icon>
      <slot name="icon" />
    </template>
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
 *   primary        -> default severity, solid
 *   secondary      -> severity="secondary" + outlined
 *   secondary-text -> severity="secondary" + text (the secondary severity at
 *                     rest: Aura surface.500 / surface.400)
 *   ghost          -> text
 *   link           -> PrimeVue's `link` flag (transparent hover, underlined
 *                     label on hover) - deliberately distinct from `text`
 *   danger         -> severity="danger", solid
 *   warning        -> severity="warn", solid (the warning token pair)
 *   danger-text    -> text + the app's destructive ink, with a
 *                     destructive/10 hover tint. Carries `ui-button-danger-text`;
 *                     the tinted hover is not expressible as a PrimeVue flag.
 *   plain          -> text + the default foreground ink with an accent hover.
 *                     Distinct from `ghost` (primary-tinted hover) and from
 *                     `secondary-text` (the muted secondary ink). Carries
 *                     `ui-button-plain`.
 *   action         -> solid `--action-build` (the pipeline toolbar's build
 *                     action hue - indigo - which is NOT `--primary`). Carries
 *                     `ui-button-action`; the colour is app tokens, not a
 *                     PrimeVue severity.
 *   action-link    -> link tone in `--action-link` (transparent background,
 *                     action-hue ink, brighter on hover, label underlined on
 *                     hover like `link`). Carries `ui-button-action-link`.
 *
 * The four variants with no PrimeVue severity are styled from the SFC's scoped
 * style block against the app's semantic tokens. A Tailwind utility on the
 * element cannot do it: PrimeVue injects its component CSS unlayered, so it
 * beats every `@layer utilities` declaration, and the scoped rules below win on
 * specificity instead (`.p-button.ui-button-*` vs `.p-button`).
 *
 * `secondary-text` is the text-button form of `secondary`: it keeps the
 * secondary severity's resting colour instead of the primary one. `link` is the
 * underlined-on-hover action the `text` flag does not render.
 *
 * The `warning` variant is solid (PrimeVue `severity="warn"`), using the Aura
 * warn background/colour token pair. An outlined warning action is reachable
 * through the `outlined` override (`variant="warning" outlined`), the same way
 * the legacy combinations below are.
 *
 * Two legacy flag combinations the variants do not express are reachable
 * through the `outlined` override: a solid secondary button
 * (`variant="secondary" :outlined="false"`) and an outlined danger button
 * (`variant="danger" :outlined="true"`). Pass it only to preserve an existing
 * site's exact rendering; new call sites use the variants unchanged.
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
 *
 * Loading: `loading` must also disable the button, and the primitive binds
 * `:disabled="disabled || loading"` to get it. PrimeVue computes its own
 * `disabled` as `$attrs.disabled || loading`, but it merges incoming attrs
 * AFTER that computation, so a bare `:disabled="disabled"` (false by default)
 * overwrites the computed `true` and leaves a loading button clickable.
 *
 * Sizes: `md` (default), `sm` (PrimeVue `small`) and `xs` - the compact
 * toolbar/titlebar density: `small`'s 0.75rem label plus a pinned 1.75rem row,
 * 0.625rem inline padding and a 0.375rem gap, which is exactly the pipeline
 * toolbar's metric. The icon-only `xs` form is a 1.75rem square with its padding
 * zeroed, so a caller never hand-writes `p-1`/`h-7`/`w-7` to get a compact icon
 * button. Both are pinned in the scoped style block below (see the note there).
 *
 * Element access: the component exposes the underlying element (`el`) and a
 * `focus()` that focuses it, so a caller holding a template ref can keep using
 * it - including the generic `querySelector`/geometry/focus code that used to
 * receive a raw `<button>` ref. `$el` is read here because a template ref on a
 * component resolves to the instance, not the element.
 */
import { computed, onBeforeMount, ref, useAttrs, useSlots } from 'vue'
import PrimeButton from 'primevue/button'

type ButtonVariant = 'primary' | 'secondary' | 'secondary-text' | 'ghost' | 'link' | 'danger' | 'warning' | 'danger-text' | 'plain' | 'action' | 'action-link'
type ButtonSize = 'xs' | 'sm' | 'md'

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
  // variants do not cover. `null` means "use the variant's default".
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

const VARIANT_FLAGS: Record<ButtonVariant, { severity?: 'secondary' | 'danger' | 'warn'; outlined: boolean; text: boolean; link: boolean }> = {
  primary: { severity: undefined, outlined: false, text: false, link: false },
  secondary: { severity: 'secondary', outlined: true, text: false, link: false },
  'secondary-text': { severity: 'secondary', outlined: false, text: true, link: false },
  ghost: { severity: undefined, outlined: false, text: true, link: false },
  link: { severity: undefined, outlined: false, text: false, link: true },
  danger: { severity: 'danger', outlined: false, text: false, link: false },
  warning: { severity: 'warn', outlined: false, text: false, link: false },
  'danger-text': { severity: undefined, outlined: false, text: true, link: false },
  plain: { severity: undefined, outlined: false, text: true, link: false },
  action: { severity: undefined, outlined: false, text: false, link: false },
  'action-link': { severity: undefined, outlined: false, text: false, link: true },
}

/**
 * The app-token variants have no PrimeVue severity: `danger-text`, `plain`,
 * `action` and `action-link` are painted from the scoped style block below, and
 * this class is what selects those rules. `undefined` for every variant that is
 * fully expressed by a PrimeVue flag.
 */
const VARIANT_CLASS: Record<ButtonVariant, string | undefined> = {
  primary: undefined,
  secondary: undefined,
  'secondary-text': undefined,
  ghost: undefined,
  link: undefined,
  danger: undefined,
  warning: undefined,
  'danger-text': 'ui-button-danger-text',
  plain: 'ui-button-plain',
  action: 'ui-button-action',
  'action-link': 'ui-button-action-link',
}

const variantFlags = computed(() => VARIANT_FLAGS[props.variant])
const severity = computed(() => variantFlags.value.severity)
const outlined = computed(() => props.outlined ?? variantFlags.value.outlined)
const text = computed(() => variantFlags.value.text)
const link = computed(() => variantFlags.value.link)
const variantClass = computed(() => VARIANT_CLASS[props.variant])

// Both compact sizes render PrimeVue `small` (the 0.75rem label); `xs` adds the
// class that pins its own 1.75rem row and padding (see the style block).
const primeSize = computed(() => (props.size === 'sm' || props.size === 'xs' ? 'small' : undefined))
const sizeClass = computed(() => (props.size === 'xs' ? 'ui-button-xs' : undefined))

const hasDefaultSlot = computed(() => !!slots.default)
// Mirrors PrimeVue's own icon-only test (`hasIcon && !label && !badge`): an icon
// supplied either by the `icon` prop or by the `#icon` slot counts.
const hasIcon = computed(() => !!props.icon || !!slots.icon)
const isIconOnly = computed(() => hasIcon.value && !props.label && !hasDefaultSlot.value)

// A caller may pass `aria-label` directly (several pre-adoption sites do). Vue
// matches that kebab-case attribute to this camelCase `ariaLabel` prop, so it
// always arrives as `props.ariaLabel` - there is no separate attrs fallback, and
// binding it here stops an undefined value from overwriting the name PrimeVue
// derives from `label`.
const resolvedAriaLabel = computed(() => props.ariaLabel)

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

// A template ref on a component resolves to the instance, so the element is read
// off `$el`. Exposed (not just internal) because call sites that used to hold a
// raw `<button>` ref need the element for geometry, `querySelector` and focus.
const primeButton = ref<InstanceType<typeof PrimeButton> | null>(null)

const el = computed<HTMLElement | null>(() => {
  const instance = primeButton.value as unknown as { $el?: unknown } | null
  const node = instance?.$el
  return node instanceof HTMLElement ? node : null
})

/** Focus the underlying element (the pre-adoption `ref.focus()` call shape). */
function focus(options?: FocusOptions): void {
  el.value?.focus(options)
}

defineExpose({ el, focus })
</script>

<style scoped>
/*
 * App-token variants. Tailwind utilities cannot paint these: PrimeVue injects its
 * component CSS UNLAYERED, which beats every `@layer utilities` declaration, so
 * the app's own rules have to win on specificity instead - two classes here
 * (`.p-button.ui-button-*`) against PrimeVue's one.
 *
 * `--action-build` / `--action-link` are raw colour values (oklch), not hsl
 * triplets, so they are used unwrapped; `--destructive` / `--foreground` /
 * `--accent` are hsl triplets and are wrapped.
 */

/* Solid build action (the pipeline toolbar's Run): indigo, deliberately not
   `--primary`. */
.p-button.ui-button-action {
  background: var(--action-build);
  border-color: var(--action-build);
  color: #fff;
}

.p-button.ui-button-action:not(:disabled):hover {
  background: var(--action-build-hover);
  border-color: var(--action-build-hover);
  color: #fff;
}

.p-button.ui-button-action:not(:disabled):active {
  background: var(--action-build-hover);
  border-color: var(--action-build-hover);
  color: #fff;
}

/* Link tone in the build hue: transparent at rest, brighter on hover. The label
   underline comes from PrimeVue's `link` flag, exactly as the `link` variant. */
.p-button.ui-button-action-link {
  background: transparent;
  border-color: transparent;
  color: var(--action-link);
}

.p-button.ui-button-action-link:not(:disabled):hover {
  background: transparent;
  border-color: transparent;
  color: var(--action-link-hover);
}

.p-button.ui-button-action-link:not(:disabled):active {
  background: transparent;
  border-color: transparent;
  color: var(--action-link-hover);
}

/* Destructive ink with a destructive/10 hover tint (the delete/remove icon
   action). A PrimeVue severity tint would use Aura's own red scale, not ours. */
.p-button.ui-button-danger-text {
  background: transparent;
  border-color: transparent;
  color: hsl(var(--destructive));
}

.p-button.ui-button-danger-text:not(:disabled):hover {
  background: hsl(var(--destructive) / 0.1);
  border-color: transparent;
  color: hsl(var(--destructive));
}

.p-button.ui-button-danger-text:not(:disabled):active {
  background: hsl(var(--destructive) / 0.1);
  border-color: transparent;
  color: hsl(var(--destructive));
}

/* Default foreground ink with an accent hover - the confirmation pair's
   "cancel/stay" action. Distinct from `ghost` (primary ink + primary tint) and
   from `secondary-text` (muted ink). */
.p-button.ui-button-plain {
  background: transparent;
  border-color: transparent;
  color: hsl(var(--foreground));
}

.p-button.ui-button-plain:not(:disabled):hover {
  background: hsl(var(--accent));
  border-color: transparent;
  color: hsl(var(--foreground));
}

.p-button.ui-button-plain:not(:disabled):active {
  background: hsl(var(--accent));
  border-color: transparent;
  color: hsl(var(--foreground));
}

/*
 * `xs` - the compact toolbar/titlebar density. `small` already supplies the
 * 0.75rem label; this pins the toolbar's own geometry (1.75rem row, 0.625rem
 * inline padding, 0.375rem icon gap) so a toolbar control lines up with the
 * hand-written `h-7 px-2.5 text-xs gap-1.5` it replaces, and the icon-only form
 * is a 1.75rem square with its padding zeroed - no `p-1`/`h-7`/`w-7` at the
 * call site. Two classes beat `.p-button-sm` on specificity; the extra
 * `.p-button-icon-only` beats `.p-button-sm.p-button-icon-only`.
 */
.p-button-sm.ui-button-xs {
  height: 1.75rem;
  padding: 0 0.625rem;
  gap: 0.375rem;
  white-space: nowrap;
}

.p-button-sm.ui-button-xs.p-button-icon-only {
  width: 1.75rem;
  height: 1.75rem;
  padding: 0;
}
</style>
