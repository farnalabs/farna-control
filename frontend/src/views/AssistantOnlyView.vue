<template>
  <div class="assistant-only-view flex h-screen h-screen-dvh flex-col overflow-hidden bg-background" data-testid="assistant-only-view">
    <header class="flex items-center justify-between gap-3 border-b bg-card px-4 py-2">
      <div class="flex min-w-0 items-center gap-2">
        <div class="flex items-center justify-center rounded-lg bg-primary/10 p-1.5">
          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
            <rect width="18" height="10" x="3" y="11" rx="2" />
            <circle cx="12" cy="5" r="2" />
            <path d="M12 7v4" />
            <line x1="8" y1="16" x2="8" y2="16" />
            <line x1="16" y1="16" x2="16" y2="16" />
          </svg>
        </div>
        <div class="min-w-0">
          <h1 class="truncate text-sm font-semibold">{{ $t('components.assistant.AssistantOnlyView.assistant_title') }}</h1>
          <span class="text-xs text-muted-foreground">{{ $t('components.assistant.AssistantOnlyView.assistant_only_mode') }}</span>
        </div>
      </div>
      <Button variant="secondary" size="sm" data-testid="assistant-only-return-home" @click="returnHome">
        <ArrowLeft class="mr-1 h-3.5 w-3.5" aria-hidden="true" />
        {{ $t('components.assistant.AssistantOnlyView.return_home') }}
      </Button>
    </header>

    <div
      class="border-b bg-primary/5 px-4 py-1.5 text-xs text-muted-foreground"
      data-testid="assistant-only-banner"
    >
      {{ $t('components.assistant.AssistantOnlyView.banner') }}
    </div>

    <template v-if="assistantUnavailable">
      <div class="flex flex-1 items-center justify-center p-8" data-testid="assistant-only-unavailable">
        <div class="max-w-md space-y-2 text-center">
          <h2 class="text-lg font-semibold">{{ $t('components.assistant.AssistantOnlyView.assistant_unavailable') }}</h2>
          <p class="text-sm text-muted-foreground">{{ $t('components.assistant.AssistantOnlyView.assistant_unavailable_desc') }}</p>
          <Button variant="secondary" size="sm" data-testid="assistant-only-return-home" @click="returnHome">
            {{ $t('components.assistant.AssistantOnlyView.return_home') }}
          </Button>
        </div>
      </div>
    </template>

    <template v-else-if="store.sessionsLoading">
      <div class="flex flex-1 items-center justify-center">
        <span class="text-sm text-muted-foreground">{{ $t('common.loading') }}</span>
      </div>
    </template>

    <template v-else>
      <div class="flex items-center gap-1.5 overflow-x-auto border-b px-3 py-1.5">
        <StateTabs
          :ariaLabel="$t('components.assistant.AssistantOnlyView.sessions_tab')"
          :model-value="activeSessionTabKey"
          :panel-id="panelsVisible ? 'assistant-only-panel-chat' : undefined"
          :tabs="sessionTabItems"
          data-testid="assistant-only-tab-bar"
          id-prefix="assistant-only-tab-button"
          size="compact"
          testid-prefix="assistant-only-tab"
          @update:model-value="onSessionTabSelected"
        >
          <template #tab="{ tab }">
            <span class="max-w-[180px] truncate">{{ tab.label }}</span>
            <span
              v-if="showLiveDot(tab.id)"
              class="assistant-live-dot"
              :title="$t('components.assistant.AssistantOnlyView.live')"
              :aria-label="$t('components.assistant.AssistantOnlyView.live')"
            />
          </template>
          <template #tab-trailing="{ tab }">
            <button
              type="button"
              class="assistant-only-tab-close"
              :data-testid="`assistant-only-tab-close-${tab.id}`"
              :aria-label="$t('components.assistant.AssistantOnlyView.close_tab')"
              :title="$t('components.assistant.AssistantOnlyView.close_tab')"
              @click="closeTabById(tab.id)"
            >
              <X class="h-3 w-3" aria-hidden="true" />
            </button>
          </template>
        </StateTabs>
        <button
          type="button"
          class="assistant-only-new-tab"
          data-testid="assistant-only-new-tab"
          :title="$t('components.assistant.AssistantOnlyView.new_tab')"
          :aria-label="$t('components.assistant.AssistantOnlyView.new_tab')"
          @click="startNew"
        >
          <Plus class="h-3.5 w-3.5" aria-hidden="true" />
        </button>
      </div>

      <StateTabs
        :ariaLabel="$t('components.assistant.AssistantOnlyView.sub_tabs_label')"
        :model-value="subTab"
        :panel-id-prefix="panelsVisible ? 'assistant-only-panel' : undefined"
        :tabs="subTabItems"
        class="border-b px-3"
        data-testid="assistant-only-subtab-bar"
        id-prefix="assistant-only-subtab"
        size="compact"
        testid-prefix="assistant-only-subtab"
        @update:model-value="onSubTabSelected"
      />

      <div
        v-if="tabsStore.tabs.length === 0"
        class="flex flex-1 items-center justify-center p-8"
        data-testid="assistant-only-empty"
      >
        <EmptyState
          :title="$t('components.assistant.AssistantOnlyView.no_tabs_yet')"
          :description="$t('components.assistant.AssistantOnlyView.start_new')"
        >
          <Button size="sm" @click="startNew">{{ $t('components.assistant.AssistantOnlyView.start_new') }}</Button>
        </EmptyState>
      </div>

      <div
        v-else-if="!store.activeSessionId"
        class="flex flex-1 items-center justify-center p-8"
        data-testid="assistant-only-no-active"
      >
        <EmptyState :title="$t('components.assistant.AssistantOnlyView.select_tab_or_new')">
          <Button size="sm" @click="startNew">{{ $t('components.assistant.AssistantOnlyView.start_new') }}</Button>
        </EmptyState>
      </div>

      <div v-else class="flex flex-1 overflow-hidden" data-testid="assistant-only-chat">
        <div class="flex flex-1 flex-col overflow-hidden">
          <div
            v-show="subTab === 'chat'"
            id="assistant-only-panel-chat"
            role="tabpanel"
            :aria-labelledby="activeSessionTabId"
            class="flex flex-1 flex-col overflow-hidden"
          >
            <AssistantChat ref="chatRef" assistantOnly class="flex-1" />
          </div>
          <div
            v-show="subTab === 'skills'"
            id="assistant-only-panel-skills"
            role="tabpanel"
            aria-labelledby="assistant-only-subtab-skills"
            class="flex flex-1 overflow-hidden"
          >
            <AssistantSkillManager />
          </div>
          <div
            v-show="subTab === 'sources'"
            id="assistant-only-panel-sources"
            role="tabpanel"
            aria-labelledby="assistant-only-subtab-sources"
            class="flex flex-1 overflow-hidden"
          >
            <AssistantContextSources />
          </div>
          <div
            v-show="subTab === 'sessions'"
            id="assistant-only-panel-sessions"
            role="tabpanel"
            aria-labelledby="assistant-only-subtab-sessions"
            class="flex flex-1 overflow-hidden"
          >
            <AssistantSessionDrawer @select-session="onSessionSelected" />
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import Button from '../components/ui/Button.vue'
import { ArrowLeft, Plus, X } from '@lucide/vue'
import { shortId } from '@/utils/format'
import { useAssistantStore } from '@/composables/useAssistantStore'
import { useAssistantTabsStore, type AssistantTab } from '@/composables/useAssistantTabsStore'
import { usePlanStore } from '@/stores/planStore'
import StateTabs, { type StateTab } from '@/components/shared/StateTabs.vue'
import AssistantChat from '@/components/assistant/AssistantChat.vue'
import AssistantSkillManager from '@/components/assistant/AssistantSkillManager.vue'
import AssistantContextSources from '@/components/assistant/AssistantContextSources.vue'
import AssistantSessionDrawer from '@/components/assistant/AssistantSessionDrawer.vue'
import EmptyState from '@/components/shared/EmptyState.vue'

const router = useRouter()
const { t } = useI18n()
const store = useAssistantStore()
const tabsStore = useAssistantTabsStore()
const planStore = usePlanStore()
const chatRef = ref<InstanceType<typeof AssistantChat> | null>(null)

type SubTabKey = 'chat' | 'skills' | 'sources' | 'sessions'
const subTab = ref<SubTabKey>('chat')

const subTabItems = computed<StateTab[]>(() => [
  { id: 'chat', label: t('components.assistant.AssistantOnlyView.chat_tab') },
  { id: 'skills', label: t('components.assistant.AssistantOnlyView.skills_tab') },
  { id: 'sources', label: t('components.assistant.AssistantOnlyView.sources_tab') },
  { id: 'sessions', label: t('components.assistant.AssistantOnlyView.sessions_tab') },
])

const SUB_TAB_KEYS: readonly SubTabKey[] = ['chat', 'skills', 'sources', 'sessions']

/** Only the declared sub-tabs are selectable, whatever a caller sends. */
function onSubTabSelected(id: string) {
  const next = SUB_TAB_KEYS.find((key) => key === id)
  if (next) subTab.value = next
}

/** One tab per open assistant session; the id is the tab id the store owns. */
const sessionTabItems = computed<StateTab[]>(() =>
  tabsStore.tabs.map((tab) => {
    const title = sessionTitle(tab.sessionId)
    return { id: tab.tabId, label: title, title }
  }),
)

/** The open tab that fronts the active session (none when no tab is active). */
const activeSessionTabKey = computed(
  () => tabsStore.tabs.find((tab) => tab.sessionId === store.activeSessionId)?.tabId ?? '',
)

const activeSessionTabId = computed(() => {
  const active = tabsStore.tabs.find(t => t.sessionId === store.activeSessionId)
  return active ? `assistant-only-tab-button-${active.tabId}` : 'assistant-only-subtab-chat'
})

// The tabpanels (#assistant-only-panel-*) only render when there is at least one tab
// AND an active session; omit aria-controls when the controlled panel is absent.
const panelsVisible = computed(() => tabsStore.tabs.length > 0 && Boolean(store.activeSessionId))

function showLiveDot(tabId: string): boolean {
  const tab = tabsStore.tabs.find((candidate) => candidate.tabId === tabId)
  return Boolean(tab && tab.sessionId === store.activeSessionId && store.isStreaming)
}

function onSessionTabSelected(tabId: string) {
  const tab = tabsStore.tabs.find((candidate) => candidate.tabId === tabId)
  if (tab) selectTab(tab)
}

const assistantUnavailable = computed(() => !planStore.devMode)

function sessionTitle(sessionId: string): string {
  const session = store.sessions.find(s => s.id === sessionId)
  if (!session) return '...'
  if (session.name) return session.name
  return session.session_number ? `Session #${session.session_number}` : shortId(session.id)
}

function returnHome() {
  router.push('/')
}

async function startNew() {
  chatRef.value?.disconnect()
  await tabsStore.addTab()
  subTab.value = 'chat'
}

function selectTab(tab: AssistantTab) {
  if (tab.sessionId === store.activeSessionId) {
    subTab.value = 'chat'
    return
  }
  chatRef.value?.disconnect()
  tabsStore.resumeTab(tab.sessionId)
  subTab.value = 'chat'
}

function closeTab(tab: AssistantTab) {
  chatRef.value?.disconnect()
  tabsStore.closeTab(tab.tabId)
}

function closeTabById(tabId: string) {
  const tab = tabsStore.tabs.find((candidate) => candidate.tabId === tabId)
  if (tab) closeTab(tab)
}

function onSessionSelected() {
  if (store.activeSessionId) {
    chatRef.value?.disconnect()
    tabsStore.resumeTab(store.activeSessionId)
    subTab.value = 'chat'
  }
}

watch(
  () => store.activeSessionId,
  (id) => {
    if (!id) return
    const live = store.sessions.some(s => s.id === id)
    if (live && !tabsStore.tabs.some(t => t.sessionId === id)) {
      tabsStore.resumeTab(id)
    }
  },
)

onMounted(async () => {
  if (!planStore.loaded) {
    await planStore.fetchPlan().catch(() => {})
  }
  await store.fetchSessions()
  tabsStore.reconcile()
  if (store.activeSessionId) {
    await store.loadSession(store.activeSessionId)
  }
})
</script>

<style scoped>
@reference "../style.css";
.assistant-only-tab-close {
  @apply flex items-center justify-center rounded p-0.5 transition-colors shrink-0 border-0 bg-transparent;
  color: hsl(var(--muted-foreground));
}
.assistant-only-tab-close:hover {
  color: hsl(var(--foreground));
  background-color: hsl(var(--accent));
}
.assistant-live-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background-color: hsl(var(--primary));
  animation: assistant-live-pulse 1.5s ease-in-out infinite;
}
@media (prefers-reduced-motion: reduce) {
  .assistant-live-dot {
    animation: none;
  }
}
@keyframes assistant-live-pulse {
  0%,
  100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.4;
    transform: scale(0.75);
  }
}
.assistant-only-new-tab {
  @apply flex items-center justify-center rounded-md p-1.5 transition-colors shrink-0;
  color: hsl(var(--muted-foreground));
}
.assistant-only-new-tab:hover {
  color: hsl(var(--foreground));
  background-color: hsl(var(--accent));
}
</style>
