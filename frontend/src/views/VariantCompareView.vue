<template>
  <div class="page-wide">
    <div v-if="loading" class="space-y-6" data-testid="variant-compare-loading-skeleton">
      <div class="space-y-2">
        <SkeletonBlock height-class="h-7 w-64" />
        <SkeletonBlock class="mt-2" height-class="h-4 w-3/4" />
      </div>
      <SkeletonBlock class="mt-6" height-class="h-10 w-96" />
      <div class="overflow-hidden rounded-lg border bg-card">
        <div v-for="n in 5" :key="`variant-compare-skeleton-row-${n}`" class="flex items-center gap-4 border-b px-4 py-3 last:border-b-0">
          <SkeletonBlock height-class="h-4 w-32" />
          <SkeletonBlock height-class="h-4 w-24" />
          <SkeletonBlock height-class="h-4 w-24" />
          <SkeletonBlock height-class="h-4 w-24" />
        </div>
      </div>
    </div>
    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadGroups" />
    <template v-else>
      <PageHeader :title="$t('views.variantCompare.title')" :subtitle="$t('views.variantCompare.subtitle')">
        <template #right>
          <div class="flex flex-wrap items-center gap-2">
            <Button :disabled="!selectedGroupId || runningVariants.size > 0" data-testid="variant-compare-run-variants" @click="runVariants">
              <span v-if="runningVariants.size > 0" class="inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
              {{ runningVariants.size > 0 ? $t('views.variantCompare.running') : $t('views.variantCompare.runVariants') }}
            </Button>

            <Button
              variant="secondary"
              data-testid="variant-compare-new-group"
              @click="showBuilder = true"
            >
              {{ $t('views.variantCompare.newComparison') }}
            </Button>
          </div>
        </template>
      </PageHeader>

      <!-- FAR-2598: one line naming what a comparison is and where evals live.
           The old unlabelled dropdown read as a list of evals; this states the
           relationship up front and links to the full eval list. -->
      <p class="mt-2 text-sm text-muted-foreground">
        {{ $t('views.variantCompare.explainer') }}
        <router-link to="/evals" class="font-medium text-primary hover:underline">
          {{ $t('views.variantCompare.explainerEvalsLink') }}
        </router-link>
      </p>

      <VariantGroupBuilder
        v-if="showBuilder"
        :initial-pipeline-id="deepLinkedPipelineId"
        @cancel="showBuilder = false"
      />

      <template v-else>
      <div class="mt-6 space-y-4">
        <FilterBar
          data-testid="variant-compare-filters"
          :search="{ placeholder: $t('views.variantCompare.searchPlaceholder') }"
          :search-value="filterSearch"
          :filters="pipelineFilterOptions"
          :filter-values="{ pipeline_id: filterPipelineId }"
          @update:search="filterSearch = $event"
          @update:filter="handleFilterUpdate"
        />

        <EmptyState
          v-if="groups.length === 0"
          :title="$t('views.variantCompare.emptyGroupsTitle')"
          :description="$t('views.variantCompare.emptyGroupsDescription')"
        >
          <Button data-testid="variant-compare-empty-create" @click="showBuilder = true">
            {{ $t('views.variantCompare.newComparison') }}
          </Button>
        </EmptyState>

        <template v-else>
          <ErrorAlert
            v-if="pipelinesError"
            compact
            :message="$t('views.variantCompare.pipelinesLoadFailed', { error: pipelinesError })"
            :retryable="false"
          />

          <DataTable
            data-testid="variant-compare-groups-table"
            :columns="groupColumns"
            :rows="groupRows"
            :row-clickable="true"
            fit
            @row-click="selectGroupRow"
          >
            <template #cell-name="{ row }">
              <span class="font-medium">{{ row.name }}</span>
            </template>
            <template #cell-pipeline="{ row }">
              <span class="text-muted-foreground">{{ row.pipeline }}</span>
            </template>
            <template #cell-variants_count="{ row }">
              <span class="tabular-nums">{{ row.variants_count }}</span>
            </template>
            <template #cell-declared_evals_count="{ row }">
              <span
                class="tabular-nums"
                :title="$t('views.variantCompare.declaredEvalsTooltip')"
              >{{ row.declared_evals_count }}</span>
            </template>
            <template #cell-strategy="{ row }">
              <span class="text-muted-foreground">{{ row.strategy }}</span>
            </template>
            <template #cell-run_count="{ row }">
              <span class="tabular-nums">{{ $t('views.variantCompare.runs', { count: Number(row.run_count ?? 0) }) }}</span>
            </template>
            <template #cell-degraded="{ row }">
              <Badge
                v-if="row.degraded"
                variant="warning"
                :title="$t('views.variantCompare.degradedTooltip')"
              >
                {{ $t('views.variantCompare.degradedBadge') }}
              </Badge>
              <span v-else class="text-xs text-muted-foreground">—</span>
            </template>
            <template #cell-actions="{ row }">
              <Button
                size="sm"
                variant="secondary"
                :data-testid="`variant-compare-select-${row.id}`"
                :aria-label="selectedGroupId === row.id
                  ? $t('views.variantCompare.viewingComparison', { name: String(row.name) })
                  : $t('views.variantCompare.viewComparison', { name: String(row.name) })"
                @click.stop="selectGroupRow(row)"
              >
                {{ selectedGroupId === row.id ? $t('views.variantCompare.viewing') : $t('views.variantCompare.view') }}
              </Button>
            </template>
            <template #empty>
              {{ $t('views.variantCompare.noMatchingComparisons') }}
            </template>
          </DataTable>

          <!-- FAR-2598: the list endpoint returns a bare array capped at
               `page_size` (no total), so state the count outright and say so
               when the load may have been capped rather than implying the
               table is the complete inventory. -->
          <p class="text-xs text-muted-foreground" data-testid="variant-compare-count">
            {{ shownCountLabel }}
          </p>
          <p v-if="groupsCapped" class="text-xs text-muted-foreground">
            {{ $t('views.variantCompare.cappedComparisons', { count: GROUP_PAGE_SIZE }) }}
          </p>

      <div v-if="selectedGroup" class="space-y-4">
        <h2 class="text-base font-semibold">{{ selectedGroup.name }}</h2>
        <div class="table-wrapper">
          <table class="w-full text-left text-sm">
            <thead>
              <tr>
                <th class="table-header sticky left-0 z-10 bg-card">{{ $t('views.variantCompare.node') }}</th>
                <th
                  v-for="v in variants"
                  :key="v.name"
                  class="table-header min-w-[160px]"
                >
                  <div class="flex flex-col gap-0.5">
                    <span>{{ v.name }}</span>
                    <span class="text-xs font-normal text-muted-foreground">{{ $t('views.variantCompare.weight') }}: {{ v.weight }}</span>
                  </div>
                </th>
              </tr>
            </thead>
            <tbody class="divide-y divide-border">
              <tr
                v-for="node in nodeNames"
                :key="node"
                class="hover:bg-muted/30"
              >
                <td class="table-cell sticky left-0 z-10 bg-card font-mono text-xs">
                  {{ node }}
                </td>
                <td
                  v-for="v in variants"
                  :key="`${node}-${v.name}`"
                  class="table-cell align-top"
                >
                  <div class="flex flex-col gap-1.5">
                    <div>
                  <Badge
                    v-if="getCellStatus(node, v.name) === 'pass'"
                    variant="success"
                    class="capitalize"
                  >
                    {{ $t('views.variantCompare.statusPass') }}
                  </Badge>
                  <Badge
                    v-else-if="getCellStatus(node, v.name) === 'fail'"
                    variant="danger"
                    class="capitalize"
                  >
                    {{ $t('views.variantCompare.statusFail') }}
                  </Badge>
                  <Badge
                    v-else-if="getCellStatus(node, v.name) === 'partial'"
                    variant="warning"
                    class="capitalize"
                  >
                    {{ $t('views.variantCompare.statusPartial') }}
                  </Badge>
                      <span
                        v-else
                        class="text-xs text-muted-foreground"
                      >—</span>
                    </div>
                    <div v-if="getNodeEvalResults(node, v.name).length > 0" class="flex flex-wrap gap-1">
                      <span
                        v-for="er in getNodeEvalResults(node, v.name)"
                        :key="er.eval_id"
                        class="inline-flex items-center gap-0.5 rounded bg-muted px-1.5 py-0.5 text-[10px] tabular-nums text-muted-foreground"
                        :title="er.detail ?? undefined"
                      >
                        {{ er.score !== null ? er.score.toFixed(2) : '-' }}
                      </span>
                    </div>
                  </div>
                </td>
              </tr>
              <tr v-if="nodeNames.length === 0">
                <td colspan="100" class="table-cell text-center text-muted-foreground">
                  <template v-if="runEntries.size === 0">
                    {{ $t('views.variantCompare.noRunData') }}
                  </template>
                  <template v-else>
                    {{ $t('views.variantCompare.waitingForRuns') }}
                  </template>
                </td>
              </tr>
            </tbody>
            <tfoot v-if="summaryByVariant.length > 0">
              <tr class="border-t bg-muted/30">
                <td class="table-cell sticky left-0 z-10 bg-card">{{ $t('views.variantCompare.summary') }}</td>
                <td
                  v-for="s in summaryByVariant"
                  :key="s.name"
                  class="table-cell"
                >
                  <div class="flex flex-col gap-1 text-xs">
                    <div class="flex items-center gap-1.5">
                      <span
                        v-if="s.passRate !== null"
                        class="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium"
                        :class="passRateClass(s.passRate)"
                      >
                        {{ s.passRate.toFixed(0) }}%
                      </span>
                      <span v-else class="text-muted-foreground">—</span>
                      <span class="text-muted-foreground">{{ $t('views.variantCompare.pass') }}</span>
                    </div>
                    <div class="text-muted-foreground">
                      {{ $t('views.variantCompare.cost', { cost: s.totalCost !== null ? formatMoney(Number(s.totalCost), currencyCode, 6) : '—' }) }}
                    </div>
                    <div class="text-muted-foreground">
                      {{ $t('views.variantCompare.tokens', { count: s.tokenTotal !== null ? s.tokenTotal.toLocaleString() : '—' }) }}
                    </div>
                    <div class="flex gap-2 text-muted-foreground">
                      <span class="text-muted-foreground">{{ $t('views.variantCompare.noHitl') }}</span>
                    </div>
                  </div>
                </td>
              </tr>
            </tfoot>
          </table>
        </div>

        <div v-if="nodeNames.length > 0 && diffVariantsAvailable.length >= 2" class="space-y-4">
          <h2 class="text-base font-semibold">{{ $t('views.variantCompare.outputDiffViewer') }}</h2>
          <div class="flex flex-wrap gap-4">
            <label class="flex items-center gap-2 text-sm">
              <span class="text-muted-foreground">{{ $t('views.variantCompare.node') }}:</span>
              <Select
  v-model="diffNode"
  :placeholder="$t('views.variantCompare.selectNode')"
  data-testid="variant-compare-diff-node"
  :aria-label="$t('views.variantCompare.diffNodeLabel')"
  :options="nodeNames.map(n => ({ value: n, label: n }))"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
            </label>
            <label class="flex items-center gap-2 text-sm">
              <span class="text-muted-foreground">{{ $t('views.variantCompare.variantA') }}:</span>
              <Select
  v-model="diffVarA"
  :placeholder="$t('views.variantCompare.selectVariant')"
  data-testid="variant-compare-diff-variant-a"
  :aria-label="$t('views.variantCompare.variantA')"
  :options="diffVariantsAvailable.map(v => ({ value: v, label: v }))"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
            </label>
            <label class="flex items-center gap-2 text-sm">
              <span class="text-muted-foreground">{{ $t('views.variantCompare.variantB') }}:</span>
              <Select
  v-model="diffVarB"
  :placeholder="$t('views.variantCompare.selectVariant')"
  data-testid="variant-compare-diff-variant-b"
  :aria-label="$t('views.variantCompare.variantB')"
  :options="diffVariantsAvailable.map(v => ({ value: v, label: v }))"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
            </label>
          </div>
          <div class="grid grid-cols-1 gap-4 md:grid-cols-2">
            <div class="overflow-auto rounded-lg border bg-card">
              <div class="border-b bg-muted/50 px-3 py-2 text-xs font-medium text-muted-foreground">
                {{ diffVarA || '-' }}
              </div>
              <JsonViewer :data="diffDataA" :show-toolbar="false" :max-height="'20rem'" />
            </div>
            <div class="overflow-auto rounded-lg border bg-card">
              <div class="border-b bg-muted/50 px-3 py-2 text-xs font-medium text-muted-foreground">
                {{ diffVarB || '-' }}
              </div>
              <JsonViewer :data="diffDataB" :show-toolbar="false" :max-height="'20rem'" />
            </div>
          </div>
        </div>
      </div>

      <EmptyState v-else :title="$t('views.variantCompare.selectGroupFirst')" />
        </template>
      </div>
      </template>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onBeforeUnmount, watch, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute } from 'vue-router'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import type { components } from '../lib/api/client'
import SkeletonBlock from '../components/shared/SkeletonBlock.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import JsonViewer from '../components/shared/JsonViewer.vue'
import PageHeader from '../components/shared/PageHeader.vue'
import Badge from '../components/ui/Badge.vue'
import { formatApiError } from '../lib/api/formatError'
import Button from '../components/ui/Button.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import FilterBar from '../components/shared/FilterBar.vue'
import Select from '../components/shared/AppSelect.vue'
import VariantGroupBuilder from '../components/variants/VariantGroupBuilder.vue'
import { DataTable, type Column, type DataTableRow } from '../components/ui/data-table'
import { formatMoney } from '../lib/money'
import { useOrgCurrency } from '../composables/useOrgCurrency'
import { TERMINAL_STATUSES } from '../constants/runStatuses'

const { t } = useI18n()
const { currencyCode, loadCurrency } = useOrgCurrency()
const route = useRoute()

type VariantGroup = components['schemas']['VariantGroupResponse']
type PipelineResponse = components['schemas']['PipelineResponse']
type RunResponse = components['schemas']['RunResponse']
type RunIOResponse = components['schemas']['RunIOResponse']
interface RunEvalItem {
  eval_id: string
  node_id: string
  passed: boolean
  score: number | null
  detail?: string | null
}

interface RunEvalListResponse { items?: RunEvalItem[] }
interface VariantDefinition {
  [key: string]: unknown
  name: string
  weight: number
}

interface RunEntry {
  runId: string
  variantName: string
  runStatus: string
  totalCostUsd: number | null
  tokenConsumption: Record<string, unknown> | null
  nodeOutputs: Record<string, unknown> | null
  evalResults: RunEvalItem[]
}

// The endpoint returns a bare array and defaults to page_size=20, so the list
// would silently truncate at 20. Ask for the endpoint's maximum page and tell
// the user when the load may have been capped (there is no total to compare).
const GROUP_PAGE_SIZE = 100

const { loading, error, data: groupsResp, load: loadGroups } = useDataFetch(
  () => api.GET('/api/v1/variant-groups', { params: { query: { page_size: GROUP_PAGE_SIZE } } }),
  { initialValue: [] as VariantGroup[] },
)

const groups = computed(() => {
  const raw = groupsResp.value
  return (Array.isArray(raw) ? raw : (raw as any)?.items ?? []) as VariantGroup[]
})

// FAR-2598: the groups list is a comparison-groups table now, so the pipeline
// column needs the pipeline's display name. The pipeline read uses the same
// page-size ceiling as the groups read and its failure is surfaced (a silent
// failure would leave raw UUIDs in the table with no explanation).
const { data: pipelinesResp, error: pipelinesError } = useDataFetch(
  () => api.GET('/api/v1/pipelines', { params: { query: { page_size: GROUP_PAGE_SIZE } } }),
  { initialValue: null as unknown as { items: PipelineResponse[] } },
)

const pipelines = computed<PipelineResponse[]>(() => {
  const raw = pipelinesResp.value as unknown as
    | { items?: PipelineResponse[] }
    | PipelineResponse[]
    | null
  if (Array.isArray(raw)) return raw
  return raw?.items ?? []
})

const filterSearch = ref('')
const filterPipelineId = ref('')

const pipelineOptions = computed(() => {
  const names = new Map<string, string>()
  for (const pipeline of pipelines.value) {
    if (pipeline?.id) names.set(pipeline.id, pipeline.name ?? pipeline.id)
  }
  // A group can reference a pipeline the caller cannot list (deleted or
  // team-scoped); keep it filterable by id rather than dropping the row.
  for (const group of groups.value) {
    if (group.pipeline_id && !names.has(group.pipeline_id)) {
      names.set(group.pipeline_id, group.pipeline_id)
    }
  }
  return Array.from(names, ([value, label]) => ({ value, label }))
})

const pipelineFilterOptions = computed(() => [
  {
    key: 'pipeline_id',
    label: t('views.variantCompare.pipelineFilter'),
    options: pipelineOptions.value,
  },
])

function handleFilterUpdate(key: string, value: string) {
  if (key === 'pipeline_id') filterPipelineId.value = value
}

/**
 * Distinct eval ids DECLARED on a group's variants. These are declarations
 * only: nothing uses them to score a run, and the UI builder writes them as an
 * empty list, so this is deliberately labelled "Declared evals" rather than a
 * score count. Real per-run scores live on the run/batch surfaces.
 */
function declaredEvalCount(group: VariantGroup): number {
  const ids = new Set<string>()
  for (const variant of group.variants ?? []) {
    const list = (variant as { eval_definition_ids?: unknown }).eval_definition_ids
    if (!Array.isArray(list)) continue
    for (const id of list) {
      if (typeof id === 'string' && id) ids.add(id)
    }
  }
  return ids.size
}

const groupRows = computed<DataTableRow[]>(() => {
  const nameById = new Map(pipelines.value.map(pipeline => [pipeline.id, pipeline.name]))
  const search = filterSearch.value.trim().toLowerCase()
  return groups.value
    .filter(group => !filterPipelineId.value || group.pipeline_id === filterPipelineId.value)
    .filter(group => !search || (group.name ?? '').toLowerCase().includes(search))
    .map(group => ({
      id: group.id,
      name: group.name,
      pipeline: nameById.get(group.pipeline_id) ?? group.pipeline_id,
      variants_count: (group.variants ?? []).length,
      declared_evals_count: declaredEvalCount(group),
      strategy: group.selection_strategy || '—',
      run_count: group.run_count ?? 0,
      degraded: Boolean(group.degraded_evals),
    }))
})

// Percentages sum to 100 so the `fit` table (fixed layout from `lg` up)
// compresses instead of pushing the actions column off-screen; low-priority
// columns drop out on narrower screens while the table keeps its horizontal
// scroll below `lg`.
const groupColumns = computed<Column[]>(() => [
  { key: 'name', label: t('views.variantCompare.columnName'), sortable: true, width: '22%' },
  { key: 'pipeline', label: t('views.variantCompare.columnPipeline'), sortable: true, width: '18%', hideBelow: 'md' },
  { key: 'variants_count', label: t('views.variantCompare.columnVariants'), numeric: true, sortable: true, width: '8%' },
  { key: 'declared_evals_count', label: t('views.variantCompare.columnDeclaredEvals'), numeric: true, sortable: true, width: '8%', hideBelow: 'sm' },
  { key: 'strategy', label: t('views.variantCompare.columnStrategy'), width: '12%', hideBelow: 'xl' },
  { key: 'run_count', label: t('views.variantCompare.columnRuns'), numeric: true, sortable: true, width: '9%', hideBelow: 'sm' },
  { key: 'degraded', label: t('views.variantCompare.columnDegraded'), width: '9%', hideBelow: 'md' },
  { key: 'actions', label: '', width: '14%' },
])

const groupsCapped = computed(() => groups.value.length >= GROUP_PAGE_SIZE)

const shownCountLabel = computed(() =>
  groupRows.value.length === 1
    ? t('views.variantCompare.showingOneComparison')
    : t('views.variantCompare.showingComparisons', { count: groupRows.value.length }),
)

function selectGroupRow(row: DataTableRow) {
  if (typeof row.id === 'string' && row.id) selectedGroupId.value = row.id
}

const selectedGroupId = ref<string | null>(null)
const selectedGroup = ref<VariantGroup | null>(null)
const isUnmounted = ref(false)

// The variant-group builder lives on this page (FAR-936 retired the separate
// AB Test Models page). It opens automatically for the `pipeline_id` deep-link
// from PipelineListView's "Run as variant" action.
const showBuilder = ref(false)
const deepLinkedPipelineId = computed(() =>
  typeof route.query.pipeline_id === 'string' && route.query.pipeline_id ? route.query.pipeline_id : null,
)

const runEntries = ref<Map<string, RunEntry>>(new Map())
const runningVariants = ref<Set<string>>(new Set())

const diffNode = ref<string | null>(null)
const diffVarA = ref<string | null>(null)
const diffVarB = ref<string | null>(null)

const firedRunsSeeded = ref(false)

const terminalStatuses: Set<string> = new Set(TERMINAL_STATUSES)

const variants = computed<VariantDefinition[]>(() =>
  (selectedGroup.value?.variants ?? []).filter(
    (variant): variant is VariantDefinition =>
      typeof variant.name === 'string' && typeof variant.weight === 'number',
  ),
)

const nodeNames = computed(() => {
  const names = new Set<string>()
  for (const entry of runEntries.value.values()) {
    if (entry.nodeOutputs) {
      Object.keys(entry.nodeOutputs).forEach(k => names.add(k))
    }
  }
  return Array.from(names).sort((a, b) => a.localeCompare(b))
})

const variantRunData = computed(() => {
  const map = new Map<string, RunEntry>()
  for (const entry of runEntries.value.values()) {
    map.set(entry.variantName, entry)
  }
  return map
})

const diffVariantsAvailable = computed(() => {
  return Array.from(variantRunData.value.keys())
})

const summaryByVariant = computed(() => {
  const result: Array<{
    name: string
    passRate: number | null
    totalCost: number | null
    tokenTotal: number | null
    approved: number
    rejected: number
    pending: number
  }> = []
  const seen = new Set<string>()

  for (const entry of runEntries.value.values()) {
    const passCount = entry.evalResults.filter(r => r.passed).length
    const totalCount = entry.evalResults.length
    const tc = entry.tokenConsumption as { total_tokens?: number } | null
    result.push({
      name: entry.variantName,
      passRate: totalCount > 0 ? (passCount / totalCount) * 100 : null,
      totalCost: entry.totalCostUsd,
      tokenTotal: tc?.total_tokens ?? null,
      approved: 0,
      rejected: 0,
      pending: 0,
    })
    seen.add(entry.variantName)
  }

  for (const v of variants.value) {
    if (!seen.has(v.name)) {
      result.push({
        name: v.name,
        passRate: null,
        totalCost: null,
        tokenTotal: null,
        approved: 0,
        rejected: 0,
        pending: 0,
      })
    }
  }

  return result
})

function passRateClass(rate: number): string {
  if (rate >= 80) return 'badge badge-status-success'
  if (rate >= 40) return 'badge badge-status-warning'
  return 'badge badge-status-destructive'
}

function getNodeEvalResults(nodeName: string, variantName: string): RunEvalItem[] {
  const entry = variantRunData.value.get(variantName)
  if (!entry) return []
  return entry.evalResults.filter(r => r.node_id === nodeName)
}

function getCellStatus(nodeName: string, variantName: string): 'pass' | 'fail' | 'partial' | null {
  const evals = getNodeEvalResults(nodeName, variantName)
  if (evals.length === 0) return null
  const allPass = evals.every(r => r.passed)
  if (allPass) return 'pass'
  const allFail = evals.every(r => !r.passed)
  if (allFail) return 'fail'
  return 'partial'
}

function getNodeOutput(nodeName: string, variantName: string): unknown {
  const entry = variantRunData.value.get(variantName)
  if (!entry?.nodeOutputs) return null
  return (entry.nodeOutputs as Record<string, unknown>)[nodeName] ?? null
}

const diffDataA = computed(() => {
  if (!diffNode.value || !diffVarA.value) return null
  return getNodeOutput(diffNode.value, diffVarA.value)
})

const diffDataB = computed(() => {
  if (!diffNode.value || !diffVarB.value) return null
  return getNodeOutput(diffNode.value, diffVarB.value)
})

watch(selectedGroupId, async (id) => {
  if (id) {
    await fetchGroupDetail(id)
    seedFiredRuns()
  }
})

watch(diffVariantsAvailable, (available) => {
  if (!diffVarA.value && available.length > 0) {
    diffVarA.value = available[0]
  }
  if (!diffVarB.value && available.length > 1) {
    diffVarB.value = available[1]
  }
  if (diffVarA.value && available.length > 0 && diffNode.value === null && nodeNames.value.length > 0) {
    diffNode.value = nodeNames.value[0]
  }
})

onBeforeUnmount(() => {
  isUnmounted.value = true
})

watch(groups, (list) => {
  if (list.length === 0) return
  const paramId = typeof route.params.batchId === 'string' ? route.params.batchId : null
  const pipelineId = typeof route.query.pipeline_id === 'string' ? route.query.pipeline_id : null
  const pipelineGroup = pipelineId ? list.find(g => g.pipeline_id === pipelineId) : undefined
  if (paramId && list.some(g => g.id === paramId)) {
    selectedGroupId.value = paramId
  } else if (pipelineGroup) {
    selectedGroupId.value = pipelineGroup.id
  } else if (!selectedGroupId.value) {
    selectedGroupId.value = list[0].id
  }
}, { immediate: true })

// The table can never land empty-handed: when a filter empties the current
// selection out of the visible rows (or the selection is still unset), select
// the first visible comparison so the detail panel below always matches a row.
watch(groupRows, (rows) => {
  // A filter that hides everything must also clear the detail panel — never
  // render a comparison underneath a table that says nothing matches.
  if (rows.length === 0) {
    selectedGroup.value = null
    selectedGroupId.value = null
    return
  }
  const current = selectedGroupId.value
  const stillVisible = current !== null && rows.some(row => row.id === current)
  if (!stillVisible) {
    const first = rows[0].id
    if (typeof first === 'string') selectedGroupId.value = first
  }
})

interface FiredRun {
  run_id: string
  variant_name: string
}

function seedFiredRuns() {
  if (firedRunsSeeded.value) return
  const state = history.state as { firedRuns?: FiredRun[] } | null
  const fired = state?.firedRuns
  if (!fired || fired.length === 0) return
  firedRunsSeeded.value = true
  for (const r of fired) {
    if (runEntries.value.has(r.variant_name)) continue
    runEntries.value.set(r.variant_name, {
      runId: r.run_id,
      variantName: r.variant_name,
      runStatus: 'pending',
      totalCostUsd: null,
      tokenConsumption: null,
      nodeOutputs: null,
      evalResults: [],
    })
    runningVariants.value.add(r.variant_name)
    void pollRunStatus(r.run_id, r.variant_name)
  }
}

onMounted(() => {
  seedFiredRuns()
  if (deepLinkedPipelineId.value) showBuilder.value = true
})

watch(deepLinkedPipelineId, (id) => {
  if (id) showBuilder.value = true
})

async function fetchGroupDetail(id: string) {
  error.value = null
  try {
    const { data, error: err } = await api.GET('/api/v1/variant-groups/{group_id}', {
      params: { path: { group_id: id } },
    })
    if (err) {
      error.value = `${t('views.variantCompare.failedToLoadGroup')} ${formatApiError(err)}`
      return
    }
    selectedGroup.value = data as unknown as VariantGroup
  } catch (e: unknown) {
    error.value = `${t('views.variantCompare.failedToLoadGroup')} ${formatApiError(e)}`
  }
}

async function runVariants() {
  if (!selectedGroupId.value) return
  const groupId = selectedGroupId.value
  error.value = null

  try {
    const { data, error: err } = await api.POST('/api/v1/variant-groups/{group_id}/run', {
      params: { path: { group_id: groupId } },
      body: {},
    })

    if (err) {
      error.value = `${t('views.variantCompare.runFailed')} ${formatApiError(err)}`
      return
    }

    if (!data) return

    const { run_id, variant_name } = data as unknown as {
      run_id: string
      variant_name: string
    }

    runningVariants.value.add(variant_name)

    const entry: RunEntry = {
      runId: run_id,
      variantName: variant_name,
      runStatus: 'pending',
      totalCostUsd: null,
      tokenConsumption: null,
      nodeOutputs: null,
      evalResults: [],
    }
    runEntries.value.set(variant_name, entry)

    await pollRunStatus(run_id, variant_name)
  } catch (e: unknown) {
    error.value = `${t('views.variantCompare.failedToRunVariants')} ${formatApiError(e)}`
  }
}

// Fetch one poll and fold the result into the variant's entry. Returns the
// run's status, or null when the response carried no data (poll again).
async function refreshRunEntry(runId: string, variantName: string): Promise<string | null> {
  const { data } = await api.GET('/api/v1/runs/{run_id}', {
    params: { path: { run_id: runId } },
  })
  if (!data) return null

  const runResp = data as unknown as RunResponse
  const existing = runEntries.value.get(variantName)
  if (existing) {
    runEntries.value.set(variantName, {
      ...existing,
      runStatus: runResp.status,
      totalCostUsd: runResp.total_cost_usd == null ? null : Number(runResp.total_cost_usd),
      tokenConsumption: runResp.token_consumption ?? null,
    })
  }
  return runResp.status
}

async function handleTerminalStatus(runId: string, variantName: string, status: string): Promise<void> {
  if (status === 'complete') {
    await fetchRunIO(runId, variantName)
    await fetchRunEvals(runId, variantName)
  } else {
    error.value = `${t('views.variantCompare.runFailedStatus')} ${status}`
  }
}

async function pollRunStatus(runId: string, variantName: string) {
  let status = 'pending'

  while (!isUnmounted.value && !terminalStatuses.has(status)) {
    await delay(2000)

    try {
      const nextStatus = await refreshRunEntry(runId, variantName)
      if (nextStatus === null) continue

      status = nextStatus
      if (terminalStatuses.has(status)) {
        await handleTerminalStatus(runId, variantName, status)
        break
      }
    } catch (e) {
      error.value = `${t('views.variantCompare.runFailed')} ${formatApiError(e)}`
    }
  }

  runningVariants.value.delete(variantName)
}

async function fetchRunIO(runId: string, variantName: string) {
  try {
    const { data } = await api.GET('/api/v1/runs/{run_id}/io', {
      params: { path: { run_id: runId } },
    })
    if (data) {
      const ioResp = data as unknown as RunIOResponse
      const existing = runEntries.value.get(variantName)
      if (existing) {
        runEntries.value.set(variantName, {
          ...existing,
          nodeOutputs: ioResp.outputs_json ?? null,
        })
      }

      if (diffNode.value === null && ioResp.outputs_json) {
        const keys = Object.keys(ioResp.outputs_json)
        if (keys.length > 0) {
          diffNode.value = keys[0]
        }
      }
    }
  } catch (e) {
    error.value = `${t('views.variantCompare.failedToLoadGroup')} ${formatApiError(e)}`
  }
}

async function fetchRunEvals(runId: string, variantName: string) {
  try {
    const { data } = await api.GET('/api/v1/runs/{run_id}/evals', {
      params: { path: { run_id: runId } },
    })
    if (data) {
      const evalResp = data as unknown as RunEvalListResponse
      const existing = runEntries.value.get(variantName)
      if (existing) {
        runEntries.value.set(variantName, {
          ...existing,
          evalResults: evalResp.items ?? [],
        })
      }
    }
  } catch (e) {
    error.value = `${t('views.variantCompare.failedToLoadGroup')} ${formatApiError(e)}`
  }
}

function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

void loadCurrency()
</script>
