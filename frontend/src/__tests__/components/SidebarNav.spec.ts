import { describe, it, expect, vi, beforeEach } from 'vitest'
import { nextTick, reactive } from 'vue'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { useRoute } from 'vue-router'

const mockManifest = vi.hoisted(() => ({
  sidebar_groups: {
    build: { label: 'BUILD', order: 1, default_expanded: true },
    admin: { label: 'ADMIN', order: 2, default_expanded: false, system_admin_only: true },
    monitor: { label: 'MONITOR', order: 3, default_expanded: true },
  },
  routes: {
    '/': { name: 'dashboard', breadcrumb: 'Dashboard', sidebar_group: 'build', sidebar_order: 1, type: 'page', required_tier: null, required_roles: null, required_permissions: null, exact: true },
    '/pipelines': { name: 'pipeline-list', breadcrumb: 'Pipelines', sidebar_group: 'build', sidebar_order: 2, type: 'list_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null, exact: true },
    '/runs': { name: 'runs-list', breadcrumb: 'Runs', sidebar_group: 'build', sidebar_order: 3, type: 'list_page', required_tier: null, required_roles: null, required_permissions: null, exact: true },
    '/settings/license': { name: 'settings-license', breadcrumb: 'License', sidebar_group: 'admin', sidebar_order: 1, type: 'form_page', required_tier: null, required_roles: null, required_permissions: null },
    // FAR-2602: a list row plus a detail route that has NO nav row of its own,
    // mirroring the real `/runs` + `/runs/:id` pair (detail routes declare the
    // list page as their `parent`).
    '/admin/error-log': { name: 'admin-error-log', breadcrumb: 'Error Log', sidebar_group: 'admin', sidebar_order: 2, type: 'list_page', required_tier: null, required_roles: null, required_permissions: null },
    '/admin/error-log/:id': { name: 'admin-error-detail', breadcrumb: 'Error Detail', parent: '/admin/error-log', sidebar_group: 'admin', sidebar_order: 3, type: 'detail_page', required_tier: null, required_roles: null, required_permissions: null },
    '/evals/editor': { name: 'eval-editor', breadcrumb: 'Evals', sidebar_group: 'monitor', sidebar_order: 1, type: 'form_page', required_tier: null, required_roles: null, required_permissions: null, visibility: 'private_preview' },
    '/admin/notification-delivery': { name: 'admin-notification-delivery', breadcrumb: 'Webhook Notifications', sidebar_group: 'monitor', sidebar_order: 2, feature_flag: 'webhook_notification_log', type: 'list_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null },
    '/my-profile': { name: 'my-profile', breadcrumb: 'My Profile', sidebar_group: null, sidebar_order: null, type: 'page', required_tier: null, required_roles: null, required_permissions: null },
    '/runs/:id': { name: 'run-detail', breadcrumb: 'Run Detail', sidebar_group: 'build', sidebar_order: 4, type: 'detail_page', required_tier: null, required_roles: null, required_permissions: null },
  },
}))

vi.mock('@/manifest.yaml', () => ({
  default: mockManifest,
}))

// Wrap the visibility composable so a test can supply a group that the real
// nav cache (populated on first use) cannot otherwise be made to return.
vi.mock('../../config/navigation', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../config/navigation')>()
  return {
    ...actual,
    getVisibleNavGroups: vi.fn(actual.getVisibleNavGroups),
  }
})

import SidebarNav from '../../components/SidebarNav.vue'
import { getVisibleNavGroups } from '../../config/navigation'
import { usePlanStore } from '../../stores/planStore'
import { setDemoSession } from '../../lib/api/auth'

function mountSidebar(props = {}) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const wrapper = mount(SidebarNav, {
    props: {
      isSystemAdmin: false,
      userRole: 'viewer',
      userPermissions: [],
      ...props,
    },
    global: {
      plugins: [pinia],
      stubs: {
        OverlayScrollbarsComponent: { template: '<div class="os-stub"><slot /></div>' },
        SvgIcon: { template: '<span class="svg-stub" />' },
      },
    },
  })
  return { wrapper, store: usePlanStore() }
}

// The app-wide setup mock returns one mutable route object shared by every
// component in a test; assign `path` before mounting to place the shell on a
// route.
function setRoute(path: string) {
  ;(useRoute() as unknown as { path: string }).path = path
}

function groupHeader(wrapper: ReturnType<typeof mountSidebar>['wrapper'], label: string) {
  const header = wrapper.findAll('.sidebar-group-header').find((h) => h.text() === label)
  expect(header, `expected group header "${label}" to render`).toBeTruthy()
  return header!
}

// Parsed equality beats a substring check: it proves the exact preference map
// rather than merely that a group id appears somewhere in the blob.
function storedPrefs(): Record<string, boolean> {
  return JSON.parse(localStorage.getItem('sidebar-group-prefs') ?? '{}')
}

// Only links the user can actually SEE. A collapsed `SidebarGroup` keeps its
// items mounted under `v-show` (display:none), so a raw `findAll` still finds
// them — the acceptance criterion is about a *visible* affordance.
function linkHrefs(wrapper: ReturnType<typeof mountSidebar>['wrapper']) {
  return wrapper
    .findAll('a[data-testid="router-link-stub"]')
    .filter((link) => link.isVisible())
    .map((link) => link.attributes('href'))
}

describe('SidebarNav', () => {
  beforeEach(() => {
    localStorage.clear()
    setRoute('/')
    // useSidebar's useStorage refs are module singletons — localStorage.clear()
    // alone leaves the in-memory refs polluted across tests (a prior test may
    // have collapsed a group). Dispatch a synthetic storage event so
    // @vueuse/core's listener resets the 'sidebar-group-prefs' ref to its
    // default empty state.
    window.dispatchEvent(
      new StorageEvent('storage', {
        key: 'sidebar-group-prefs',
        newValue: '{}',
        storageArea: localStorage,
      }),
    )
    vi.clearAllMocks()
  })

  it('renders visible groups in manifest order with their labels', async () => {
    const { wrapper } = mountSidebar()
    await flushPromises()
    const headers = wrapper.findAll('.sidebar-group-header')
    const labels = headers.map((h) => h.text())
    // admin group is systemAdminOnly and hidden for non-admin; monitor is private_preview gated
    expect(labels).toEqual(['BUILD'])
  })

  it('renders systemAdminOnly groups for system admins', async () => {
    const { wrapper } = mountSidebar({ isSystemAdmin: true })
    await flushPromises()
    const labels = wrapper.findAll('.sidebar-group-header').map((h) => h.text())
    expect(labels).toContain('BUILD')
    expect(labels).toContain('ADMIN')
  })

  it('renders links for unrestricted items', async () => {
    const { wrapper } = mountSidebar()
    await flushPromises()
    const links = wrapper.findAll('a[data-testid="router-link-stub"]')
    const hrefs = links.map((l) => l.attributes('href'))
    expect(hrefs).toContain('/')
    expect(hrefs).toContain('/runs')
  })

  it('hides tier- and role-gated items from viewers on the community tier', async () => {
    const { wrapper } = mountSidebar({ userRole: 'viewer' })
    await flushPromises()
    const hrefs = wrapper.findAll('a[data-testid="router-link-stub"]').map((l) => l.attributes('href'))
    expect(hrefs).not.toContain('/pipelines')
  })

  it('shows tier- and role-gated items to admins on a sufficient tier', async () => {
    const { wrapper, store } = mountSidebar({ isSystemAdmin: true, userRole: 'admin' })
    store.currentTier = 'team'
    await flushPromises()
    const hrefs = wrapper.findAll('a[data-testid="router-link-stub"]').map((l) => l.attributes('href'))
    expect(hrefs).toContain('/pipelines')
  })

  it('hides tier-gated items when the plan is not loaded yet', async () => {
    const { wrapper, store } = mountSidebar({ isSystemAdmin: true, userRole: 'admin' })
    store.tierRanks = {}
    await flushPromises()
    const hrefs = wrapper.findAll('a[data-testid="router-link-stub"]').map((l) => l.attributes('href'))
    expect(hrefs).not.toContain('/pipelines')
  })

  it('shows the Webhook Notifications item to team admins by default (FAR-2235)', async () => {
    const { wrapper, store } = mountSidebar({ isSystemAdmin: true, userRole: 'admin' })
    store.currentTier = 'team'
    store.features['webhook_notification_log'] = true
    await flushPromises()
    const hrefs = wrapper.findAll('a[data-testid="router-link-stub"]').map((l) => l.attributes('href'))
    expect(hrefs).toContain('/admin/notification-delivery')
  })

  it('shows the Webhook Deliveries item when the community flag is explicitly enabled (FAR-656)', async () => {
    const { wrapper, store } = mountSidebar({ isSystemAdmin: true, userRole: 'admin' })
    store.currentTier = 'team'
    store.features['webhook_notification_log'] = true
    await flushPromises()
    const links = wrapper.findAll('a[data-testid="router-link-stub"]')
    const webhookLog = links.find((l) => l.attributes('href') === '/admin/notification-delivery')
    expect(webhookLog).toBeDefined()
    expect(webhookLog!.text()).toContain('Webhook Deliveries')
  })

  it('hides the flag-gated Webhook Notifications item from viewers even when the flag is enabled (FAR-656)', async () => {
    const { wrapper, store } = mountSidebar({ userRole: 'viewer' })
    store.currentTier = 'community'
    store.features['webhook_notification_log'] = true
    await flushPromises()
    const hrefs = wrapper.findAll('a[data-testid="router-link-stub"]').map((l) => l.attributes('href'))
    expect(hrefs).not.toContain('/admin/notification-delivery')
  })

  it('hides private_preview items unless dev mode is enabled', async () => {
    const { wrapper } = mountSidebar()
    await flushPromises()
    const hrefs = wrapper.findAll('a[data-testid="router-link-stub"]').map((l) => l.attributes('href'))
    expect(hrefs).not.toContain('/evals/editor')
    expect(wrapper.text()).not.toContain('MONITOR')

    const { wrapper: devWrapper, store } = mountSidebar()
    store.devMode = true
    await flushPromises()
    const devHrefs = devWrapper.findAll('a[data-testid="router-link-stub"]').map((l) => l.attributes('href'))
    expect(devHrefs).toContain('/evals/editor')
    expect(devWrapper.text()).toContain('MONITOR')
  })

  it('hides private_preview items during a demo session even when dev mode is enabled', async () => {
    // FAR-535: a demo visitor on an instance with devMode ON must not see
    // private_preview surfaces in the nav (server-side denial still holds).
    setDemoSession(true)
    const { wrapper, store } = mountSidebar()
    store.devMode = true
    await flushPromises()
    const hrefs = wrapper.findAll('a[data-testid="router-link-stub"]').map((l) => l.attributes('href'))
    expect(hrefs).not.toContain('/evals/editor')
    expect(wrapper.text()).not.toContain('MONITOR')

    // Without the demo flag the dev-mode behaviour is unchanged.
    setDemoSession(false)
    const { wrapper: devWrapper, store: devStore } = mountSidebar()
    devStore.devMode = true
    await flushPromises()
    const devHrefs = devWrapper.findAll('a[data-testid="router-link-stub"]').map((l) => l.attributes('href'))
    expect(devHrefs).toContain('/evals/editor')
    expect(devWrapper.text()).toContain('MONITOR')
  })

  it('drops groups whose items are all filtered out', async () => {
    // monitor contains only a private_preview item -> hidden without dev mode
    const { wrapper } = mountSidebar()
    await flushPromises()
    const headers = wrapper.findAll('.sidebar-group-header')
    expect(headers.map((h) => h.text())).not.toContain('MONITOR')
  })

  it('keeps groups with unrestricted items visible when tier info is not loaded', async () => {
    const { wrapper, store } = mountSidebar()
    store.tierRanks = {}
    store.devMode = false
    await flushPromises()
    // core's dashboard/runs are unrestricted, so BUILD still renders even though
    // tier info has not loaded and dev mode is off
    const labels = wrapper.findAll('.sidebar-group-header').map((h) => h.text())
    expect(labels).toContain('BUILD')
  })

  it('persists group collapse state and honours it across mounts', async () => {
    const { wrapper } = mountSidebar()
    await flushPromises()
    // BUILD is default-expanded
    expect(wrapper.find('.sidebar-group-header').attributes('aria-expanded')).toBe('true')
    await wrapper.find('.sidebar-group-header').trigger('click')
    await flushPromises()
    expect(wrapper.find('.sidebar-group-header').attributes('aria-expanded')).toBe('false')
    expect(storedPrefs()).toEqual({ build: true })
  })

  it('renders a collapsed rail with per-group disclosure controls instead of a flat list', async () => {
    const { wrapper } = mountSidebar({ collapsed: true })
    await flushPromises()
    // no full sidebar group headers in the rail
    expect(wrapper.findAll('.sidebar-group-header').length).toBe(0)
    // one disclosure button per visible group (BUILD is the only visible group for a viewer)
    const toggles = wrapper.findAll('.sidebar-group-rail-toggle')
    expect(toggles.length).toBe(1)
    const toggle = toggles[0]
    // BUILD is default-expanded -> aria-expanded true
    expect(toggle.attributes('aria-expanded')).toBe('true')
    expect(toggle.attributes('aria-controls')).toBe('sidebar-group-rail-build')
    expect(toggle.attributes('title')).toBeTruthy()
    expect(toggle.attributes('aria-label')).toBeTruthy()
    // expanded groups render their item links (dashboard and runs for a viewer)
    const links = wrapper.findAll('.sidebar-link')
    expect(links.length).toBe(2)
    expect(links.map((l) => l.attributes('href'))).toEqual(expect.arrayContaining(['/', '/runs']))
    expect(links.every((l) => l.attributes('title'))).toBe(true)
  })

  it('collapses groups in the rail, hiding their items, and persists the toggle', async () => {
    const { wrapper } = mountSidebar({ collapsed: true })
    await flushPromises()
    const toggle = wrapper.find('.sidebar-group-rail-toggle')
    expect(toggle.attributes('aria-expanded')).toBe('true')
    await toggle.trigger('click')
    await flushPromises()
    expect(wrapper.find('.sidebar-group-rail-toggle').attributes('aria-expanded')).toBe('false')
    // a collapsed group renders no item links in the rail
    expect(wrapper.findAll('.sidebar-link').length).toBe(0)
    // persisted for the next mount
    expect(storedPrefs()).toEqual({ build: true })
  })

  it('honours per-group collapse state for system admins in the rail', async () => {
    const { wrapper } = mountSidebar({ collapsed: true, isSystemAdmin: true })
    await flushPromises()
    const toggles = wrapper.findAll('.sidebar-group-rail-toggle')
    expect(toggles.length).toBe(2)
    const byControls = Object.fromEntries(
      toggles.map((b) => [b.attributes('aria-controls'), b.attributes('aria-expanded')]),
    )
    // BUILD is default-expanded, ADMIN is default-collapsed
    expect(byControls['sidebar-group-rail-build']).toBe('true')
    expect(byControls['sidebar-group-rail-admin']).toBe('false')
    // expanded group's links render; collapsed group's links do not
    expect(wrapper.findAll('.sidebar-link').map((l) => l.attributes('href'))).toEqual(
      expect.arrayContaining(['/', '/runs']),
    )
    expect(wrapper.findAll('.sidebar-link').map((l) => l.attributes('href'))).not.toContain(
      '/settings/license',
    )
  })

  describe('touch sizing (collapsed rail)', () => {
    it('keeps the default 40x26 toggle box and 40x40 link boxes in default mode', async () => {
      const { wrapper } = mountSidebar({ collapsed: true })
      await flushPromises()
      const toggle = wrapper.find('.sidebar-group-rail-toggle')
      expect(toggle.exists()).toBe(true)
      expect(toggle.classes()).toContain('sidebar-group-rail-toggle')
      expect(toggle.classes()).not.toContain('sidebar-group-rail-toggle--touch')
      const links = wrapper.findAll('.sidebar-link')
      expect(links.length).toBeGreaterThan(0)
      for (const link of links) {
        expect(link.classes()).toContain('sidebar-link--collapsed')
        expect(link.classes()).not.toContain('sidebar-link--collapsed--touch')
      }
    })

    it('uses the 44x44 toggle box in touch mode', async () => {
      const { wrapper } = mountSidebar({ collapsed: true, size: 'touch' })
      await flushPromises()
      const toggle = wrapper.find('.sidebar-group-rail-toggle')
      expect(toggle.exists()).toBe(true)
      expect(toggle.classes()).toContain('sidebar-group-rail-toggle--touch')
    })

    it('forwards touch sizing to every rail link in touch mode', async () => {
      const { wrapper } = mountSidebar({ collapsed: true, size: 'touch' })
      await flushPromises()
      const links = wrapper.findAll('.sidebar-link')
      expect(links.length).toBeGreaterThan(0)
      for (const link of links) {
        expect(link.classes()).toContain('sidebar-link--collapsed--touch')
      }
    })

    it('drops touch sizing from the expanded sidebar even when size is touch', async () => {
      const { wrapper } = mountSidebar({ size: 'touch' })
      await flushPromises()
      expect(wrapper.findAll('.sidebar-group-rail-toggle').length).toBe(0)
      for (const link of wrapper.findAll('.sidebar-link')) {
        expect(link.classes()).not.toContain('sidebar-link--collapsed--touch')
      }
    })
  })

  it('falls back to the raw group label when the group has no translation key', async () => {
    // groupLabel normally resolves the label through i18n. Guard the fallback
    // for a group that carries no usable key by supplying one directly — the
    // built-in nav cache is populated on first use, so a manifest mutation
    // cannot reach it here.
    const craftedGroup = {
      id: 'build',
      label: 'RAW_BUILD',
      labelKey: '',
      defaultCollapsed: false,
      items: [
        {
          to: '/',
          icon: 'LayoutDashboard',
          label: 'Dashboard',
          labelKey: 'components.SidebarNav.item_dashboard',
          exact: true,
        },
      ],
    }
    const mocked = vi.mocked(getVisibleNavGroups)
    const realImpl = mocked.getMockImplementation()
    mocked.mockReturnValue([craftedGroup] as never)
    try {
      const { wrapper } = mountSidebar({ collapsed: true })
      await flushPromises()

      const toggle = wrapper
        .findAll('.sidebar-group-rail-toggle')
        .find(
          (candidate) => candidate.attributes('aria-controls') === 'sidebar-group-rail-build',
        )!
      // groupToggleLabel prefixes the accessible name with groupLabel(group);
      // an empty key must fall back to the raw label, not the i18n lookup.
      expect(toggle.attributes('aria-label')).toContain('RAW_BUILD')
    } finally {
      if (realImpl) mocked.mockImplementation(realImpl)
      else mocked.mockReset()
    }
  })

  describe('active-route reveal (FAR-2602)', () => {
    it('reveals a never-toggled default-collapsed group holding the active route', async () => {
      setRoute('/settings/license')
      const { wrapper } = mountSidebar({ isSystemAdmin: true })
      await flushPromises()

      // ADMIN ships default-collapsed; the active route lives inside it.
      expect(groupHeader(wrapper, 'ADMIN').attributes('aria-expanded')).toBe('true')
      expect(linkHrefs(wrapper)).toContain('/settings/license')
    })

    it('does not record a preference when it merely reveals an active group', async () => {
      setRoute('/settings/license')
      const { wrapper } = mountSidebar({ isSystemAdmin: true })
      await flushPromises()

      // Revealed on entry, yet nothing was written: a reveal is transient
      // state, not a user choice. If it were persisted the group would stay
      // expanded after navigating away — the behaviour FAR-2602 forbids.
      expect(groupHeader(wrapper, 'ADMIN').attributes('aria-expanded')).toBe('true')
      expect(localStorage.getItem('sidebar-group-prefs')).toBeNull()
    })

    it('keeps an explicitly collapsed active group collapsed when the preference is seeded before mount', async () => {
      // Seed the stored preference the same way a prior session would, then
      // let the module-level useStorage ref observe it before mounting.
      const seeded = JSON.stringify({ admin: true })
      localStorage.setItem('sidebar-group-prefs', seeded)
      window.dispatchEvent(
        new StorageEvent('storage', {
          key: 'sidebar-group-prefs',
          newValue: seeded,
          storageArea: localStorage,
        }),
      )
      setRoute('/settings/license')

      const { wrapper } = mountSidebar({ isSystemAdmin: true })
      await flushPromises()

      expect(groupHeader(wrapper, 'ADMIN').attributes('aria-expanded')).toBe('false')
      expect(linkHrefs(wrapper)).not.toContain('/settings/license')
      expect(storedPrefs()).toEqual({ admin: true })
    })

    it('keeps a group the user explicitly collapsed collapsed while it holds the active route', async () => {
      setRoute('/settings/license')
      const { wrapper } = mountSidebar({ isSystemAdmin: true })
      await flushPromises()
      // Revealed on entry because the user has never toggled it…
      expect(groupHeader(wrapper, 'ADMIN').attributes('aria-expanded')).toBe('true')

      // …collapsing it is an explicit preference, and that must win.
      await groupHeader(wrapper, 'ADMIN').trigger('click')
      await flushPromises()

      expect(groupHeader(wrapper, 'ADMIN').attributes('aria-expanded')).toBe('false')
      expect(linkHrefs(wrapper)).not.toContain('/settings/license')
      expect(storedPrefs()).toEqual({ admin: true })
    })

    it('keeps an explicitly collapsed group collapsed across a remount on the same route', async () => {
      setRoute('/settings/license')
      const first = mountSidebar({ isSystemAdmin: true })
      await flushPromises()
      await groupHeader(first.wrapper, 'ADMIN').trigger('click')
      await flushPromises()
      expect(groupHeader(first.wrapper, 'ADMIN').attributes('aria-expanded')).toBe('false')
      first.wrapper.unmount()

      const second = mountSidebar({ isSystemAdmin: true })
      await flushPromises()
      expect(groupHeader(second.wrapper, 'ADMIN').attributes('aria-expanded')).toBe('false')
      expect(linkHrefs(second.wrapper)).not.toContain('/settings/license')
    })

    it('leaves an expanded group unchanged', async () => {
      setRoute('/settings/license')
      const { wrapper } = mountSidebar({ isSystemAdmin: true })
      await flushPromises()

      // BUILD is default-expanded and does not hold the active route here.
      expect(groupHeader(wrapper, 'BUILD').attributes('aria-expanded')).toBe('true')
      expect(linkHrefs(wrapper)).toContain('/')
    })

    it('reveals the parent group of a detail route that has no nav row of its own', async () => {
      setRoute('/admin/error-log/abc')
      const { wrapper } = mountSidebar({ isSystemAdmin: true })
      await flushPromises()

      const hrefs = linkHrefs(wrapper)
      // The detail route itself has no sidebar row…
      expect(hrefs).not.toContain('/admin/error-log/abc')
      // …yet it is owned by the list row, so it activates and reveals the group.
      expect(groupHeader(wrapper, 'ADMIN').attributes('aria-expanded')).toBe('true')
      expect(hrefs).toContain('/admin/error-log')
    })

    it('reveals the active default-collapsed group in the collapsed rail too', async () => {
      setRoute('/settings/license')
      const { wrapper } = mountSidebar({ collapsed: true, isSystemAdmin: true })
      await flushPromises()

      const adminToggle = wrapper
        .findAll('.sidebar-group-rail-toggle')
        .find((toggle) => toggle.attributes('aria-controls') === 'sidebar-group-rail-admin')!
      expect(adminToggle.attributes('aria-expanded')).toBe('true')
      expect(linkHrefs(wrapper)).toContain('/settings/license')

      // An explicit collapse still wins in the rail.
      await adminToggle.trigger('click')
      await flushPromises()
      const collapsedToggle = wrapper
        .findAll('.sidebar-group-rail-toggle')
        .find((toggle) => toggle.attributes('aria-controls') === 'sidebar-group-rail-admin')!
      expect(collapsedToggle.attributes('aria-expanded')).toBe('false')
      expect(linkHrefs(wrapper)).not.toContain('/settings/license')
    })

    it('collapses an auto-revealed group when the route moves outside it', async () => {
      // The shared route double is a plain object, so a computed cannot track
      // it — supply a reactive route instead. The sidebar mounts once, then the
      // route changes under it, which is the dominant in-app journey.
      const route = reactive({ path: '/settings/license' })
      vi.mocked(useRoute).mockReturnValueOnce(route as never)

      const { wrapper } = mountSidebar({ isSystemAdmin: true })
      await flushPromises()
      expect(groupHeader(wrapper, 'ADMIN').attributes('aria-expanded')).toBe('true')

      route.path = '/runs'
      await nextTick()

      expect(groupHeader(wrapper, 'ADMIN').attributes('aria-expanded')).toBe('false')
      expect(linkHrefs(wrapper)).not.toContain('/settings/license')
      // Navigated away without a toggle, so still no stored preference.
      expect(localStorage.getItem('sidebar-group-prefs')).toBeNull()
    })
  })
})
