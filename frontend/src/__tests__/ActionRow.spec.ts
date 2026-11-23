import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ActionRow from '../components/shared/ActionRow.vue'
import CardHeader from '../components/ui/CardHeader.vue'

describe('ActionRow', () => {
  it('stacks below sm and becomes a row at sm', () => {
    const wrapper = mount(ActionRow, { slots: { default: 'Content', actions: '<button>Go</button>' } })
    const classes = wrapper.classes()
    expect(classes).toContain('flex-col')
    expect(classes).toContain('sm:flex-row')
    expect(classes).toContain('sm:justify-between')
  })

  it('lets content shrink and the actions wrap', () => {
    const wrapper = mount(ActionRow, { slots: { default: 'Content', actions: '<button>Go</button>' } })
    expect(wrapper.get('[data-testid="action-row-content"]').classes()).toContain('min-w-0')
    const actions = wrapper.get('[data-testid="action-row-actions"]').classes()
    expect(actions).toContain('flex-wrap')
    expect(actions).toContain('sm:flex-nowrap')
    expect(actions).toContain('actions-nowrap')
  })

  it('does not apply actions-nowrap to the root or the content wrapper', () => {
    const wrapper = mount(ActionRow, { slots: { default: '<a href="#">long link</a>', actions: '<button>Go</button>' } })
    expect(wrapper.classes()).not.toContain('actions-nowrap')
    expect(wrapper.get('[data-testid="action-row-content"]').classes()).not.toContain('actions-nowrap')
  })

  it('omits the actions block without an actions slot', () => {
    const wrapper = mount(ActionRow, { slots: { default: 'Content' } })
    expect(wrapper.find('[data-testid="action-row-actions"]').exists()).toBe(false)
  })
})

describe('CardHeader mobile layout (FAR-2537)', () => {
  it('stacks on mobile and restores the row layout at sm', () => {
    const wrapper = mount(CardHeader, { props: { title: 'T' }, slots: { actions: '<button>Act</button>' } })
    const classes = wrapper.classes()
    expect(classes).toContain('flex-col')
    expect(classes).toContain('sm:flex-row')
    expect(classes).toContain('sm:items-start')
    expect(classes).toContain('sm:justify-between')
  })

  it('gives the actions full width that wraps below sm and stays a no-shrink row at sm', () => {
    const wrapper = mount(CardHeader, { props: { title: 'T' }, slots: { actions: '<button>Act</button>' } })
    const actions = wrapper.get('[data-testid^="card-header-actions-"]').classes()
    expect(actions).toContain('w-full')
    expect(actions).toContain('flex-wrap')
    expect(actions).toContain('sm:w-auto')
    expect(actions).toContain('sm:shrink-0')
    expect(actions).toContain('sm:flex-nowrap')
    expect(actions).toContain('actions-nowrap')
  })
})
