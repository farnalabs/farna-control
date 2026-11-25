import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'

import Notice from '../components/shared/Notice.vue'

describe('Notice', () => {
  it.each([
    ['success', ['border-success/50', 'bg-success/10', 'text-success-text']],
    ['warning', ['border-warning/40', 'bg-warning/10', 'text-warning-text']],
    ['danger', ['border-destructive/50', 'bg-destructive/10', 'text-destructive-text']],
    ['info', ['border-primary/20', 'bg-primary/5', 'text-primary-text']],
  ] as const)('renders the %s token pair', (variant, tokens) => {
    const wrapper = mount(Notice, { props: { variant, message: 'hello' } })

    for (const token of tokens) expect(wrapper.classes()).toContain(token)
  })

  it('renders a variant icon by default and suppresses it when icon is false', () => {
    const withIcon = mount(Notice, { props: { variant: 'warning', message: 'careful' } })
    const withoutIcon = mount(Notice, { props: { variant: 'warning', message: 'careful', icon: false } })

    expect(withIcon.find('svg').exists()).toBe(true)
    expect(withoutIcon.find('svg').exists()).toBe(false)
  })

  it('renders title and message, and lets the default slot carry rich body content', () => {
    const titled = mount(Notice, { props: { title: 'Saved', message: 'All good' } })
    expect(titled.find('p.font-semibold').text()).toBe('Saved')
    expect(titled.text()).toContain('All good')

    const slotted = mount(Notice, {
      props: { variant: 'info' },
      slots: { default: '<ul><li>first</li><li>second</li></ul>' },
    })
    expect(slotted.findAll('li').length).toBe(2)
  })

  it('is not a live region by default, and opts in when live is passed', () => {
    const staticBanner = mount(Notice, { props: { message: 'static' } })
    expect(staticBanner.attributes('aria-live')).toBeUndefined()
    expect(staticBanner.attributes('role')).toBeUndefined()

    const polite = mount(Notice, { props: { message: 'dynamic', live: 'polite' } })
    expect(polite.attributes('aria-live')).toBe('polite')
    expect(polite.attributes('role')).toBeUndefined()

    const assertive = mount(Notice, { props: { message: 'urgent', live: 'assertive' } })
    expect(assertive.attributes('aria-live')).toBe('assertive')
    expect(assertive.attributes('role')).toBe('alert')
  })

  it('emits dismiss and invokes onDismiss when the dismiss control is clicked', async () => {
    const onDismiss = vi.fn()
    const wrapper = mount(Notice, { props: { message: 'done', dismissible: true, onDismiss } })

    const dismiss = wrapper.find('[data-testid="notice-dismiss"]')
    expect(dismiss.attributes('aria-label')).toBe('Dismiss')
    await dismiss.trigger('click')

    expect(wrapper.emitted('dismiss')).toHaveLength(1)
    expect(onDismiss).toHaveBeenCalledTimes(1)
  })

  it('keeps an existing data-testid and class on the root through attribute fall-through', () => {
    const wrapper = mount(Notice, {
      props: { message: 'saved' },
      attrs: { 'data-testid': 'my-notice', class: 'mb-4 text-sm' },
    })

    expect(wrapper.attributes('data-testid')).toBe('my-notice')
    expect(wrapper.classes()).toContain('mb-4')
    expect(wrapper.classes()).toContain('text-sm')
  })
})
