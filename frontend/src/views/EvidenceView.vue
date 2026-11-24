<template>
  <div class="page-wide space-y-6">
    <PageHeader
      :title="$t('views.EvidenceView.title')"
      :subtitle="$t('views.EvidenceView.subtitle')"
    />

    <DataCard
      :title="$t('views.EvidenceView.run_picker_title')"
      :description="$t('views.EvidenceView.run_picker_description')"
      heading-level="h2"
      variant="bordered"
      padding="md"
      role="region"
      :aria-label="$t('views.EvidenceView.run_picker_title')"
      data-testid="evidence-run-picker"
    >
      <div class="flex flex-wrap items-end gap-3">
        <div class="min-w-72 flex-1">
          <label for="evidence-run-search" class="mb-1 block text-sm font-medium">
            {{ $t('views.EvidenceView.run_search') }}
          </label>
          <input
            id="evidence-run-search"
            v-model="runSearch"
            type="search"
            data-testid="evidence-run-search"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            :placeholder="$t('views.EvidenceView.run_search_placeholder')"
          />
        </div>
        <div class="min-w-72 flex-1">
          <label for="evidence-recent-run" class="mb-1 block text-sm font-medium">
            {{ $t('views.EvidenceView.recent_run') }}
          </label>
          <AppSelect
            id="evidence-recent-run"
            v-model="runId"
            data-testid="evidence-recent-run"
            class="w-full"
            :options="recentRunOptions"
            option-label="label"
            option-value="value"
            :label="$t('views.EvidenceView.recent_run')"
            :disabled="recentRunsLoading || recentRuns.length === 0"
          >
            <template #option="{ option }">
              <span :data-value="option.value">{{ option.label }}</span>
            </template>
          </AppSelect>
        </div>
      </div>
      <ErrorAlert
        v-if="recentRunsError"
        :message="recentRunsError"
        :on-retry="loadRecentRuns"
        compact
        data-testid="evidence-recent-runs-error"
      />
      <p v-else-if="!recentRunsLoading && recentRuns.length === 0" class="text-sm text-muted-foreground" data-testid="evidence-recent-runs-empty">
        {{ $t('views.EvidenceView.no_recent_runs') }}
      </p>
    </DataCard>

    <form class="flex flex-wrap items-end gap-3" @submit.prevent="load">
      <div>
        <label for="evidence-run-id" class="mb-1 block text-sm font-medium">
          {{ $t('views.EvidenceView.manual_run_id') }}
        </label>
        <input
          id="evidence-run-id"
          v-model="runId"
          type="text"
          data-testid="evidence-run-id"
          class="w-72 rounded-lg border border-input bg-background px-3 py-2 text-sm"
          :placeholder="$t('views.EvidenceView.run_id_placeholder')"
        />
      </div>
      <div>
        <label for="evidence-node-id" class="mb-1 block text-sm font-medium">
          {{ $t('views.EvidenceView.node_id') }}
        </label>
        <input
          id="evidence-node-id"
          v-model="nodeId"
          type="text"
          data-testid="evidence-node-id"
          class="w-56 rounded-lg border border-input bg-background px-3 py-2 text-sm"
        />
      </div>
      <div>
        <label for="evidence-key" class="mb-1 block text-sm font-medium">
          {{ $t('views.EvidenceView.key') }}
        </label>
        <input
          id="evidence-key"
          v-model="keyFilter"
          type="text"
          data-testid="evidence-key"
          class="w-56 rounded-lg border border-input bg-background px-3 py-2 text-sm"
        />
      </div>
      <button
        type="submit"
        data-testid="evidence-load"
        class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-muted"
      >
        {{ $t('views.EvidenceView.load') }}
      </button>
    </form>

    <div
      v-if="loading"
      aria-live="polite"
      data-testid="evidence-loading"
      class="flex items-center gap-3 text-sm text-muted-foreground"
    >
      <LoadingSpinner />
      <span>{{ $t('views.EvidenceView.loading') }}</span>
    </div>

    <ErrorAlert v-else-if="loadError" :message="loadError" :on-retry="load" />

    <EmptyState
      v-else-if="!hasLoaded"
      :title="$t('views.EvidenceView.prompt_title')"
      :description="$t('views.EvidenceView.prompt_description')"
    />

    <EmptyState
      v-else-if="rows.length === 0"
      :title="$t('views.EvidenceView.empty_title')"
      :description="$t('views.EvidenceView.empty_description')"
    />

    <div v-else class="space-y-3">
      <p aria-live="polite" data-testid="evidence-count" class="text-sm text-muted-foreground">
        {{ $t('views.EvidenceView.row_count', { count: rows.length }) }}
      </p>
      <p class="text-xs text-muted-foreground">{{ $t('views.EvidenceView.redaction_note') }}</p>
      <div class="table-wrapper">
        <table class="w-full text-left text-sm">
          <thead>
            <tr>
              <th scope="col" class="table-header">{{ $t('views.EvidenceView.col_key') }}</th>
              <th scope="col" class="table-header">{{ $t('views.EvidenceView.col_subject') }}</th>
              <th scope="col" class="table-header">{{ $t('views.EvidenceView.col_observed') }}</th>
              <th scope="col" class="table-header">{{ $t('views.EvidenceView.col_value') }}</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-border">
            <tr v-for="row in rows" :key="row.id" class="align-top">
              <td class="table-cell font-mono text-xs">{{ row.key }}</td>
              <td class="table-cell">
                <div>{{ row.subject_type }}</div>
                <div class="font-mono text-xs text-muted-foreground">{{ row.subject_id }}</div>
              </td>
              <td class="table-cell text-xs">{{ row.observed_at }}</td>
              <td class="table-cell">
                <pre class="max-w-xl whitespace-pre-wrap break-words font-mono text-xs">{{ formatValue(row.value) }}</pre>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute } from 'vue-router'
import PageHeader from '../components/shared/PageHeader.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import AppSelect from '../components/shared/AppSelect.vue'
import DataCard from '../components/ui/DataCard.vue'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import { fetchRuns, type RunListItem } from '../lib/api/runs'

interface EvidenceRow {
  id: string
  organisation_id: string
  key: string
  subject_type: string
  subject_id: string
  value: unknown
  observed_at: string
  producer_type: string
  producer_id: string | null
  created_at: string
  updated_at: string
}

const { t } = useI18n()
const route = useRoute()

const runId = ref('')
const nodeId = ref('')
const keyFilter = ref('')
const runSearch = ref('')
const recentRuns = ref<RunListItem[]>([])
const recentRunsLoading = ref(false)
const recentRunsError = ref<string | null>(null)
const rows = ref<EvidenceRow[]>([])

const filteredRecentRuns = computed(() => {
  const query = runSearch.value.trim().toLowerCase()
  if (!query) return recentRuns.value
  return recentRuns.value.filter((run) => run.run_id.toLowerCase().includes(query) || (run.pipeline_name ?? '').toLowerCase().includes(query))
})
const loading = ref(false)
const hasLoaded = ref(false)
const loadError = ref<string | null>(null)

function runLabel(run: RunListItem): string {
  const pipeline = run.pipeline_name ?? t('views.EvidenceView.unknown_pipeline')
  const created = run.created_at ? new Date(run.created_at).toLocaleString() : t('views.EvidenceView.unknown_time')
  return `${pipeline} · ${run.status} · ${created} · ${run.run_id}`
}

// AppSelect option list for the recent-run picker. The empty entry mirrors the
// native control's `<option value="">` placeholder: it reflects the loading /
// "choose a run" state and keeps clearing the picker meaningful.
const recentRunOptions = computed(() => [
  {
    value: '',
    label: recentRunsLoading.value
      ? t('views.EvidenceView.recent_runs_loading')
      : t('views.EvidenceView.choose_recent_run'),
  },
  ...filteredRecentRuns.value.map((run) => ({ value: run.run_id, label: runLabel(run) })),
])

async function loadRecentRuns(): Promise<void> {
  recentRunsLoading.value = true
  recentRunsError.value = null
  try {
    const response = await fetchRuns({ page: 1, page_size: 50 })
    recentRuns.value = response.items
  } catch (err) {
    recentRunsError.value = formatApiError(err)
  } finally {
    recentRunsLoading.value = false
  }
}

function formatValue(value: unknown): string {
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value, null, 2) ?? String(value)
  } catch {
    return String(value)
  }
}

async function load(): Promise<void> {
  if (!runId.value) {
    loadError.value = t('views.EvidenceView.run_id_required')
    return
  }

  loading.value = true
  loadError.value = null

  try {
    const { data, error, response } = await api.GET('/api/v1/evidence', {
      params: {
        query: {
          run_id: runId.value,
          node_id: nodeId.value || undefined,
          key: keyFilter.value || undefined,
        },
      },
    })
    if (error || !response?.ok) {
      throw new Error(
        formatApiError(error) ||
          t('views.EvidenceView.load_failed', { status: response?.status ?? 0 }),
      )
    }
    // The payload is already redacted server-side — render it as-is, with no
    // client-side field filtering.
    rows.value = data ?? []
    hasLoaded.value = true
  } catch (err) {
    loadError.value = formatApiError(err)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void loadRecentRuns()
  const queryRunId = route.query.run_id
  if (typeof queryRunId === 'string' && queryRunId) {
    runId.value = queryRunId
    const queryNodeId = route.query.node_id
    if (typeof queryNodeId === 'string') nodeId.value = queryNodeId
    const queryKey = route.query.key
    if (typeof queryKey === 'string') keyFilter.value = queryKey
    void load()
  }
})
</script>
