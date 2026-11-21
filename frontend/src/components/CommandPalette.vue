<template>
  <Teleport to="body">
    <dialog
      v-if="isOpen"
      ref="dialogRef"
      class="mx-auto mt-[15vh] w-full max-w-lg rounded-lg border bg-background p-0 text-foreground shadow-xl backdrop:bg-black/50"
      :aria-label="$t('components.CommandPalette.label')"
      @click="onBackdropClick"
      @keydown.escape="close"
      @close="isOpen = false"
    >
      <div class="flex items-center gap-2 border-b px-4">
        <SvgIcon name="Search" class="h-4 w-4 shrink-0 text-muted-foreground" />
        <input
          id="commandpalette-search-input"
          ref="inputRef"
          v-model="query"
          :aria-label="$t('components.AppLayout.search_pages')"
          :placeholder="$t('components.CommandPalette.search_placeholder')"
          class="h-12 flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
          @keydown.down.prevent="selectedIndex = Math.min(selectedIndex + 1, filteredItems.length - 1)"
          @keydown.up.prevent="selectedIndex = Math.max(selectedIndex - 1, 0)"
          @keydown.enter.prevent="navigate"
        />
      </div>
      <div class="max-h-80 overflow-y-auto p-2" data-cmdk-container>
        <div
          v-if="filteredItems.length === 0 && query.length > 0"
          class="px-3 py-8 text-center text-sm text-muted-foreground"
        >
          {{ $t('components.CommandPalette.no_results') }}
        </div>
        <button type="button"
          v-for="(item, idx) in filteredItems"
          :key="item.path + item.label"
          :class="[
            'flex w-full items-center gap-3 rounded-md px-3 py-2.5 text-left text-sm transition-colors',
            idx === selectedIndex ? 'bg-accent text-accent-foreground' : 'text-foreground',
          ]"
          @click="goTo(item.path)"
          @mouseenter="selectedIndex = idx"
          @focus="selectedIndex = idx"
        >
          <SvgIcon :name="item.icon" class="h-4 w-4 shrink-0 text-muted-foreground" />
          <span class="flex-1 truncate">{{ item.label }}</span>
          <span class="shrink-0 text-xs text-muted-foreground">{{ item.section }}</span>
        </button>
      </div>
      <div class="border-t px-4 py-2 text-xs text-muted-foreground text-right">
        &uarr;&darr; navigate &middot; &crarr; open &middot; Esc close
      </div>
    </dialog>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { getNavGroups, getVisibleNavGroups } from '../config/navigation'
import SvgIcon from './SvgIcon.vue'
import { useNavVisibilityContext } from '../composables/useNavVisibilityContext'

interface SearchItem {
  label: string
  path: string
  icon: string
  section: string
  /**
   * Instance-level pages (manifest `system_admin_only`) are not sidebar items,
   * so the nav-membership gate in `searchItems` cannot see their gate. Such an
   * extra applies the system-admin check directly instead of being shown to
   * every user and then bounced by the router guard.
   */
  systemAdminOnly?: boolean
}

const router = useRouter()
const { t } = useI18n()
const navContext = useNavVisibilityContext()
const isOpen = ref(false)
const query = ref('')
const selectedIndex = ref(0)
const inputRef = ref<HTMLInputElement | null>(null)
const dialogRef = ref<HTMLDialogElement | null>(null)

// Sections are the manifest's sidebar-group labels (FAR-2605: pipelines,
// controls, runs, configuration, administration, system), so a result never
// advertises a group the sidebar no longer has. The pinned Dashboard sits above
// the groups and borrows the closest one. Every curated `path` below is a real
// route the app serves (FAR-2616): there is no /settings hub, so no Settings
// entry is curated, and no /admin/system hub, so the System entry points at
// the deployment-wide page /admin/system/config. Each section resolves through
// $t() against the same labelKey the sidebar group uses, so the palette never
// drifts into hardcoded English.
const paletteExtras: SearchItem[] = [
  { label: 'Dashboard', path: '/', icon: 'LayoutDashboard', section: t('components.SidebarNav.group_pipelines') },
  { label: 'Pipelines', path: '/pipelines', icon: 'GitFork', section: t('components.SidebarNav.group_pipelines') },
  { label: 'Library', path: '/library', icon: 'BookOpen', section: t('components.SidebarNav.group_pipelines') },
  { label: 'Runs', path: '/runs', icon: 'CirclePlay', section: t('components.SidebarNav.group_runs') },
  { label: 'Evals', path: '/evals', icon: 'CheckSquare', section: t('components.SidebarNav.group_controls') },
  { label: 'Schemas', path: '/schemas', icon: 'Database', section: t('components.SidebarNav.group_pipelines') },
  { label: 'Connectors', path: '/admin/connectors', icon: 'Plug', section: t('components.SidebarNav.group_configuration') },
  { label: 'Organization', path: '/admin/org', icon: 'Building', section: t('components.SidebarNav.group_administration') },
  { label: 'System', path: '/admin/system/config', icon: 'Settings', section: t('components.SidebarNav.group_system'), systemAdminOnly: true },
  { label: 'Cost Management', path: '/admin/costs', icon: 'DollarSign', section: t('components.SidebarNav.group_runs') },
]

const searchItems = computed<SearchItem[]>(() => {
  const seen = new Set<string>()
  const items: SearchItem[] = []
  const ctx = navContext.value
  const visibleGroups = getVisibleNavGroups(ctx)
  const visiblePaths = new Set(visibleGroups.flatMap((g) => g.items.map((i) => i.to)))
  for (const group of visibleGroups) {
    for (const navItem of group.items) {
      const path = navItem.to
      if (seen.has(path)) continue
      seen.add(path)
      items.push({
        label: navItem.label,
        path,
        icon: navItem.icon,
        section: group.label,
      })
    }
  }
  // Curated parent/landing routes that are not sidebar items in the manifest.
  // Gate them with the SAME navbar rules: if a path resolves to a sidebar item
  // it must appear in the visible set (group-level systemAdminOnly + item-level
  // gates already applied), otherwise it stays hidden. An extra whose route the
  // sidebar never lists (an instance-level system page) has no nav entry for
  // that check to read, so it declares its own `systemAdminOnly` flag.
  const extras: SearchItem[] = paletteExtras
  const rawItems = getNavGroups().flatMap((g) => g.items)
  for (const extra of extras) {
    if (seen.has(extra.path)) continue
    if (extra.systemAdminOnly && !ctx.isSystemAdmin) continue
    const hasNavItem = rawItems.some((i) => i.to === extra.path)
    if (hasNavItem && !visiblePaths.has(extra.path)) continue
    seen.add(extra.path)
    items.push(extra)
  }
  return items
})

const filteredItems = computed(() => {
  const q = query.value.toLowerCase().trim()
  if (!q) return searchItems.value
  return searchItems.value.filter(
    item =>
      item.label.toLowerCase().includes(q) ||
      item.section.toLowerCase().includes(q),
  )
})

watch(selectedIndex, (idx) => {
  nextTick(() => {
    const container = document.querySelector('[data-cmdk-container]')
    if (!container) return
    const buttons = container.querySelectorAll('button')
    buttons[idx]?.scrollIntoView({ block: 'nearest' })
  })
})

watch(filteredItems, () => {
  const count = filteredItems.value.length
  if (count === 0) {
    selectedIndex.value = 0
  } else if (selectedIndex.value > count - 1) {
    selectedIndex.value = count - 1
  }
})

async function open() {
  isOpen.value = true
  query.value = ''
  selectedIndex.value = 0
  await nextTick()
  // A modal palette: showModal() makes the opaque sheet genuinely modal —
  // the background is inert and focus is contained — instead of only looking
  // modal while Tab can still reach the page behind it. Guarded because a
  // second showModal() on an already-open dialog throws InvalidStateError.
  const dialog = dialogRef.value
  if (dialog && !dialog.open) dialog.showModal()
  inputRef.value?.focus()
}

function close() {
  const dialog = dialogRef.value
  if (dialog?.open) dialog.close()
  isOpen.value = false
}

/** A click whose target is the dialog itself is a click on ::backdrop. */
function onBackdropClick(event: MouseEvent) {
  if (event.target === event.currentTarget) close()
}

// `paletteExtras` is exposed so the spec can assert every curated section
// label against the manifest's current sidebar groups (FAR-2605) and every
// curated path against the routes the manifest declares (FAR-2616).
defineExpose({ open, paletteExtras })

function navigate() {
  const items = filteredItems.value
  if (items.length === 0) return
  const item = items[selectedIndex.value]
  if (item) goTo(item.path)
}

function goTo(path: string) {
  close()
  router.push(path)
}

function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && isOpen.value) {
    close()
    return
  }
  if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
    e.preventDefault()
    if (isOpen.value) {
      close()
    } else {
      void open()
    }
  }
}

onMounted(() => {
  document.addEventListener('keydown', handleKeydown)
})

onUnmounted(() => {
  document.removeEventListener('keydown', handleKeydown)
})
</script>
