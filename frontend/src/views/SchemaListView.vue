<template>
  <div class="h-[calc(100vh-3.5rem)] h-chrome-dvh bg-background flex flex-col">
    <div class="page-wide">
      <PageHeader
        :title="$t('views.SchemaListView.schemas')"
        :subtitle="$t('views.SchemaListView.manage_schemas_and_deprecate_outdated_definitions')"
        :tabs="[
          { label: $t('views.SchemaInferenceView.browse'), to: '/schemas' },
          { label: $t('views.SchemaInferenceView.editor'), to: '/schemas/editor' },
          { label: $t('views.SchemaInferenceView.infer'), to: '/schemas/infer' },
        ]"
      />
    </div>

    <div class="flex flex-1 min-h-0">
      <!-- Folder sidebar -->
      <FolderTree
        :selected-folder-id="selectedFolderId"
        :item-counts="folderSchemaCounts"
        api-base="/api/v1/schema-folders"
        i18n-ns="views.SchemaListView"
        all-items-key="all_schemas"
        :item-noun="$t('views.SchemaListView.schemas')"
        @select-folder="onSelectFolder"
        @folders-changed="onFoldersChanged"
        @move-pipeline="onMoveSchema"
      />

      <section class="flex-1 page-wide min-w-0 overflow-y-auto">
        <!-- Folder-list load failure. The <md owner of the folder-error live
             region (FolderTree's sidebar owns >=md), hence `gated`. The shared
             contract — single live region, non-dismissible, retry re-fetches —
             lives in FolderListError.vue. -->
        <FolderListError
          v-if="folderError"
          :message="`${$t('views.SchemaListView.failed_to_load_folders')} ${folderError}`"
          :on-retry="loadFolders"
          gated
          :data-testid="'schema-list-folder-error'"
        />

        <!-- Retry is deliberately omitted (retryable=false): a failed move can only
             be redone by re-dragging (or re-running the move dialog), so the shared
             alert's retryable prop is used for exactly this opt-out. -->
        <ErrorAlert
          v-if="moveError"
          :message="moveError"
          :retryable="false"
          :dismiss-label="$t('common.close')"
          :on-dismiss="() => { moveError = null }"
          class="mb-4"
          data-testid="schema-list-move-error"
          role="alert"
        />

        <!-- STATE-3 (ux-conformance): the column count is known before the
             first row arrives, so the loading placeholder is the shared
             determinate TableSkeleton (4 columns / 6 rows, matching the real
             <th> row and row count) instead of a hand-rolled pulse table.
             `aria-hidden` keeps the loading placeholder out of the
             accessibility tree, as the bespoke placeholder was. -->
        <TableSkeleton v-if="loading" :columns="4" :rows="6" aria-hidden="true" />

        <ErrorAlert v-else-if="error" :message="error" :on-retry="loadSchemas" />

        <template v-else>
          <!-- Mobile folder filter — the FolderTree is hidden below md -->
          <div v-if="foldersList.length > 0" class="md:hidden mb-4">
            <Select
  v-model="mobileFolderSelectValue"
  :aria-label="$t('views.SchemaListView.folders')"
  :placeholder="$t('views.SchemaListView.folders')"
  data-testid="schema-list-mobile-folder-select"
  class="w-full"
  :options="[{ value: '__all__', label: $t('views.SchemaListView.all_schemas') }, ...sortedFolders.map(f => ({ value: f.id, label: f.name }))]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
          </div>

          <!-- Breadcrumb navigation -->
          <div class="mb-4 flex items-center gap-2 text-sm">
            <template v-if="selectedFolderId && selectedFolderName">
              <button type="button" class="text-muted-foreground hover:text-foreground transition-colors" data-testid="schema-list-all-schemas" @click="onSelectFolder(null)">
                {{ $t('views.SchemaListView.all_schemas') }}
              </button>
              <ChevronRight class="h-3 w-3 text-muted-foreground" aria-hidden="true" />
              <span class="font-medium text-foreground">{{ selectedFolderName }}</span>
            </template>
            <h2 v-else class="text-base font-semibold text-foreground">{{ $t('views.SchemaListView.all_schemas') }}</h2>
          </div>

          <EmptyState
            v-if="schemas.length === 0"
            :title="$t('views.SchemaListView.no_schemas_found')"
            :description="$t('views.SchemaListView.empty_hint')"
          />

          <template v-else>
            <div class="table-wrapper">
              <table class="w-full text-left text-sm">
                <thead>
                  <tr>
                    <th class="table-header">{{ $t('views.SchemaListView.name') }}</th>
                    <th class="table-header">{{ $t('views.SchemaListView.description') }}</th>
                    <th class="table-header capitalize">{{ $t('views.SchemaListView.status') }}</th>
                    <th class="table-header table-cell-numeric">{{ $t('views.SchemaListView.actions') }}</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-border">
                  <tr
                    v-for="schema in schemas"
                    :key="schema.id"
                    class="cursor-pointer hover:bg-muted/30 transition-colors"
                    tabindex="0"
                    @click="openEditor(schema)"
                    @keydown.enter="openEditor(schema)"
                    @keydown.space.prevent="openEditor(schema)"
                    :data-testid="`schema-row-${schema.id}`"
                    draggable="true"
                    @dragstart="onSchemaDragStart(schema, $event)"
                    @dragover.prevent
                  >
                    <td class="table-cell font-medium">{{ schema.name }}</td>
                    <td class="table-cell text-muted-foreground">{{ schema.description ?? '—' }}</td>
                    <td class="table-cell">
                      <span
                        v-if="schema.deprecated"
                        class="inline-flex items-center gap-1 rounded-full bg-destructive/10 px-2.5 py-0.5 text-xs font-medium text-destructive-text"
                      >
                        <span class="h-1.5 w-1.5 rounded-full bg-destructive" aria-hidden="true" />
                        {{ $t('views.SchemaListView.deprecated') }}
                      </span>
                      <span
                        v-else
                        class="inline-flex items-center gap-1 rounded-full bg-success/10 px-2.5 py-0.5 text-xs font-medium text-success-text"
                      >
                        <span class="h-1.5 w-1.5 rounded-full bg-success" aria-hidden="true" />
                        {{ $t('views.SchemaListView.active') }}
                      </span>
                    </td>
                    <td class="table-cell-numeric">
                      <button
                        type="button"
                        class="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
                        data-testid="schema-action-menu"
                        :aria-label="$t('views.SchemaListView.schema_actions')"
                        @click.stop="openActionMenu($event, schema)"
                      >
                        <MoreVertical class="h-3.5 w-3.5" aria-hidden="true" />
                      </button>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </template>
        </template>
      </section>
    </div>

    <!-- Deprecation Confirmation Dialog -->
    <FormDialog
      v-model:open="deprecateDialogOpen"
      :title="$t('views.SchemaListView.deprecation_title', { name: deprecateConfirmName })"
      :description="$t('views.SchemaListView.deprecation_description')"
      :confirm-text="$t('views.SchemaListView.deprecate')"
      :confirm-loading-text="$t('views.SchemaListView.deprecating')"
      :loading="deprecating"
      confirm-severity="danger"
      cancel-test-id="schema-deprecate-cancel"
      confirm-test-id="schema-deprecate-confirm"
      data-testid="schema-deprecate-dialog"
      @confirm="deprecateSchema"
    >
      <!-- Retry is deliberately omitted (retryable=false): a failed deprecate is
           retried via the dialog's Confirm button, which re-invokes deprecateSchema. -->
      <ErrorAlert v-if="deprecateError" :message="deprecateError" :retryable="false" />
    </FormDialog>

    <!-- Move to Folder Dialog -->
    <FormDialog
      v-model:open="showMoveToFolder"
      :title="$t('views.SchemaListView.move_to_folder')"
      :description="moveTarget ? $t('views.SchemaListView.move_to_folder_description', { name: moveTarget.name }) : undefined"
      :confirm-text="$t('common.save')"
      :confirm-loading-text="$t('common.saving')"
      :loading="moving"
      cancel-test-id="schema-move-cancel"
      confirm-test-id="schema-move-confirm"
      data-testid="schema-move-dialog"
      @confirm="handleMoveToFolder"
    >
      <div class="space-y-2">
        <button
          type="button"
          v-for="f in sortedFolders"
          :key="f.id"
          class="flex w-full items-center gap-2 rounded-lg border px-3 py-2 text-sm hover:bg-accent transition-colors text-left"
          :class="moveToFolderId === f.id ? 'border-primary bg-accent' : 'border-border'"
          :aria-pressed="moveToFolderId === f.id"
          :data-testid="`schema-move-folder-${f.id}`"
          @click="moveToFolderId = f.id"
        >
          <Folder class="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
          {{ f.name }}
        </button>
        <button
          type="button"
          class="flex w-full items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm hover:bg-accent transition-colors text-left"
          :class="moveToFolderId === null ? 'border-primary bg-accent' : ''"
          :aria-pressed="moveToFolderId === null"
          data-testid="schema-move-nofolder"
          @click="moveToFolderId = null"
        >
          <FolderOpen class="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
          {{ $t('views.SchemaListView.no_folder') }}
        </button>
        <!-- No retry was offered on this box before the unification; the dialog's
             Confirm button re-invokes handleMoveToFolder, so keep it non-retryable. -->
        <ErrorAlert v-if="moveError" :message="moveError" :retryable="false" role="alert" />
      </div>
    </FormDialog>

    <Menu ref="actionMenuRef" :model="actionMenuItems" popup />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import { useApi } from '../composables/useApi'
import { formatApiError } from '../lib/api/formatError'
import { invalidFolderListMessage, isFolderList, type FolderItem } from '../lib/api/folders'
import type { components } from '../lib/api/client'
import FormDialog from '../components/shared/FormDialog.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import PageHeader from '../components/shared/PageHeader.vue'
import TableSkeleton from '../components/shared/TableSkeleton.vue'
import FolderTree from '../components/pipelines/FolderTree.vue'
import FolderListError from '../components/pipelines/FolderListError.vue'
import { ChevronRight, Folder, FolderOpen, MoreVertical } from '@lucide/vue'
import Menu from 'primevue/menu'
import Select from '../components/shared/AppSelect.vue'

type SchemaItem = components['schemas']['farna__api__routes__schemas__SchemaResponse'] & {
  folder_id?: string | null
}

interface SchemaListResponse {
  items: SchemaItem[]
  total: number
  page: number
  page_size: number
}

const router = useRouter()
const { t } = useI18n()
const { get, patch: patchUntyped } = useApi()

const selectedFolderId = ref<string | null>(null)

const { loading, error, data: schemasResp, load: loadSchemas } = useDataFetch<SchemaListResponse>(
  () => {
    const query: { page: number; page_size: number; folder_id?: string } = { page: 1, page_size: 100 }
    if (selectedFolderId.value) {
      query.folder_id = selectedFolderId.value
    }
    return api.GET('/api/v1/schemas', {
      params: { query },
    })
  },
  { initialValue: { items: [] as SchemaItem[], total: 0, page: 1, page_size: 100 } },
)

const schemas = computed(() => schemasResp.value?.items ?? [])

const foldersList = ref<FolderItem[]>([])
const folderError = ref<string | null>(null)

const folderSchemaCounts = ref<Record<string, number>>({})

async function loadSchemaCounts() {
  try {
    const { data } = await api.GET('/api/v1/schemas/counts')
    if (!data) return
    folderSchemaCounts.value = data.by_folder
      ? { ...data.by_folder, __all__: data.total }
      : { __all__: data.total }
  } catch {
    console.warn('Failed to load schema counts')
  }
}

const folderNameMap = computed(() => {
  const map = new Map<string, string>()
  for (const f of foldersList.value) {
    map.set(f.id, f.name)
  }
  return map
})

const selectedFolderName = computed(() => {
  if (!selectedFolderId.value) return ''
  return folderNameMap.value.get(selectedFolderId.value) ?? ''
})

const sortedFolders = computed(() => [...foldersList.value].sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base', numeric: true })))

const mobileFolderSelectValue = computed<string>({
  get: () => selectedFolderId.value ?? '__all__',
  set: (val: string) => onSelectFolder(val === '__all__' ? null : val),
})

async function loadFolders() {
  folderError.value = null
  try {
    const response = await get<FolderItem[]>('/api/v1/schema-folders')
    // Guard before assigning: `get<FolderItem[]>` is an unchecked cast and
    // useApi returns `undefined as T` on a 204, while foldersList is
    // dereferenced (foldersList.length / spread iteration / f.id / f.name) on
    // every render — a non-array would crash the view. Do NOT coerce to []:
    // that would hide a real contract violation behind an empty list.
    if (!isFolderList(response)) {
      console.error(invalidFolderListMessage('/api/v1/schema-folders', response))
      folderError.value = t('views.SchemaListView.invalid_folder_list_response')
      return
    }
    foldersList.value = response
  } catch (e: unknown) {
    folderError.value = formatApiError(e)
  }
}

function onSelectFolder(folderId: string | null) {
  selectedFolderId.value = folderId
  void loadSchemas()
}

function onFoldersChanged() {
  void loadSchemas()
  void loadFolders()
  void loadSchemaCounts()
}

function onSchemaDragStart(schema: SchemaItem, event: DragEvent) {
  event.dataTransfer?.setData('text/plain', schema.id)
  if (event.dataTransfer) {
    event.dataTransfer.effectAllowed = 'move'
  }
}

const moving = ref(false)
const moveError = ref<string | null>(null)

async function moveSchema(schemaId: string, folderId: string | null) {
  if (moving.value) return
  const schema = schemas.value.find(s => s.id === schemaId)
  if (!schema) return
  if ((schema.folder_id ?? null) === folderId) return
  moveError.value = null
  moving.value = true
  try {
    await patchUntyped(`/api/v1/schemas/${schemaId}/folder`, {
      folder_id: folderId,
    })
    await loadSchemas()
    await loadSchemaCounts()
  } catch (e: unknown) {
    moveError.value = formatApiError(e)
  } finally {
    moving.value = false
  }
}

async function onMoveSchema(ev: { pipelineId: string; folderId: string | null }) {
  await moveSchema(ev.pipelineId, ev.folderId)
}

const showMoveToFolder = ref(false)
const moveTarget = ref<SchemaItem | null>(null)
const moveToFolderId = ref<string | null>(null)

function openMoveToFolder(schema: SchemaItem) {
  moveTarget.value = schema
  moveToFolderId.value = schema.folder_id ?? null
  moveError.value = null
  showMoveToFolder.value = true
}

async function handleMoveToFolder() {
  if (!moveTarget.value) return
  const targetId = moveTarget.value.id
  const folderId = moveToFolderId.value
  await moveSchema(targetId, folderId)
  if (!moveError.value) {
    showMoveToFolder.value = false
    moveTarget.value = null
  }
}

function openEditor(schema: SchemaItem) {
  router.push({ name: 'schema-editor', params: { id: schema.id } })
}

const deprecateDialogOpen = ref(false)
const deprecateConfirmId = ref<string | null>(null)
const deprecateConfirmName = ref('')
const deprecating = ref(false)
const deprecateError = ref<string | null>(null)

const actionMenuRef = ref<InstanceType<typeof Menu> | null>(null)
const actionMenuSchema = ref<SchemaItem | null>(null)
const actionMenuItems = computed(() => [
  {
    label: t('views.SchemaListView.view_edit'),
    dataTestid: 'schema-view-edit',
    command: () => {
      if (actionMenuSchema.value) openEditor(actionMenuSchema.value)
    },
  },
  {
    label: t('views.SchemaListView.move_to_folder'),
    dataTestid: 'schema-move-folder',
    command: () => {
      if (actionMenuSchema.value) openMoveToFolder(actionMenuSchema.value)
    },
  },
  ...(!actionMenuSchema.value?.deprecated
    ? [{
        label: t('views.SchemaListView.deprecate'),
        dataTestid: 'schema-deprecate',
        class: 'text-destructive',
        command: () => {
          if (actionMenuSchema.value) confirmDeprecate(actionMenuSchema.value)
        },
      }]
    : []),
])

function openActionMenu(event: MouseEvent, schema: SchemaItem) {
  actionMenuSchema.value = schema
  actionMenuRef.value?.toggle(event)
}

function confirmDeprecate(schema: SchemaItem) {
  deprecateConfirmId.value = schema.id
  deprecateConfirmName.value = schema.name
  deprecateError.value = null
  deprecateDialogOpen.value = true
}

async function deprecateSchema() {
  if (!deprecateConfirmId.value) return
  deprecating.value = true
  deprecateError.value = null
  try {
    const { data, error: err } = await api.PATCH('/api/v1/schemas/{schema_id}/deprecate', {
      params: { path: { schema_id: deprecateConfirmId.value } },
    })
    if (err) {
      deprecateError.value = formatApiError(err)
    } else if (data) {
      const idx = schemas.value.findIndex(s => s.id === deprecateConfirmId.value)
      if (idx >= 0) {
        const s = schemasResp.value
        if (s) {
          schemasResp.value = {
            ...s,
            items: s.items.map((item, itemIdx) => itemIdx === idx ? (data as SchemaItem) : item),
          }
        }
      }
      deprecateDialogOpen.value = false
    }
  } catch (e: unknown) {
    deprecateError.value = formatApiError(e)
  } finally {
    deprecating.value = false
  }
}

onMounted(() => {
  void loadFolders()
  void loadSchemaCounts()
})
</script>
