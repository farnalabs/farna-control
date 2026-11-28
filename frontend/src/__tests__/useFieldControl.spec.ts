import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'

import Input from '../components/ui/Input.vue'
import { resolveAutocomplete } from '../composables/useFieldControl'

describe('useFieldControl aria-required', () => {
  it('passes an explicit string aria-required through', () => {
    const wrapper = mount(Input, {
      props: { modelValue: '' },
      attrs: { 'aria-required': 'false', 'data-testid': 'control' },
    })

    expect(wrapper.get('[data-testid="control"]').attributes('aria-required')).toBe('false')
  })

  it('passes an explicit boolean aria-required through', () => {
    const wrapper = mount(Input, {
      props: { modelValue: '' },
      attrs: { 'aria-required': false, 'data-testid': 'control' },
    })

    expect(wrapper.get('[data-testid="control"]').attributes('aria-required')).toBe('false')
  })
})

describe('resolveAutocomplete', () => {
  it('maps the legacy boolean form to on/off and passes strings through', () => {
    expect(resolveAutocomplete(true)).toBe('on')
    expect(resolveAutocomplete(false)).toBe('off')
    expect(resolveAutocomplete('email')).toBe('email')
    expect(resolveAutocomplete(undefined)).toBeUndefined()
  })
})
