import { describe, it, expect } from 'vitest'
import { nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import SidebarGroup from '../../components/SidebarGroup.vue'

function mountGroup(props = {}) {
  return mount(SidebarGroup, {
    props: {
      id: 'core',
      label: 'BUILD',
      collapsed: false,
      ...props,
    },
    slots: {
      default: '<a href="/">Dashboard</a>',
    },
  })
}

function mountAttachedGroup(props = {}) {
  return mount(SidebarGroup, {
    props: {
      id: 'core',
      label: 'BUILD',
      collapsed: false,
      ...props,
    },
    slots: {
      default: '<a href="/">Dashboard</a>',
    },
    attachTo: document.body,
  })
}

describe('SidebarGroup', () => {
  it('renders the label and emits toggle on header click', async () => {
    const wrapper = mountGroup()
    expect(wrapper.text()).toContain('BUILD')
    await wrapper.find('.sidebar-group-header').trigger('click')
    expect(wrapper.emitted('toggle')).toHaveLength(1)
  })

  it('exposes expanded/collapsed state via aria attributes', () => {
    const expanded = mountGroup({ collapsed: false })
    expect(expanded.find('.sidebar-group-header').attributes('aria-expanded')).toBe('true')
    expect(expanded.find('.sidebar-group-items').attributes('aria-label')).toBe('BUILD')

    const collapsed = mountGroup({ collapsed: true })
    expect(collapsed.find('.sidebar-group-header').attributes('aria-expanded')).toBe('false')
  })

  it('shows slot content only while expanded', () => {
    const expanded = mountGroup({ collapsed: false })
    expect(expanded.text()).toContain('Dashboard')

    const collapsed = mountGroup({ collapsed: true })
    expect(collapsed.find('.sidebar-group-items').isVisible()).toBe(false)
  })

  it('applies active styling when the group is active', () => {
    const wrapper = mountGroup({ isActive: true })
    expect(wrapper.find('.sidebar-group-header').classes()).toContain('sidebar-group-header--active')
  })

  it('moves focus from a focused link to the header when the group collapses', async () => {
    const wrapper = mountAttachedGroup()
    const link = wrapper.find('.sidebar-group-items a').element as HTMLElement
    link.focus()
    expect(document.activeElement).toBe(link)

    // An auto-revealed group collapses when the route leaves it. The focused
    // link becomes display:none, so focus must not fall through to <body>.
    await wrapper.setProps({ collapsed: true })
    await nextTick()

    expect(document.activeElement).toBe(wrapper.find('.sidebar-group-header').element)
    wrapper.unmount()
  })

  it('leaves focus alone when the collapsing group does not contain it', async () => {
    const wrapper = mountAttachedGroup()
    const outside = document.createElement('button')
    document.body.appendChild(outside)
    outside.focus()
    expect(document.activeElement).toBe(outside)

    await wrapper.setProps({ collapsed: true })
    await nextTick()

    expect(document.activeElement).toBe(outside)
    wrapper.unmount()
    outside.remove()
  })
})
