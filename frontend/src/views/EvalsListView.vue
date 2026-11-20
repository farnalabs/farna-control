<template>
  <FeatureGate feature-name="eval_system" required-tier="community" show-disabled>
    <div class="page-wide pt-4">
      <PageHeader
        :title="$t('views.EvalsListView.title')"
        :subtitle="$t('views.EvalsListView.subtitle')"
        :tabs="evalPageTabs"
      >
        <template #right>
          <button
            type="button"
            data-testid="evals-list-new"
            class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            @click="openEditor()"
          >
            {{ $t('views.EvalsListView.new_eval') }}
          </button>
        </template>
      </PageHeader>

      <FilterBar
        :filters="filterDefs"
        :filter-values="{ pipeline_id: filterPipelineId, eval_type: filterEvalType }"
        @update:filter="handleFilterUpdate"
      />

      <!-- Skeleton rows (STATE-3): the shared DataTable renders pulse
           placeholders, so a slow fetch never shows an unqualified spinner.
           Decorative only — the scroll region has no focusable content while
           loading, which axe flags as `scrollable-region-focusable`. -->
      <div v-if="loading" aria-hidden="true" data-testid="evals-list-loading">
        <DataTable :columns="columns" :rows="[]" :loading="true" :loading-rows="5" :row-clickable="false" />
      </div>

      <ErrorAlert v-else-if="error" :message="error" :on-retry="loadEvals" />

      <!-- Two distinct empty states: a genuinely empty organisation needs the
           concept explained and a way to start; a filtered-to-nothing result
           only needs the filters cleared. -->
      <EmptyState
        v-else-if="items.length === 0 && !hasFilters"
        data-testid="evals-list-empty"
        :title="$t('views.EvalsListView.empty_title')"
        :description="$t('views.EvalsListView.empty_body')"
      >
        <button
          type="button"
          data-testid="evals-list-empty-cta"
          class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          @click="openEditor()"
        >
          {{ $t('views.EvalsListView.empty_cta') }}
        </button>
      </EmptyState>

      <EmptyState
        v-else-if="items.length === 0"
        data-testid="evals-list-filtered-empty"
        :title="$t('views.EvalsListView.filtered_empty_title')"
        :description="$t('views.EvalsListView.filtered_empty_body')"
      >
        <button
          type="button"
          data-testid="evals-list-reset"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          @click="resetFilters"
        >
          {{ $t('views.EvalsListView.reset_filters') }}
        </button>
      </EmptyState>

      <template v-else>
        <DataTable
          :columns="columns"
          :rows="rows"
          row-testid="evals-list-row"
          @row-click="row => openEditor(row.pipeline_id as string)"
        >
          <template #cell-pipeline="{ row }">
            <span class="min-w-0 truncate">{{ pipelineLabel(row.pipeline_id as string) }}</span>
          </template>
          <template #cell-node="{ row }">
            <span v-if="row.node_id" class="font-mono text-xs text-muted-foreground">{{ shortId(row.node_id as string) }}</span>
            <span v-else class="text-muted-foreground">{{ $t('views.EvalsListView.node_all_outputs') }}</span>
          </template>
          <template #cell-eval_type="{ row }">
            <span class="inline-block rounded bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary-text">
              {{ evalTypeLabel(row.eval_type as string) }}
            </span>
          </template>
          <template #cell-judging="{ row }">
            <span class="text-muted-foreground">{{ row.judging || '—' }}</span>
          </template>
          <template #cell-pass_threshold="{ row }">
            <span class="font-mono text-xs">{{ formatThreshold(row.pass_threshold as number | null | undefined) }}</span>
          </template>
          <template #cell-version="{ row }">
            <span class="font-mono text-xs">{{ row.version ?? 1 }}</span>
          </template>
        </DataTable>

        <div class="mt-4 flex items-center justify-between">
          <span class="text-sm text-muted-foreground" data-testid="evals-list-count">
            {{ $t('views.EvalsListView.showing_count', { count: items.length, total }) }}
          </span>
          <div class="flex items-center gap-2">
            <button
              type="button"
              :disabled="page <= 1"
              data-testid="evals-list-previous-page"
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-30"
              @click="prevPage"
            >
              {{ $t('common.previous') }}
            </button>
            <span class="text-sm text-muted-foreground" data-testid="evals-list-page-indicator">
              {{ $t('common.page_label', { page }) }}
            </span>
            <button
              type="button"
              :disabled="page * PAGE_SIZE >= total"
              data-testid="evals-list-next-page"
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-30"
              @click="nextPage"
            >
              {{ $t('common.next') }}
            </button>
          </div>
        </div>
      </template>
    </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import FeatureGate from '../components/FeatureGate.vue'
import PageHeader from '../components/shared/PageHeader.vue'
import FilterBar from '../components/shared/FilterBar.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import { DataTable, type Column, type DataTableRow } from '../components/ui/data-table'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import { shortId } from '../utils/format'

const { t } = useI18n()
const router = useRouter()

const PAGE_SIZE = 25

interface PipelineItem {
  id: string
  name: string
}

interface EvalItem {
  id: string
  pipeline_id: string
  node_id: string | null
  name: string
  eval_type: string
  pass_threshold?: number | null
  version?: number
  judge?: { display?: string } | null
}

type EvalTypeOption = { value: string; labelKey: string }

const EVAL_TYPES: EvalTypeOption[] = [
  { value: 'model_judge', labelKey: 'views.EvalsListView.type_model_judge' },
  { value: 'regex', labelKey: 'views.EvalsListView.type_regex' },
  { value: 'json_schema', labelKey: 'views.EvalsListView.type_json_schema' },
  { value: 'custom_function', labelKey: 'views.EvalsListView.type_custom_function' },
  { value: 'human_set', labelKey: 'views.EvalsListView.type_human_set' },
  { value: 'guardrail', labelKey: 'views.EvalsListView.type_guardrail' },
]

const pipelines = ref<PipelineItem[]>([])
const items = ref<EvalItem[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const error = ref<string | null>(null)

const filterPipelineId = ref('')
const filterEvalType = ref('')

const hasFilters = computed(() => filterPipelineId.value !== '' || filterEvalType.value !== '')

const evalPageTabs = computed(() => [
  { label: t('views.EvalsListView.tabs_all'), to: '/evals' },
  { label: t('views.EvalsListView.tabs_editor'), to: '/evals/editor' },
  { label: t('views.EvalsListView.tabs_proposals'), to: '/evals/proposals' },
])

const columns = computed<Column[]>(() => [
  { key: 'name', label: t('views.EvalsListView.column_name'), width: '24%' },
  { key: 'pipeline', label: t('views.EvalsListView.column_pipeline') },
  { key: 'node', label: t('views.EvalsListView.column_node'), hideBelow: 'md' },
  { key: 'eval_type', label: t('views.EvalsListView.column_type') },
  { key: 'judging', label: t('views.EvalsListView.column_judging'), hideBelow: 'lg' },
  { key: 'pass_threshold', label: t('views.EvalsListView.column_pass_threshold'), numeric: true, hideBelow: 'md' },
  { key: 'version', label: t('views.EvalsListView.column_version'), numeric: true, hideBelow: 'lg' },
])

const filterDefs = computed(() => [
  {
    key: 'pipeline_id',
    label: t('views.EvalsListView.column_pipeline'),
    options: pipelines.value.map(p => ({ value: p.id, label: p.name })),
  },
  {
    key: 'eval_type',
    label: t('views.EvalsListView.column_type'),
    options: EVAL_TYPES.map(option => ({ value: option.value, label: t(option.labelKey) })),
  },
])

const rows = computed<DataTableRow[]>(() =>
  items.value.map(item => ({
    id: item.id,
    name: item.name,
    pipeline: item.pipeline_id,
    pipeline_id: item.pipeline_id,
    node_id: item.node_id,
    eval_type: item.eval_type,
    judging: item.judge?.display ?? '',
    pass_threshold: item.pass_threshold ?? null,
    version: item.version ?? 1,
  })),
)

function pipelineLabel(pipelineId: string): string {
  const match = pipelines.value.find(p => p.id === pipelineId)
  return match?.name ?? shortId(pipelineId)
}

function evalTypeLabel(evalType: string): string {
  const option = EVAL_TYPES.find(o => o.value === evalType)
  return option ? t(option.labelKey) : evalType
}

function formatThreshold(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : value.toFixed(2)
}

async function loadPipelines(): Promise<void> {
  try {
    const { data } = await api.GET('/api/v1/pipelines')
    pipelines.value = (data?.items ?? []) as PipelineItem[]
  } catch (e) {
    // Best-effort join: the eval list still renders with short pipeline ids,
    // but the failure is logged rather than silently swallowed.
    pipelines.value = []
    console.warn('Failed to load pipelines for the eval list:', e)
  }
}

async function loadEvals(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const { data, error: apiError } = await api.GET('/api/v1/evals', {
      params: {
        query: {
          page: page.value,
          page_size: PAGE_SIZE,
          ...(filterPipelineId.value ? { pipeline_id: filterPipelineId.value } : {}),
          ...(filterEvalType.value ? { eval_type: filterEvalType.value } : {}),
        },
      },
    })
    if (apiError) throw apiError
    items.value = (data?.items ?? []) as EvalItem[]
    total.value = data?.total ?? 0
  } catch (e) {
    items.value = []
    total.value = 0
    error.value = `${t('views.EvalsListView.load_failed')} ${formatApiError(e)}`
  } finally {
    loading.value = false
  }
}

function handleFilterUpdate(key: string, value: string): void {
  if (key === 'pipeline_id') filterPipelineId.value = value
  else if (key === 'eval_type') filterEvalType.value = value
}

function resetFilters(): void {
  filterPipelineId.value = ''
  filterEvalType.value = ''
}

function prevPage(): void {
  if (page.value <= 1) return
  page.value -= 1
  void loadEvals()
}

function nextPage(): void {
  if (page.value * PAGE_SIZE >= total.value) return
  page.value += 1
  void loadEvals()
}

function openEditor(pipelineId?: string): void {
  void router.push(pipelineId
    ? { path: '/evals/editor', query: { pipeline_id: pipelineId } }
    : { path: '/evals/editor' })
}

// A filter change re-queries from the first page — page 3 of the unfiltered
// list is meaningless once a pipeline is selected.
watch([filterPipelineId, filterEvalType], () => {
  page.value = 1
  void loadEvals()
})

onMounted(() => {
  void loadPipelines()
  void loadEvals()
})
</script>
