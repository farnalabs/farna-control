<template>
  <FeatureGate feature-name="webhook_notification_log" show-disabled>
    <div class="page-wide">
    <PageHeader :title="$t('views.AdminNotificationDeliveryLogView.webhook_deliveries')" :subtitle="$t('views.AdminNotificationDeliveryLogView.webhook_deliveries_subtitle')" data-testid="admin-notification-log-title">
      <p data-testid="admin-notification-log-boundary" class="text-sm text-muted-foreground">
        {{ $t('views.AdminNotificationDeliveryLogView.boundary_prefix') }}
        <RouterLink to="/admin/trigger-events" data-testid="admin-notification-log-trigger-events-link" class="font-medium text-primary-text underline hover:no-underline">{{ $t('views.AdminNotificationDeliveryLogView.boundary_link') }}</RouterLink>{{ $t('views.AdminNotificationDeliveryLogView.boundary_suffix') }}
      </p>
    </PageHeader>

    <!-- FAR-2596: the warning is announced through a PERSISTENTLY mounted
         live region, so it is announced when it appears rather than depending
         on a just-inserted aria-live node being picked up by the AT.

         The absolute "nothing will appear here" claim is made ONLY when the
         log is empty as well. A deleted webhook drops out of
         GET /api/v1/notifications while its delivery history stays in the log
         (the FK is nulled, the rows are kept), so an empty endpoint list can
         sit above a full table — in that case a quieter notice replaces the
         banner rather than contradicting the rows beneath it. -->
    <div aria-live="polite">
      <Notice
        v-if="showNoEndpointsBanner"
        variant="warning"
        :icon="false"
        padding="none"
        data-testid="admin-notification-log-no-endpoints"
        class="mb-4 px-4 py-3"
      >
        <p class="text-sm font-semibold text-warning-text">
          {{ $t('views.AdminNotificationDeliveryLogView.no_outbound_endpoints_title') }}
        </p>
        <p class="mt-1 text-sm text-muted-foreground">
          {{ $t('views.AdminNotificationDeliveryLogView.no_outbound_endpoints_body') }}
        </p>
        <template v-if="canConfigureEndpoints">
          <RouterLink
            to="/settings/teams"
            data-testid="admin-notification-log-add-endpoint"
            class="mt-2 inline-block text-sm font-medium text-primary-text underline hover:no-underline"
          >
            {{ $t('views.AdminNotificationDeliveryLogView.add_webhook_endpoint') }}
          </RouterLink>
          <!-- Endpoints are created per team: the CTA dead-ends for an org
               that has no team yet, so say what to do first. -->
          <span
            data-testid="admin-notification-log-team-hint"
            class="mt-1 block text-xs text-muted-foreground"
          >
            {{ $t('views.AdminNotificationDeliveryLogView.add_endpoint_needs_team') }}
          </span>
        </template>
      </Notice>

      <p
        v-else-if="showHistoryOnlyNotice"
        data-testid="admin-notification-log-no-endpoints-history"
        class="mb-4 text-sm text-muted-foreground"
      >
        {{ $t('views.AdminNotificationDeliveryLogView.no_outbound_endpoints_history') }}
        <RouterLink
          v-if="canConfigureEndpoints"
          to="/settings/teams"
          data-testid="admin-notification-log-add-endpoint"
          class="font-medium text-primary-text underline hover:no-underline"
        >
          {{ $t('views.AdminNotificationDeliveryLogView.add_webhook_endpoint') }}
        </RouterLink>
        <span v-if="canConfigureEndpoints" class="text-xs">
          {{ $t('views.AdminNotificationDeliveryLogView.add_endpoint_needs_team_inline') }}
        </span>
      </p>
    </div>

    <DataCard variant="bordered" padding="md" elevation="sm">
      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <div>
          <label for="adminnotificationdeliverylogview-field-4" class="mb-1 block text-xs font-medium text-muted-foreground capitalize">{{ $t('views.AdminNotificationDeliveryLogView.status') }}</label>
          <Select
  :aria-label="$t('views.AdminNotificationDeliveryLogView.status')"
  input-id="adminnotificationdeliverylogview-field-4"
  v-model="filterStatus"
  :placeholder="$t('views.AdminNotificationDeliveryLogView.status')"
  data-testid="admin-notification-log-status"
  class="w-full"
  :options="[{ value: '__all__', label: $t('views.AdminErrorsView.all_statuses') }, { value: 'delivered', label: $t('views.AdminNotificationDeliveryLogView.delivered') }, { value: 'failed', label: $t('views.AdminNotificationDeliveryLogView.failed') }, { value: 'dead_lettered', label: $t('views.AdminNotificationDeliveryLogView.dead_lettered') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
        </div>
        <div>
          <label for="adminnotificationdeliverylogview-field-3" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminAuditView.event_type') }}</label>
          <Select
  :aria-label="$t('views.AdminAuditView.event_type')"
  input-id="adminnotificationdeliverylogview-field-3"
  v-model="filterEventType"
  :placeholder="$t('views.AdminAuditView.event_type')"
  data-testid="admin-notification-log-event-type"
  class="w-full"
  :options="eventTypeOptions"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
          <output
            v-if="availableEventsError"
            aria-live="polite"
            class="block mt-1 text-xs text-destructive"
            data-testid="admin-notification-log-events-unavailable"
          >
            {{ $t('views.AdminNotificationDeliveryLogView.event_list_unavailable') }}
            <button
              type="button"
              class="ml-1 underline"
              data-testid="admin-notification-log-events-retry"
              @click="loadAvailableEvents"
            >
              {{ $t('views.AdminNotificationDeliveryLogView.retry') }}
            </button>
          </output>
        </div>
        <Field
          id="adminnotificationdeliverylogview-field-2"
          :label="$t('views.AdminNotificationDeliveryLogView.from')"
          label-class="mb-1 block text-xs font-medium text-muted-foreground"
        >
          <Input
            v-model="filterDateFrom"
            type="date"
            data-testid="admin-notification-log-date-from"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          />
        </Field>
        <Field
          id="adminnotificationdeliverylogview-field-1"
          :label="$t('views.AdminNotificationDeliveryLogView.to')"
          label-class="mb-1 block text-xs font-medium text-muted-foreground"
        >
          <Input
            v-model="filterDateTo"
            type="date"
            data-testid="admin-notification-log-date-to"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          />
        </Field>
        <div class="flex items-end gap-2">
          <Button data-testid="admin-notification-log-apply" @click="applyFilters">
            {{ $t('views.AdminNotificationDeliveryLogView.apply') }}
          </Button>
          <button
            type="button"
            data-testid="admin-notification-log-reset"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
            @click="resetFilters"
          >
            {{ $t('views.AdminNotificationDeliveryLogView.reset') }}
          </button>
          <button
            v-if="hasRetryableItems && !retryAllConfirm"
            type="button"
            :disabled="retryingAll"
            data-testid="admin-notification-log-retry-all"
            class="rounded-lg border border-warning/40 bg-warning/10 px-4 py-2 text-sm font-medium text-warning-text hover:bg-warning/20 disabled:opacity-40"
            @click="retryAllConfirm = true"
          >
            {{ $t('views.AdminNotificationDeliveryLogView.retry_all_failed') }}
          </button>
          <div v-if="retryAllConfirm" class="flex items-center gap-2">
            <span class="text-sm text-muted-foreground">{{ $t('views.AdminNotificationDeliveryLogView.retry_all_confirm') }}</span>
            <button
              type="button"
              :disabled="retryingAll"
              data-testid="admin-notification-log-retry-all-confirm"
              class="rounded-lg bg-warning px-3 py-1.5 text-sm font-medium text-warning-foreground hover:bg-warning/90 disabled:opacity-50"
              @click="retryAllFailed"
            >
              {{ retryingAll ? $t('views.AdminNotificationDeliveryLogView.retrying_all') : $t('views.AdminNotificationDeliveryLogView.confirm') }}
            </button>
            <button
              type="button"
              data-testid="admin-notification-log-retry-all-cancel"
              class="rounded-lg border border-input bg-background px-3 py-1.5 text-sm font-medium hover:bg-accent"
              @click="retryAllConfirm = false"
            >
              {{ $t('views.AdminNotificationDeliveryLogView.cancel') }}
            </button>
          </div>
        </div>
      </div>
      <div v-if="total > 0" class="mt-3 text-sm text-muted-foreground">
        {{ $t('views.AdminNotificationDeliveryLogView.deliveries_count', { count: total }, total) }}
      </div>
      <Notice
        v-if="retrySuccessMessage"
        variant="success"
        :icon="false"
        padding="none"
        data-testid="admin-notification-log-retry-success"
        class="mt-3 px-4 py-3 text-sm"
      >
        {{ retrySuccessMessage }}
      </Notice>
    </DataCard>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadDeliveries" />

    <EmptyState
      v-else-if="items.length === 0 && hasActiveFilters"
      data-testid="admin-notification-log-empty-filters"
      :title="$t('views.AdminNotificationDeliveryLogView.no_deliveries_match_title')"
      :description="$t('views.AdminNotificationDeliveryLogView.no_deliveries_match_body')"
    >
       <button
         type="button"
         data-testid="admin-notification-log-empty-reset"
         :aria-label="$t('views.AdminNotificationDeliveryLogView.reset_all_filters')"
         class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
         @click="resetFilters"
       >
         {{ $t('views.AdminNotificationDeliveryLogView.reset_filters') }}
       </button>
     </EmptyState>

    <!-- FAR-2596: "No deliveries yet" is only rendered once the endpoint read
         has positively resolved to `configured`. While the state is `unknown`
         (still loading, failed, or a malformed body) nothing is claimed; when
         it is `none` the banner above is the whole story. -->
    <EmptyState
      v-else-if="items.length === 0 && endpointsConfigured"
      data-testid="admin-notification-log-empty"
      :title="$t('views.AdminNotificationDeliveryLogView.no_deliveries_yet_title')"
      :description="$t('views.AdminNotificationDeliveryLogView.no_deliveries_yet_body')"
    />

    <template v-else>
      <div class="table-wrapper focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset" role="region" tabindex="0" :aria-label="$t('components.DataTable.scroll_region_label')"> <!-- NOSONAR: a named, focusable scroll region keeps the wide delivery table keyboard-reachable (axe scrollable-region-focusable), including when there are no rows. -->
        <table class="w-full">
          <thead>
            <tr>
              <th class="w-8 table-header"></th>
              <th class="table-header">{{ $t('views.AdminNotificationDeliveryLogView.timestamp') }}</th>
              <th class="table-header">{{ $t('views.AdminAuditView.event_type') }}</th>
              <th class="table-header">{{ $t('views.AdminNotificationDeliveryLogView.destination') }}</th>
              <th class="table-header capitalize">{{ $t('views.AdminNotificationDeliveryLogView.status') }}</th>
              <th class="table-header table-cell-numeric">{{ $t('views.AdminNotificationDeliveryLogView.attempts') }}</th>
              <th class="table-header">{{ $t('views.AdminNotificationDeliveryLogView.error') }}</th>
              <th class="table-header">{{ $t('views.AdminNotificationDeliveryLogView.actions') }}</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-border">
            <tr
              v-for="entry in items"
              :key="entry.id"
              class="transition-colors hover:bg-muted/30 cursor-pointer"
              tabindex="0"
              @click="toggleRow(entry.id)"
              @keydown.enter="toggleRow(entry.id)"
              @keydown.space.prevent="toggleRow(entry.id)"
            >
              <td class="table-cell text-muted-foreground">
                <button
                  type="button"
                  class="inline-flex items-center rounded p-1 hover:bg-muted/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  :aria-label="$t('views.AdminNotificationDeliveryLogView.expand_delivery', { id: entry.id })"
                  :data-testid="'admin-notification-log-expand-' + entry.id"
                  @click.stop="toggleRow(entry.id)"
                >
                  <ChevronRight
                    :class="expandedId === entry.id ? 'rotate-90' : ''"
                    class="h-3.5 w-3.5 transition-transform"
                    aria-hidden="true"
                  />
                </button>
              </td>
              <td class="table-cell whitespace-nowrap text-muted-foreground">
                {{ formatTimestamp(entry.created_at) }}
              </td>
              <td class="table-cell font-medium">{{ deliveryLabel(entry.event_type) }}</td>
              <td class="table-cell max-w-xs truncate text-muted-foreground" :title="entry.endpoint_url ?? undefined">
                {{ entry.endpoint_url || '—' }}
              </td>
              <td class="table-cell">
                <span :class="statusBadge(entry.status)">
                  {{ deliveryLabel(entry.status) }}
                </span>
              </td>
              <td class="table-cell table-cell-numeric text-muted-foreground">{{ entry.attempt_count }}</td>
              <td class="table-cell max-w-xs truncate text-muted-foreground" :title="entry.last_error ?? undefined">
                {{ entry.last_error || '—' }}
              </td>
              <td class="table-cell">
                <div v-if="retryMessages[entry.id]" class="text-xs" :class="retryMessages[entry.id].type === 'error' ? 'text-destructive' : 'text-success-text'">
                  {{ retryMessages[entry.id].text }}
                </div>
                <button
                  v-else-if="entry.status === 'failed' || entry.status === 'dead_lettered'"
                  type="button"
                  :disabled="retryingId === entry.id"
                  data-testid="admin-notification-log-retry"
                  class="rounded-md bg-primary/10 px-2.5 py-1 text-xs font-medium text-primary-text hover:bg-primary/20 disabled:opacity-40"
                  @click.stop="retryDelivery(entry)"
                >
                  {{ retryingId === entry.id ? $t('views.AdminNotificationDeliveryLogView.retrying') : $t('views.AdminNotificationDeliveryLogView.retry') }}
                </button>
              </td>
            </tr>
            <template v-if="expandedId">
              <tr v-for="entry in expandedEntries" :key="`exp-${entry.id}`">
              <td colspan="8" class="table-cell bg-muted/20">
                <div class="space-y-2 text-sm">
                  <div v-if="entry.response_body" class="rounded border bg-card p-3">
                    <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminNotificationDeliveryLogView.response_body') }}</span>
                    <pre class="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all font-mono text-xs">{{ entry.response_body }}</pre>
                  </div>
                  <div v-if="entry.last_error" class="rounded border border-destructive/30 bg-destructive/10 p-3">
                    <span class="text-xs font-medium text-destructive-text">{{ $t('views.AdminNotificationDeliveryLogView.error_details') }}</span>
                    <pre class="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all font-mono text-xs text-destructive-text">{{ entry.last_error }}</pre>
                  </div>
                  <div v-if="entry.response_code" class="text-muted-foreground">
                    <span class="text-xs font-medium">{{ $t('views.AdminNotificationDeliveryLogView.http_response_code') }}</span>
                    <code class="ml-1 font-mono text-xs">{{ entry.response_code }}</code>
                  </div>
                  <div v-if="!entry.response_body && !entry.last_error && !entry.response_code" class="text-xs text-muted-foreground italic">
                    {{ $t('views.AdminNotificationDeliveryLogView.no_additional_details') }}
                  </div>
                </div>
              </td>
            </tr>
            </template>
          </tbody>
        </table>
      </div>

      <p
        data-testid="admin-notification-log-dead-lettered-help"
        class="mt-2 text-xs text-muted-foreground"
      >
        {{ $t('views.AdminNotificationDeliveryLogView.dead_lettered_help') }}
      </p>

      <div class="flex items-center justify-between">
        <button
          type="button"
          :disabled="!prevCursor"
          data-testid="admin-notification-log-previous"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-30 disabled:cursor-not-allowed"
          @click="goToPage(prevCursor)"
        >
          {{ $t('views.AdminNotificationDeliveryLogView.previous') }}
        </button>
        <span class="text-sm text-muted-foreground">
          {{ $t('views.AdminNotificationDeliveryLogView.of_deliveries', { count: items.length, total }) }}
        </span>
        <button
          type="button"
          :disabled="!nextCursor"
          data-testid="admin-notification-log-next"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-30 disabled:cursor-not-allowed"
          @click="goToPage(nextCursor)"
        >
          {{ $t('views.AdminNotificationDeliveryLogView.next') }}
        </button>
      </div>

      <DataCard
        v-if="deadLetteredCount > 0"
        data-testid="admin-notification-log-dlq"
        elevation="sm"
        :title="$t('views.AdminNotificationDeliveryLogView.dead_letter_queue')"
        :description="$t('views.AdminNotificationDeliveryLogView.dead_letter_queue_description', { count: deadLetteredCount }, deadLetteredCount)"
      >
        <template #headerActions>
          <button
            type="button"
            data-testid="admin-notification-log-dlq-filter"
            class="rounded-lg border border-input bg-background px-3 py-1.5 text-sm font-medium hover:bg-accent"
            @click="showDeadLettered"
          >
            {{ $t('views.AdminNotificationDeliveryLogView.view_dead_lettered') }}
          </button>
        </template>
      </DataCard>
    </template>
  </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import FeatureGate from '../components/FeatureGate.vue'
import { ref, computed, onMounted } from 'vue'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import { formatApiError } from '../lib/api/formatError'
import { dateRangeQuery, defaultDateRange } from '../lib/formatDate'
import type { components, paths } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import Notice from '../components/shared/Notice.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import DataCard from '../components/ui/DataCard.vue'
import Field from '../components/ui/Field.vue'
import Input from '../components/ui/Input.vue'
import Button from '../components/ui/Button.vue'
import Select from '../components/shared/AppSelect.vue'
import { ChevronRight } from '@lucide/vue'
import { useI18n } from 'vue-i18n'
import { usePlanStore } from '../stores/planStore'

const { t, te } = useI18n()
const planStore = usePlanStore()

type DeliveryLogEntry = components['schemas']['DeliveryLogEntry']
interface DeliveryLogPage {
  items: DeliveryLogEntry[]
  total: number
  next_cursor: string | null
  prev_cursor?: string | null
}
type DeliveryLogQuery = NonNullable<paths['/api/v1/admin/notifications/deliveries']['get']>['parameters']['query']

const cursor = ref<string | null>(null)
const initialDateRange = defaultDateRange()
const filterStatus = ref('__all__')
const filterEventType = ref('__all__')
const filterDateFrom = ref(initialDateRange.from)
const filterDateTo = ref(initialDateRange.to)

const { data: deliveriesData, loading, error, load: loadDeliveries } = useDataFetch<DeliveryLogPage>(
  async () => {
    const params: DeliveryLogQuery = { limit: 50 }
    if (cursor.value) params.cursor = cursor.value
    if (filterStatus.value !== '__all__') params.status = filterStatus.value
    if (filterEventType.value !== '__all__') params.event_type = filterEventType.value
    Object.assign(params, dateRangeQuery(filterDateFrom.value, filterDateTo.value))
    const response = await api.GET('/api/v1/admin/notifications/deliveries', { params: { query: params } })
    return { data: response.data as unknown as DeliveryLogPage | undefined, error: response.error }
  },
  { initialValue: { items: [] as DeliveryLogEntry[], total: 0, next_cursor: null as string | null, prev_cursor: null as string | null } }
)

const items = computed(() => deliveriesData.value?.items ?? [])

type EndpointState = 'unknown' | 'none' | 'configured'

/**
 * FAR-2596 — "no outbound webhooks are configured" is read from the org's
 * endpoint list (GET /api/v1/notifications), NEVER inferred from the delivery
 * log: a soft-deleted endpoint leaves its historical deliveries behind, so an
 * empty log cannot tell "nothing was ever configured" apart from "configured,
 * nothing has fired yet". This page lists outbound deliveries only; inbound
 * trigger webhooks are recorded on the Trigger Event Log.
 *
 * ONE tri-state drives every surface that would otherwise guess, so no claim is
 * rendered before the read resolves:
 *
 * - ``unknown``    — not resolved yet, or the read failed / returned an
 *                    unexpected body. Fail open: no warning, and no
 *                    "your webhooks are configured" claim either.
 * - ``none``       — positively read an EMPTY list.
 * - ``configured`` — positively read at least one endpoint.
 */
const endpointState = ref<EndpointState>('unknown')
const endpointsConfigured = computed(() => endpointState.value === 'configured')
const noEndpointsConfigured = computed(() => endpointState.value === 'none')

/**
 * Best-effort and FAIL OPEN: never surfaces an error and never touches the
 * page's error state. A failure (or a body that is not an array) leaves the
 * state ``unknown``, so it can never accuse the operator of having no
 * endpoints.
 */
async function loadEndpoints(): Promise<void> {
  try {
    const response = await api.GET('/api/v1/notifications')
    const body: unknown = response.data
    if (!Array.isArray(body)) return
    endpointState.value = body.length === 0 ? 'none' : 'configured'
  } catch {
    // Leave `unknown` — no claim either way.
  }
}

onMounted(() => {
  void loadEndpoints()
})

/**
 * The empty state must distinguish "your webhooks are configured, nothing has
 * fired yet" from "the current filters match nothing". The default range is
 * always applied, so a user-applied filter is anything differing from the
 * initial defaults.
 */
const hasActiveFilters = computed(
  () =>
    filterStatus.value !== '__all__' ||
    filterEventType.value !== '__all__' ||
    filterDateFrom.value !== initialDateRange.from ||
    filterDateTo.value !== initialDateRange.to
)

/**
 * The CTA mirrors FeatureGate's own predicate for `/settings/teams`
 * (`feature-name="team_rbac"`), so the warning never
 * links into a disabled gate.
 */
const canConfigureEndpoints = computed(
  () => planStore.featureEnabled('team_rbac')
)

/**
 * Render an event type / delivery status with its human label. The locale
 * namespace carries labels for the registry events and the delivery statuses,
 * so a raw `run_failed` / `dead_lettered` never reaches the table. A value with
 * no label (an event registered on the server before its copy shipped, or an
 * unexpected status) degrades to itself rather than to blank — the same
 * fallback the event-type filter already uses.
 */
function deliveryLabel(value: string): string {
  const key = `views.AdminNotificationDeliveryLogView.${value}`
  return te(key) ? t(key) : value
}

/**
 * FAR-1319 — the event-type filter is driven by the server-side registry
 * (GET /api/v1/admin/notifications/available-events) instead of a hardcoded
 * subset, so a newly registered event (e.g. hitl_deadline_warning) is
 * filterable without a frontend edit. Fetched independently of the deliveries
 * query: a registry failure leaves the filter at "All types" rather than
 * breaking the delivery list itself, and surfaces a small inline retry so the
 * admin can re-fetch without reloading the page.
 */
const { data: availableEventsData, error: availableEventsError, load: loadAvailableEvents } = useDataFetch<string[]>(
  async () => {
    const response = await api.GET('/api/v1/admin/notifications/available-events')
    // Guard the shape: a mocked/failed response must degrade to an empty
    // event list, never throw inside the options computed.
    const events = Array.isArray(response.data) ? response.data : []
    return { data: events, error: response.error }
  },
  { initialValue: [] }
)

const eventTypeOptions = computed(() => [
  { value: '__all__', label: t('views.AdminNotificationDeliveryLogView.all_types') },
  ...availableEventsData.value.map(eventType => ({
    value: eventType,
    label: deliveryLabel(eventType),
  })),
])
const total = computed(() => deliveriesData.value?.total ?? 0)

/**
 * FAR-2596: a deleted webhook disappears from the endpoint list while its
 * delivery history stays in the log (the FK is nulled, the rows are kept), so
 * the absolute "nothing will appear here" claim is only true when the LOG is
 * empty too. With retained history a quieter notice replaces the banner rather
 * than contradicting the table beneath it.
 */
const logIsEmpty = computed(() => items.value.length === 0 && total.value === 0)
const showNoEndpointsBanner = computed(
  () => noEndpointsConfigured.value && logIsEmpty.value && !loading.value
)
const showHistoryOnlyNotice = computed(
  () => noEndpointsConfigured.value && !logIsEmpty.value
)
const nextCursor = computed(() => deliveriesData.value?.next_cursor ?? null)
const prevCursor = computed(() => {
  const dc = deliveriesData.value
  if (!dc?.prev_cursor) return null
  return dc.prev_cursor
})

const expandedId = ref<string | null>(null)
const retryingId = ref<string | null>(null)
const retryingAll = ref(false)
const retryAllConfirm = ref(false)
const retrySuccessMessage = ref<string | null>(null)
const retryMessages = ref<Record<string, { type: string; text: string }>>({})

const hasRetryableItems = computed(() => items.value.some(e => e.status === 'failed' || e.status === 'dead_lettered'))

const deadLetteredCount = computed(() => items.value.filter(e => e.status === 'dead_lettered').length)

const expandedEntries = computed(() => {
  if (!expandedId.value) return []
  return items.value.filter(e => e.id === expandedId.value)
})

function formatTimestamp(ts: string | null): string {
  if (!ts) return '—'
  const d = new Date(ts)
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function statusBadge(status: string): string {
  if (status === 'delivered' || status === 'success') return 'badge badge-status-success'
  if (status === 'failed') return 'badge badge-status-destructive'
  if (status === 'dead_lettered') return 'badge badge-context-slate'
  if (status === 'pending') return 'badge badge-status-warning'
  return 'badge badge-context-slate'
}

function toggleRow(id: string) {
  expandedId.value = expandedId.value === id ? null : id
}

function goToPage(c: string | null) {
  if (!c) return
  cursor.value = c
  retrySuccessMessage.value = null
  void loadDeliveries()
}

function applyFilters() {
  cursor.value = null
  retrySuccessMessage.value = null
  void loadDeliveries()
}

function resetFilters() {
  const dateRange = defaultDateRange()
  filterStatus.value = '__all__'
  filterEventType.value = '__all__'
  filterDateFrom.value = dateRange.from
  filterDateTo.value = dateRange.to
  cursor.value = null
  retrySuccessMessage.value = null
  void loadDeliveries()
}

function showDeadLettered() {
  filterStatus.value = 'dead_lettered'
  filterEventType.value = '__all__'
  filterDateFrom.value = ''
  filterDateTo.value = ''
  cursor.value = null
  retrySuccessMessage.value = null
  void loadDeliveries()
}

async function retryDelivery(entry: DeliveryLogEntry) {
  if (!entry.endpoint_id) {
    retryMessages.value[entry.id] = { type: 'error', text: t('views.AdminNotificationDeliveryLogView.cannot_retry_missing_endpoint_id') }
    return
  }
  retryingId.value = entry.id
  error.value = null
  delete retryMessages.value[entry.id]
  try {
    const { data, error: err } = await api.POST(
      '/api/v1/admin/notifications/{webhook_id}/deliveries/{delivery_id}/retry',
      {
        params: {
          path: {
            webhook_id: entry.endpoint_id,
            delivery_id: entry.id,
          },
        },
      },
    )
    if (err) {
      retryMessages.value[entry.id] = { type: 'error', text: `${t('views.AdminNotificationDeliveryLogView.retry_failed')} ${formatApiError(err)}` }
    } else if (data) {
      if (data.success) {
        await loadDeliveries()
        retryMessages.value[entry.id] = { type: 'success', text: t('views.AdminNotificationDeliveryLogView.retry_succeeded') }
      } else {
        await loadDeliveries()
        const failureDetail = data.error || `HTTP ${data.status_code}`
        retryMessages.value[entry.id] = { type: 'error', text: `${t('views.AdminNotificationDeliveryLogView.retry_failed')} ${failureDetail}` }
      }
    }
  } catch (e: unknown) {
    retryMessages.value[entry.id] = { type: 'error', text: `${t('views.AdminNotificationDeliveryLogView.retry_request_failed')} ${formatApiError(e)}` }
  } finally {
    retryingId.value = null
  }
}

async function retryAllFailed() {
  retryingAll.value = true
  retrySuccessMessage.value = null
  error.value = null
  retryMessages.value = {}
  try {
    const { data, error: err } = await api.POST('/api/v1/admin/notifications/deliveries/retry-all-failed', {})
    if (err) {
      error.value = `${t('views.AdminNotificationDeliveryLogView.retry_all_failed_1')} ${formatApiError(err)}`
    } else if (data) {
      await loadDeliveries()
      const result = data as unknown as { retried: number; success: boolean; errors?: unknown[] }
      const retried = t('views.AdminNotificationDeliveryLogView.retried_deliveries_count', { count: result.retried }, result.retried)
      retrySuccessMessage.value = result.success
        ? retried
        : t('views.AdminNotificationDeliveryLogView.retried_with_errors', {
            retried,
            errors: t('views.AdminNotificationDeliveryLogView.errors_count', { count: result.errors?.length || 0 }, result.errors?.length || 0),
          })
    }
  } catch (e: unknown) {
    error.value = `${t('views.AdminNotificationDeliveryLogView.retry_all_request_failed')} ${formatApiError(e)}`
  } finally {
    retryingAll.value = false
    retryAllConfirm.value = false
  }
}

/* onMounted handled by useDataFetch */
</script>
