<template>
  <DataCard
    as="section"
    data-testid="determination-scan"
    :title="$t('views.DeterminationView.scan_title')"
    heading-level="h2"
  >
    <template v-if="scan" #headerActions>
      <span class="text-sm text-muted-foreground" data-testid="determination-scan-summary">
        {{ scan.summary }}
      </span>
    </template>

    <div class="space-y-4">
    <div
      v-if="loading"
      class="flex items-center gap-3 text-sm text-muted-foreground"
      aria-live="polite"
      data-testid="determination-scan-loading"
    >
      <LoadingSpinner />
      <span>{{ $t('views.DeterminationView.scan_loading') }}</span>
    </div>

    <ErrorAlert v-else-if="error" :message="error" :on-retry="onRetry" />

    <p v-else-if="!scan" class="text-sm text-muted-foreground" data-testid="determination-scan-empty">
      {{ $t('views.DeterminationView.scan_empty') }}
    </p>

    <template v-else>
      <section class="space-y-2" data-testid="determination-samples">
        <h3 class="text-sm font-medium">
          {{ $t('views.DeterminationView.samples_title') }}
          <span class="text-muted-foreground">
            ({{ $t('views.DeterminationView.samples_count', { count: scan.samples.length }) }})
          </span>
        </h3>
        <p v-if="scan.samples.length === 0" class="text-sm text-muted-foreground" data-testid="determination-samples-empty">
          {{ $t('views.DeterminationView.samples_empty') }}
        </p>
        <DataTable
          v-else
          :columns="sampleColumns"
          :rows="sampleRows"
          :row-clickable="false"
          row-testid="determination-sample-row"
        >
          <template #cell-sample_count="{ value }">
            <span class="tabular-nums">{{ value }}</span>
          </template>
          <template #cell-connector_id="{ value }">
            <span class="font-mono text-xs break-all">{{ value }}</span>
          </template>
          <template #cell-error="{ value }">
            <span v-if="value" class="text-destructive-text">{{ value }}</span>
            <span v-else class="text-muted-foreground">{{ $t('views.DeterminationView.sample_no_error') }}</span>
          </template>
        </DataTable>
      </section>

      <section class="space-y-2" data-testid="determination-findings">
        <h3 class="text-sm font-medium">
          {{ $t('views.DeterminationView.findings_title') }}
          <span class="text-muted-foreground">
            ({{ $t('views.DeterminationView.findings_count', { count: scan.findings.length }) }})
          </span>
        </h3>
        <p v-if="scan.findings.length === 0" class="text-sm text-muted-foreground" data-testid="determination-findings-empty">
          {{ $t('views.DeterminationView.findings_empty') }}
        </p>
        <ul v-else class="space-y-2">
          <li
            v-for="(finding, index) in scan.findings"
            :key="`${finding.category}-${index}`"
            class="rounded-lg border bg-muted/20 p-3 space-y-1"
            data-testid="determination-finding-row"
          >
            <div class="flex flex-wrap items-center gap-2">
              <span class="rounded-full bg-muted px-2 py-0.5 text-xs font-medium">{{ finding.category }}</span>
              <span class="rounded-full px-2 py-0.5 text-xs font-medium" :class="confidenceBadgeClass(finding.confidence)">
                {{ $t('views.DeterminationView.confidence_' + finding.confidence) }}
              </span>
            </div>
            <p class="text-sm font-medium">{{ finding.finding }}</p>
            <p class="text-sm text-muted-foreground">
              <span class="font-medium">{{ $t('views.DeterminationView.finding_evidence') }}:</span> {{ finding.evidence }}
            </p>
            <p v-if="finding.uncertainty" class="text-sm text-muted-foreground">
              <span class="font-medium">{{ $t('views.DeterminationView.finding_uncertainty') }}:</span> {{ finding.uncertainty }}
            </p>
          </li>
        </ul>
      </section>
    </template>
    </div>
  </DataCard>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import ErrorAlert from '../shared/ErrorAlert.vue'
import LoadingSpinner from '../shared/LoadingSpinner.vue'
import DataCard from '../ui/DataCard.vue'
import { DataTable, type Column, type DataTableRow } from '../ui/data-table'
import { confidenceBadgeClass } from './confidencePresentation'
import type { DeterminationScanResponse } from '../../composables/useDetermination'

const props = defineProps<{
  scan: DeterminationScanResponse | null
  loading: boolean
  error: string | null
}>()

const emit = defineEmits<{ (event: 'retry'): void }>()

function onRetry(): void {
  emit('retry')
}

// Read-only sample table (FAR-2728): DataTable owns the shell, the header row
// and the rows. The row testid is preserved through `row-testid`, and the three
// cells whose content is styled beyond plain text (tabular count, monospace
// connector id, error/no-error span) are cell slots.
const { t } = useI18n()

const sampleColumns: Column[] = [
  { key: 'connector_type', label: t('views.DeterminationView.sample_type') },
  { key: 'resource', label: t('views.DeterminationView.sample_resource') },
  { key: 'sample_count', label: t('views.DeterminationView.sample_count') },
  { key: 'connector_id', label: t('views.DeterminationView.sample_connector') },
  { key: 'error', label: t('views.DeterminationView.sample_error') },
]

// The table renders only inside the `v-else` where `scan` is non-null; the
// optional chain keeps the computed total if that ever changes.
const sampleRows = computed<DataTableRow[]>(() =>
  (props.scan?.samples ?? []).map((sample) => ({
    // The source row key is the connector/resource pair, so the DataTable row
    // key stays stable rather than falling back to the index.
    id: `${sample.connector_id}-${sample.resource}`,
    connector_type: sample.connector_type,
    resource: sample.resource,
    sample_count: sample.sample_count,
    connector_id: sample.connector_id,
    error: sample.error,
  })),
)
</script>
