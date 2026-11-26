import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, ref } from 'vue'

import Checkbox from '../components/ui/Checkbox.vue'
import Field from '../components/ui/Field.vue'

function mountHarness(template: string, setupState: Record<string, unknown> = {}) {
  const Host = defineComponent({
    components: { UiCheckbox: Checkbox, Field },
    setup() {
      return setupState
    },
    template,
  })
  return mount(Host)
}

describe('Checkbox', () => {
  it('wires the visible label to the control and emits the flipped state', async () => {
    const wrapper = mountHarness(
      '<UiCheckbox v-model="active" label="Active" data-testid="control" />',
      { active: ref(false) },
    )
    const control = wrapper.get('[data-testid="control"]')
    const label = wrapper.get('label')

    // The label is associated with the control (same id) and its text is the
    // accessible name; the control is a real native checkbox.
    expect(label.attributes('for')).toBe(control.attributes('id'))
    expect(label.text()).toBe('Active')
    expect(control.attributes('type')).toBe('checkbox')

    await control.setValue(true)

    expect(control.element).toBeInstanceOf(HTMLInputElement)
    expect((control.element as HTMLInputElement).checked).toBe(true)
    expect(wrapper.vm.active).toBe(true)
  })

  it('reflects the bound model value on the native control', () => {
    const checked = mountHarness('<UiCheckbox v-model="on" label="On" data-testid="control" />', { on: ref(true) })
    const unchecked = mountHarness('<UiCheckbox v-model="on" label="On" data-testid="control" />', { on: ref(false) })

    expect((checked.get('[data-testid="control"]').element as HTMLInputElement).checked).toBe(true)
    expect((unchecked.get('[data-testid="control"]').element as HTMLInputElement).checked).toBe(false)
  })

  it('passes disabled through to the native control and blocks the change', async () => {
    const wrapper = mountHarness(
      '<UiCheckbox v-model="on" label="On" disabled data-testid="control" />',
      { on: ref(false) },
    )
    const control = wrapper.get('[data-testid="control"]')

    expect(control.attributes('disabled')).toBeDefined()
    expect((control.element as HTMLInputElement).disabled).toBe(true)
  })

  it('sets the indeterminate DOM property, which is not an attribute', () => {
    const wrapper = mountHarness(
      '<UiCheckbox v-model="on" label="Partially selected" indeterminate data-testid="control" />',
      { on: ref(false) },
    )
    const control = wrapper.get('[data-testid="control"]')

    expect((control.element as HTMLInputElement).indeterminate).toBe(true)
  })

  it('uses aria-label as the accessible name in the bare form', () => {
    const wrapper = mountHarness('<UiCheckbox v-model="on" bare aria-label="Select row" data-testid="control" />', {
      on: ref(false),
    })
    const control = wrapper.get('[data-testid="control"]')

    // Bare: the control is the root, with no label wrapper.
    expect(wrapper.element.tagName).toBe('INPUT')
    expect(control.attributes('aria-label')).toBe('Select row')
    expect(wrapper.find('label').exists()).toBe(false)
  })

  it('applies the caller class to the control in the bare form', () => {
    const wrapper = mountHarness(
      '<UiCheckbox v-model="on" bare aria-label="Select row" class="h-4 w-4" data-testid="control" />',
      { on: ref(false) },
    )
    const control = wrapper.get('[data-testid="control"]')

    expect(control.classes()).toContain('h-4')
    expect(control.classes()).toContain('w-4')
  })

  it('renders help text and references it from a bare control', () => {
    const wrapper = mountHarness(
      '<UiCheckbox v-model="on" bare aria-label="Select row" help="Applies to every row" data-testid="control" />',
      { on: ref(false) },
    )
    const control = wrapper.get('[data-testid="control"]')
    const help = wrapper.get('p')

    expect(help.text()).toBe('Applies to every row')
    expect(help.attributes('id')).toBe(`${control.attributes('id')}-help`)
    expect(control.attributes('aria-describedby')).toBe(help.attributes('id'))
  })

  it('renders rich label content through the default slot instead of the label prop', () => {
    const wrapper = mountHarness(
      '<UiCheckbox v-model="on" data-testid="control"><span class="rich">Rich label</span></UiCheckbox>',
      { on: ref(false) },
    )

    expect(wrapper.get('label').text()).toBe('Rich label')
    expect(wrapper.find('.rich').exists()).toBe(true)
  })

  it('associates optional help text with the control via aria-describedby', () => {
    const wrapper = mountHarness(
      '<UiCheckbox v-model="on" label="Enable" help="Only applies to new runs" />',
      { on: ref(false) },
    )
    const control = wrapper.get('input')
    const help = wrapper.get('p')

    expect(help.text()).toBe('Only applies to new runs')
    expect(help.attributes('id')).toBe(`${control.attributes('id')}-help`)
    expect(control.attributes('aria-describedby')).toBe(help.attributes('id'))
  })

  it('takes its id, description and invalid state from an enclosing Field', () => {
    const wrapper = mountHarness(
      `<Field label="Notifications" help="Email only">
        <UiCheckbox v-model="on" label="Enabled" data-testid="control" />
      </Field>`,
      { on: ref(false) },
    )
    const control = wrapper.get('[data-testid="control"]')

    expect(control.attributes('id')).toBeTruthy()
    expect(wrapper.get('label').attributes('for')).toBe(control.attributes('id'))
    expect(control.attributes('aria-describedby')).toContain(`${control.attributes('id')}-help`)
  })

  it('marks the control aria-invalid when the enclosing Field has an error', () => {
    const wrapper = mountHarness(
      `<Field label="Accept" error="Required">
        <UiCheckbox v-model="on" label="I agree" data-testid="control" />
      </Field>`,
      { on: ref(false) },
    )

    expect(wrapper.get('[data-testid="control"]').attributes('aria-invalid')).toBe('true')
  })

  it('honours an explicit string aria-invalid attribute over the Field state', () => {
    const wrapper = mountHarness(
      '<UiCheckbox v-model="on" label="On" aria-invalid="grammar" data-testid="control" />',
      { on: ref(false) },
    )

    expect(wrapper.get('[data-testid="control"]').attributes('aria-invalid')).toBe('grammar')
  })

  it('honours an explicit boolean aria-invalid attribute over the Field state', () => {
    const wrapper = mountHarness(
      '<UiCheckbox v-model="on" label="On" :aria-invalid="true" data-testid="control" />',
      { on: ref(false) },
    )

    expect(wrapper.get('[data-testid="control"]').attributes('aria-invalid')).toBe('true')
  })

  it('puts the caller class on the label wrapper and the control classes on the control', () => {
    const wrapper = mountHarness(
      '<UiCheckbox v-model="on" label="On" class="flex items-center gap-2" control-class="h-4 w-4" data-testid="control" />',
      { on: ref(false) },
    )

    expect(wrapper.get('label').classes()).toContain('flex')
    expect(wrapper.get('label').classes()).toContain('gap-2')
    expect(wrapper.get('[data-testid="control"]').classes()).toContain('h-4')
    expect(wrapper.get('[data-testid="control"]').classes()).toContain('w-4')
    expect(wrapper.get('label').classes()).not.toContain('h-4')
  })

  it('forwards extra attributes such as value and name to the control', () => {
    const wrapper = mountHarness(
      '<UiCheckbox v-model="on" label="Operator" value="operator" name="roles" data-testid="control" />',
      { on: ref(false) },
    )
    const control = wrapper.get('[data-testid="control"]')

    expect(control.attributes('value')).toBe('operator')
    expect(control.attributes('name')).toBe('roles')
  })

  it('generates a distinct id for every instance', () => {
    const first = mountHarness('<UiCheckbox v-model="on" label="First" data-testid="a" />', { on: ref(false) })
    const second = mountHarness('<UiCheckbox v-model="on" label="Second" data-testid="b" />', { on: ref(false) })

    expect(first.get('[data-testid="a"]').attributes('id')).not.toBe(second.get('[data-testid="b"]').attributes('id'))
  })
})
