import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { nextTick } from 'vue'

const STORAGE_KEY = 'sidebar-group-prefs'

beforeEach(() => {
  vi.resetModules()
  localStorage.clear()
})

afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
})

async function setupSidebar() {
  const mod = await import('../../composables/useSidebar')
  return mod.useSidebar()
}

describe('useSidebar', () => {
  it('falls back to the default collapse state when no preference is stored', async () => {
    const sidebar = await setupSidebar()

    expect(sidebar.isGroupCollapsed('runs', true)).toBe(true)
    expect(sidebar.isGroupCollapsed('runs', false)).toBe(false)
  })

  it('toggleGroup flips a collapsed default to expanded', async () => {
    const sidebar = await setupSidebar()
    expect(sidebar.isGroupCollapsed('runs', true)).toBe(true)
    sidebar.toggleGroup('runs', true)
    expect(sidebar.isGroupCollapsed('runs', true)).toBe(false)
  })

  it('toggleGroup flips an expanded default to collapsed', async () => {
    const sidebar = await setupSidebar()
    expect(sidebar.isGroupCollapsed('agents', false)).toBe(false)
    sidebar.toggleGroup('agents', false)
    expect(sidebar.isGroupCollapsed('agents', false)).toBe(true)
  })

  it('toggles back and forth across repeated calls', async () => {
    const sidebar = await setupSidebar()
    sidebar.toggleGroup('runs', true)
    sidebar.toggleGroup('runs', true)
    expect(sidebar.isGroupCollapsed('runs', true)).toBe(true)
    sidebar.toggleGroup('runs', true)
    expect(sidebar.isGroupCollapsed('runs', true)).toBe(false)
  })

  it('persists toggled prefs to localStorage', async () => {
    const sidebar = await setupSidebar()
    sidebar.toggleGroup('runs', true)
    await vi.waitFor(() => expect(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}')).toEqual({ runs: false }))
  })

  it('reads a persisted pref back through isGroupCollapsed', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ runs: false }))
    const sidebar = await setupSidebar()
    expect(sidebar.isGroupCollapsed('runs', true)).toBe(false)
  })

  it('a stored preference wins over the default', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ triggers: false }))
    const sidebar = await setupSidebar()

    expect(sidebar.isGroupCollapsed('triggers', true)).toBe(false)
  })

  it('keeps group prefs independent of one another', async () => {
    const sidebar = await setupSidebar()
    sidebar.toggleGroup('runs', true)
    sidebar.toggleGroup('agents', false)
    expect(sidebar.isGroupCollapsed('runs', true)).toBe(false)
    expect(sidebar.isGroupCollapsed('agents', false)).toBe(true)
  })

  it('is a module-level singleton shared across consumers', async () => {
    const { useSidebar } = await import('../../composables/useSidebar')
    const first = useSidebar()
    const second = useSidebar()

    first.toggleGroup('runs', true)
    expect(second.isGroupCollapsed('runs', true)).toBe(false)
  })

  it('restores preferences from a prior session on a fresh module import', async () => {
    const first = await import('../../composables/useSidebar')
    first.useSidebar().toggleGroup('runs', true)
    await nextTick()

    vi.resetModules()
    const second = await import('../../composables/useSidebar')
    expect(second.useSidebar().isGroupCollapsed('runs', true)).toBe(false)
  })

  describe('active-route reveal (FAR-2602)', () => {
    it('reveals a never-toggled default-collapsed group while it holds the active route', async () => {
      const sidebar = await setupSidebar()

      expect(sidebar.isGroupCollapsed('runs', true, true)).toBe(false)
    })

    it('keeps a never-toggled default-collapsed group collapsed when no active route is inside it', async () => {
      const sidebar = await setupSidebar()

      expect(sidebar.isGroupCollapsed('runs', true, false)).toBe(true)
    })

    it('leaves a default-expanded group expanded whether or not it holds the active route', async () => {
      const sidebar = await setupSidebar()

      expect(sidebar.isGroupCollapsed('build', false, true)).toBe(false)
      expect(sidebar.isGroupCollapsed('build', false, false)).toBe(false)
    })

    it('honours an explicit collapse even while the group holds the active route', async () => {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ runs: true }))
      const sidebar = await setupSidebar()

      expect(sidebar.isGroupCollapsed('runs', true, true)).toBe(true)
    })

    it('honours an explicit expand even when no active route is inside the group', async () => {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ runs: false }))
      const sidebar = await setupSidebar()

      expect(sidebar.isGroupCollapsed('runs', true, false)).toBe(false)
    })

    it('collapses an active-revealed group on the first toggle and records an explicit preference', async () => {
      const sidebar = await setupSidebar()
      // The user sees it expanded because the active route is inside it…
      expect(sidebar.isGroupCollapsed('runs', true, true)).toBe(false)

      sidebar.toggleGroup('runs', true, true)

      // …so the first toggle must collapse it rather than "expand" it again.
      expect(sidebar.isGroupCollapsed('runs', true, true)).toBe(true)
      await vi.waitFor(() =>
        expect(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}')).toEqual({ runs: true }),
      )
    })

    it('does not record a preference when it merely reveals an active group', async () => {
      const sidebar = await setupSidebar()

      expect(sidebar.isGroupCollapsed('runs', true, true)).toBe(false)

      // A reveal is transient state, not a user choice: writing it would keep
      // the group expanded after navigating away (FAR-2602 review).
      expect(sidebar.groupPrefs.value).toEqual({})
    })

    it('keeps the two-argument call meaning "default only"', async () => {
      const sidebar = await setupSidebar()

      expect(sidebar.isGroupCollapsed('runs', true)).toBe(true)
      sidebar.toggleGroup('runs', true)
      expect(sidebar.isGroupCollapsed('runs', true)).toBe(false)
    })
  })

  it('exposes groupPrefs as a readonly ref', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const { useSidebar } = await import('../../composables/useSidebar')
    const sidebar = useSidebar()

    expect(sidebar.groupPrefs.value).toEqual({})

    // @ts-expect-error mutating a readonly ref
    sidebar.groupPrefs.value = { runs: true }

    expect(sidebar.groupPrefs.value).toEqual({})
    expect(warn).toHaveBeenCalled()
    warn.mockRestore()
  })
})
