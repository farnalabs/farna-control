import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, ref } from 'vue'

import SegmentedControl from '../components/ui/SegmentedControl.vue'

const OPTIONS = [
  { value: 'organisation', label: 'Organisation', testid: 'scope-organisation' },
  { value: 'instance', label: 'Instance', testid: 'scope-instance' },
]

function mountControl(props: Record<string, unknown> = {}) {
  return mount(SegmentedControl, {
    props: { modelValue: 'organisation', options: OPTIONS, ariaLabel: 'Scope', ...props },
  })
}

describe('SegmentedControl', () => {
  it('exposes a named group of native toggle buttons with per-option aria-pressed', () => {
    const wrapper = mountControl()

    const group = wrapper.get('[role="group"]')
    expect(group.attributes('aria-label')).toBe('Scope')
    expect(group.attributes('aria-labelledby')).toBeUndefined()

    const buttons = wrapper.findAll('button')
    expect(buttons).toHaveLength(2)
    expect(buttons[0].attributes('type')).toBe('button')
    expect(buttons[0].attributes('aria-pressed')).toBe('true')
    expect(buttons[1].attributes('aria-pressed')).toBe('false')
  })

  it('names the group from a supplied labelled-by id instead of an aria-label', () => {
    const wrapper = mount(SegmentedControl, {
      props: { modelValue: 'organisation', options: OPTIONS, ariaLabelledBy: 'scope-heading' },
    })

    const group = wrapper.get('[role="group"]')
    expect(group.attributes('aria-labelledby')).toBe('scope-heading')
    expect(group.attributes('aria-label')).toBeUndefined()
  })

  it('emits the selected value once and keeps selection single', async () => {
    const wrapper = mountControl()

    await wrapper.get('[data-testid="scope-instance"]').trigger('click')
    expect(wrapper.emitted('update:modelValue')).toEqual([['instance']])

    await wrapper.setProps({ modelValue: 'instance' })
    expect(wrapper.get('[data-testid="scope-instance"]').attributes('aria-pressed')).toBe('true')
    expect(wrapper.get('[data-testid="scope-organisation"]').attributes('aria-pressed')).toBe('false')

    // Re-selecting the active option emits nothing: it is already the value.
    await wrapper.get('[data-testid="scope-instance"]').trigger('click')
    expect(wrapper.emitted('update:modelValue')).toEqual([['instance']])
  })

  it('supports v-model for single selection', async () => {
    const Host = defineComponent({
      components: { SegmentedControl },
      setup() {
        const scope = ref('organisation')
        return { scope, options: OPTIONS }
      },
      template: '<SegmentedControl v-model="scope" :options="options" aria-label="Scope" />',
    })

    const wrapper = mount(Host)
    await wrapper.get('[data-testid="scope-instance"]').trigger('click')

    expect(wrapper.get('[data-testid="scope-instance"]').attributes('aria-pressed')).toBe('true')
    expect(wrapper.get('[data-testid="scope-organisation"]').attributes('aria-pressed')).toBe('false')
  })

  it('keeps the caller classes for layout, selected and unselected states', () => {
    const wrapper = mountControl({
      class: 'inline-flex rounded-lg border',
      optionClass: 'rounded-md px-3',
      selectedClass: 'bg-muted text-foreground',
      unselectedClass: 'text-muted-foreground hover:text-foreground',
    })

    expect(wrapper.classes()).toContain('inline-flex')
    expect(wrapper.classes()).toContain('rounded-lg')

    const selected = wrapper.get('[data-testid="scope-organisation"]')
    const unselected = wrapper.get('[data-testid="scope-instance"]')
    expect(selected.classes()).toContain('rounded-md')
    expect(selected.classes()).toContain('bg-muted')
    expect(selected.classes()).not.toContain('text-muted-foreground')
    expect(unselected.classes()).toContain('rounded-md')
    expect(unselected.classes()).toContain('text-muted-foreground')
    expect(unselected.classes()).not.toContain('bg-muted')
  })

  it('disables every option, and a per-option disabled flag only that option', () => {
    const all = mountControl({ disabled: true })
    expect(all.findAll('button').every((button) => button.attributes('disabled') !== undefined)).toBe(true)

    const one = mountControl({
      options: [OPTIONS[0], { ...OPTIONS[1], disabled: true }],
    })
    expect(one.get('[data-testid="scope-organisation"]').attributes('disabled')).toBeUndefined()
    expect(one.get('[data-testid="scope-instance"]').attributes('disabled')).toBeDefined()
  })

  it('leaves keyboard activation to the native buttons', async () => {
    const wrapper = mountControl()

    // Native <button> elements activate on Enter and Space; the component adds
    // no keydown handling that could pre-empt or duplicate that.
    expect(wrapper.find('[role="group"]').attributes('tabindex')).toBeUndefined()
    await wrapper.get('[data-testid="scope-instance"]').trigger('click')
    expect(wrapper.emitted('update:modelValue')).toEqual([['instance']])
  })

  it('falls other attributes such as data-testid through to the group root', () => {
    const wrapper = mountControl({ 'data-testid': 'scope-control', id: 'scope-group' })

    expect(wrapper.attributes('data-testid')).toBe('scope-control')
    expect(wrapper.attributes('id')).toBe('scope-group')
  })
})
