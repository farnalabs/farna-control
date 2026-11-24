import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { h } from 'vue'
import StateTabs from '../components/shared/StateTabs.vue'
import type { StateTab } from '../components/shared/StateTabs.vue'

function mountTabs(
  props: Record<string, unknown> = {},
  tabs: StateTab[] = [
    { id: 'alpha', label: 'Alpha' },
    { id: 'beta', label: 'Beta' },
    { id: 'gamma', label: 'Gamma' },
  ],
) {
  return mount(StateTabs, {
    props: { tabs, modelValue: 'alpha', ariaLabel: 'Example sections', ...props },
    attachTo: document.body,
  })
}

function tab(wrapper: ReturnType<typeof mountTabs>, testid: string) {
  return wrapper.get(`[data-testid="${testid}"]`)
}

describe('StateTabs', () => {
  it('exposes the tablist and tab roles with a roving tabindex', () => {
    const wrapper = mountTabs({ testidPrefix: 'example' })
    const list = wrapper.get('[role="tablist"]')

    expect(list.attributes('aria-label')).toBe('Example sections')
    expect(list.attributes('aria-orientation')).toBe('horizontal')
    expect(tab(wrapper, 'example-alpha').attributes('role')).toBe('tab')
    expect(tab(wrapper, 'example-alpha').attributes('aria-selected')).toBe('true')
    expect(tab(wrapper, 'example-alpha').attributes('tabindex')).toBe('0')
    expect(tab(wrapper, 'example-beta').attributes('aria-selected')).toBe('false')
    expect(tab(wrapper, 'example-beta').attributes('tabindex')).toBe('-1')
    expect(tab(wrapper, 'example-gamma').attributes('tabindex')).toBe('-1')
    wrapper.unmount()
  })

  it('names each tab from its label and count', () => {
    const wrapper = mountTabs({ testidPrefix: 'example' }, [
      { id: 'alpha', label: 'Org events', count: 12 },
      { id: 'beta', label: 'System events' },
    ])

    expect(tab(wrapper, 'example-alpha').text()).toContain('Org events')
    expect(tab(wrapper, 'example-alpha').text()).toContain('12')
    expect(tab(wrapper, 'example-beta').text()).toContain('System events')
    wrapper.unmount()
  })

  it('composes ids and aria-controls from the supplied prefixes', () => {
    const wrapper = mountTabs({
      idPrefix: 'panel-tab',
      panelIdPrefix: 'panel-body',
      testidPrefix: 'example',
    })

    expect(tab(wrapper, 'example-alpha').attributes('id')).toBe('panel-tab-alpha')
    expect(tab(wrapper, 'example-alpha').attributes('aria-controls')).toBe('panel-body-alpha')
    expect(tab(wrapper, 'example-beta').attributes('aria-controls')).toBe('panel-body-beta')
    wrapper.unmount()
  })

  it('omits aria-controls when no panel is supplied', () => {
    const wrapper = mountTabs({ testidPrefix: 'example' })

    expect(tab(wrapper, 'example-alpha').attributes('aria-controls')).toBeUndefined()
    wrapper.unmount()
  })

  it('points every tab at one shared panel when panelId is given', () => {
    const wrapper = mountTabs({ panelId: 'shared-panel', testidPrefix: 'example' })

    expect(tab(wrapper, 'example-alpha').attributes('aria-controls')).toBe('shared-panel')
    expect(tab(wrapper, 'example-gamma').attributes('aria-controls')).toBe('shared-panel')
    wrapper.unmount()
  })

  it('emits the tab id on click, including a re-click of the active tab', async () => {
    const wrapper = mountTabs({ testidPrefix: 'example' })

    await tab(wrapper, 'example-beta').trigger('click')
    expect(wrapper.emitted('update:modelValue')).toEqual([['beta']])

    await tab(wrapper, 'example-alpha').trigger('click')
    expect(wrapper.emitted('update:modelValue')).toEqual([['beta'], ['alpha']])
    wrapper.unmount()
  })

  it('moves selection and focus with ArrowRight/ArrowLeft, wrapping at both ends', async () => {
    const wrapper = mountTabs({ testidPrefix: 'example' })

    await tab(wrapper, 'example-alpha').trigger('keydown', { key: 'ArrowRight' })
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['beta'])
    expect(document.activeElement).toBe(tab(wrapper, 'example-beta').element)

    await wrapper.setProps({ modelValue: 'gamma' })
    await tab(wrapper, 'example-gamma').trigger('keydown', { key: 'ArrowRight' })
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['alpha'])

    await tab(wrapper, 'example-alpha').trigger('keydown', { key: 'ArrowLeft' })
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['gamma'])
    wrapper.unmount()
  })

  it('jumps to the first and last tab with Home and End', async () => {
    const wrapper = mountTabs({ modelValue: 'beta', testidPrefix: 'example' })

    await tab(wrapper, 'example-beta').trigger('keydown', { key: 'Home' })
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['alpha'])
    expect(document.activeElement).toBe(tab(wrapper, 'example-alpha').element)

    await tab(wrapper, 'example-beta').trigger('keydown', { key: 'End' })
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['gamma'])
    expect(document.activeElement).toBe(tab(wrapper, 'example-gamma').element)
    wrapper.unmount()
  })

  it('skips disabled tabs when moving with arrows, Home and End', async () => {
    const wrapper = mountTabs({ testidPrefix: 'example' }, [
      { id: 'alpha', label: 'Alpha' },
      { id: 'beta', label: 'Beta', disabled: true },
      { id: 'gamma', label: 'Gamma' },
    ])

    expect(tab(wrapper, 'example-beta').attributes('disabled')).toBeDefined()
    expect(tab(wrapper, 'example-beta').attributes('tabindex')).toBe('-1')

    await tab(wrapper, 'example-alpha').trigger('keydown', { key: 'ArrowRight' })
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['gamma'])
    expect(document.activeElement).toBe(tab(wrapper, 'example-gamma').element)

    await tab(wrapper, 'example-gamma').trigger('keydown', { key: 'ArrowLeft' })
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['alpha'])

    await tab(wrapper, 'example-gamma').trigger('keydown', { key: 'Home' })
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['alpha'])

    await tab(wrapper, 'example-alpha').trigger('keydown', { key: 'End' })
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['gamma'])
    wrapper.unmount()
  })

  it('does not activate a disabled tab on click', async () => {
    const wrapper = mountTabs({ testidPrefix: 'example' }, [
      { id: 'alpha', label: 'Alpha' },
      { id: 'beta', label: 'Beta', disabled: true },
    ])

    await tab(wrapper, 'example-beta').trigger('click')
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    wrapper.unmount()
  })

  it('keeps the list tabbable at the first enabled tab when nothing is selected', () => {
    const wrapper = mountTabs({ modelValue: 'missing', testidPrefix: 'example' }, [
      { id: 'alpha', label: 'Alpha', disabled: true },
      { id: 'beta', label: 'Beta' },
      { id: 'gamma', label: 'Gamma' },
    ])

    expect(tab(wrapper, 'example-alpha').attributes('tabindex')).toBe('-1')
    expect(tab(wrapper, 'example-beta').attributes('tabindex')).toBe('0')
    wrapper.unmount()
  })

  it('moves the tab stop to the first enabled tab when the selected tab is disabled', () => {
    const wrapper = mountTabs({ modelValue: 'beta', testidPrefix: 'example' }, [
      { id: 'alpha', label: 'Alpha' },
      { id: 'beta', label: 'Beta', disabled: true },
      { id: 'gamma', label: 'Gamma' },
    ])

    // The disabled tab stays selected but must not own the tab stop.
    expect(tab(wrapper, 'example-beta').attributes('aria-selected')).toBe('true')
    expect(tab(wrapper, 'example-beta').attributes('tabindex')).toBe('-1')
    expect(tab(wrapper, 'example-alpha').attributes('tabindex')).toBe('0')
    expect(tab(wrapper, 'example-gamma').attributes('tabindex')).toBe('-1')
    wrapper.unmount()
  })

  it('keeps a single tab stop on the active tab, not the first tab', () => {
    const wrapper = mountTabs({ modelValue: 'beta', testidPrefix: 'example' })

    expect(tab(wrapper, 'example-alpha').attributes('tabindex')).toBe('-1')
    expect(tab(wrapper, 'example-beta').attributes('tabindex')).toBe('0')
    expect(tab(wrapper, 'example-gamma').attributes('tabindex')).toBe('-1')
    wrapper.unmount()
  })

  it('still exposes one entry point when every tab is disabled', () => {
    const wrapper = mountTabs({ modelValue: 'alpha', testidPrefix: 'example' }, [
      { id: 'alpha', label: 'Alpha', disabled: true },
      { id: 'beta', label: 'Beta', disabled: true },
    ])

    expect(tab(wrapper, 'example-alpha').attributes('tabindex')).toBe('0')
    expect(tab(wrapper, 'example-beta').attributes('tabindex')).toBe('-1')
    wrapper.unmount()
  })

  it('uses Up/Down and a vertical orientation when orientation is vertical', async () => {
    const wrapper = mountTabs({ orientation: 'vertical', testidPrefix: 'example' })

    expect(wrapper.get('[role="tablist"]').attributes('aria-orientation')).toBe('vertical')
    expect(wrapper.classes()).toContain('flex-col')

    await tab(wrapper, 'example-alpha').trigger('keydown', { key: 'ArrowDown' })
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['beta'])

    await tab(wrapper, 'example-beta').trigger('keydown', { key: 'ArrowUp' })
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['alpha'])

    await tab(wrapper, 'example-alpha').trigger('keydown', { key: 'ArrowRight' })
    expect(wrapper.emitted('update:modelValue')).toHaveLength(2)
    wrapper.unmount()
  })

  it('renders the custom label slot per tab', () => {
    const wrapper = mount(StateTabs, {
      props: {
        tabs: [{ id: 'alpha', label: 'Alpha' }, { id: 'beta', label: 'Beta' }],
        modelValue: 'alpha',
        ariaLabel: 'Example sections',
        testidPrefix: 'example',
      },
      slots: {
        tab: ({ tab: slotTab }: { tab: StateTab }) =>
          h('span', { class: 'custom-label' }, `Custom ${slotTab.label}`),
      },
      attachTo: document.body,
    })

    expect(wrapper.get('.custom-label').text()).toBe('Custom Alpha')
    expect(wrapper.get('[role="tablist"]').element.contains(wrapper.get('.custom-label').element)).toBe(true)
    wrapper.unmount()
  })

  it('keeps a per-tab action outside the tablist and claims the tabs with aria-owns', () => {
    const wrapper = mount(StateTabs, {
      props: {
        tabs: [{ id: 'alpha', label: 'Alpha' }, { id: 'beta', label: 'Beta' }],
        modelValue: 'alpha',
        ariaLabel: 'Example sections',
        testidPrefix: 'example',
      },
      slots: {
        'tab-trailing': ({ tab: slotTab }: { tab: StateTab }) =>
          h('button', { 'type': 'button', 'data-testid': `close-${slotTab.id}` }, 'x'),
      },
      attachTo: document.body,
    })

    const tablist = wrapper.get('[role="tablist"]')
    // An extra focusable child inside a tablist fails `aria-required-children`,
    // so the tablist stays empty and owns its tabs by id instead.
    expect(tablist.element.children).toHaveLength(0)
    expect(tablist.attributes('aria-label')).toBe('Example sections')
    expect(tablist.attributes('aria-owns')?.split(' ')).toHaveLength(2)

    const ownedIds = wrapper.get('[role="tablist"]').attributes('aria-owns')!.split(' ')
    expect(ownedIds).toHaveLength(2)
    for (const id of ownedIds) {
      const button = wrapper.get(`#${id}`)
      expect(button.attributes('role')).toBe('tab')
      expect(tablist.element.contains(button.element)).toBe(false)
    }

    // The per-tab action renders beside its tab and is not a tab itself.
    expect(wrapper.get('[data-testid="close-beta"]').attributes('role')).toBeUndefined()
    wrapper.unmount()
  })
})
