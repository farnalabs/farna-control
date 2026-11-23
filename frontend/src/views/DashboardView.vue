<template>
  <div class="page-wide">
    <DashboardNotificationsPanel class="-mt-3 mb-4" />
    <PageHeader :title="$t('views.DashboardView.dashboard')" :subtitle="$t('views.DashboardView.overview_of_your_organisations_pipelines_and_runs')" data-testid="dashboard-title">
      <template #right>
        <div v-if="trendToggleVisible" class="flex flex-wrap justify-start sm:justify-end gap-1">
          <button type="button"
            v-for="w in trendWindows"
            :key="w.value ?? 'all'"
            :data-testid="'trend-toggle-' + (w.value ?? 'all')"
            :aria-pressed="selectedWindow === w.value"
            :class="[
              'px-3 py-1 text-xs font-medium rounded transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
              selectedWindow === w.value ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground hover:bg-muted/80',
            ]"
            @click="selectWindow(w.value)"
          >
            {{ $t(w.labelKey) }}
          </button>
        </div>
      </template>
    </PageHeader>
    <!-- Loading skeleton grid -->
    <div v-if="loading" class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <CardSkeleton v-for="n in 6" :key="n" :lines="2" />
    </div>
    <!-- Full-page error -->
    <ErrorAlert v-else-if="error && !summary" :message="error" :on-retry="retryDashboard" />
    <template v-else-if="summary">
      <!-- A failed refresh while data is already on screen. Without this the
           dashboard kept rendering the stale summary and the failure was
           silent: `error` is only rendered by the full-page branch above,
           which requires `!summary`. -->
      <ErrorAlert v-if="error" class="mb-4" :message="error" :on-retry="retryDashboard" />
      <DashboardSystemOverview
        v-if="systemOverviewVisible"
        :window-days="selectedWindow ?? 7"
        class="mb-4"
      />
      <!-- Row 1: Summary stat cards -->
      <div :class="['grid gap-4 sm:grid-cols-2 lg:grid-cols-4 transition-opacity duration-200', periodRefreshing ? 'opacity-60' : 'opacity-100']">
        <StatCard data-testid="dashboard-stats-card-pipelines" :label="$t('views.DashboardView.pipelines')" :value="cardValue(summary.period?.metrics?.active_pipelines?.current, summary.active_pipelines)" color="primary" to="/pipelines" :delta="periodMetrics?.active_pipelines ?? null" :no-baseline-label="selectedWindow != null ? $t('views.DashboardView.no_prior_data') : undefined" :neutral-trend="true">
          <template #icon><GitBranch class="h-4 w-4" aria-hidden="true" /></template>
        </StatCard>
        <StatCard data-testid="dashboard-stats-card-total-runs" :label="$t('views.DashboardView.total_runs')" :value="cardValue(summary.period?.metrics?.total_runs?.current, summary.total_runs)" color="primary" to="/runs" :delta="periodMetrics?.total_runs ?? null" :no-baseline-label="selectedWindow != null ? $t('views.DashboardView.no_prior_data') : undefined">
          <template #icon><BarChart2 class="h-4 w-4" aria-hidden="true" /></template>
        </StatCard>
        <StatCard data-testid="dashboard-stats-card-running" :label="$t('views.DashboardView.running')" :value="cardValue(summary.period?.metrics?.run_counts_by_status?.running?.current, summary.run_counts_by_status?.running ?? 0)" color="success" :to="`/runs?status=${RUN_STATUS.RUNNING}`" :delta="periodMetrics?.run_counts_by_status?.running ?? null" :no-baseline-label="selectedWindow != null ? $t('views.DashboardView.no_prior_data') : undefined">
          <template #icon><Activity class="h-4 w-4" aria-hidden="true" /></template>
        </StatCard>
        <StatCard data-testid="dashboard-stats-card-awaiting-human" :label="$t('views.DashboardView.awaiting_human')" :value="cardValue(summary.period?.metrics?.run_counts_by_status?.awaiting_human?.current, summary.run_counts_by_status?.awaiting_human ?? 0)" color="warning" :to="`/runs?status=${RUN_STATUS.AWAITING_HUMAN}`" :delta="periodMetrics?.run_counts_by_status?.awaiting_human ?? null" :no-baseline-label="selectedWindow != null ? $t('views.DashboardView.no_prior_data') : undefined" :inverted="true">
          <template #icon><Clock class="h-4 w-4" aria-hidden="true" /></template>
        </StatCard>
      </div>
      <div class="grid gap-4 sm:grid-cols-2">
        <StatCard data-testid="dashboard-stats-card-failed" :label="$t('views.DashboardView.failed')" :value="cardValue(summary.period?.metrics?.run_counts_by_status?.failed?.current, summary.run_counts_by_status?.failed ?? 0)" color="destructive" :to="`/runs?status=${RUN_STATUS.FAILED}`" :delta="periodMetrics?.run_counts_by_status?.failed ?? null" :no-baseline-label="selectedWindow != null ? $t('views.DashboardView.no_prior_data') : undefined" :inverted="true">
          <template #icon><XCircle class="h-4 w-4" aria-hidden="true" /></template>
        </StatCard>
        <StatCard data-testid="dashboard-stats-card-idle" :label="$t('views.DashboardView.idle')" :value="cardValue(summary.period?.metrics?.run_counts_by_status?.idle?.current, summary.run_counts_by_status?.idle ?? 0)" color="muted" to="/pipelines" :delta="periodMetrics?.run_counts_by_status?.idle ?? null" :no-baseline-label="selectedWindow != null ? $t('views.DashboardView.no_prior_data') : undefined" :inverted="true">
          <template #icon><PauseCircle class="h-4 w-4" aria-hidden="true" /></template>
        </StatCard>
      </div>
      <!-- Eval pass rate + Token spend -->
      <div class="grid gap-4 sm:grid-cols-2">
        <!-- Eval pass rate card -->
        <StatCard
          to="/eval-editor"
          data-testid="dashboard-eval-rate"
          :label="$t('views.DashboardView.eval_pass_rate')"
          :value="hasEvalData ? evalRateDisplay : undefined"
        >
          <template #content>
           <div v-if="hasEvalData" class="mt-1">
             <div class="flex items-center gap-2 mt-1">
               <span v-if="evalPeriodDelta?.delta_pct != null" :class="evalDeltaClass" class="inline-flex items-center text-sm font-medium">
                 <span aria-hidden="true">{{ evalDeltaArrow }}</span>{{ evalDeltaPctText }}
               </span>
               <span v-else :class="evalTrendClass" class="inline-flex items-center text-sm font-medium">
                 <ChevronUp v-if="evalTrend === 'up'" class="h-3.5 w-3.5" aria-hidden="true" />
                 <ChevronDown v-else class="h-3.5 w-3.5" aria-hidden="true" />
                 {{ evalTrendLabel }}
               </span>
               <span class="text-xs text-muted-foreground">{{ summary.eval_pass_rate?.total_evals }} {{ $t('views.DashboardView.total_evals_all_time') }}</span>
             </div>
              <Sparkline v-if="hasEvalChartData" class="mt-2 h-12 w-full" :data="evalSparklineData" :labels="summaryTrendLabels" unit="%" color="hsl(var(--primary-text))" :show-y-axis="true" />
              <div v-else class="mt-2 flex h-12 items-center justify-center rounded-md border border-dashed border-border text-xs text-muted-foreground" aria-live="polite">
                {{ $t('components.Sparkline.no_data') }}
              </div>
           </div>
           <div v-else class="flex flex-col items-center justify-center gap-1 py-6 text-sm text-muted-foreground" aria-live="polite">
             <span class="font-semibold">{{ $t('common.na') }}</span>
             <span>{{ $t('views.DashboardView.no_eval_data_yet') }}</span>
           </div>
          </template>
        </StatCard>
        <!-- Token spend card -->
        <StatCard
          :to="costsDestination"
          data-testid="dashboard-token-spend"
          :label="spendLabel"
          :value="hasSpendData ? formatMoney(totalSpend, currencyCode, 2) : undefined"
        >
          <template #content>
          <template v-if="hasSpendData">
            <div v-if="spendDeltaPct != null" class="mt-1">
              <span :class="spendDeltaClass" class="text-xs font-medium">
                <span aria-hidden="true">{{ spendDeltaArrow }}</span>{{ spendDeltaPctText }}
              </span>
            </div>
            <p v-if="selectedWindow != null" class="text-xs text-muted-foreground mt-1">{{ spendTrackedDays }} {{ $t('views.DashboardView.days_tracked') }}</p>
            <Sparkline class="mt-2 h-12 w-full" :data="spendSparklineData" :labels="summaryTrendLabels" missing-data="zero" unit="$" color="hsl(var(--warning-text))" :show-y-axis="true" />
          </template>
          <div v-else class="flex flex-col items-center justify-center gap-1 py-6 text-sm text-muted-foreground" aria-live="polite" data-testid="dashboard-token-spend-empty">
            <span>{{ $t('views.DashboardView.no_token_spend_data_yet') }}</span>
          </div>
          </template>
        </StatCard>
      </div>
      <!-- Team breakdown (Team only) -->
      <DataCard
        v-if="isTeam && summary.teams && summary.teams.length > 0"
        :title="$t('views.DashboardView.team_breakdown')"
        heading-level="h2"
      >
        <template #headerActions>
          <span class="text-xs font-medium text-muted-foreground bg-muted px-2 py-0.5 rounded">{{ $t('views.DashboardView.team') }}</span>
        </template>
        <!-- FAR-2539: clip at the card edge and scroll inside it on phones. -->
        <!-- A11Y-3: the scroll region is a keyboard-reachable focus stop. The
             sr-only instructions and focus ring mirror the DataTable and
             PipelineListView scroll-region patterns so keyboard users can
             reach the right-hand columns when the table is wider than the
             viewport. -->
        <p id="dashboard-team-breakdown-scroll-instructions" class="sr-only">
          {{ $t('views.DashboardView.team_breakdown_scroll_instructions') }}
        </p>
        <div
          class="max-w-full overflow-x-auto overscroll-x-contain focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
          role="region"
          tabindex="0"
          :aria-label="$t('views.DashboardView.team_breakdown_scroll_label')"
          aria-describedby="dashboard-team-breakdown-scroll-instructions"
          data-scroll="team-breakdown"
        > <!-- NOSONAR: named, focusable scroll region preserves keyboard access to the wide table. -->
        <table class="w-full min-w-max text-sm">
          <thead>
            <tr>
              <th class="table-header">{{ $t('views.DashboardView.team') }}</th>
              <th class="table-header table-cell-numeric">{{ $t('views.DashboardView.runs') }}</th>
              <th class="table-header table-cell-numeric">{{ $t('views.DashboardView.completed') }}</th>
              <th class="table-header table-cell-numeric">{{ $t('views.DashboardView.running') }}</th>
              <th class="table-header table-cell-numeric">{{ $t('views.DashboardView.failed') }}</th>
              <th class="table-header table-cell-numeric">{{ $t('views.DashboardView.eval_pass') }}</th>
              <th class="table-header table-cell-numeric w-8"></th>
            </tr>
          </thead>
          <tbody class="divide-y divide-border">
            <tr v-for="team in summary.teams" :key="team.id"
                :data-testid="'dashboard-team-row-' + team.id"
                class="hover:bg-muted/50"
                @click="toggleTeam(team.id)"
                @keydown.enter.self="toggleTeam(team.id)"
                @keydown.space.self.prevent="toggleTeam(team.id)">
              <td class="table-cell font-medium">
                <button
                  type="button"
                  :aria-expanded="expandedTeam === team.id"
                  class="text-left hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  @click.stop="toggleTeam(team.id)"
                >{{ team.name }}</button>
              </td>
              <td class="table-cell-numeric">{{ team.total_runs }}</td>
              <td class="table-cell-numeric text-success-text">{{ team.completed_runs ?? team.run_counts_by_status?.completed ?? 0 }}</td>
              <td class="table-cell-numeric text-success-text">{{ team.run_counts_by_status?.running ?? 0 }}</td>
              <td class="table-cell-numeric text-destructive">{{ team.run_counts_by_status?.failed ?? 0 }}</td>
                <td class="table-cell-numeric">{{ team.eval_pass_rate?.total_evals ? team.eval_pass_rate.pass_rate + '%' : $t('common.na') }}</td>
              <td class="table-cell-numeric">
                <ChevronUp v-if="expandedTeam === team.id" class="h-3.5 w-3.5" aria-hidden="true" />
                <ChevronDown v-else class="h-3.5 w-3.5" aria-hidden="true" />
              </td>
            </tr>
            <tr v-if="expandedTeam && expandedTeamData">
              <td colspan="7" class="table-cell pl-6">
                <div class="text-xs text-muted-foreground space-y-1">
                  <p>{{ $t('views.DashboardView.pipelines') }}: <span class="font-medium text-foreground">{{ expandedTeamData.active_pipelines }}</span></p>
                  <p>{{ $t('views.DashboardView.awaiting_human') }}: <span class="font-medium text-foreground">{{ expandedTeamData.run_counts_by_status.awaiting_human }}</span></p>
                  <p>{{ $t('views.DashboardView.idle') }}: <span class="font-medium text-foreground">{{ expandedTeamData.run_counts_by_status.idle }}</span></p>
                  <p v-if="expandedTeamData.eval_pass_rate">
                    {{ $t('views.DashboardView.evals') }}: <span class="font-medium text-foreground">{{ expandedTeamData.eval_pass_rate.passed_evals }} / {{ expandedTeamData.eval_pass_rate.total_evals }} {{ $t('views.DashboardView.passed') }}</span>
                  </p>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
        </div>
      </DataCard>
      <!-- Trend chart section -->
      <DataCard
        v-if="planStore.featureEnabled('dashboard_charts')"
        data-testid="dashboard-run-activity"
        :title="$t('views.DashboardView.run_activity')"
        heading-level="h2"
      >
        <div v-if="trendData.length > 1" class="space-y-4">
          <div>
            <p class="text-xs font-medium text-muted-foreground mb-1">{{ $t('views.DashboardView.run_count') }}</p>
            <Sparkline class="h-16 w-full" :data="trendRunCounts" :labels="trendLabels" missing-data="zero" :unit="$t('views.DashboardView.runs')" color="hsl(var(--primary-text))" :show-y-axis="true" :show-x-ticks="true" />
          </div>
          <div>
            <p class="text-xs font-medium text-muted-foreground mb-1">{{ $t('views.DashboardView.eval_pass_rate_label') }}</p>
            <Sparkline class="h-16 w-full" :data="trendEvalRates" :labels="trendLabels" unit="%" color="hsl(var(--success-text))" :show-y-axis="true" :show-x-ticks="true" />
          </div>
          <div>
            <p class="text-xs font-medium text-muted-foreground mb-1">{{ $t('views.DashboardView.token_spend') }}</p>
            <Sparkline class="h-16 w-full" :data="trendSpendData" :labels="trendLabels" missing-data="zero" unit="$" color="hsl(var(--warning-text))" :show-y-axis="true" :show-x-ticks="true" />
          </div>
          <div>
            <p class="text-xs font-medium text-muted-foreground mb-1">{{ $t('views.DashboardView.avg_approval_time') }}</p>
            <Sparkline class="h-16 w-full" :data="trendHitlWaitMs" :labels="trendLabels" unit="ms" color="hsl(var(--warning-text))" :show-y-axis="true" :show-x-ticks="true" />
          </div>
          <div>
            <p class="text-xs font-medium text-muted-foreground mb-1">{{ $t('views.DashboardView.rejection_rate') }}</p>
            <Sparkline class="h-16 w-full" :data="trendRejectionRate" :labels="trendLabels" unit="%" color="hsl(var(--destructive-text))" :show-y-axis="true" :show-x-ticks="true" />
          </div>
        </div>
        <div v-else class="mt-2 flex h-12 items-center justify-center rounded-md border border-dashed border-border text-xs text-muted-foreground" aria-live="polite">
          {{ $t('views.DashboardView.no_data_trends') }}
        </div>
      </DataCard>
      <!-- Recent runs list -->
      <TableCard data-testid="dashboard-recent-runs" :title="$t('views.DashboardView.recent_runs')" heading-level="h2">
        <DataTable
          :columns="recentRunColumns"
          :rows="summary.recent_runs"
          :row-clickable="false"
        >
          <template #empty>
            {{ $t('views.DashboardView.no_runs_yet') }}
          </template>
          <template #cell-pipeline_name="{ row }">
            <router-link :to="'/runs/' + String(row.id)" class="block min-w-0 font-medium hover:underline" :data-testid="'dashboard-recent-run-' + String(row.id)">
              <span class="block truncate" v-tooltip.top="String(row.pipeline_name)">{{ row.pipeline_name }}</span>
              <span class="block text-xs font-normal text-muted-foreground">{{ formatRunDate(String(row.created_at)) }}</span>
            </router-link>
          </template>
          <template #cell-status="{ row }">
            <div class="flex flex-wrap items-center gap-1">
              <span :class="runStatusBadgeClass(String(row.status))" class="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium capitalize">
                {{ runStatusLabel(String(row.status)) }}
              </span>
              <span
                v-if="isDispatchedRun(typeof row.execution_origin === 'string' ? row.execution_origin : null)"
                :data-testid="'dashboard-dispatched-' + String(row.id)"
                :title="$t('common.execution_origin.dispatched_hint')"
                class="inline-flex items-center rounded-full bg-cyan-500/10 px-2 py-0.5 text-xs font-medium text-cyan-700 dark:bg-cyan-900 dark:text-cyan-300"
              >{{ $t('common.execution_origin.dispatched') }}</span>
            </div>
          </template>
          <template #cell-trigger_type="{ row }">
            <span class="text-xs text-muted-foreground">{{ String(row.trigger_type ?? '') }}</span>
          </template>
        </DataTable>
      </TableCard>
    </template>
  </div>
</template>
<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import PageHeader from '../components/shared/PageHeader.vue'
import DashboardNotificationsPanel from '../components/DashboardNotificationsPanel.vue'
import DashboardSystemOverview from '../components/dashboard/DashboardSystemOverview.vue'
import { usePlanStore } from '../stores/planStore'
import { useSystemMapsStore } from '../stores/systemMaps'
import { useDashboardStore } from '../stores/dashboard'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import Sparkline from '../components/shared/Sparkline.vue'
import StatCard from '../components/ui/StatCard.vue'
import DataCard from '../components/ui/DataCard.vue'
import TableCard from '../components/ui/TableCard.vue'
import CardSkeleton from '../components/ui/CardSkeleton.vue'
import { DataTable, type Column } from '../components/ui/data-table'
import { ChevronUp, ChevronDown, GitBranch, BarChart2, Activity, Clock, XCircle, PauseCircle } from '@lucide/vue'
import { RUN_STATUS } from '../constants/filters'
import { formatMoney } from '../lib/money'
import { runStatusBadgeClass, runStatusLabel, formatRunDate, isDispatchedRun } from '../utils/runUtils'
import { useOrgCurrency } from '../composables/useOrgCurrency'
import { useCurrentUser } from '../composables/useCurrentUser'
import { getNavGroups, nonTierRefusal } from '../config/navigation'

const { t } = useI18n()

const { currencyCode, loadCurrency } = useOrgCurrency()

const planStore = usePlanStore()
const systemMapsStore = useSystemMapsStore()
const dashboardStore = useDashboardStore()

/**
 * FAR-2638: System Maps ships at Beta, so its dashboard entry point obeys the
 * SAME release gate the sidebar and the router read from the manifest
 * (`systemMapsStore.systemMapsReleased` → `private_preview` needs developer
 * mode, never a demo session). `systemMapsEnabled` alone is not enough: it
 * derives from the viewer-floor `system_maps.read` permission, so on its own
 * it kept the card on an alpha dashboard while the route bounced back here.
 */
const systemOverviewVisible = computed(
  () =>
    systemMapsStore.accessLoaded &&
    systemMapsStore.systemMapsEnabled &&
    systemMapsStore.systemMapsReleased,
)

const loading = computed(() => dashboardStore.loading)
const error = computed(() => dashboardStore.error)
const summary = computed(() => dashboardStore.summary)
const totalSpend = computed(() => dashboardStore.totalSpend)
// Spend is only meaningful once the ledger has recorded something. A zero
// total (no runs, or a window with no billable activity) renders the same
// compact empty state as the eval chart rather than a misleading "$0.00".
const hasSpendData = computed(() => totalSpend.value > 0)
const periodRefreshing = computed(() => dashboardStore.periodRefreshing)

const isTeam = computed(() => planStore.isTeam)

const { jwtPayload, isSystemAdmin, orgRole } = useCurrentUser()
const costsNavItem = getNavGroups()
  .flatMap((group) => group.items)
  .find((item) => item.to === '/admin/costs')

/**
 * Where the token-spend card leads (FAR-2664).
 *
 * The card used to link at /admin/costs unconditionally, so a caller the route
 * refuses was bounced to the Dashboard with no message — the card looked
 * broken. Costs is role-gated (admin) and tier-gated (Team):
 *
 *  - a caller who passes the ROLE gate links straight at Costs; a below-tier
 *    caller lands on the locked page naming the capability (the router guard's
 *    FAR-2664 tier lock);
 *  - a caller who does NOT pass it links at the same locked page with
 *    `reason=role`, because no licence upgrade can grant them access.
 *
 * The non-tier refusal is decided by the SAME helper the capacity banner uses
 * (`nonTierRefusal`), so the two audited links cannot diverge. The role comes
 * synchronously from the access token: an unreadable token keeps the plain
 * destination and lets the guard be the authority, while a readable token
 * whose `org_role` is empty is a refusal — not an unknown.
 */
const costsDestination = computed(() => {
  if (!costsNavItem) return '/admin/costs'
  const refusal = nonTierRefusal(costsNavItem, {
    hasReadableToken: jwtPayload.value !== null,
    isSystemAdmin: isSystemAdmin.value,
    orgRole: orgRole.value,
  })
  if (!refusal) return '/admin/costs'
  const params = new URLSearchParams({ from: costsNavItem.to, reason: refusal })
  if (costsNavItem.requiredEntitlement) params.set('entitlement', costsNavItem.requiredEntitlement)
  // FAR-2688: carry the roles that would grant access so the locked page can
  // name them instead of falling back to the generic "ask for the role that
  // includes it". The naming copy is FAR-2687 (open PR at the time of writing);
  // carrying the parameter here is additive — until it lands the locked page
  // simply ignores it, and a below-tier/tier lock is unchanged.
  if (refusal === 'role' && costsNavItem.requiredRoles?.length) {
    params.set('role', costsNavItem.requiredRoles.join(','))
  }
  return `/locked?${params.toString()}`
})

const recentRunColumns = computed<Column[]>(() => [
  { key: 'pipeline_name', label: t('views.DashboardView.pipeline') },
  { key: 'status', label: t('views.DashboardView.status') },
  { key: 'trigger_type', label: t('views.DashboardView.trigger') },
])

const hasEvalData = computed(() => {
  if (!summary.value) return false;
  if (selectedWindow.value != null && summary.value.period) {
    return summary.value.period.metrics?.eval_pass_rate?.current != null;
  }
  return summary.value.eval_pass_rate != null && summary.value.eval_pass_rate.total_evals > 0;
})

const expandedTeam = ref<string | null>(null)

function toggleTeam(teamId: string) {
  expandedTeam.value = expandedTeam.value === teamId ? null : teamId
}

const expandedTeamData = computed(() => {
  if (!expandedTeam.value || !summary.value?.teams) return null
  return summary.value.teams.find(t => t.id === expandedTeam.value) ?? null
})

const evalSparklineData = computed((): Array<number | null> => {
  if (!summary.value?.trend) return [];
  // The summary trend is a fixed backend trend, not an all-time series. Finite
  // windows use the dashboard trends request; All time has no chart source.
  if (selectedWindow.value == null) return [];
  const windowedRates = dashboardStore.trends?.eval_pass_rates ?? [];
  return trendData.value.map((entry) => windowedRates.find((rate) => rate.date === entry.date)?.pass_rate ?? null);
})

const lastEvalRates = computed(() => evalSparklineData.value.filter((r): r is number => r !== null && Number.isFinite(r)))

// Sparkline only draws once it has two real observations. Keep the meaningful
// summary value visible above that threshold, but avoid mounting the primitive
// (and its full-width empty plot) when there is nothing to chart.
const hasEvalChartData = computed(() => lastEvalRates.value.length >= 2)

const evalTrend = computed(() => {
  const rates = lastEvalRates.value
  if (selectedWindow.value != null && !summary.value?.period) return 'flat'
  if (rates.length < 2) return 'flat'
  const firstHalf = rates.slice(0, Math.floor(rates.length / 2))
  const secondHalf = rates.slice(Math.floor(rates.length / 2))
  const firstAvg = firstHalf.reduce((a, b) => a + b, 0) / firstHalf.length
  const secondAvg = secondHalf.reduce((a, b) => a + b, 0) / secondHalf.length
  if (secondAvg > firstAvg) return 'up'
  if (secondAvg < firstAvg) return 'down'
  return 'flat'
})

const evalTrendClass = computed(() => {
  if (evalTrend.value === 'up') return 'text-success-text'
  if (evalTrend.value === 'down') return 'text-destructive'
  return 'text-muted-foreground'
})

const evalTrendLabel = computed(() => {
  if (evalTrend.value === 'up') return t('views.DashboardView.improving')
  if (evalTrend.value === 'down') return t('views.DashboardView.declining')
  return t('views.DashboardView.stable')
})

const spendSparklineData = computed(() => trendData.value.map(d => d.token_spend_usd))

const summaryTrendLabels = computed(() => {
  if (selectedWindow.value == null) return []
  return trendLabels.value
})

// --- Rolling-window toggle (FAR-92 / FAR-115) ---
// Gated on the analytics_page flag (FAR-92): hidden when off — the dashboard
// keeps the legacy all-time shape and the backend ignores `days` (scoping its
// summary cache by the flag state).
const trendToggleVisible = computed(() => planStore.featureEnabled('analytics_page'))

const trendWindows: Array<{ labelKey: string; value: number | null }> = [
  { labelKey: 'views.DashboardView.trend_24h', value: 1 },
  { labelKey: 'views.DashboardView.trend_3d', value: 3 },
  { labelKey: 'views.DashboardView.trend_7d', value: 7 },
  { labelKey: 'views.DashboardView.trend_30d', value: 30 },
  { labelKey: 'views.DashboardView.trend_90d', value: 90 },
  { labelKey: 'views.DashboardView.trend_all_time', value: null },
]

const TREND_WINDOW_STORAGE_KEY = 'modulo.dashboard.trendWindow'
const TREND_WINDOW_ALLOWED = new Set([1, 3, 7, 30, 90])

function loadTrendWindow(): number | null {
  try {
    const raw = localStorage.getItem(TREND_WINDOW_STORAGE_KEY)
    if (raw === 'all') return null
    const parsed = Number(raw)
    if (TREND_WINDOW_ALLOWED.has(parsed)) return parsed
  } catch {
    // localStorage unavailable (private mode / SSR) — fall through to default.
  }
  return 3
}

function persistTrendWindow(days: number | null) {
  try {
    localStorage.setItem(TREND_WINDOW_STORAGE_KEY, days == null ? 'all' : String(days))
  } catch {
    // localStorage unavailable — persistence is best-effort.
  }
}

const selectedWindow = ref<number | null>(null)

function selectWindow(days: number | null) {
  selectedWindow.value = days
  persistTrendWindow(days)
  if (days == null) {
    // All-time has no finite trend source, so clear both finite chart data and
    // any in-flight period metrics before reloading the all-time summary.
    dashboardStore.clearTrends()
    dashboardStore.clearPeriodMetrics()
    void dashboardStore.fetchSummary()
  } else {
    // Period windows refresh the period block and the charts from the same
    // selected window — no full-page skeleton.
    void dashboardStore.fetchPeriodMetrics(days)
    void dashboardStore.fetchTrends(days)
  }
}

// Bug A: `??` only falls back on null/undefined, so a period window with zero
// runs shows 0 instead of the all-time figure. Show the period current only
// when it's a meaningful number (non-zero, or the all-time figure is also
// zero/absent); otherwise fall back to the all-time value.
function cardValue(periodCurrent: number | null | undefined, allTime: number | undefined): number {
  if (typeof periodCurrent === 'number') {
    const allTimeVal = allTime ?? 0
    if (periodCurrent !== 0 || allTimeVal === 0) return periodCurrent
  }
  return allTime ?? 0
}

// --- Period-scoped metrics (same-source/same-window value AND arrow) ---
const periodMetrics = computed(() => selectedWindow.value == null ? null : summary.value?.period?.metrics ?? null)

const evalPeriodDelta = computed(() => periodMetrics.value?.eval_pass_rate ?? null)
const evalRateDisplay = computed(() => {
  const period = evalPeriodDelta.value
  if (selectedWindow.value != null && period) {
    const current = period.current
    return current == null ? t('common.na') : `${current}%`
  }
  const allTime = summary.value?.eval_pass_rate?.overall_pass_rate
  return allTime == null ? t('common.na') : `${allTime}%`
})
const evalDeltaDirection = computed(() => deltaDirection(evalPeriodDelta.value?.delta_pct ?? null))
const evalDeltaArrow = computed(() => {
  if (evalDeltaDirection.value === 'up') return '▲'
  if (evalDeltaDirection.value === 'down') return '▼'
  return '→'
})
const evalDeltaClass = computed(() => {
  if (evalDeltaDirection.value === 'up') return 'text-success-text'
  if (evalDeltaDirection.value === 'down') return 'text-destructive'
  return 'text-muted-foreground'
})
const evalDeltaPctText = computed(() => {
  const d = evalPeriodDelta.value?.delta_pct
  return typeof d === 'number' && Number.isFinite(d) ? `${Math.abs(d).toFixed(1)}%` : ''
})

const spendDeltaPct = computed(() => {
  const d = summary.value?.period?.metrics?.spend?.delta_pct
  return typeof d === 'number' && Number.isFinite(d) ? d : null
})
const spendDeltaDirection = computed(() => deltaDirection(spendDeltaPct.value))
const spendDeltaArrow = computed(() => {
  if (spendDeltaDirection.value === 'up') return '▲'
  if (spendDeltaDirection.value === 'down') return '▼'
  return '→'
})
const spendDeltaClass = computed(() => {
  // Inverted for a cost metric: spend going UP is bad (red), spend going DOWN is good (green).
  if (spendDeltaDirection.value === 'up') return 'text-destructive'
  if (spendDeltaDirection.value === 'down') return 'text-success-text'
  return 'text-muted-foreground'
})
const spendDeltaPctText = computed(() => {
  const d = spendDeltaPct.value
  return d == null ? '' : `${Math.abs(d).toFixed(1)}%`
})
const spendTrackedDays = computed(() => selectedWindow.value == null ? 0 : summary.value?.period?.days ?? 0)

const SPEND_LABEL_KEY_BY_DAYS: Record<number, string> = {
  1: 'views.DashboardView.token_spend_24h',
  3: 'views.DashboardView.token_spend_3d',
  7: 'views.DashboardView.token_spend_7d',
  30: 'views.DashboardView.token_spend_30d',
  90: 'views.DashboardView.token_spend_90d',
}

const spendLabel = computed(() => {
  const days = selectedWindow.value
  return t(days == null
    ? 'views.DashboardView.token_spend_all_time'
    : SPEND_LABEL_KEY_BY_DAYS[days])
})

function deltaDirection(deltaPct: number | null): 'up' | 'down' | 'flat' | null {
  if (deltaPct == null) return null
  if (deltaPct > 0) return 'up'
  if (deltaPct < 0) return 'down'
  return 'flat'
}

const trendsRaw = computed(() => dashboardStore.trends)

const trendData = computed(() => {
  const tr = trendsRaw.value
  if (!tr) return []
  const items: Array<{ date: string; run_count: number; eval_pass_rate: number | null; token_spend_usd: number; avg_time_to_approve_ms: number | null; rejection_rate: number | null }> = []
  const evalMap = new Map(tr.eval_pass_rates.map(r => [r.date, r.pass_rate]))
  const spendMap = new Map(tr.token_spend.map(r => [r.date, r.total_spend_usd]))
  const hitlMap = new Map(tr.hitl_volume.map(h => [h.date, h.avg_time_to_approve_ms]))
  const rejectionMap = new Map(tr.rejection_trend.map(r => [r.date, r.rolling_rejection_rate]))
  // Build a combined series covering all dates
  for (const entry of tr.run_counts) {
    items.push({
      date: entry.date,
      run_count: entry.run_count,
      eval_pass_rate: evalMap.get(entry.date) ?? null,
      token_spend_usd: spendMap.get(entry.date) ?? 0,
      avg_time_to_approve_ms: hitlMap.get(entry.date) ?? null,
      rejection_rate: rejectionMap.get(entry.date) ?? null,
    })
  }
  return items
})

const trendRunCounts = computed(() => trendData.value.map(d => d.run_count))
const trendEvalRates = computed((): Array<number | null> => trendData.value.map(d => d.eval_pass_rate))
const trendSpendData = computed(() => trendData.value.map(d => d.token_spend_usd))
const trendHitlWaitMs = computed((): Array<number | null> => trendData.value.map(d => d.avg_time_to_approve_ms))
const trendRejectionRate = computed((): Array<number | null> => trendData.value.map(d => d.rejection_rate))
const trendLabels = computed(() => trendData.value.map(d => d.date))

// Retry re-runs EVERY source the view reads, with the window the user is
// actually looking at: the summary (retrying it with no argument fell back to
// all-time and dropped the selected 1d/3d/… window) and, for a finite window,
// the trend series that shares that window. Retrying only the summary left a
// failed trends fetch in place, so the charts stayed empty with no error.
function retryDashboard() {
  void dashboardStore.fetchSummary(selectedWindow.value ?? undefined)
  if (selectedWindow.value != null) void dashboardStore.fetchTrends(selectedWindow.value)
}

onMounted(async () => {
  // Restore the persisted window (FAR-115); default to 3d when nothing is stored.
  selectedWindow.value = loadTrendWindow();
  const promises: Promise<unknown>[] = [
    dashboardStore.fetchSummary(selectedWindow.value ?? undefined),
    ...(selectedWindow.value == null ? [] : [dashboardStore.fetchTrends(selectedWindow.value)]),
    loadCurrency(),
  ];
  if (!planStore.currentTier || planStore.currentTier === 'community') {
    promises.push(planStore.fetchPlan());
  }
  promises.push(systemMapsStore.fetchAccess())
  await Promise.all(promises);
})
</script>
