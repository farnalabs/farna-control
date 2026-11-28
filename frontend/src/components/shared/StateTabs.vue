<template>
  <div
    :aria-label="ownsTabs ? undefined : ariaLabel"
    :aria-orientation="ownsTabs ? undefined : orientation"
    :class="listClasses"
    :role="ownsTabs ? undefined : 'tablist'"
  >
    <!-- A per-tab trailing action (e.g. a close button) cannot sit inside the
         tablist: `aria-required-children` (WCAG 1.3.1) rejects any child role
         other than `tab`. In that mode the tablist claims its buttons with
         `aria-owns` and the rows render as its DOM siblings instead. -->
    <div
      v-if="ownsTabs"
      :aria-label="ariaLabel"
      :aria-orientation="orientation"
      :aria-owns="ownedTabIds"
      class="contents"
      role="tablist"
    ></div>
    <div
      v-for="(tab, index) in tabs"
      :key="tab.id"
      class="flex shrink-0 items-center gap-1"
      role="none"
    >
      <button
        :id="domId(tab)"
        :ref="(el) => setTabRef(tab.id, el)"
        :aria-controls="panelFor(tab)"
        :aria-selected="isActive(tab) ? 'true' : 'false'"
        :class="resolveTabClass(tab)"
        :data-testid="testId(tab)"
        :disabled="tab.disabled"
        :tabindex="tabindexFor(tab, index)"
        :title="tab.title"
        role="tab"
        type="button"
        @click="activate(tab)"
        @keydown="onKeydown($event, tab)"
      >
        <slot :tab="tab" name="tab">
          <span class="truncate">{{ tab.label }}</span>
          <span
            v-if="tab.count !== undefined"
            class="inline-flex min-w-5 items-center justify-center rounded-full bg-primary/10 px-1.5 text-xs font-semibold text-primary-text"
          >{{ tab.count }}</span>
        </slot>
      </button>
      <slot :tab="tab" name="tab-trailing" />
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * In-page state tabs (WAI-ARIA tabs pattern) for switching what a single view
 * shows without changing the route.
 *
 * `PageTabs` cannot serve this: it is route-bound (router-links, query-driven
 * active state, manifest plan-lock), not a `v-model` state switch.
 *
 * The component owns the tablist and the tab buttons; the caller owns the
 * panels and keeps them in its own template. Accessibility contract:
 * - `role="tablist"` with `aria-orientation` and one `role="tab"` button per
 *   option, carrying `aria-selected` and a roving `tabindex` (0 on the active
 *   tab, -1 elsewhere),
 * - `aria-controls` when the caller supplies a panel id (or id prefix),
 * - the full WAI-ARIA keyboard pattern implemented here, never by the browser:
 *   Left/Right (Up/Down when vertical) move with wrap-around, Home/End jump to
 *   the first/last tab, disabled tabs are skipped, and activation follows focus
 *   (automatic activation),
 * - the `tab-trailing` slot is for a per-tab action such as a close button. It
 *   switches the tablist to the `aria-owns` form (see the comment in the
 *   template): an extra focusable child inside a tablist fails the
 *   `aria-required-children` rule, so the rows render as siblings and the empty
 *   tablist claims its tabs by id. Without that slot the tabs are tablist
 *   children, which is the preferred shape.
 *
 * DOM ids and `data-testid`s are composed from the optional prefixes so an
 * adopting view keeps its existing ids. The container `data-testid` arrives
 * through attribute fallthrough (`<StateTabs data-testid="..." />`) so the
 * static literal stays in the adopting view for the product-map guard: it lands
 * on the tablist normally, and on the wrapping row when the `tab-trailing` slot
 * switches to the `aria-owns` form.
 *
 * Visual treatment: the default pill is the page density, but a strip with its
 * own look (a flush border-bottom strip, say) can keep it exactly - `listClass`
 * replaces the list wrapper classes, and `tabClass` REPLACES the whole per-tab
 * class string rather than appending to it, so leftover base classes cannot
 * fight the caller's own. `activeClass` is the active-only half and works
 * either way. A caller that keeps its own strip therefore needs no separate
 * "underline variant"; the primitives stay single-purpose.
 */
import { computed, useId, useSlots, type ComponentPublicInstance } from 'vue'

export interface StateTab {
  /** Stable identifier, emitted as `update:modelValue` when the tab activates. */
  id: string
  label: string
  /** Optional count rendered as a badge inside the tab (part of its name). */
  count?: number | string
  /** A disabled tab stays visible and is skipped by keyboard navigation. */
  disabled?: boolean
  /** Optional native tooltip for the tab button. */
  title?: string
}

const props = withDefaults(
  defineProps<{
    tabs: StateTab[]
    modelValue: string
    /** Accessible name of the tablist. */
    ariaLabel: string
    /** `compact` is the dialog/toolbar density; `default` is the page density. */
    size?: 'default' | 'compact'
    orientation?: 'horizontal' | 'vertical'
    /**
     * DOM id prefix for each tab button (`${idPrefix}-${tab.id}`). Required when
     * a panel points back at its tab with `aria-labelledby`.
     */
    idPrefix?: string
    /** `data-testid` prefix for each tab button (`${testidPrefix}-${tab.id}`). */
    testidPrefix?: string
    /** Panel id prefix: `aria-controls` becomes `${panelIdPrefix}-${tab.id}`. */
    panelIdPrefix?: string
    /** One panel id every tab controls (when a single panel serves the list). */
    panelId?: string
    /**
     * Replaces the default list wrapper classes (`flex items-center gap-2`, or
     * the vertical stack). For a site whose strip is not the default pill row -
     * e.g. a flush, border-bottom strip.
     */
    listClass?: string
    /**
     * Replaces the whole per-tab class string (the base pill, the size padding
     * and the active/inactive pair). It REPLACES rather than appends: a caller
     * keeping its own visual treatment must not have to fight leftover base
     * classes. Omit it to keep the default pill.
     */
    tabClass?: string
    /**
     * The class applied to the active tab only. Works with the default pill
     * (replacing just the active half) and with `tabClass` (appended when the
     * tab is active).
     */
    activeClass?: string
  }>(),
  {
    size: 'default',
    orientation: 'horizontal',
  },
)

const emit = defineEmits<{
  (event: 'update:modelValue', id: string): void
}>()

const slots = useSlots()

const BASE_CLASS =
  'inline-flex items-center gap-1.5 rounded-lg border text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50'

const SIZE_CLASS: Record<'default' | 'compact', string> = {
  default: 'px-4 py-2',
  compact: 'px-3 py-1.5',
}

const ACTIVE_CLASS = 'border-input bg-accent'
const INACTIVE_CLASS = 'border-input bg-background hover:bg-accent'

/** A fallback id namespace so `aria-owns` always has ids to reference. */
const instanceId = useId()

/** A per-tab action forces the `aria-owns` tablist shape (see the template). */
const ownsTabs = computed(() => Boolean(slots['tab-trailing']))

const listClasses = computed(() =>
  props.listClass ?? (props.orientation === 'vertical'
    ? 'flex flex-col items-start gap-2'
    : 'flex items-center gap-2'),
)

const enabledTabs = computed(() => props.tabs.filter((tab) => !tab.disabled))

const activeTabId = computed(() => {
  const match = props.tabs.find((tab) => tab.id === props.modelValue)
  return match ? match.id : null
})

/** When nothing matches `modelValue`, the first enabled tab keeps the list tabbable. */
const fallbackTabbableId = computed(() =>
  activeTabId.value === null ? (enabledTabs.value[0]?.id ?? null) : null,
)

const ownedTabIds = computed(() =>
  props.tabs.map((tab) => domId(tab)).filter(Boolean).join(' '),
)

const tabElements = new Map<string, HTMLButtonElement>()

function setTabRef(id: string, el: Element | ComponentPublicInstance | null) {
  if (el instanceof HTMLButtonElement) tabElements.set(id, el)
  else tabElements.delete(id)
}

function isActive(tab: StateTab): boolean {
  return tab.id === activeTabId.value
}

function tabindexFor(tab: StateTab, index: number): 0 | -1 {
  if (isActive(tab)) return 0
  if (fallbackTabbableId.value !== null) return tab.id === fallbackTabbableId.value ? 0 : -1
  // A tab list with every tab disabled still exposes one tabbable entry point.
  return index === 0 ? 0 : -1
}

function domId(tab: StateTab): string | undefined {
  if (props.idPrefix) return `${props.idPrefix}-${tab.id}`
  return ownsTabs.value ? `${instanceId}-${tab.id}` : undefined
}

function testId(tab: StateTab): string | undefined {
  return props.testidPrefix ? `${props.testidPrefix}-${tab.id}` : undefined
}

function panelFor(tab: StateTab): string | undefined {
  if (props.panelId) return props.panelId
  return props.panelIdPrefix ? `${props.panelIdPrefix}-${tab.id}` : undefined
}

function resolveTabClass(tab: StateTab): string[] {
  const active = isActive(tab)
  if (props.tabClass !== undefined) {
    return active && props.activeClass ? [props.tabClass, props.activeClass] : [props.tabClass]
  }
  return [
    BASE_CLASS,
    SIZE_CLASS[props.size],
    active ? (props.activeClass ?? ACTIVE_CLASS) : INACTIVE_CLASS,
  ]
}

function activate(tab: StateTab) {
  if (tab.disabled) return
  // Emitted even for the already-active tab: a caller may use re-activation to
  // reset its own panel state (AssistantOnlyView returns to the chat sub-tab).
  emit('update:modelValue', tab.id)
}

/** The enabled tab `step` places away from `tab`, wrapping at both ends. */
function neighbour(tab: StateTab, step: 1 | -1): StateTab | undefined {
  const enabled = enabledTabs.value
  if (enabled.length === 0) return undefined
  const index = enabled.findIndex((candidate) => candidate.id === tab.id)
  if (index === -1) return enabled[0]
  return enabled[(index + step + enabled.length) % enabled.length]
}

function onKeydown(event: KeyboardEvent, tab: StateTab) {
  if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return

  const nextKey = props.orientation === 'vertical' ? 'ArrowDown' : 'ArrowRight'
  const previousKey = props.orientation === 'vertical' ? 'ArrowUp' : 'ArrowLeft'

  let target: StateTab | undefined
  switch (event.key) {
    case nextKey:
      target = neighbour(tab, 1)
      break
    case previousKey:
      target = neighbour(tab, -1)
      break
    case 'Home':
      target = enabledTabs.value[0]
      break
    case 'End':
      target = enabledTabs.value[enabledTabs.value.length - 1]
      break
    default:
      return
  }
  if (!target) return

  event.preventDefault()
  tabElements.get(target.id)?.focus()
  activate(target)
}
</script>
