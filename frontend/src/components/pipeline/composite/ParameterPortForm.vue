<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import AppSelect from "../../shared/AppSelect.vue";
import Checkbox from "../../ui/Checkbox.vue";
import Field from "../../ui/Field.vue";
import Input from "../../ui/Input.vue";
import Textarea from "../../ui/Textarea.vue";
import type { ParameterPort } from "../../../types/pipeline";

const { t } = useI18n();

const props = defineProps<{
  port: ParameterPort;
  modelValue: unknown;
}>();

const emit = defineEmits<{
  (e: "update:modelValue", value: unknown): void;
}>();

function resolveDefaultFallback(type: string): unknown {
  if (type === 'boolean') return false
  if (type === 'number') return 0
  return ''
}

const localValue = computed({
  get: () =>
    props.modelValue ??
    props.port.default_value ??
    resolveDefaultFallback(props.port.type),
  set: (val: unknown) => emit("update:modelValue", val),
});

function onStringChange(event: Event) {
  const target = event.target as HTMLInputElement;
  localValue.value = target.value;
}

function onNumberChange(event: Event) {
  const target = event.target as HTMLInputElement;
  localValue.value = target.valueAsNumber;
}

function onSelectChange(value: unknown) {
  localValue.value = value as string;
}

// AppSelect option list for a `select`-typed port (was a native <select>). The
// empty entry is the "not chosen" state the native placeholder option provided.
const selectOptions = computed(() => [
  { value: "", label: t("components.pipeline.composite.ParameterPortForm.select") },
  ...(props.port.options ?? []).map((opt) => ({ value: opt.value, label: opt.label })),
]);

function onBooleanChange(checked: boolean) {
  localValue.value = checked;
}
</script>

<template>
  <div class="space-y-1.5">
    <template v-if="port.type === 'string' && port.multiline">
      <Field
        id="parameterportform-field-2"
        :label="port.label"
        :required="port.required"
        :help="port.description ?? undefined"
        class="space-y-1.5"
        label-class="flex items-center gap-1 text-sm font-medium"
        help-class="text-xs text-muted-foreground"
      >
        <Textarea
          :value="localValue as string"
          class="min-h-[80px] w-full rounded-lg border border-input bg-background px-3 py-2 text-sm placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
          :placeholder="$t('components.pipeline.composite.ParameterPortForm.portdefault_as_string')"
          @change="onStringChange"
        />
      </Field>
    </template>

    <Field
      v-else-if="port.type === 'string'"
      id="parameterportform-field-3"
      :label="port.label"
      :required="port.required"
      :help="port.description ?? undefined"
      class="space-y-1.5"
      label-class="flex items-center gap-1 text-sm font-medium"
      help-class="text-xs text-muted-foreground"
    >
      <Input
        :value="localValue as string"
        :aria-label="port.label"
        class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
        :placeholder="(port.default_value as string) ?? ''"
        @change="onStringChange"
      />
    </Field>

    <Field
      v-else-if="port.type === 'number'"
      :label="port.label"
      :required="port.required"
      :help="port.description ?? undefined"
      class="space-y-1.5"
      label-class="flex items-center gap-1 text-sm font-medium"
      help-class="text-xs text-muted-foreground"
    >
      <Input
        :aria-label="port.label"
        :value="localValue as number"
        type="number"
        class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
        :placeholder="port.default_value != null ? String(port.default_value) : ''"
        @change="onNumberChange"
      />
    </Field>

    <template v-else>
      <label for="parameterportform-field-1" class="flex items-center gap-1 text-sm font-medium">
        {{ port.label }}
        <span v-if="port.required" class="text-destructive">*</span>
      </label>
      <p v-if="port.description" class="text-xs text-muted-foreground">
        {{ port.description }}
      </p>

      <Checkbox
        v-if="port.type === 'boolean'"
        id="parameterportform-field-1"
        :model-value="!!localValue"
        :label="localValue ? 'Enabled' : 'Disabled'"
        class="inline-flex cursor-pointer items-center gap-2"
        label-class="text-sm"
        control-class="rounded border-gray-300 text-node-port focus:ring-node-port"
        @update:model-value="onBooleanChange"
      />

      <AppSelect
        v-if="port.type === 'select'"
        :model-value="localValue as string"
        class="w-full"
        :options="selectOptions"
        option-label="label"
        option-value="value"
        :label="port.label || $t('components.pipeline.composite.ParameterPortForm.select')"
        @update:model-value="onSelectChange"
      >
        <template #option="{ option }">
          <span :data-value="option.value">{{ option.label }}</span>
        </template>
      </AppSelect>

      <div
        v-if="
          port.type === 'model_backend_ref' || port.type === 'schema_ref'
        "
        class="rounded-lg border bg-muted px-3 py-2 text-sm text-muted-foreground"
      >
        {{
          port.type === "model_backend_ref"
            ? "Model backend picker"
            : "Schema picker"
        }}
        <span class="block text-xs">(integration pending)</span>
      </div>
    </template>
  </div>
</template>
