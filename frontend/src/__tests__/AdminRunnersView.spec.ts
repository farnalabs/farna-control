import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import AdminRunnersView from '../views/AdminRunnersView.vue'

const getMock = vi.hoisted(() => vi.fn())

// FAR-2723: the route decides whether the layout title is the page heading or
// a context label above a nested editor that renders its own <h1>.
const routeState = vi.hoisted(() => ({
  name: 'admin-runners-profiles' as string,
  path: '/admin/runners/profiles',
  params: {} as Record<string, string>,
  query: {} as Record<string, string>,
}))

vi.mock('../lib/api/client', () => ({
  api: { GET: getMock },
}))

vi.mock('../components/FeatureGate.vue', () => ({
  default: { template: '<div data-testid="feature-gate"><slot /></div>' },
}))

vi.mock('../components/PageTabs.vue', () => ({
  default: {
    name: 'PageTabs',
    props: ['tabs'],
    template: '<nav><a v-for="tab in tabs" :key="tab.to" :href="tab.to">{{ tab.label }}</a></nav>',
  },
}))

vi.mock('../components/runners/RunnerStatusStrip.vue', () => ({
  default: {
    name: 'RunnerStatusStrip',
    props: ['status', 'errored'],
    template: '<div data-testid="runner-status-strip">{{ errored ? "failed" : status?.aggregate_state ?? "loading" }}</div>',
  },
}))

vi.mock('../components/shared/ErrorAlert.vue', () => ({
  default: {
    name: 'ErrorAlert',
    props: ['message'],
    template: '<div data-testid="runner-status-error">{{ message }}</div>',
  },
}))

vi.mock('vue-router', () => ({
  RouterView: {
    template: '<div data-testid="router-view"><slot :Component="null" /></div>',
  },
  useRoute: () => routeState,
  useRouter: () => ({ push: vi.fn() }),
}))

const status = {
  aggregate_state: 'healthy',
  profiles: [{ provider_type: 'runner_docker' }],
  machines: [],
}

describe('AdminRunnersView', () => {
  beforeEach(() => {
    getMock.mockReset()
    getMock.mockResolvedValue({ data: status, error: undefined })
    routeState.name = 'admin-runners-profiles'
    routeState.path = '/admin/runners/profiles'
    routeState.params = {}
    routeState.query = {}
  })

  it('loads runner status and renders both runner navigation tabs', async () => {
    const wrapper = mount(AdminRunnersView, {
      global: { stubs: { RouterView: { template: '<div><slot :Component="null" /></div>' } } },
    })
    await flushPromises()

    expect(getMock).toHaveBeenCalledWith('/api/v1/runners/status')
    expect(wrapper.find('a[href="/admin/runners/profiles"]').exists()).toBe(true)
    expect(wrapper.find('a[href="/admin/runners/concurrency"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="runner-status-strip"]').text()).toBe('healthy')
  })

  it('renders the status error and failed strip when the API request fails', async () => {
    getMock.mockResolvedValue({ data: undefined, error: { detail: 'runner service unavailable' } })

    const wrapper = mount(AdminRunnersView, {
      global: { stubs: { RouterView: { template: '<div><slot :Component="null" /></div>' } } },
    })
    await flushPromises()

    expect(wrapper.find('[data-testid="runner-status-error"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="runner-status-error"]').text()).toContain('runner status unavailable')
    expect(wrapper.find('[data-testid="runner-status-strip"]').text()).toBe('failed')
  })

  describe('page heading structure (FAR-2723)', () => {
    // The nested editor routes render EnvironmentProfileForm, whose own
    // PageHeader is the page title. This stub stands in for that child <h1>
    // (its single heading is pinned by the EnvironmentProfileForm spec).
    function mountWithEditorChild(childTitle?: string) {
      const child = childTitle
        ? `<h1 data-page-heading>${childTitle}</h1>`
        : ''
      return mount(AdminRunnersView, {
        global: {
          stubs: {
            RouterView: {
              template: `<div data-testid="router-view">${child}<slot :Component="null" /></div>`,
            },
          },
        },
      })
    }

    it('keeps the Runners title as the only <h1> on the profiles list route', async () => {
      const wrapper = mountWithEditorChild()
      await flushPromises()

      const headings = wrapper.findAll('h1')
      expect(headings).toHaveLength(1)
      expect(headings[0].text()).toBe('Runners')
    })

    it('renders exactly one <h1> on the new-profile editor route — the form title, not the layout title', async () => {
      routeState.name = 'admin-runners-profile-new'
      routeState.path = '/admin/runners/profiles/new'

      const wrapper = mountWithEditorChild('New Environment Profile')
      await flushPromises()

      const headings = wrapper.findAll('h1')
      expect(headings).toHaveLength(1)
      expect(headings[0].text()).toBe('New Environment Profile')
      // Only the real page title claims the route-change focus target.
      const pageHeadings = wrapper.findAll('[data-page-heading]')
      expect(pageHeadings).toHaveLength(1)
      expect(pageHeadings[0].text()).toBe('New Environment Profile')
      // The layout title stays visible, just demoted to a context label.
      expect(wrapper.text()).toContain('Runners')
    })

    it('renders exactly one <h1> on the edit-profile editor route — the form title, not the layout title', async () => {
      routeState.name = 'admin-runners-profile-edit'
      routeState.path = '/admin/runners/profiles/prof-1/edit'
      routeState.params = { id: 'prof-1' }

      const wrapper = mountWithEditorChild('Edit Environment Profile')
      await flushPromises()

      const headings = wrapper.findAll('h1')
      expect(headings).toHaveLength(1)
      expect(headings[0].text()).toBe('Edit Environment Profile')
      expect(wrapper.findAll('[data-page-heading]')).toHaveLength(1)
      expect(wrapper.text()).toContain('Runners')
    })
  })
})
