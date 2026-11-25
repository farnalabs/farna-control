<template>
  <div :class="rootClass" :aria-live="liveAttr" :role="roleAttr">
    <template v-if="!icon && !dismissible">
      <p v-if="title" class="font-semibold">{{ title }}</p>
      <p v-if="message" :class="title ? 'mt-1' : ''">{{ message }}</p>
      <slot />
      <div v-if="$slots.actions" class="mt-2 flex flex-wrap items-center gap-2">
        <slot name="actions" />
      </div>
    </template>
    <div v-else class="flex items-start gap-2">
      <component :is="iconComponent" v-if="icon" :size="16" class="mt-0.5 shrink-0" aria-hidden="true" />
      <div class="min-w-0 flex-1">
        <p v-if="title" class="font-semibold">{{ title }}</p>
        <p v-if="message" :class="title ? 'mt-1' : ''">{{ message }}</p>
        <slot />
        <div v-if="$slots.actions" class="mt-2 flex flex-wrap items-center gap-2">
          <slot name="actions" />
        </div>
      </div>
      <button
        v-if="dismissible"
        type="button"
        class="shrink-0 -m-1 p-1"
        :aria-label="dismissLabel || $t('common.dismiss')"
        data-testid="notice-dismiss"
        @click="dismiss"
      >
        <X :size="16" aria-hidden="true" />
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
/*
 * The ONE shared semantic banner surface.
 *
 * `ErrorAlert` owns FAILED OPERATIONS (load/save/mutation): it carries
 * `role="alert"`, retry and dismiss. `Notice` owns STATE AND CONSEQUENCE
 * messages - success confirmations, warnings, permission/limit notices and
 * destructive consequence warnings that are not operation failures.
 *
 * Live-region contract: `live` defaults to `'none'`, so a static banner is NOT
 * a live region on mount. Pass `live="polite"` (or `"assertive"` for an error
 * that interrupts) ONLY where the banner appears in response to a user action.
 * An explicitly passed `aria-live`/`role` still wins via attribute fall-through.
 *
 * Visual contract: the variant token pair is the only styling the primitive
 * adds besides the radius, border and padding. A caller keeps its own text
 * size, margin and layout classes through `class`; a banner that had no icon
 * passes `:icon="false"` so adoption is visually identical.
 */
import { computed } from 'vue'
import { AlertTriangle, Check, Info, X, XCircle } from '@lucide/vue'

type NoticeVariant = 'success' | 'warning' | 'danger' | 'info'

const props = withDefaults(defineProps<{
  variant?: NoticeVariant
  title?: string
  message?: string
  dismissible?: boolean
  onDismiss?: () => void
  dismissLabel?: string
  live?: 'none' | 'polite' | 'assertive'
  icon?: boolean
  padding?: 'none' | 'xs' | 'sm' | 'md'
  radius?: 'sm' | 'md' | 'lg'
}>(), {
  variant: 'info',
  dismissible: false,
  live: 'none',
  icon: true,
  padding: 'md',
  radius: 'lg',
})

const emit = defineEmits<{ (e: 'dismiss'): void }>()

// The file keeps the contract name (`Notice.vue`) while the runtime component
// name satisfies vue/multi-word-component-names (same pattern as Pagination).
defineOptions({ name: 'InlineNotice' })

const VARIANT_CLASS: Record<NoticeVariant, string> = {
  success: 'border-success/50 bg-success/10 text-success-text',
  warning: 'border-warning/40 bg-warning/10 text-warning-text',
  danger: 'border-destructive/50 bg-destructive/10 text-destructive-text',
  info: 'border-primary/20 bg-primary/5 text-primary-text',
}

const VARIANT_ICON = {
  success: Check,
  warning: AlertTriangle,
  danger: XCircle,
  info: Info,
}

const PADDING_CLASS = {
  none: '',
  xs: 'p-2',
  sm: 'p-3',
  md: 'p-4',
}

const RADIUS_CLASS = {
  sm: 'rounded',
  md: 'rounded-md',
  lg: 'rounded-lg',
}

const rootClass = computed(() =>
  [
    RADIUS_CLASS[props.radius],
    'border',
    VARIANT_CLASS[props.variant],
    PADDING_CLASS[props.padding],
  ].filter(Boolean).join(' '),
)
const iconComponent = computed(() => VARIANT_ICON[props.variant])
const liveAttr = computed(() => (props.live === 'none' ? undefined : props.live))
const roleAttr = computed(() => (props.live === 'assertive' ? 'alert' : undefined))

function dismiss(): void {
  // Vue resolves an `onDismiss` prop as this event's handler, so emitting is
  // the single call path; calling props.onDismiss here too would double-fire.
  emit('dismiss')
}
</script>
