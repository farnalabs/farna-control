<template>
  <Dialog v-model:visible="modelValue" :id="dialogId" :modal="true" :dismissable-mask="true" :style="{ width }">
    <template #header>
      <div>
        <div :id="`${dialogId}_header`" class="text-lg font-semibold">{{ title }}</div>
        <div v-if="description" class="mt-0.5 text-sm text-muted-foreground">{{ description }}</div>
      </div>
    </template>
    <slot />
    <template #footer>
      <div class="flex gap-2 justify-end">
        <Button
          variant="secondary"
          :disabled="cancelDisabled"
          :data-testid="cancelTestId"
          @click="modelValue = false"
        >{{ $t('common.cancel') }}</Button>
        <Button
          :variant="confirmVariant"
          :outlined="confirmOutlined"
          :data-testid="confirmTestId"
          :disabled="confirmDisabled || loading"
          :loading="loading"
          @click="emit('confirm')"
        >
          {{ loading ? (confirmLoadingText || confirmText || $t('common.confirm')) : (confirmText || $t('common.confirm')) }}
        </Button>
      </div>
    </template>
  </Dialog>
</template>

<script setup lang="ts">
import { computed, useId } from 'vue'
import Dialog from 'primevue/dialog'
import Button from '../ui/Button.vue'

// PrimeVue names a dialog via `aria-labelledby="<id>_header"` and renders the
// matching id on its default title span. Because this component replaces the
// `header` slot, the id must be recreated explicitly or the dialog has no
// accessible name. Passing the id to Dialog makes its `$id` (and therefore the
// generated `_header` reference) stable and unique per instance.
const dialogId = useId()

const props = withDefaults(defineProps<{
  title: string
  description?: string
  confirmText?: string
  confirmLoadingText?: string
  confirmDisabled?: boolean
  loading?: boolean
  /** Disable Cancel while an in-flight request must not be dismissed. */
  cancelDisabled?: boolean
  /*
   * The confirm action's tone. Narrowed to the tones the shared Button
   * primitive can express without changing rendering, so an unsupported
   * PrimeVue severity cannot silently fall back to primary. Every call site
   * passes `danger` or nothing.
   */
  confirmSeverity?: 'primary' | 'secondary' | 'danger'
  cancelTestId?: string
  confirmTestId?: string
  /** Dialog width — the historical default is 28rem. */
  width?: string
}>(), { width: '28rem' })

const confirmVariant = computed<'primary' | 'secondary' | 'danger'>(() => {
  if (props.confirmSeverity === 'danger') return 'danger'
  if (props.confirmSeverity === 'secondary') return 'secondary'
  return 'primary'
})

// The primitive's `secondary` variant is outlined; the historical
// `confirmSeverity="secondary"` rendered solid, so keep it solid with the
// primitive's documented outlined override.
const confirmOutlined = computed<boolean | null>(() => (props.confirmSeverity === 'secondary' ? false : null))

const modelValue = defineModel<boolean>('open')
const emit = defineEmits<{
  confirm: []
}>()
</script>
