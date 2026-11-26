<template>
  <div class="fixed inset-0 z-50" data-testid="system-map-discovery-config">
    <button type="button" class="absolute inset-0 bg-black/50" :aria-label="$t('common.close')" :disabled="submitting" @click="$emit('close')" data-testid="system-map-discovery-config-backdrop" />
    <dialog open class="absolute right-0 top-0 h-full w-full max-w-xl m-0 bg-card shadow-2xl overflow-hidden flex flex-col" aria-labelledby="discovery-config-title">
      <header class="border-b px-4 py-3 flex items-center justify-between">
        <h2 id="discovery-config-title" class="font-semibold">{{ $t('views.SystemMapDetailView.discovery_title') }}</h2>
        <button type="button" class="rounded p-1" :aria-label="$t('common.close')" :disabled="submitting" @click="$emit('close')" data-testid="system-map-discovery-config-close">×</button>
      </header>
      <form class="flex-1 overflow-y-auto p-4 space-y-5" @submit.prevent="submit">
        <fieldset :disabled="submitting">
          <legend class="text-sm font-medium">{{ $t('views.SystemMapDetailView.discovery_mode_label') }}</legend>
          <RadioGroup
            v-model="mode"
            name="system-map-discovery-mode"
            :aria-label="$t('views.SystemMapDetailView.discovery_mode_label')"
            :options="discoveryModeOptions"
            class="mt-2 grid gap-2"
            option-class="rounded border p-3 cursor-pointer"
            selected-class="border-primary bg-primary/5"
            control-class="mr-2"
            label-class="font-medium"
            description-class="block text-xs text-muted-foreground ml-6"
            badge-class="ml-2 rounded bg-muted px-1.5 py-0.5 text-xs"
          />
          <!-- The AI path is decided from what the caller can actually read
               (their organisation's model backends and model-judge evals).
               There is no system-admin-only access probe, so an ordinary org
               admin sees the reason and a route to fix it instead of a
               silently unavailable option. -->
          <p v-if="canUseAi && optionsLoaded && !modelBackendsConfigured" class="mt-2 rounded border bg-muted/40 p-3 text-sm text-muted-foreground" aria-live="polite" data-testid="system-map-discovery-no-model-backend">
            {{ $t('views.SystemMapDetailView.ai_needs_model_backend') }}
            <a href="/admin/model-backends" class="font-medium text-primary-text underline" data-testid="system-map-discovery-configure-backend-link">{{ $t('views.SystemMapDetailView.configure_model_backend_link') }}</a>
          </p>
          <p v-else-if="canUseAi && optionsLoaded && !modelJudgeConfigured" class="mt-2 rounded border bg-muted/40 p-3 text-sm text-muted-foreground" aria-live="polite" data-testid="system-map-discovery-no-eval-judge">
            {{ $t('views.SystemMapDetailView.ai_needs_eval_judge') }}
            <a href="/evals/editor" class="font-medium text-primary-text underline" data-testid="system-map-discovery-configure-eval-link">{{ $t('views.SystemMapDetailView.configure_eval_judge_link') }}</a>
          </p>
        </fieldset>

        <div v-if="mode === 'ai_assisted'" class="space-y-4" data-testid="system-map-ai-controls">
          <p class="text-sm text-muted-foreground">{{ $t('views.SystemMapDetailView.ai_gate_explanation') }}</p>
          <div>
            <label for="discovery-backend" class="block text-sm font-medium">{{ $t('views.SystemMapDetailView.model_backend_label') }}</label>
            <AppSelect id="discovery-backend" v-model="backendId" class="mt-1 w-full" :options="backendOptions" option-label="label" option-value="value" :label="$t('views.SystemMapDetailView.model_backend_label')" :disabled="loading || failed || submitting || !modelBackendsConfigured" data-testid="system-map-discovery-backend">
              <template #option="{ option }">
                <span :data-value="option.value">{{ option.label }}</span>
              </template>
            </AppSelect>
          </div>
          <div>
            <label for="discovery-eval" class="block text-sm font-medium">{{ $t('views.SystemMapDetailView.eval_judge_label') }}</label>
            <AppSelect id="discovery-eval" v-model="evalId" class="mt-1 w-full" :options="evalOptions" option-label="label" option-value="value" :label="$t('views.SystemMapDetailView.eval_judge_label')" :disabled="loading || failed || submitting || !modelJudgeConfigured" data-testid="system-map-discovery-eval">
              <template #option="{ option }">
                <span :data-value="option.value">{{ option.label }}</span>
              </template>
            </AppSelect>
          </div>
          <p v-if="loading" class="text-sm text-muted-foreground" aria-live="polite">{{ $t('views.SystemMapDetailView.loading_ai_options') }}</p>
          <ErrorAlert v-if="failed" compact :message="failedMessage" :retryable="false" />
        </div>
        <p v-if="submitting" class="rounded border bg-muted/40 p-3 text-sm" aria-live="polite" data-testid="system-map-discovery-request-loading">{{ $t('views.SystemMapDetailView.discovery_request_loading') }}</p>
        <ErrorAlert v-if="submitError" compact :message="submitError" :retryable="false" data-testid="system-map-discovery-request-error" />
      </form>
      <footer class="border-t px-4 py-3 flex justify-end gap-2">
        <button type="button" class="rounded border px-3 py-1.5 text-sm" :disabled="submitting" @click="$emit('close')" data-testid="system-map-discovery-config-cancel">{{ $t('common.cancel') }}</button>
        <button type="button" class="rounded bg-primary px-3 py-1.5 text-sm text-primary-foreground disabled:opacity-50" :disabled="!canSubmit || submitting" @click="submit" data-testid="system-map-discovery-submit">{{ submitting ? $t('views.SystemMapDetailView.discovery_request_loading') : $t('views.SystemMapDetailView.start_discovery') }}</button>
      </footer>
    </dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../../lib/api/client'
import type { components } from '../../lib/api/schema'
import type { DiscoveryMode } from '../../stores/systemMaps'
import AppSelect from '../shared/AppSelect.vue'
import ErrorAlert from '../shared/ErrorAlert.vue'
import RadioGroup, { type RadioOption } from '../ui/RadioGroup.vue'

type Backend = components['schemas']['ModelBackendResponse']
type Evaluation = components['schemas']['EvalDefinitionResponse']

const { t: $t } = useI18n()

const props = withDefaults(defineProps<{ canUseAi?: boolean; submitting?: boolean; submitError?: string | null }>(), {
  canUseAi: false,
  submitting: false,
  submitError: null,
})
const emit = defineEmits<{ (event: 'close'): void; (event: 'submit', options?: { modelBackendId?: string; evalDefinitionId?: string; modelBackendName?: string; evalName?: string; evalVersion?: number }): void }>()
const mode = ref<DiscoveryMode>('deterministic')
const backendId = ref('')
const evalId = ref('')
const backends = ref<Backend[]>([])
const evals = ref<Evaluation[]>([])
const loading = ref(false)
const failed = ref(false)
const failedMessage = ref('')
// True once a successful options read has completed. A read that returns no
// usable backend/eval is still a SUCCESSFUL read: the caller is authorised,
// their organisation simply has not configured the AI path yet. That is a
// distinct state from a failed request and from an access refusal.
const optionsLoaded = ref(false)
const modelBackendsConfigured = computed(() => backends.value.length > 0)
const modelJudgeConfigured = computed(() => evals.value.length > 0)
// ``canUseAi`` is the org-aware authoring gate passed in by the detail view
// (``store.hasAccess``: the org's effective ``system_maps`` flag + operator or
// admin role + ``system_maps_access``). AI discovery needs no higher role than
// authoring itself, so this is the only role/flag input.
const aiDiscoveryAvailable = computed(
  () => props.canUseAi && optionsLoaded.value && !loading.value && !failed.value && modelBackendsConfigured.value && modelJudgeConfigured.value,
)

// The AI option only exists when the caller may author at all, and stays
// disabled until a completed options read proves the AI path is usable.
const discoveryModeOptions = computed<RadioOption<DiscoveryMode>[]>(() => {
  const options: RadioOption<DiscoveryMode>[] = [
    {
      value: 'deterministic',
      label: $t('views.SystemMapDetailView.mode_deterministic'),
      description: $t('views.SystemMapDetailView.deterministic_hint'),
      testid: 'system-map-discovery-mode-deterministic',
    },
  ]
  if (props.canUseAi) {
    options.push({
      value: 'ai_assisted',
      label: $t('views.SystemMapDetailView.mode_ai_assisted'),
      badge: $t('views.SystemMapDetailView.beta_private_preview'),
      description: $t('views.SystemMapDetailView.ai_assisted_hint'),
      disabled: optionsLoaded.value && !aiDiscoveryAvailable.value,
      testid: 'system-map-discovery-mode-ai',
    })
  }
  return options
})

// AppSelect option lists for the AI-assisted controls (both were native
// <select> elements). The leading empty entry is the "not selected" placeholder.
const backendOptions = computed(() => [
  { value: '', label: $t('views.SystemMapDetailView.select_model_backend') },
  ...backends.value.map((backend) => ({
    value: backend.id,
    label: `${backend.display_name} (${backend.provider})`,
  })),
])
const evalOptions = computed(() => [
  { value: '', label: $t('views.SystemMapDetailView.select_eval_judge') },
  ...evals.value.map((evaluation) => ({
    value: evaluation.id,
    label: `${evaluation.name} (v${evaluation.version})`,
  })),
])

const canSubmit = computed(
  () => mode.value === 'deterministic' || (aiDiscoveryAvailable.value && Boolean(backendId.value) && Boolean(evalId.value)),
)

let loadToken = 0

async function loadAiOptions(): Promise<void> {
  const token = ++loadToken
  loading.value = true
  failed.value = false
  failedMessage.value = ''
  optionsLoaded.value = false
  try {
    // Both reads are viewer-scoped (``model_backend.list`` / ``eval.list``),
    // so an ordinary org admin can make them. AI discovery genuinely needs an
    // active org-visible model backend; the model-judge eval is what the panel
    // gates the candidate against. The old system-admin-only
    // ``/api/v1/admin/feature-flags`` probe was removed (FAR-2612): it 403'd
    // every org admin and hid the AI option with no explanation.
    const [backendResponse, evalResponse] = await Promise.all([
      api.GET('/api/v1/model-backends', { params: { query: { page: 1, page_size: 100 } } }),
      api.GET('/api/v1/evals', { params: { query: { page: 1, page_size: 100, eval_type: 'model_judge' } } }),
    ])
    if (token !== loadToken) return
    const responses = [backendResponse, evalResponse]
    const denied = responses.some((response) => response.response?.status === 403)
    if (denied) {
      failedMessage.value = $t('views.SystemMapDetailView.ai_options_unavailable_private')
      throw new Error('AI discovery options are not available to this caller')
    }
    const unavailable = responses.some((response) => response.response?.status !== undefined && response.response.status >= 400 && response.response.status < 500)
    if (unavailable) {
      failedMessage.value = $t('views.SystemMapDetailView.ai_options_unavailable_private')
      throw new Error('AI discovery options are unavailable to this caller')
    }
    if (backendResponse.error || evalResponse.error || !backendResponse.data || !evalResponse.data) throw new Error('AI discovery options unavailable')
    backends.value = backendResponse.data.items.filter((backend) => backend.visibility === 'org' && backend.tier !== 'in_dev')
    evals.value = evalResponse.data.items.filter((evaluation) => evaluation.eval_type === 'model_judge')
    optionsLoaded.value = true
  } catch {
    if (token !== loadToken) return
    failed.value = true
    if (!failedMessage.value) failedMessage.value = $t('views.SystemMapDetailView.ai_options_unavailable')
    backends.value = []
    evals.value = []
  } finally {
    if (token === loadToken) loading.value = false
  }
}

// Load as soon as the caller is allowed to author at all, so the panel can
// state availability (and the fix) up front rather than after a failed try.
watch(() => props.canUseAi, (allowed) => { if (allowed) void loadAiOptions() }, { immediate: true })
// Selecting AI after a failed/skipped read gives the caller a retry path.
watch(mode, (value) => { if (value === 'ai_assisted' && props.canUseAi && !optionsLoaded.value && !loading.value) void loadAiOptions() })

function submit(): void {
  if (!canSubmit.value) return
  if (mode.value === 'deterministic') emit('submit')
  else {
    const backend = backends.value.find((item) => item.id === backendId.value)
    const evaluation = evals.value.find((item) => item.id === evalId.value)
    emit('submit', {
      modelBackendId: backendId.value,
      evalDefinitionId: evalId.value,
      modelBackendName: backend?.display_name,
      evalName: evaluation?.name,
      evalVersion: evaluation?.version,
    })
  }
}
</script>
