<template>
  <div class="card p-4 space-y-4" data-testid="risk-assessment-facets">
    <h3 class="text-base font-semibold">{{ $t('views.RiskAssessmentView.facets_title') }}</h3>

    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      <!-- Key Prefix Filter -->
      <div>
        <label for="risk-assessment-key-prefix" class="mb-1 block text-sm font-medium">
          {{ $t('views.RiskAssessmentView.key_prefix_label') }}
        </label>
        <input
          id="risk-assessment-key-prefix"
          v-model="localFilters.keyPrefix"
          type="text"
          placeholder="risk."
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
          data-testid="risk-assessment-key-prefix"
        />
      </div>

      <!-- Pipeline Filter -->
      <div>
        <label for="risk-assessment-pipeline" class="mb-1 block text-sm font-medium">
          {{ $t('views.RiskAssessmentView.pipeline_label') }}
        </label>
        <AppSelect
          id="risk-assessment-pipeline"
          v-model="localFilters.pipelineId"
          class="w-full"
          :options="pipelineOptions"
          option-label="label"
          option-value="value"
          :label="$t('views.RiskAssessmentView.pipeline_label')"
          data-testid="risk-assessment-pipeline"
        >
          <template #option="{ option }">
            <span :data-value="option.value">{{ option.label }}</span>
          </template>
        </AppSelect>
      </div>

      <!-- Time Window Filter -->
      <div>
        <label for="risk-assessment-days" class="mb-1 block text-sm font-medium">
          {{ $t('views.RiskAssessmentView.time_window_label') }}
        </label>
        <AppSelect
          id="risk-assessment-days"
          v-model="localFilters.days"
          class="w-full"
          :options="windowOptions"
          option-label="label"
          option-value="value"
          :label="$t('views.RiskAssessmentView.time_window_label')"
          data-testid="risk-assessment-days"
        >
          <template #option="{ option }">
            <span :data-value="option.value">{{ option.label }}</span>
          </template>
        </AppSelect>
      </div>

      <!-- Action Filter -->
      <div>
        <label for="risk-assessment-action" class="mb-1 block text-sm font-medium">
          {{ $t('views.RiskAssessmentView.action_label') }}
        </label>
        <AppSelect
          id="risk-assessment-action"
          v-model="localFilters.action"
          class="w-full"
          :options="actionOptions"
          option-label="label"
          option-value="value"
          :label="$t('views.RiskAssessmentView.action_label')"
          :disabled="facetDegraded"
          data-testid="risk-assessment-action"
        >
          <template #option="{ option }">
            <span :data-value="option.value">{{ option.label }}</span>
          </template>
        </AppSelect>
        <p v-if="facetDegraded" class="mt-1 text-xs text-warning-text" data-testid="risk-assessment-facet-degraded-note">
          {{ $t('views.RiskAssessmentView.facet_degraded_note') }}
        </p>
        <div v-if="pipelinesError" class="mt-1 flex items-center gap-2 text-xs text-destructive-text" data-testid="risk-assessment-pipelines-error">
          <span data-testid="risk-assessment-pipelines-error-message">{{ pipelinesError }}</span>
          <button
            type="button"
            class="underline"
            data-testid="risk-assessment-pipelines-retry"
            @click="retryPipelines"
          >
            {{ $t('components.ErrorAlert.retry') }}
          </button>
        </div>
      </div>
    </div>

    <div class="flex items-center gap-2 pt-2 border-t" data-testid="risk-assessment-facet-actions">
      <Button
        variant="outline"
        @click="applyFilters"
        :disabled="isLoading"
        data-testid="risk-assessment-apply"
      >
        {{ $t('views.RiskAssessmentView.apply') }}
      </Button>
      <Button
        variant="ghost"
        @click="resetFilters"
        :disabled="isLoading"
        data-testid="risk-assessment-reset"
      >
        {{ $t('views.RiskAssessmentView.reset') }}
      </Button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Button from 'primevue/button'
import AppSelect from '../shared/AppSelect.vue'
import {
  RISK_ACTIONS,
  RISK_WINDOW_CHOICES,
  defaultRiskAssessmentFilters,
  type RiskAssessmentFilters,
} from '../../lib/riskAssessment'

interface Props {
  filters: RiskAssessmentFilters
  facetDegraded: boolean
  isLoading: boolean
  pipelines: Array<{ id: string; name: string }>
  pipelinesError?: string
}

const props = defineProps<Props>()

const emit = defineEmits<{
  'update:filters': [filters: RiskAssessmentFilters]
  apply: [filters: RiskAssessmentFilters]
  reset: []
  retryPipelines: []
}>()

// Local reactive copy of filters to avoid mutating props directly
const localFilters = reactive<RiskAssessmentFilters>(defaultRiskAssessmentFilters())

const { t } = useI18n()

// AppSelect option lists. Each keeps the native control's leading empty entry
// ("all pipelines" / "all actions"), so clearing a facet still means "no
// filter"; the window list is the shared deep-link vocabulary, unchanged.
const pipelineOptions = computed(() => [
  { value: '', label: t('views.RiskAssessmentView.all_pipelines') },
  ...props.pipelines.map((pipeline) => ({ value: pipeline.id, label: pipeline.name })),
])

const windowOptions = computed(() =>
  RISK_WINDOW_CHOICES.map((choice) => ({
    value: choice.value,
    label: t(`views.RiskAssessmentView.${choice.labelKey}`),
  })),
)

const actionOptions = computed(() => [
  { value: '', label: t('views.RiskAssessmentView.all_actions') },
  ...RISK_ACTIONS.map((action) => ({
    value: action,
    label: t(`views.RiskAssessmentView.action_${action}`),
  })),
])

// Sync local filters with props when props change
watch(
  () => props.filters,
  (newFilters) => {
    localFilters.keyPrefix = newFilters.keyPrefix
    localFilters.pipelineId = newFilters.pipelineId
    localFilters.days = newFilters.days
    localFilters.action = newFilters.action
    localFilters.limit = newFilters.limit
  },
  { immediate: true, deep: true },
)

// Emit updates when local filters change
watch(
  localFilters,
  (newFilters) => {
    emit('update:filters', { ...newFilters })
  },
  { deep: true },
)

function applyFilters(): void {
  emit('apply', { ...localFilters })
}

function retryPipelines(): void {
  emit('retryPipelines')
}

function resetFilters(): void {
  Object.assign(localFilters, defaultRiskAssessmentFilters())
  emit('reset')
}
</script>
