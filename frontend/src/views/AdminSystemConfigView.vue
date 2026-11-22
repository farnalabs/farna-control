<template>
  <FeatureGate feature-name="runtime_config" show-disabled>
  <div class="page-wide">
    <PageHeader :title="$t('views.AdminSystemConfigView.system_admin_config')" :subtitle="$t('views.AdminSystemConfigView.deploymentwide_system_configuration_system_admin_only')">
      <template #right>
        <button
          type="button"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
          :disabled="loading"
          data-testid="admin-system-config-refresh"
          @click="loadConfig"
        >
          {{ $t('views.AdminSystemConfigView.refresh') }}
        </button>
      </template>
    </PageHeader>

    <ErrorAlert v-if="error" :message="error" :on-retry="loadConfig" />

    <EmptyState
      v-else-if="!loading && items.length === 0"
      :title="$t('views.AdminSystemConfigView.no_configuration_entries_found')"
    />

    <div v-else class="table-wrapper">
      <table class="w-full">
        <thead>
          <tr>
            <th class="table-header">{{ $t('views.AdminSystemConfigView.key') }}</th>
            <th class="table-header">{{ $t('views.AdminSystemConfigView.value') }}</th>
            <th class="table-header">{{ $t('views.AdminSystemConfigView.updated') }}</th>
          </tr>
        </thead>
        <tbody v-if="loading" class="divide-y divide-border">
          <tr v-for="n in 3" :key="n">
            <td class="table-cell"><div class="h-5 w-24 animate-pulse rounded bg-muted" /></td>
            <td class="table-cell"><div class="h-16 animate-pulse rounded bg-muted" /></td>
            <td class="table-cell"><div class="h-4 w-32 animate-pulse rounded bg-muted" /></td>
          </tr>
        </tbody>
        <tbody v-else class="divide-y divide-border">
          <tr
            v-for="entry in items"
            :key="entry.key"
            class="hover:bg-muted/50 transition-colors"
          >
            <td class="table-cell">
              <code class="text-sm font-mono">{{ entry.key }}</code>
            </td>
            <td class="table-cell">
              <JsonViewer :data="entry.value" :show-toolbar="true" :max-height="'20rem'" />
            </td>
            <td class="table-cell text-muted-foreground">
              {{ entry.updated_at || '—' }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import JsonViewer from '../components/shared/JsonViewer.vue'
import FeatureGate from '../components/FeatureGate.vue'
import { Ref, ref, watch } from 'vue'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import EmptyState from '../components/shared/EmptyState.vue'

interface ConfigEntry {
  key: string
  value: unknown
  updated_at: string | null
}

const { loading, error, data, load: loadConfig } = useDataFetch(
  () => api.GET('/api/v1/system-admin/config'),
)

const items: Ref<ConfigEntry[]> = ref([])
watch(data, (d) => {
  if (d) items.value = d as unknown as ConfigEntry[]
}, { immediate: true })
</script>
