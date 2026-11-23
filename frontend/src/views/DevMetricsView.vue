<template>
  <div class="page-wide">
    <PageHeader
      title="Web Vitals Analytics"
      subtitle="Frontend performance metrics — LCP, FCP, CLS, INP, TTFB"
    >
      <template #right>
        <AppSelect
          :model-value="selectedDays"
          class="min-w-36"
          :options="timeRangeOptions"
          option-label="label"
          option-value="value"
          label="Metrics time range"
          data-testid="dev-metrics-time-range"
          @update:model-value="onTimeRangeChange"
        >
          <template #option="{ option }">
            <span :data-value="option.value">{{ option.label }}</span>
          </template>
        </AppSelect>
        <button type="button"
          class="rounded-lg border border-input bg-background px-3 py-1.5 text-sm hover:bg-accent"
          data-testid="dev-metrics-refresh"
          @click="loadData"
          :disabled="loading"
        >
          {{ loading ? 'Loading...' : 'Refresh' }}
        </button>
      </template>
    </PageHeader>

    <LoadingSpinner v-if="loading && !summary.length" />

    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadData" class="mb-6" />

    <template v-else-if="summary.length === 0">
      <EmptyState
        title="No data yet"
        description="Web vitals data will appear here once users navigate the application with the feature enabled."
      />
    </template>

    <template v-else>
      <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 mb-8">
        <MetricCard
          v-for="item in summary"
          :key="item.metric_name"
          :label="metricLabel(item.metric_name)"
          :value="formatMetricValue(item.metric_name, item.avg_value)"
          :color="metricColor(item)"
        >
          <div class="flex items-center gap-3 mt-1 text-xs text-muted-foreground">
            <span>min {{ formatMetricValue(item.metric_name, item.min_value) }}</span>
            <span>max {{ formatMetricValue(item.metric_name, item.max_value) }}</span>
          </div>
          <div class="mt-2 flex items-center gap-1">
            <div class="h-1.5 flex-1 rounded-full bg-muted overflow-hidden">
              <div
                v-if="item.good_pct != null"
                class="h-full rounded-full transition-all"
                :class="item.good_pct >= 90 ? 'bg-success' : item.good_pct >= 50 ? 'bg-warning' : 'bg-destructive'"
                :style="{ width: item.good_pct + '%' }"
              />
            </div>
            <span class="text-xs tabular-nums" :class="item.good_pct != null && item.good_pct >= 90 ? 'text-success-text' : item.good_pct != null && item.good_pct >= 50 ? 'text-warning-text' : 'text-destructive'">
              {{ item.good_pct != null ? item.good_pct + '% good' : '—' }}
            </span>
          </div>
          <p class="text-xs text-muted-foreground mt-1">{{ item.count }} measurements</p>
        </MetricCard>
      </div>

      <div class="space-y-6">
        <BaseCard v-for="ts in timeSeriesData" :key="ts.metric_name" padding="md">
          <h3 class="text-sm font-semibold mb-4">{{ metricLabel(ts.metric_name) }} over time</h3>
          <div v-if="ts.points.length === 0" class="text-sm text-muted-foreground py-8 text-center">
            No data for this period.
          </div>
          <div v-else>
            <div class="flex items-end gap-1 h-32 mb-2">
              <div
                v-for="(pt, i) in ts.points"
                :key="i"
                class="flex-1 flex flex-col items-center justify-end h-full"
              >
                <div
                  class="w-full rounded-t transition-all hover:opacity-80"
                  :style="{ height: barHeight(pt.avg_value, ts.maxValue) + '%' }"
                  :class="barColor(pt.avg_value, ts.metric_name)"
                  :title="pt.date + ': ' + formatMetricValue(ts.metric_name, pt.avg_value)"
                />
              </div>
            </div>
            <div class="flex justify-between text-xs text-muted-foreground">
              <span>{{ ts.points[0].date }}</span>
              <span v-if="ts.points.length > 2">{{ ts.points[Math.floor(ts.points.length / 2)].date }}</span>
              <span>{{ ts.points[ts.points.length - 1].date }}</span>
            </div>
          </div>
        </BaseCard>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import PageHeader from '../components/shared/PageHeader.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import AppSelect from '../components/shared/AppSelect.vue'
import BaseCard from '../components/ui/BaseCard.vue'
import MetricCard from '../components/ui/MetricCard.vue'
import { useApi } from '../composables/useApi'

const { get } = useApi()

interface SummaryItem {
  metric_name: string
  avg_value: number
  min_value: number
  max_value: number
  count: number
  good_pct: number | null
}

interface TimeSeriesPoint {
  date: string
  metric_name: string
  avg_value: number
  count: number
}

interface TimeSeriesData {
  metric_name: string
  points: TimeSeriesPoint[]
  maxValue: number
}

const METRIC_LABELS: Record<string, string> = {
  CLS: 'Cumulative Layout Shift',
  FCP: 'First Contentful Paint',
  INP: 'Interaction to Next Paint',
  LCP: 'Largest Contentful Paint',
  TTFB: 'Time to First Byte',
}

const METRIC_THRESHOLDS: Record<string, { good: number; poor: number }> = {
  CLS: { good: 0.1, poor: 0.25 },
  FCP: { good: 1800, poor: 3000 },
  INP: { good: 200, poor: 500 },
  LCP: { good: 2500, poor: 4000 },
  TTFB: { good: 800, poor: 1800 },
}

const loading = ref(false)
const error = ref<string | null>(null)
const summary = ref<SummaryItem[]>([])
const timeSeriesData = ref<TimeSeriesData[]>([])
const selectedDays = ref(7)

// The time-range dropdown is the design-system AppSelect; its options carry the
// numeric day value the fetch query needs.
const timeRangeOptions = [
  { value: 7, label: 'Last 7 days' },
  { value: 30, label: 'Last 30 days' },
  { value: 90, label: 'Last 90 days' },
]

function onTimeRangeChange(value: unknown): void {
  selectedDays.value = Number(value)
  void loadData()
}

function metricLabel(name: string): string {
  return METRIC_LABELS[name] || name
}

function formatMetricValue(name: string, value: number): string {
  if (name === 'CLS') return value.toFixed(3)
  if (value >= 1000) return (value / 1000).toFixed(2) + 's'
  return Math.round(value) + 'ms'
}

function metricColor(item: SummaryItem): 'success' | 'warning' | 'destructive' | undefined {
  const thresholds = METRIC_THRESHOLDS[item.metric_name]
  if (!thresholds) return undefined
  if (item.avg_value <= thresholds.good) return 'success'
  if (item.avg_value <= thresholds.poor) return 'warning'
  return 'destructive'
}

function barColor(value: number, metricName: string): string {
  const thresholds = METRIC_THRESHOLDS[metricName]
  if (!thresholds) return 'bg-primary'
  if (value <= thresholds.good) return 'bg-success'
  if (value <= thresholds.poor) return 'bg-warning'
  return 'bg-destructive'
}

function barHeight(value: number, maxValue: number): number {
  if (maxValue <= 0) return 0
  return Math.max(5, (value / maxValue) * 100)
}

async function loadData() {
  loading.value = true
  error.value = null
  try {
    const summaryPromise = get<SummaryItem[]>(`/api/v1/metrics/web-vitals/summary?days=${selectedDays.value}`)

    const metricNames = ['CLS', 'FCP', 'INP', 'LCP', 'TTFB']
    const timeseriesPromises = metricNames.map(name =>
      get<TimeSeriesPoint[]>(`/api/v1/metrics/web-vitals/timeseries?metric_name=${name}&days=${selectedDays.value}`)
        .then(points => ({ metric_name: name, points, maxValue: Math.max(...points.map(p => p.avg_value), 1) }))
        .catch(() => ({ metric_name: name, points: [] as TimeSeriesPoint[], maxValue: 1 }))
    )

    const [summaryResult, ...tsResults] = await Promise.all([
      summaryPromise.catch(() => null),
      ...timeseriesPromises,
    ])

    if (summaryResult) {
      summary.value = summaryResult
    }
    timeSeriesData.value = tsResults.filter(ts => ts.points.length > 0)

    if (!summaryResult && tsResults.every(ts => ts.points.length === 0)) {
      error.value = 'Failed to load web vitals data. The API may be unavailable.'
    }
  } catch (e) {
    error.value = 'Failed to load web vitals data. The API may be unavailable.'
    console.error('Failed to load web vitals data:', e)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void loadData()
})
</script>
