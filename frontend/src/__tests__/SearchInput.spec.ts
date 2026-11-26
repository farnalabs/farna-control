import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, ref } from 'vue'

import Field from '../components/ui/Field.vue'
import Input from '../components/ui/Input.vue'
import SearchInput from '../components/ui/SearchInput.vue'

function mountHarness(template: string, setupState: Record<string, unknown> = {}) {
  const Host = defineComponent({
    components: { Field, SearchInput },
    setup() {
      return setupState
    },
    template,
  })
  return mount(Host)
}

function inputOf(wrapper: ReturnType<typeof mountHarness>) {
  return wrapper.get('[data-testid="control"]')
}

describe('SearchInput', () => {
  it('renders a leading, decorative search icon next to a text input', () => {
    const wrapper = mountHarness('<SearchInput data-testid="control" />')

    expect(wrapper.findAll('svg')).toHaveLength(1)
    expect(wrapper.get('svg').attributes('aria-hidden')).toBe('true')
    expect(inputOf(wrapper).attributes('type')).toBe('text')
    expect(inputOf(wrapper).attributes('inputmode')).toBe('search')
  })

  // PrimeVue's `.p-inputtext` padding is unlayered CSS, so it beats every
  // ordinary (layered) Tailwind utility. Only an important utility takes effect,
  // and jsdom computes no layout, so the class list is what we can pin (FAR-2809).
  it('reserves room for the icon with padding that overrides the PrimeVue base style', () => {
    const wrapper = mountHarness('<SearchInput data-testid="control" />')

    expect(inputOf(wrapper).classes()).toContain('p-inputtext')
    expect(inputOf(wrapper).classes()).toContain('pl-9!')
  })

  it('reserves room for the clear button with overriding padding only while it is shown', async () => {
    const value = ref('')
    const wrapper = mountHarness(
      '<SearchInput v-model="value" data-testid="control" clearable clear-label="Clear search" />',
      { value },
    )

    expect(inputOf(wrapper).classes()).not.toContain('pr-9!')

    await inputOf(wrapper).setValue('query')

    expect(inputOf(wrapper).classes()).toContain('pr-9!')
  })

  it('puts the caller class on the wrapper and inputClass on the control', () => {
    const wrapper = mountHarness(
      '<SearchInput data-testid="control" class="w-full" input-class="font-mono" />',
    )

    expect(wrapper.get('.relative').classes()).toContain('w-full')
    expect(inputOf(wrapper).classes()).toContain('font-mono')
  })

  it('forwards the accessible name, placeholder and testid to the control', () => {
    const wrapper = mountHarness(
      '<SearchInput data-testid="control" aria-label="Search pipelines" placeholder="Search..." />',
    )

    expect(inputOf(wrapper).attributes('aria-label')).toBe('Search pipelines')
    expect(inputOf(wrapper).attributes('placeholder')).toBe('Search...')
  })

  it('takes its accessible name from a Field label when one wraps it', () => {
    const wrapper = mountHarness('<Field label="Search runs"><SearchInput data-testid="control" /></Field>')

    const id = inputOf(wrapper).attributes('id')

    expect(id).toBeTruthy()
    expect(wrapper.get('label').attributes('for')).toBe(id)
    expect(inputOf(wrapper).attributes('aria-describedby')).toBeUndefined()
  })

  it('emits every keystroke immediately, with no debounce of its own', async () => {
    const wrapper = mountHarness('<SearchInput data-testid="control" />')
    const input = inputOf(wrapper)

    await input.setValue('r')
    await input.setValue('ru')
    await input.setValue('run')

    expect(wrapper.findComponent(SearchInput).emitted('update:modelValue')).toEqual([
      ['r'],
      ['ru'],
      ['run'],
    ])
  })

  it('keeps the clear button opt-in', () => {
    const wrapper = mountHarness('<SearchInput data-testid="control" model-value="abc" />')

    expect(wrapper.find('button').exists()).toBe(false)
  })

  it('shows the clear button only while a clearable field has a value', async () => {
    const value = ref('')
    const wrapper = mountHarness(
      '<SearchInput v-model="value" data-testid="control" clearable clear-label="Clear search" />',
      { value },
    )

    expect(wrapper.find('button').exists()).toBe(false)

    await inputOf(wrapper).setValue('query')

    expect(wrapper.get('button').attributes('aria-label')).toBe('Clear search')
  })

  it('empties the field and emits clear when the clear button is used', async () => {
    const value = ref('query')
    const wrapper = mountHarness(
      '<SearchInput v-model="value" data-testid="control" clearable clear-label="Clear search" />',
      { value },
    )

    await wrapper.get('button').trigger('click')

    const search = wrapper.findComponent(SearchInput)

    expect(value.value).toBe('')
    expect(search.emitted('update:modelValue')).toEqual([['']])
    expect(search.emitted('clear')).toHaveLength(1)
    expect(wrapper.find('button').exists()).toBe(false)
  })

  it('treats an explicit null model value as empty for the clear affordance', () => {
    const wrapper = mountHarness('<SearchInput :model-value="null" data-testid="control" clearable clear-label="Clear search" />')

    expect(wrapper.find('button').exists()).toBe(false)
    expect(inputOf(wrapper).classes()).toContain('pr-3')
  })

  it('normalises a null update from the control to an empty string', async () => {
    const wrapper = mountHarness('<SearchInput data-testid="control" />')

    wrapper.findComponent(Input).vm.$emit('update:model-value', null)
    await wrapper.vm.$nextTick()

    expect(wrapper.findComponent(SearchInput).emitted('update:modelValue')).toEqual([['']])
  })
})
