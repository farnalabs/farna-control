import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'

import Badge from '../components/ui/Badge.vue'

describe('Badge', () => {
  it('maps every semantic variant onto its token class', () => {
    const cases = [
      ['neutral', 'badge-status-muted'],
      ['success', 'badge-status-success'],
      ['warning', 'badge-status-warning'],
      ['danger', 'badge-status-destructive'],
      ['info', 'badge-status-primary'],
    ] as const

    for (const [variant, expected] of cases) {
      const wrapper = mount(Badge, { props: { variant }, slots: { default: 'State' } })
      expect(wrapper.classes()).toContain('badge')
      expect(wrapper.classes()).toContain(expected)
    }
  })

  it('defaults to the neutral variant', () => {
    const wrapper = mount(Badge, { slots: { default: 'State' } })
    expect(wrapper.classes()).toContain('badge-status-muted')
    expect(wrapper.classes()).not.toContain('badge-status-success')
  })

  it('renders its slot content', () => {
    const wrapper = mount(Badge, { props: { variant: 'success' }, slots: { default: 'Active' } })
    expect(wrapper.text()).toBe('Active')
  })

  it('falls attributes through to the root span', () => {
    const wrapper = mount(Badge, {
      props: { variant: 'warning' },
      attrs: { 'data-testid': 'sample-state', class: 'capitalize whitespace-nowrap' },
      slots: { default: 'Partial' },
    })

    // The variants the migrated call keeps: the test id lands on the pill
    // itself, and the caller's own utility classes merge with the primitive's.
    expect(wrapper.attributes('data-testid')).toBe('sample-state')
    expect(wrapper.classes()).toContain('badge')
    expect(wrapper.classes()).toContain('badge-status-warning')
    expect(wrapper.classes()).toContain('capitalize')
    expect(wrapper.classes()).toContain('whitespace-nowrap')
  })

  it('renders a single root span so existing element selectors keep matching', () => {
    const wrapper = mount(Badge, { props: { variant: 'danger' }, slots: { default: 'Failed' } })
    expect(wrapper.element.tagName).toBe('SPAN')
    expect(wrapper.findAll('span')).toHaveLength(1)
  })
})
