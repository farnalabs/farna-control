<template>
  <div class="page-narrow space-y-6" data-testid="evidence-index-page">
    <PageHeader :title="$t('views.EvidenceIndexView.title')" :subtitle="$t('views.EvidenceIndexView.subtitle')">
      <template #right>
        <button
          type="button"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-muted disabled:opacity-50"
          data-testid="evidence-index-refresh"
          :disabled="store.isLoading"
          @click="store.fetchIndex"
        >
          {{ store.isLoading ? $t('views.EvidenceIndexView.refreshing') : $t('views.EvidenceIndexView.refresh') }}
        </button>
      </template>
    </PageHeader>

    <div v-if="store.isLoading && !store.index" aria-live="polite" data-testid="evidence-index-loading" class="flex items-center gap-3 text-sm text-muted-foreground">
      <LoadingSpinner />
      <span>{{ $t('views.EvidenceIndexView.loading') }}</span>
    </div>
    <ErrorAlert v-if="store.error" :message="store.error" :on-retry="store.fetchIndex" />
    <template v-if="store.index">
      <section class="grid gap-3 sm:grid-cols-3" :aria-label="$t('views.EvidenceIndexView.summary_label')">
        <StatCard :label="$t('views.EvidenceIndexView.pack_version')" :value="store.index.pack_version" />
        <StatCard :label="$t('views.EvidenceIndexView.generated_at')" :value="formatDate(store.index.generated_at)" />
        <StatCard :label="$t('views.EvidenceIndexView.coverage')" :value="store.index.complete ? $t('views.EvidenceIndexView.complete') : $t('views.EvidenceIndexView.incomplete')" />
      </section>

      <DataCard
        :title="$t('views.EvidenceIndexView.artifacts_title')"
        heading-level="h2"
        variant="bordered"
        padding="md"
        role="region"
        :aria-label="$t('views.EvidenceIndexView.artifacts_title')"
      >
        <template #headerActions>
          <span aria-live="polite" class="text-sm text-muted-foreground">{{ store.index.artifacts.length }}</span>
        </template>
        <EmptyState v-if="!store.index.artifacts.length" :title="$t('views.EvidenceIndexView.empty_title')" :description="$t('views.EvidenceIndexView.empty_description')" />
        <div v-else class="table-wrapper">
          <table class="w-full text-left text-sm"><thead><tr><th scope="col" class="table-header">{{ $t('views.EvidenceIndexView.col_type') }}</th><th scope="col" class="table-header">{{ $t('views.EvidenceIndexView.col_status') }}</th><th scope="col" class="table-header">{{ $t('views.EvidenceIndexView.col_provenance') }}</th><th scope="col" class="table-header">{{ $t('views.EvidenceIndexView.col_records') }}</th><th scope="col" class="table-header">{{ $t('views.EvidenceIndexView.col_location') }}</th></tr></thead>
            <tbody class="divide-y divide-border"><tr v-for="artifact in store.index.artifacts" :key="artifact.artifact_type" class="align-top"><td class="table-cell font-medium">{{ artifact.artifact_type }}</td><td class="table-cell"><span class="rounded-full bg-muted px-2 py-1 text-xs">{{ artifact.status }}</span><div v-if="artifact.absence_reason" class="mt-1 text-xs text-muted-foreground">{{ artifact.absence_reason }}</div></td><td class="table-cell">{{ artifact.provenance }}</td><td class="table-cell">{{ artifact.record_count ?? '—' }}</td><td class="table-cell max-w-sm break-words font-mono text-xs">{{ artifact.location }}</td></tr></tbody>
          </table>
        </div>
      </DataCard>
      <p v-if="store.index.missing_artifact_types.length" aria-live="polite" class="rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm text-warning-text">{{ $t('views.EvidenceIndexView.missing', { count: store.index.missing_artifact_types.length }) }}</p>
    </template>
  </div>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import PageHeader from '../components/shared/PageHeader.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import StatCard from '../components/ui/StatCard.vue'
import DataCard from '../components/ui/DataCard.vue'
import { useEvidenceStore } from '../stores/evidence'

const store = useEvidenceStore()
const formatDate = (value: string): string => new Date(value).toLocaleString()
onMounted(() => { void store.fetchIndex() })
</script>
