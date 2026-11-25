<template>
  <div class="assistant-cs flex flex-col flex-1 overflow-hidden">
    <div class="p-3 border-b">
      <h3 class="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        {{ $t('components.assistant.AssistantContextSources.knowledge_sources') }}
      </h3>
      <p class="text-xs text-muted-foreground mt-1">
        {{ $t('components.assistant.AssistantContextSources.description') }}
      </p>
    </div>

    <div v-if="error" class="px-3 py-2 text-sm text-destructive">
      {{ error }}
    </div>

    <div class="flex-1 overflow-auto">
      <div v-if="loading" class="flex items-center justify-center py-12">
        <p class="text-sm text-muted-foreground">{{ $t('common.loading') }}</p>
      </div>
      <template v-else>
        <div class="assistant-cs-header grid grid-cols-[1.5fr_auto_auto] gap-3 px-3 py-2 text-xs font-medium text-muted-foreground border-b items-center">
          <span>{{ $t('components.assistant.AssistantContextSources.source') }}</span>
          <span>{{ $t('components.assistant.AssistantContextSources.mode') }}</span>
          <span></span>
        </div>
        <div
          v-for="source in sources"
          :key="source.key"
          class="assistant-cs-row grid grid-cols-[1.5fr_auto_auto] gap-3 px-3 py-2 text-sm items-center border-b"
        >
          <div class="min-w-0">
            <p class="text-sm font-medium truncate" :title="source.name">
              {{ source.name }}
              <span
                class="inline-flex items-center ml-1 align-middle cursor-help"
                :title="`Key: ${source.key}`"
              >
                <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="text-muted-foreground/50" aria-hidden="true" focusable="false">
                  <circle cx="12" cy="12" r="10"/>
                  <path d="M12 16v-4"/>
                  <path d="M12 8h.01"/>
                </svg>
              </span>
            </p>
            <p class="text-xs text-muted-foreground truncate" :title="source.description || ''">
              {{ source.description || '—' }}
            </p>
          </div>
          <AppSelect
            class="assistant-cs-select min-w-[100px]"
            size="small"
            aria-label="Knowledge source mode"
            :model-value="source.source_mode"
            :options="modeOptions"
            option-label="label"
            option-value="value"
            label="Knowledge source mode"
            :disabled="savingKey === source.key"
            @update:model-value="onModeChange(source.key, $event)"
          >
            <template #option="{ option }">
              <span :data-value="option.value">{{ option.label }}</span>
            </template>
          </AppSelect>
          <span
            v-if="!source.is_overridden"
            class="text-xs text-muted-foreground italic"
          >
            {{ $t('components.assistant.AssistantContextSources.org_default') }}
          </span>
          <span
            v-else
            class="text-xs text-primary-text italic"
          >
            {{ $t('components.assistant.AssistantContextSources.overridden') }}
          </span>
        </div>
      </template>
    </div>

    <div class="p-3 border-t flex items-center justify-between gap-2 flex-wrap">
      <Button variant="secondary" size="sm" class="text-xs" :disabled="resetting" @click="resetToDefaults">
        {{ $t('components.assistant.AssistantContextSources.reset_to_defaults') }}
      </Button>
      <div class="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
        <span>{{ $t('components.assistant.AssistantContextSources.legend_always_on') }}</span>
        <span>{{ $t('components.assistant.AssistantContextSources.legend_tool') }}</span>
        <span>{{ $t('components.assistant.AssistantContextSources.legend_off') }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted } from "vue";
import { useI18n } from "vue-i18n";
import { api } from "@/lib/api/client";
import { formatApiError } from "@/lib/api/formatError";
import Button from '../ui/Button.vue'
import AppSelect from "../shared/AppSelect.vue";
import type { ContextSourceItem, ContextSourceMode, ContextSourceUpdate } from "@/types/assistant";

const { t } = useI18n();

// AppSelect option list for the per-source mode control (was a native
// <select> with three hand-written <option>s).
const modeOptions = computed(() => [
  { value: "always_on", label: t("components.assistant.AssistantContextSources.always_on") },
  { value: "tool", label: t("components.assistant.AssistantContextSources.tool") },
  { value: "off", label: t("components.assistant.AssistantContextSources.off") },
]);

const sources = ref<ContextSourceItem[]>([]);
const loading = ref(false);
const error = ref<string | null>(null);
const savingKey = ref<string | null>(null);
const resetting = ref(false);

async function fetchSources() {
  loading.value = true;
  error.value = null;
  try {
    const { data, error: err } = await api.GET("/api/v1/me/assistant/context-sources");
    if (err) {
      error.value = `Failed to load sources: ${formatApiError(err)}`;
    } else if (data) {
      sources.value = (data as ContextSourceItem[]) ?? [];
    }
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : "Failed to load sources";
  } finally {
    loading.value = false;
  }
}

function onModeChange(key: string, value: unknown) {
  void updateSource(key, value as ContextSourceMode);
}

async function updateSource(key: string, mode: ContextSourceMode) {
  savingKey.value = key;
  error.value = null;
  const prev = sources.value.find((s) => s.key === key);
  const prevMode = prev?.source_mode;
  if (prev) {
    prev.source_mode = mode;
  }
  try {
    const { data, error: err } = await api.PUT(
      "/api/v1/me/assistant/context-sources/{source_key}",
      {
        params: { path: { source_key: key } },
        body: { source_mode: mode } as ContextSourceUpdate,
      },
    );
    if (err) {
      error.value = `Failed to update source: ${formatApiError(err)}`;
      if (prev && prevMode) {
        prev.source_mode = prevMode;
      }
      return;
    }
    if (data && prev) {
      sources.value = data as ContextSourceItem[];
    }
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : "Failed to update source";
    if (prev && prevMode) {
      prev.source_mode = prevMode;
    }
  } finally {
    savingKey.value = null;
  }
}

async function resetToDefaults() {
  resetting.value = true;
  error.value = null;
  try {
    const { error: err } = await api.DELETE("/api/v1/me/assistant/context-sources");
    if (err) {
      error.value = `Failed to reset sources: ${formatApiError(err)}`;
      return;
    }
    await fetchSources();
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : "Failed to reset sources";
  } finally {
    resetting.value = false;
  }
}

onMounted(() => {
  void fetchSources();
});
</script>

<style scoped>
@reference "../../style.css";
.assistant-cs-select {
  @apply text-xs;
}
.assistant-cs-row {
  transition: background-color 150ms ease;
}
.assistant-cs-row:hover {
  background-color: hsl(var(--accent));
}
</style>
