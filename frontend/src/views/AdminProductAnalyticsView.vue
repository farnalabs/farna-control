<template>

    <div class="page-wide">
    <PageHeader :title="$t('views.AdminProductAnalyticsView.title')" :subtitle="$t('views.AdminProductAnalyticsView.subtitle')" />

    <LoadingSpinner v-if="store.isLoading" />
    <ErrorAlert v-else-if="store.error" :message="store.error" :on-retry="store.fetchTransparency" />

    <template v-else-if="store.transparency">
      <div class="space-y-6">
        <!-- Warning banner: one shell, content chosen by the warning value so
             the two cases stay distinct without a second hand-rolled card. -->
        <Notice v-if="warningMessage" variant="warning" :icon="false">
          <p class="text-sm font-medium">{{ warningMessage }}</p>
        </Notice>

        <!-- Transparency data -->
        <DataCard variant="bordered" heading-level="h2" :title="$t('views.AdminProductAnalyticsView.delivery_status')">
          <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div>
              <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminProductAnalyticsView.last_successful_dump') }}</span>
              <p class="mt-0.5 text-lg font-semibold" data-testid="last-dump">
                {{ store.transparency.last_successful_dump_at ? formatDate(store.transparency.last_successful_dump_at) : '—' }}
              </p>
            </div>
            <div>
              <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminProductAnalyticsView.total_dumps') }}</span>
              <p class="mt-0.5 text-lg font-semibold" data-testid="dump-count-total">
                {{ store.transparency.dump_count_total }}
              </p>
            </div>
          </div>
          <!-- Recorded failure detail: rendered even when no warning fires
               (e.g. consent is off but the last attempt still failed). -->
          <div
            v-if="store.transparency.last_failed_dump_at || store.transparency.last_dump_failure"
            class="mt-4 space-y-1 text-sm text-amber-700 dark:text-amber-300"
            data-testid="last-dump-failure-detail"
          >
            <p v-if="store.transparency.last_failed_dump_at" data-testid="last-failed-dump">
              {{ $t('views.AdminProductAnalyticsView.last_failed_dump', { when: formatDate(store.transparency.last_failed_dump_at) }) }}
            </p>
            <p v-if="store.transparency.last_dump_failure" class="break-words" data-testid="last-dump-failure">
              {{ store.transparency.last_dump_failure }}
            </p>
          </div>
        </DataCard>

        <DataCard variant="bordered" heading-level="h2" :title="$t('views.AdminProductAnalyticsView.consent_and_enforcement')">
          <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <div>
              <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminProductAnalyticsView.consent_level') }}</span>
              <p class="mt-0.5">
                <Badge :variant="store.transparency.consent_level === 'all' ? 'success' : 'neutral'" data-testid="consent-level">
                  {{ consentLevelLabel }}
                </Badge>
              </p>
            </div>
            <div>
              <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminProductAnalyticsView.egress') }}</span>
              <p class="mt-0.5">
                <Badge :variant="egressAllowed ? 'success' : 'neutral'" data-testid="egress-allowed">
                  {{ egressAllowed ? $t('views.AdminProductAnalyticsView.egress_allowed') : $t('views.AdminProductAnalyticsView.egress_blocked') }}
                </Badge>
              </p>
            </div>
            <div>
              <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminProductAnalyticsView.instance_switch') }}</span>
              <p class="mt-0.5">
                <Badge :variant="instanceEnabled ? 'success' : 'neutral'" data-testid="instance-enabled">
                  {{ instanceEnabled ? $t('views.AdminProductAnalyticsView.enabled') : $t('views.AdminProductAnalyticsView.disabled') }}
                </Badge>
              </p>
            </div>
            <div>
              <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminProductAnalyticsView.licence') }}</span>
              <p class="mt-0.5">
                <span :class="licenceRequiresAnalytics ? 'badge badge-context-purple' : 'badge badge-status-muted'" data-testid="licence-requires-analytics">
                  {{ licenceRequiresAnalytics ? $t('views.AdminProductAnalyticsView.licence_required') : $t('views.AdminProductAnalyticsView.licence_not_required') }}
                </span>
              </p>
            </div>
          </div>
        </DataCard>

        <DataCard variant="bordered" heading-level="h2" :title="$t('views.AdminProductAnalyticsView.instance_switch_control')">
          <div class="space-y-3">
            <p class="text-sm text-muted-foreground" data-testid="instance-switch-source">
              {{ switchSourceCopy }}
            </p>
            <div class="flex flex-wrap items-center gap-3">
              <Checkbox
                ref="toggleEl"
                :model-value="instanceEnabled"
                :disabled="saving"
                :label="$t('views.AdminProductAnalyticsView.instance_switch_label')"
                class="inline-flex items-center gap-2 text-sm"
                data-testid="instance-switch-toggle"
                @update:model-value="onToggle"
              />
              <button
                v-if="hasExplicitOperatorValue"
                type="button"
                class="rounded-lg border border-input px-3 py-1.5 text-sm transition-colors hover:bg-accent"
                data-testid="instance-switch-reset"
                :disabled="saving"
                @click="onReset"
              >
                {{ $t('views.AdminProductAnalyticsView.use_licence_default') }}
              </button>
            </div>
            <p v-if="saveError" class="text-sm text-destructive" data-testid="instance-switch-error" aria-live="assertive">
              {{ saveError }}
            </p>
          </div>
        </DataCard>
      </div>
    </template>
    </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { parseISO } from 'date-fns'
import PageHeader from '../components/shared/PageHeader.vue'
import Badge from '../components/ui/Badge.vue'
import DataCard from '../components/ui/DataCard.vue'
import Checkbox from '../components/ui/Checkbox.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import Notice from '../components/shared/Notice.vue'
import { useProductAnalyticsStore } from '../stores/productAnalyticsStore'
import { formatDateShort } from '../lib/formatDate'
import { formatApiError } from '../lib/api/formatError'
import { api } from '../lib/api/client'
import { useI18n } from 'vue-i18n'

/**
 * The fields this view reads from the transparency response. Declared locally
 * (rather than widening the store's own interface) so the view - the only
 * consumer of the switch provenance - owns the contract it renders.
 */
type SwitchSource = 'operator' | 'environment' | 'licence' | 'default'

interface TransparencyView {
  consent_level: string
  instance_enabled: boolean
  instance_switch_source: SwitchSource
  licence_requires_analytics: boolean
  egress_allowed: boolean
  last_failed_dump_at: string | null
  last_dump_failure: string | null
}

const store = useProductAnalyticsStore()
const { t } = useI18n()
const saving = ref(false)
const saveError = ref<string | null>(null)
const toggleEl = ref<InstanceType<typeof Checkbox> | null>(null)

const view = computed<TransparencyView | null>(
  () => (store.transparency as unknown as TransparencyView | null) ?? null,
)

const consentLevelLabel = computed(() => {
  const map: Record<string, string> = {
    all: t('views.AdminProductAnalyticsView.level_all'),
    off: t('views.AdminProductAnalyticsView.level_off'),
  }
  return map[view.value?.consent_level ?? ''] || view.value?.consent_level || t('views.AdminProductAnalyticsView.level_unknown')
})

// The transparency endpoint returns ``egress_allowed`` on ``TransparencyData``.
const egressAllowed = computed(() => view.value?.egress_allowed === true)
const instanceEnabled = computed(() => view.value?.instance_enabled === true)
const licenceRequiresAnalytics = computed(() => view.value?.licence_requires_analytics === true)

// Only an operator-set value is a stored system_config row that "Use licence
// default" can actually delete. An environment-sourced switch has no stored row
// (the deployment's env var owns it), so offering the reset would be a silent
// no-op; a licence/default value needs no reset either.
const hasExplicitOperatorValue = computed(
  () => view.value?.instance_switch_source === 'operator',
)

const switchSourceCopy = computed(() => {
  const source = view.value?.instance_switch_source ?? 'default'
  return t(`views.AdminProductAnalyticsView.switch_source_${source}`)
})

// The backend distinguishes a stale previous success (`not_reaching_farnalabs`)
// from an opted-in instance that has NEVER delivered (`never_reached_farnalabs`).
// Both must surface; the messages differ so the operator can tell them apart.
const warningMessage = computed(() => {
  const warning = store.transparency?.warning
  if (warning === 'not_reaching_farnalabs') {
    return t('views.AdminProductAnalyticsView.warning_not_reaching')
  }
  if (warning === 'never_reached_farnalabs') {
    return t('views.AdminProductAnalyticsView.warning_never_reached')
  }
  return ''
})

// `:checked` only patches the DOM when the bound value CHANGES. A user-flipped
// checkbox therefore stays flipped when the write fails (or the server's
// effective value is unchanged), so re-assert the committed state after every
// attempt settles. The native control is exposed by the Checkbox primitive.
function syncToggleDom(): void {
  const input = toggleEl.value?.inputEl
  if (input) input.checked = instanceEnabled.value
}

async function saveSwitch(enabled: boolean | null): Promise<void> {
  saving.value = true
  saveError.value = null
  try {
    const { error } = await api.PUT('/api/v1/product-analytics/instance-switch', {
      body: { enabled },
    })
    if (error) {
      saveError.value = formatApiError(error)
      return
    }
    // Re-read the full transparency state so the badge, source copy and the
    // reset affordance all reflect the committed value.
    await store.fetchTransparency()
  } catch (e: unknown) {
    saveError.value = formatApiError(e)
  } finally {
    saving.value = false
    syncToggleDom()
  }
}

function onToggle(checked: boolean): void {
  void saveSwitch(checked)
}

function onReset(): void {
  void saveSwitch(null)
}

onMounted(() => {
  store.fetchTransparency()
})

// The dump watermark is a DATE-only value. Parse it as a calendar date so it is
// not shifted a day backwards in timezones behind UTC — a bare ``new Date()``
// reads ``YYYY-MM-DD`` as UTC midnight.
function formatDate(dateStr: string): string {
  const d = parseISO(dateStr)
  if (Number.isNaN(d.getTime())) return '—'
  return formatDateShort(d)
}
</script>
