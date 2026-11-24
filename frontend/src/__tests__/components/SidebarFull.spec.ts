import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('../../lib/api/client', () => ({
  api: { GET: vi.fn().mockResolvedValue({ data: null, error: undefined }) },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
  clearAccessToken: vi.fn(),
  isDemoSession: vi.fn().mockReturnValue(false),
  onAuthChange: vi.fn(() => vi.fn()),
}))

import SidebarFull from '../../components/SidebarFull.vue'
import { usePlanStore } from '../../stores/planStore'

function mountFull() {
  const pinia = createPinia()
  setActivePinia(pinia)
  usePlanStore().currentTier = 'community'
  return mount(SidebarFull, {
    props: {
      isSystemAdmin: false,
      userEmail: 'a@example.com',
      userInitial: 'A',
      isLight: true,
    },
    global: {
      plugins: [pinia],
      stubs: { NotificationBell: true, RouterLink: { template: '<a><slot /></a>' } },
    },
  })
}

describe('SidebarFull header', () => {
  it('keeps the product name on one line', () => {
    const name = mountFull().get('[data-testid="sidebar-product-name"]')
    expect(name.classes()).toContain('whitespace-nowrap')
  })

  it('renders the product name', () => {
    const name = mountFull().get('[data-testid="sidebar-product-name"]')
    expect(name.text()).toBe('Farna Control')
  })

  it('renders the product name as non-heading text so it never precedes the page h1 (FAR-2715)', () => {
    const wrapper = mountFull()
    const name = wrapper.get('[data-testid="sidebar-product-name"]')
    expect(name.element.tagName).toBe('SPAN')
    expect(wrapper.findAll('h1,h2,h3,h4,h5,h6')).toHaveLength(0)
  })

  it('renders the tier badge', () => {
    const badge = mountFull().get('[data-testid="sidebar-tier-badge"]')
    expect(badge.text()).toBe('Community')
  })

  it('renders the collapse button', () => {
    const button = mountFull().get('[aria-label="Collapse sidebar"]')
    expect(button.attributes('type')).toBe('button')
  })
})
