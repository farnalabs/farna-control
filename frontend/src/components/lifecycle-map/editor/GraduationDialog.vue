<template>
  <FormDialog
    :open="open"
    :title="$t('components.lifecycle-map.editor.GraduationDialog.graduate_stage')"
    :description="`${$t('components.lifecycle-map.editor.GraduationDialog.graduating_stage_prefix')} ${stageName} ${$t('components.lifecycle-map.editor.GraduationDialog.graduating_stage_suffix')}`"
    width="32rem"
    confirm-text="Graduate Stage"
    confirm-loading-text="Graduating..."
    :confirm-disabled="!canGraduate"
    :loading="graduating"
    @update:open="$emit('close')"
    @confirm="handleGraduate"
  >
    <div class="space-y-4 py-2">
      <!-- Graduation action error: emitted from the confirm handler; no
           refetch loader — the Graduate button is the retry path. -->
      <ErrorAlert v-if="error" :message="error" :retryable="false" />

      <RadioGroup
        v-model="mode"
        name="graduation-target"
        :aria-label="$t('components.lifecycle-map.editor.GraduationDialog.graduate_stage')"
        :options="graduationOptions"
        class="space-y-4"
        option-class="mb-2 flex items-center gap-2 text-sm font-medium"
        control-class="h-4 w-4 accent-primary"
      >
        <template #option-after="{ option }">
          <Select
            v-if="option.value === 'existing' && mode === 'existing'"
            aria-label="Select pipeline"
            v-model="selectedPipelineId"
            placeholder="Select a pipeline..."
            class="ml-6 w-full"
            :options="pipelines.map(p => ({ value: p.id, label: p.name }))"
            option-label="label"
            option-value="value"
          >
            <template #option="{ option: pipelineOption }">
              <span :data-value="pipelineOption.value">{{ pipelineOption.label }}</span>
            </template>
          </Select>
          <Select
            v-else-if="option.value === 'new' && mode === 'new'"
            aria-label="Select template"
            v-model="selectedTemplateId"
            placeholder="Select a template..."
            class="ml-6 w-full"
            :options="[{ value: 'simple-sequential', label: $t('components.lifecycle-map.editor.GraduationDialog.simple_sequential') }, { value: 'hierarchical', label: $t('components.lifecycle-map.editor.GraduationDialog.hierarchical_agent') }, { value: 'parallel', label: $t('components.lifecycle-map.editor.GraduationDialog.parallel_processing') }]"
            option-label="label"
            option-value="value"
          >
            <template #option="{ option: templateOption }">
              <span :data-value="templateOption.value">{{ templateOption.label }}</span>
            </template>
          </Select>
        </template>
      </RadioGroup>
    </div>
  </FormDialog>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import Select from '../../shared/AppSelect.vue'
import FormDialog from '../../shared/FormDialog.vue'
import ErrorAlert from '../../shared/ErrorAlert.vue'
import RadioGroup, { type RadioOption } from '../../ui/RadioGroup.vue'
import type { PipelineSummary } from '../../../types/lifecycleMap'

// FAR-1908: a dialog's content box scrolls (`overflow-y: auto`), which clips an
// in-place AppSelect overlay and hides its lower options behind the footer.
// AppSelect detects that container from the rendered DOM and portals its
// overlays to <body> automatically, so no host declaration is needed here.

const props = defineProps<{
  open: boolean
  stageName: string
  stageId: string
  mapId: string
  versionId: string
  pipelines: PipelineSummary[]
}>()

const emit = defineEmits<{
  close: []
  confirm: [stageId: string, pipelineId: string]
}>()

const mode = ref<'existing' | 'new'>('existing')
const selectedPipelineId = ref<string | null>(null)
const selectedTemplateId = ref<string | null>(null)
const graduating = ref(false)
const error = ref<string | null>(null)

// The ids are kept from the hand-rolled markup so existing selectors still find
// the same control (the FAR-835 spec drives `#graduationdialog-field-1`).
const graduationOptions: RadioOption<'existing' | 'new'>[] = [
  { value: 'existing', label: 'Link to existing pipeline', id: 'graduationdialog-field-2' },
  { value: 'new', label: 'Create new pipeline from template', id: 'graduationdialog-field-1' },
]

const canGraduate = computed(() => {
  if (mode.value === 'existing') return !!selectedPipelineId.value
  return !!selectedTemplateId.value
})

function handleGraduate() {
  if (!canGraduate.value) return
  graduating.value = true
  error.value = null
  try {
    if (mode.value === 'existing' && selectedPipelineId.value) {
      emit('confirm', props.stageId, selectedPipelineId.value)
    } else if (mode.value === 'new') {
      emit('confirm', props.stageId, 'new-from-template')
    }
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : 'Failed to graduate stage'
  } finally {
    graduating.value = false
  }
}
</script>
