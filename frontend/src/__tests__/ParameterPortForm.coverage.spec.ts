import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ParameterPortForm from '../components/pipeline/composite/ParameterPortForm.vue'
import type { ParameterPort } from '../types/pipeline'

function makePort(type: ParameterPort['type'], default_value?: unknown, multiline = false, options: ParameterPort['options'] = []): ParameterPort {
  return {
    id: 'p1',
    name: 'param',
    label: 'Param',
    type,
    required: false,
    default_value,
    multiline,
    options,
    target_injection: { mode: 'prompt_replace', node_id: '', injection_point: 'prompt_template' },
  }
}

describe('ParameterPortForm resolveDefaultFallback (SonarCloud coverage)', () => {
  it('falls back to false for boolean ports with no value', () => {
    const wrapper = mount(ParameterPortForm, {
      props: { port: makePort('boolean'), modelValue: undefined },
    })
    expect((wrapper.vm as any).localValue).toBe(false)
  })

  it('falls back to 0 for number ports with no value', () => {
    const wrapper = mount(ParameterPortForm, {
      props: { port: makePort('number'), modelValue: undefined },
    })
    expect((wrapper.vm as any).localValue).toBe(0)
  })

  it('falls back to empty string for string ports with no value', () => {
    const wrapper = mount(ParameterPortForm, {
      props: { port: makePort('string'), modelValue: undefined },
    })
    expect((wrapper.vm as any).localValue).toBe('')
  })

  it('prefers an explicit default_value over the fallback', () => {
    const wrapper = mount(ParameterPortForm, {
      props: { port: makePort('string', 'preset'), modelValue: undefined },
    })
    expect((wrapper.vm as any).localValue).toBe('preset')
  })

  it('emits changes for each control and renders reference placeholders', async () => {
    const stringWrapper = mount(ParameterPortForm, { props: { port: makePort('string'), modelValue: '' } })
    await stringWrapper.find('input').setValue('hello')
    expect(stringWrapper.emitted('update:modelValue')?.at(-1)).toEqual(['hello'])

    const numberWrapper = mount(ParameterPortForm, { props: { port: makePort('number'), modelValue: 0 } })
    await numberWrapper.find('input').setValue('4.5')
    expect(numberWrapper.emitted('update:modelValue')?.at(-1)).toEqual([4.5])

    const booleanWrapper = mount(ParameterPortForm, { props: { port: makePort('boolean'), modelValue: false } })
    const checkbox = booleanWrapper.find('input[type="checkbox"]')
    ;(checkbox.element as HTMLInputElement).checked = true
    await checkbox.trigger('change')
    expect(booleanWrapper.emitted('update:modelValue')?.at(-1)).toEqual([true])

    // The select-typed port renders the design-system AppSelect, so a choice is
    // an `update:modelValue` emit on the inner Select rather than a native
    // <select> change.
    const selectWrapper = mount(ParameterPortForm, { props: { port: makePort('select', undefined, false, [{ label: 'One', value: 'one' }]), modelValue: '' } })
    selectWrapper.findComponent({ name: 'Select' }).vm.$emit('update:modelValue', 'one')
    await selectWrapper.vm.$nextTick()
    expect(selectWrapper.emitted('update:modelValue')?.at(-1)).toEqual(['one'])

    const multilineWrapper = mount(ParameterPortForm, { props: { port: makePort('string', undefined, true), modelValue: '' } })
    await multilineWrapper.find('textarea').setValue('long text')
    expect(multilineWrapper.emitted('update:modelValue')?.at(-1)).toEqual(['long text'])

    expect(mount(ParameterPortForm, { props: { port: makePort('model_backend_ref'), modelValue: null } }).text()).toContain('Model backend picker')
    expect(mount(ParameterPortForm, { props: { port: makePort('schema_ref'), modelValue: null } }).text()).toContain('Schema picker')
  })

  it('falls back to the i18n label and an empty option list for a select port with neither', () => {
    // A select port can arrive with no label (use the placeholder copy) and no
    // options array (treat as empty). Both are the fallback arms of the AppSelect
    // bindings the native <select> used to cover implicitly.
    const port = { ...makePort('select'), label: '', options: undefined as any }
    const wrapper = mount(ParameterPortForm, { props: { port, modelValue: '' } })
    const select = wrapper.findComponent({ name: 'Select' })
    expect(select.exists()).toBe(true)
    const options = select.props('options') as Array<{ value: string }>
    expect(options).toHaveLength(1)
  })
})
