import { readonly } from 'vue'
import { useStorage } from '@vueuse/core'

export type ViewMode = 'simple' | 'advanced'

const groupPrefs = useStorage<Record<string, boolean>>('sidebar-group-prefs', {})
const collapsed = useStorage<boolean>('sidebar-collapsed', false)

function toggleGroup(id: string, defaultCollapsed: boolean, active = false) {
  // Toggle from the state the user can SEE, not from the stored default: a
  // default-collapsed group revealed because it holds the active route is
  // showing expanded, so the first click must collapse it.
  groupPrefs.value[id] = !isGroupCollapsed(id, defaultCollapsed, active)
}

/**
 * Whether a sidebar group renders collapsed.
 *
 * An explicit toggle always wins (FAR-2602): a group the user deliberately
 * collapsed stays collapsed even when the current route lives inside it, so
 * navigation never fights that choice on every route change.
 *
 * A group the user has NEVER toggled follows its manifest default, except that
 * a default-collapsed group is revealed while it contains the active route —
 * otherwise the page the user is looking at can have no visible nav entry.
 */
function isGroupCollapsed(id: string, defaultCollapsed: boolean, active = false): boolean {
  const pref: boolean | undefined = groupPrefs.value[id]
  if (pref !== undefined) return pref
  return defaultCollapsed && !active
}

function setCollapsed(v: boolean) {
  collapsed.value = v
}

function toggleCollapsed() {
  collapsed.value = !collapsed.value
}

export function useSidebar() {
  return {
    groupPrefs: readonly(groupPrefs),
    collapsed: readonly(collapsed),
    toggleGroup,
    isGroupCollapsed,
    setCollapsed,
    toggleCollapsed,
  }
}
