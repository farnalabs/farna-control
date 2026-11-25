<template>
  <BaseCard padding="md" class="space-y-4" data-testid="analytics-filter-bar">
    <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <div>
        <p class="mb-2 text-xs font-medium text-muted-foreground">
          {{ $t("views.AnalyticsView.timespan") }}
        </p>
        <div class="flex flex-wrap gap-1">
          <button type="button"
            v-for="t in timespans"
            :key="t.value"
            :data-testid="`analytics-timespan-${t.value}`"
            :aria-pressed="filters.timespan === t.value"
            :class="[
              'rounded px-3 py-1 text-xs font-medium transition-colors',
              filters.timespan === t.value
                ? 'bg-primary text-primary-foreground'
                : 'bg-muted text-muted-foreground hover:bg-muted/80',
            ]"
            @click="emitFilters({ timespan: t.value })"
          >
            {{ $t(t.labelKey) }}
          </button>
        </div>
      </div>

      <div>
        <p class="mb-2 text-xs font-medium text-muted-foreground">
          {{ $t("views.AnalyticsView.group_by") }}
        </p>
        <div class="flex flex-wrap gap-1">
          <template v-if="isHourGranular">
            <button
              type="button"
              disabled
              aria-disabled="true"
              class="cursor-not-allowed rounded bg-primary px-3 py-1 text-xs font-medium text-primary-foreground opacity-70"
              data-testid="analytics-group-by-hour"
              :aria-pressed="true"
            >
              {{ $t("views.AnalyticsView.group_by_hour") }}
            </button>
          </template>
          <button type="button"
            v-else
            v-for="g in groupByOptions"
            :key="g.value"
            :data-testid="`analytics-group-by-${g.value}`"
            :aria-pressed="filters.groupBy === g.value"
            :class="[
              'rounded px-3 py-1 text-xs font-medium transition-colors',
              filters.groupBy === g.value
                ? 'bg-primary text-primary-foreground'
                : 'bg-muted text-muted-foreground hover:bg-muted/80',
            ]"
            @click="emitFilters({ groupBy: g.value })"
          >
            {{ $t(g.labelKey) }}
          </button>
        </div>
      </div>

      <div>
        <p class="mb-2 text-xs font-medium text-muted-foreground">
          {{ $t("views.AnalyticsView.measure") }}
        </p>
        <div class="flex flex-wrap gap-1">
          <button type="button"
            v-for="m in measures"
            :key="m.value"
            :data-testid="`analytics-measure-${m.value}`"
            :aria-pressed="measure === m.value"
            :class="[
              'rounded px-3 py-1 text-xs font-medium transition-colors',
              measure === m.value
                ? 'bg-primary text-primary-foreground'
                : 'bg-muted text-muted-foreground hover:bg-muted/80',
            ]"
            @click="emit('update:measure', m.value)"
          >
            {{ $t(m.labelKey) }}
          </button>
        </div>
      </div>

      <div>
        <label
          for="analytics-filter-dimension"
          class="mb-2 block text-xs font-medium text-muted-foreground"
        >
          {{ $t("views.AnalyticsView.dimension") }}
        </label>
        <AppSelect
          id="analytics-filter-dimension"
          :model-value="filters.dimension ?? ''"
          :options="dimensionOptions"
          option-label="label"
          option-value="value"
          :label="$t('views.AnalyticsView.dimension')"
          class="w-full"
          data-testid="analytics-filter-dimension"
          @update:model-value="onDimensionChange"
        >
          <template #option="{ option }">
            <span :data-value="option.value">{{ option.label }}</span>
          </template>
        </AppSelect>
      </div>
    </div>

    <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <div>
        <label
          for="analytics-filter-folder"
          class="text-xs font-medium text-muted-foreground"
        >{{ $t("views.AnalyticsView.filter_folder") }}</label>
        <AppSelect
          id="analytics-filter-folder"
          :model-value="filters.folderId ?? ''"
          :options="folderOptions"
          option-label="label"
          option-value="value"
          :label="$t('views.AnalyticsView.filter_folder')"
          class="mt-1 w-full"
          data-testid="analytics-filter-folder"
          @update:model-value="onFolderChange"
        >
          <template #option="{ option }">
            <span :data-value="option.value">{{ option.label }}</span>
          </template>
        </AppSelect>
      </div>

      <div>
        <label
          for="analytics-filter-pipeline"
          class="text-xs font-medium text-muted-foreground"
        >{{ $t("views.AnalyticsView.filter_pipeline") }}</label>
        <AppSelect
          id="analytics-filter-pipeline"
          :model-value="filters.pipelineId ?? ''"
          :options="pipelineOptions"
          option-label="label"
          option-value="value"
          :label="$t('views.AnalyticsView.filter_pipeline')"
          class="mt-1 w-full"
          data-testid="analytics-filter-pipeline"
          @update:model-value="onPipelineChange"
        >
          <template #option="{ option }">
            <span :data-value="option.value">{{ option.label }}</span>
          </template>
        </AppSelect>
      </div>

      <div>
        <label
          for="analytics-filter-trigger-type"
          class="text-xs font-medium text-muted-foreground"
        >{{ $t("views.AnalyticsView.filter_trigger_type") }}</label>
        <AppSelect
          id="analytics-filter-trigger-type"
          :model-value="filters.triggerType ?? ''"
          :options="triggerTypeOptions"
          option-label="label"
          option-value="value"
          :label="$t('views.AnalyticsView.filter_trigger_type')"
          class="mt-1 w-full"
          data-testid="analytics-filter-trigger-type"
          @update:model-value="onTriggerTypeChange"
        >
          <template #option="{ option }">
            <span :data-value="option.value">{{ option.label }}</span>
          </template>
        </AppSelect>
      </div>

      <div>
        <label
          for="analytics-filter-status"
          class="text-xs font-medium text-muted-foreground"
        >{{ $t("views.AnalyticsView.filter_status") }}</label>
        <AppSelect
          id="analytics-filter-status"
          :model-value="filters.status ?? ''"
          :options="statusOptions"
          option-label="label"
          option-value="value"
          :label="$t('views.AnalyticsView.filter_status')"
          class="mt-1 w-full"
          data-testid="analytics-filter-status"
          @update:model-value="onStatusChange"
        >
          <template #option="{ option }">
            <span :data-value="option.value">{{ option.label }}</span>
          </template>
        </AppSelect>
      </div>

      <Field
        :label="$t('views.AnalyticsView.filter_error_code')"
        label-class="text-xs font-medium text-muted-foreground"
      >
        <SearchInput
          :model-value="filters.errorCode ?? ''"
          class="mt-1"
          data-testid="analytics-filter-error-code"
          :placeholder="$t('views.AnalyticsView.filter_error_code_placeholder')"
          @update:model-value="onErrorCodeInput"
        />
      </Field>
    </div>
  </BaseCard>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import AppSelect from "../shared/AppSelect.vue";
import BaseCard from "../ui/BaseCard.vue";
import Field from "../ui/Field.vue";
import SearchInput from "../ui/SearchInput.vue";
import type { components } from "../../lib/api/schema";
import {
  MEASURES,
  RUN_STATUSES,
  TIMESPANS,
  TRIGGER_TYPES,
  type AnalyticsDimension,
  type AnalyticsFilters,
  type AnalyticsMeasure,
  type OptionItem,
} from "../../stores/analytics";

const props = defineProps<{
  filters: AnalyticsFilters;
  measure: AnalyticsMeasure;
  folders: OptionItem[];
  pipelines: OptionItem[];
}>();

const emit = defineEmits<{
  "update:filters": [filters: AnalyticsFilters];
  "update:measure": [measure: AnalyticsMeasure];
}>();

const { t } = useI18n();
const timespans = TIMESPANS.map((t) => ({
  value: t.value,
  labelKey: `views.AnalyticsView.timespan_${t.value}`,
}));

// Hour-granular timespans (1h/24h) force `group_by=hour` server-side, so the
// day/week granularity control is inert. Reflect the applied granularity and
// disable the control instead of letting it silently ignore clicks.
const isHourGranular = computed(() => {
  const timespan = TIMESPANS.find((t) => t.value === props.filters.timespan);
  return timespan?.granularity === "hour";
});

const groupByOptions = [
  { value: "day" as const, labelKey: "views.AnalyticsView.group_by_day" },
  { value: "week" as const, labelKey: "views.AnalyticsView.group_by_week" },
];

// The dimension picker offers the BACKEND's `AnalyticsDimension` vocabulary
// (schema.ts). The store's `AnalyticsDimension` union is kept a SUPERSET of
// that vocabulary (see stores/analytics.ts and the structural spec test), so
// every option listed here is a value the store can hold and a
// `?dimension=…` deep link can restore - including `execution_origin`
// (FAR-1141) and `trigger_id`. Typing the option list against the generated
// API type keeps the picker honest about what `/analytics` accepts without
// widening a shared type from this component; `onDimensionChange` narrows to
// the store's union at the emit boundary, so the filter type is untouched.
type PickerDimension = components["schemas"]["AnalyticsDimension"];

const dimensions: { value: PickerDimension; labelKey: string }[] = [
  { value: "trigger_type", labelKey: "views.AnalyticsView.dimension_trigger_type" },
  { value: "trigger_id", labelKey: "views.AnalyticsView.dimension_trigger_id" },
  { value: "status", labelKey: "views.AnalyticsView.dimension_status" },
  { value: "pipeline", labelKey: "views.AnalyticsView.dimension_pipeline" },
  { value: "folder", labelKey: "views.AnalyticsView.dimension_folder" },
  { value: "team", labelKey: "views.AnalyticsView.dimension_team" },
  { value: "error_code", labelKey: "views.AnalyticsView.dimension_error_code" },
  { value: "execution_origin", labelKey: "views.AnalyticsView.dimension_execution_origin" },
];

const measures = MEASURES;
const triggerTypes = TRIGGER_TYPES;
const statuses = RUN_STATUSES;

// AppSelect option lists. The empty value is the "all / none" entry the native
// selects previously rendered as `<option value="">`, so clearing a filter
// still emits the same empty string (mapped to null at the emit boundary).
const dimensionOptions = computed(() => [
  { value: "", label: t("views.AnalyticsView.dimension_none") },
  ...dimensions.map((d) => ({ value: d.value as string, label: t(d.labelKey) })),
]);

const folderOptions = computed(() => [
  { value: "", label: t("views.AnalyticsView.all") },
  ...[...props.folders]
    .sort((a, b) =>
      a.name.localeCompare(b.name, undefined, { sensitivity: "base", numeric: true }),
    )
    .map((f) => ({ value: f.id, label: f.name })),
]);

const pipelineOptions = computed(() => [
  { value: "", label: t("views.AnalyticsView.all") },
  ...props.pipelines.map((p) => ({ value: p.id, label: p.name })),
]);

const triggerTypeOptions = computed(() => [
  { value: "", label: t("views.AnalyticsView.all") },
  ...triggerTypes.map((value) => ({ value, label: value })),
]);

const statusOptions = computed(() => [
  { value: "", label: t("views.AnalyticsView.all") },
  ...statuses.map((value) => ({ value, label: value })),
]);

function emitFilters(patch: Partial<AnalyticsFilters>): void {
  emit("update:filters", { ...props.filters, ...patch });
}

// AppSelect emits `unknown`; the pickers only ever carry string values (the
// empty string being the "all / none" entry), so each handler narrows once.
function selectValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function onDimensionChange(value: unknown): void {
  emitFilters({ dimension: (selectValue(value) || null) as AnalyticsDimension | null });
}

function onFolderChange(value: unknown): void {
  emitFilters({ folderId: selectValue(value) || null });
}

function onPipelineChange(value: unknown): void {
  emitFilters({ pipelineId: selectValue(value) || null });
}

function onTriggerTypeChange(value: unknown): void {
  emitFilters({ triggerType: selectValue(value) || null });
}

function onStatusChange(value: unknown): void {
  emitFilters({ status: selectValue(value) || null });
}

function onErrorCodeInput(value: string): void {
  const trimmed = value.trim();
  emitFilters({ errorCode: trimmed || null });
}
</script>
