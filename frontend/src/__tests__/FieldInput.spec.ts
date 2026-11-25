import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, ref } from 'vue'

import Field from '../components/ui/Field.vue'
import Input from '../components/ui/Input.vue'
import Textarea from '../components/ui/Textarea.vue'

function mountHarness(template: string, setupState: Record<string, unknown> = {}) {
  const Host = defineComponent({
    components: { Field, FieldInput: Input, FieldTextarea: Textarea },
    setup() {
      return setupState
    },
    template,
  })
  return mount(Host)
}

describe('Field', () => {
  it('generates an id and wires the label for and the control aria-describedby to it', () => {
    const wrapper = mountHarness(`
      <Field label="Email" help="We never share it">
        <FieldInput data-testid="control" />
      </Field>
    `)

    const input = wrapper.get('[data-testid="control"]')
    const id = input.attributes('id')
    const help = wrapper.get('p')

    expect(id).toBeTruthy()
    expect(wrapper.get('label').attributes('for')).toBe(id)
    expect(help.attributes('id')).toBe(`${id}-help`)
    expect(input.attributes('aria-describedby')).toBe(`${id}-help`)
  })

  it('generates a distinct id for every Field instance', () => {
    const first = mountHarness('<Field label="First"><FieldInput data-testid="control" /></Field>')
    const second = mountHarness('<Field label="Second"><FieldInput data-testid="control" /></Field>')

    expect(first.get('[data-testid="control"]').attributes('id')).not.toBe(
      second.get('[data-testid="control"]').attributes('id'),
    )
  })

  it('honours an explicit id and explicit help and error ids', () => {
    const wrapper = mountHarness(`
      <Field
        id="email"
        label="Email"
        help="Hint"
        help-id="email-help"
        error="Required"
        error-id="email-error"
      >
        <FieldInput data-testid="control" />
      </Field>
    `)

    const input = wrapper.get('[data-testid="control"]')

    expect(input.attributes('id')).toBe('email')
    expect(wrapper.get('label').attributes('for')).toBe('email')
    expect(wrapper.get('#email-error').text()).toBe('Required')
    expect(input.attributes('aria-describedby')).toBe('email-error')
  })

  it('marks a required field visually and semantically', () => {
    const wrapper = mountHarness(`
      <Field label="Project" required>
        <FieldInput data-testid="control" />
      </Field>
    `)

    expect(wrapper.get('label').text()).toBe('Project *')
    expect(wrapper.get('[data-testid="control"]').attributes('required')).toBeDefined()
  })

  it('sets aria-invalid, announces the error politely and hides the help hint', () => {
    const wrapper = mountHarness(`
      <Field label="Email" help="Hint" error="Email is required">
        <FieldInput data-testid="control" />
      </Field>
    `)

    const input = wrapper.get('[data-testid="control"]')
    const error = wrapper.get('p')

    expect(input.attributes('aria-invalid')).toBe('true')
    expect(input.attributes('aria-describedby')).toBe(error.attributes('id'))
    expect(error.text()).toBe('Email is required')
    expect(error.attributes('aria-live')).toBe('polite')
    expect(wrapper.findAll('p')).toHaveLength(1)
  })

  it('associates help text without announcing it', () => {
    const wrapper = mountHarness(`
      <Field label="Email" help="Only used for receipts">
        <FieldInput data-testid="control" />
      </Field>
    `)

    const help = wrapper.get('p')

    expect(help.text()).toBe('Only used for receipts')
    expect(help.attributes('aria-live')).toBeUndefined()
    expect(wrapper.get('[data-testid="control"]').attributes('aria-invalid')).toBeUndefined()
  })
})

describe('Input', () => {
  it('forwards v-model updates', async () => {
    const value = ref('start')
    const wrapper = mountHarness(
      '<Field label="Name"><FieldInput v-model="value" data-testid="control" /></Field>',
      { value },
    )

    await wrapper.get('[data-testid="control"]').setValue('next')

    expect(value.value).toBe('next')
  })

  it('applies the .number modifier to v-model', async () => {
    const value = ref<number | string>(1)
    const wrapper = mountHarness(
      '<Field label="Count"><FieldInput v-model.number="value" data-testid="control" /></Field>',
      { value },
    )

    await wrapper.get('[data-testid="control"]').setValue('42')
    expect(value.value).toBe(42)

    await wrapper.get('[data-testid="control"]').setValue('')
    expect(value.value).toBe('')
  })

  it('forwards every listener to the underlying control', async () => {
    const onBlur = vi.fn()
    const onInput = vi.fn()
    const onKeydown = vi.fn()
    const wrapper = mountHarness(
      `<Field label="Name">
        <FieldInput
          data-testid="control"
          @blur="onBlur"
          @input="onInput"
          @keydown="onKeydown"
        />
      </Field>`,
      { onBlur, onInput, onKeydown },
    )

    const input = wrapper.get('[data-testid="control"]')
    await input.trigger('blur')
    await input.trigger('keydown', { key: 'Enter' })
    await input.setValue('typed')

    expect(onBlur).toHaveBeenCalledTimes(1)
    expect(onKeydown).toHaveBeenCalledTimes(1)
    expect(onInput).toHaveBeenCalled()
  })

  it('forwards every supported input attribute', () => {
    const wrapper = mountHarness(`
      <Field label="Amount">
        <FieldInput
          data-testid="control"
          type="number"
          placeholder="0.00"
          disabled
          autocomplete="off"
          inputmode="decimal"
          min="1"
          max="9"
          step="0.5"
          maxlength="4"
        />
      </Field>
    `)

    const input = wrapper.get('[data-testid="control"]')

    expect(input.attributes('type')).toBe('number')
    expect(input.attributes('placeholder')).toBe('0.00')
    expect(input.attributes('disabled')).toBeDefined()
    expect(input.attributes('autocomplete')).toBe('off')
    expect(input.attributes('inputmode')).toBe('decimal')
    expect(input.attributes('min')).toBe('1')
    expect(input.attributes('max')).toBe('9')
    expect(input.attributes('step')).toBe('0.5')
    expect(input.attributes('maxlength')).toBe('4')
  })

  it('renders PrimeVue InputText itself, preserving the caller classes', () => {
    const wrapper = mountHarness(`
      <Field label="Name">
        <FieldInput data-testid="control" class="input-base font-mono" />
      </Field>
    `)

    const input = wrapper.get('[data-testid="control"]')

    expect(input.element.tagName).toBe('INPUT')
    expect(input.classes()).toContain('p-inputtext')
    expect(input.classes()).toContain('input-base')
    expect(input.classes()).toContain('font-mono')
  })

  it('works without a Field, passing its id straight through', () => {
    const wrapper = mount(Input, {
      props: { modelValue: 'solo' },
      attrs: { id: 'standalone', 'data-testid': 'control' },
    })

    expect(wrapper.get('[data-testid="control"]').attributes('id')).toBe('standalone')
    expect(wrapper.element.tagName).toBe('INPUT')
  })
})

describe('Textarea', () => {
  it('uses the same Field wiring and forwards rows, classes and listeners', async () => {
    const value = ref('draft')
    const onBlur = vi.fn()
    const wrapper = mountHarness(
      `<Field label="Notes" help="Markdown is fine">
        <FieldTextarea
          v-model="value"
          rows="4"
          class="input-base font-mono"
          data-testid="control"
          @blur="onBlur"
        />
      </Field>`,
      { value, onBlur },
    )

    const control = wrapper.get('[data-testid="control"]')
    const id = control.attributes('id')

    expect(control.element.tagName).toBe('TEXTAREA')
    expect(control.attributes('rows')).toBe('4')
    expect(wrapper.get('label').attributes('for')).toBe(id)
    expect(control.attributes('aria-describedby')).toBe(`${id}-help`)
    expect(control.classes()).toContain('font-mono')

    await control.setValue('edited')
    await control.trigger('blur')

    expect(value.value).toBe('edited')
    expect(onBlur).toHaveBeenCalledTimes(1)
  })
})
