<template>
  <FormDialog
    v-model:open="dialogOpen"
    :title="editingId ? 'Edit Skill' : 'Add Skill'"
    :description="editingId ? editDescription : createDescription"
    :confirm-text="editingId ? 'Update' : 'Create'"
    :confirm-loading-text="$t('common.saving')"
    :loading="saving"
    :confirm-disabled="!form.name.trim()"
    width="32rem"
    cancel-test-id="assistant-skills-form-cancel"
    confirm-test-id="assistant-skills-form-submit"
    @confirm="save"
    @update:open="onFormOpenChange"
  >
    <form @submit.prevent="save" class="space-y-4">
        <Field
          id="assistantskilldialog-field-5"
          :label="$t('components.assistant.AssistantSkillDialog.name')"
          required
        >
          <Input
            v-model="form.name"
            type="text"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            :placeholder="$t('components.assistant.AssistantSkillDialog.skill_name')"
            data-testid="assistant-skills-form-name"
          />
        </Field>
        <Field
          id="assistantskilldialog-field-4"
          :label="$t('components.assistant.AssistantSkillDialog.description')"
        >
          <Textarea
            v-model="form.description"
            rows="2"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            :placeholder="$t('components.assistant.AssistantSkillDialog.what_this_skill_does')"
            data-testid="assistant-skills-form-description"
          />
        </Field>
        <Field
          id="assistantskilldialog-field-3"
          :label="$t('components.assistant.AssistantSkillDialog.triggers')"
          help="Comma-separated trigger keywords"
        >
          <Input
            v-model="form.triggersInput"
            type="text"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            :placeholder="$t('components.assistant.AssistantSkillDialog.trigger1_trigger2')"
            data-testid="assistant-skills-form-triggers"
          />
        </Field>
        <Field
          id="assistantskilldialog-field-2"
          :label="$t('components.assistant.AssistantSkillDialog.body_markdown')"
        >
          <Textarea
            v-model="form.body"
            rows="6"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
            :placeholder="$t('components.assistant.AssistantSkillDialog.skill_instructions_heading') + '\n' + $t('components.assistant.AssistantSkillDialog.skill_instructions_body')"
            data-testid="assistant-skills-form-body"
          />
        </Field>
        <div class="flex items-center gap-2">
          <Checkbox
            id="assistantskilldialog-field-1"
            v-model="form.active"
            label="Active"
            class="flex items-center gap-2 text-sm cursor-pointer"
            control-class="rounded border-input"
            data-testid="assistant-skills-form-active"
          />
        </div>
        <div v-if="saveError" class="text-sm text-destructive">
          {{ saveError }}
        </div>
        <!-- FormDialog renders the visible confirm button in its footer, outside
             this form; a hidden submit keeps Enter-to-submit working. -->
        <button type="submit" hidden>{{ $t('common.save') }}</button>
      </form>
  </FormDialog>

  <FormDialog
    v-model:open="deleteOpen"
    :title="$t('components.assistant.AssistantSkillDialog.delete_skill')"
    :confirm-text="$t('common.delete')"
    :confirm-loading-text="$t('common.deleting')"
    :loading="deleting"
    confirm-severity="danger"
    width="24rem"
    cancel-test-id="assistant-skills-delete-cancel"
    confirm-test-id="assistant-skills-delete-confirm"
    @confirm="confirmDelete"
  >
    <p class="text-sm text-muted-foreground">
      Are you sure you want to delete "{{ deletingName }}"? This action
      cannot be undone.
    </p>
  </FormDialog>
</template>

<script setup lang="ts">
import { ref, reactive } from "vue";
import FormDialog from "@/components/shared/FormDialog.vue";
import Checkbox from "../ui/Checkbox.vue";
import Field from "@/components/ui/Field.vue";
import Input from "@/components/ui/Input.vue";
import Textarea from "@/components/ui/Textarea.vue";
import { api } from "@/lib/api/client";
import { formatApiError } from "@/lib/api/formatError";

export interface SkillFormItem {
  id: string;
  name: string;
  description?: string | null;
  triggers?: string[] | null;
  body?: string;
  active: boolean;
}

const props = withDefaults(
  defineProps<{
    createDescription?: string;
    editDescription?: string;
    createEndpoint?: '/api/v1/admin/assistant/skills' | '/api/v1/me/assistant/skills';
    updateEndpoint?: '/api/v1/admin/assistant/skills/{skill_id}' | '/api/v1/me/assistant/skills/{skill_id}';
    deleteEndpoint?: '/api/v1/admin/assistant/skills/{skill_id}' | '/api/v1/me/assistant/skills/{skill_id}';
  }>(),
  {
    createDescription: "Create a new skill.",
    editDescription: "Update the skill configuration.",
    createEndpoint: "/api/v1/admin/assistant/skills",
    updateEndpoint: "/api/v1/admin/assistant/skills/{skill_id}",
    deleteEndpoint: "/api/v1/admin/assistant/skills/{skill_id}",
  },
);

const emit = defineEmits<{
  saved: [];
}>();

const dialogOpen = ref(false);
const deleteOpen = ref(false);
const editingId = ref<string | null>(null);
const deletingId = ref<string | null>(null);
const deletingName = ref("");
const saving = ref(false);
const deleting = ref(false);
const saveError = ref<string | null>(null);


const form = reactive({
  name: "",
  description: "",
  triggersInput: "",
  body: "",
  active: true,
});

function openCreate() {
  editingId.value = null;
  form.name = "";
  form.description = "";
  form.triggersInput = "";
  form.body = "";
  form.active = true;
  saveError.value = null;
  dialogOpen.value = true;
}

function openEdit(skill: SkillFormItem) {
  editingId.value = skill.id;
  form.name = skill.name;
  form.description = skill.description || "";
  form.triggersInput = (skill.triggers || []).join(", ");
  form.body = skill.body || "";
  form.active = skill.active;
  saveError.value = null;
  dialogOpen.value = true;
}

function closeForm() {
  dialogOpen.value = false;
  editingId.value = null;
  saveError.value = null;
}

// The old dialog's @update:visible handler reset the edit state on ANY close
// (X, mask, Escape, cancel), not only the programmatic close. Preserve that.
function onFormOpenChange(open: boolean | undefined) {
  if (!open) {
    editingId.value = null;
    saveError.value = null;
  }
}

function openDelete(skill: SkillFormItem) {
  deletingId.value = skill.id;
  deletingName.value = skill.name;
  deleteOpen.value = true;
}

async function save() {
  if (!form.name.trim()) return;
  saving.value = true;
  saveError.value = null;
  try {
    const triggers = form.triggersInput
      .split(/[\s,]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    const payload = {
      name: form.name.trim(),
      description: form.description.trim() || null,
      triggers,
      body: form.body,
      active: form.active,
    };

    if (editingId.value) {
      const { error: err } = await api.PUT(props.updateEndpoint, {
        params: { path: { skill_id: editingId.value } },
        body: payload,
      });
      if (err) {
        saveError.value = `Failed to update skill: ${formatApiError(err)}`;
        return;
      }
    } else {
      const { error: err } = await api.POST(props.createEndpoint, {
        body: payload,
      });
      if (err) {
        saveError.value = `Failed to create skill: ${formatApiError(err)}`;
        return;
      }
    }
    closeForm();
    emit("saved");
  } catch (e: unknown) {
    saveError.value = `Failed to save skill: ${formatApiError(e)}`;
  } finally {
    saving.value = false;
  }
}

async function confirmDelete() {
  if (!deletingId.value) return;
  deleting.value = true;
  try {
    const { error: err } = await api.DELETE(props.deleteEndpoint, {
      params: { path: { skill_id: deletingId.value } },
    });
    if (err) {
      saveError.value = `Failed to delete skill: ${formatApiError(err)}`;
      return;
    }
    deleteOpen.value = false;
    deletingId.value = null;
    emit("saved");
  } catch (e: unknown) {
    saveError.value = `Failed to delete skill: ${formatApiError(e)}`;
  } finally {
    deleting.value = false;
  }
}

defineExpose({ openCreate, openEdit, openDelete });
</script>
