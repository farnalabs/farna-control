import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, ref } from 'vue'

import RadioGroup from '../components/ui/RadioGroup.vue'

const OPTIONS = [
  { value: 'existing', label: 'Link to existing pipeline', id: 'radio-existing' },
  { value: 'new', label: 'Create new pipeline', id: 'radio-new' },
]

function mountHarness(template: string, setupState: Record<string, unknown> = {}) {
  const Host = defineComponent({
    components: { RadioGroup },
    setup() {
      return setupState
    },
    template,
  })
  return mount(Host)
}

describe('RadioGroup', () => {
  it('renders one native radio per option sharing a single name', () => {
    const wrapper = mountHarness('<RadioGroup v-model="mode" aria-label="Target" :options="options" />', {
      mode: ref('existing'),
      options: OPTIONS,
    })
    const radios = wrapper.findAll('input[type="radio"]')

    expect(radios).toHaveLength(2)
    const names = radios.map((radio) => radio.attributes('name'))
    expect(new Set(names).size).toBe(1)
    expect(names[0]).toBeTruthy()
  })

  it('gives the group an accessible name and native radio semantics, not tabs', () => {
    const wrapper = mountHarness('<RadioGroup v-model="mode" aria-label="Graduation target" :options="options" />', {
      mode: ref('existing'),
      options: OPTIONS,
    })
    const group = wrapper.get('[role="radiogroup"]')

    expect(group.attributes('aria-label')).toBe('Graduation target')
    expect(group.attributes('role')).toBe('radiogroup')
    expect(wrapper.find('[role="tablist"]').exists()).toBe(false)
    expect(wrapper.find('[role="tab"]').exists()).toBe(false)
  })

  it('reflects the bound value and emits the selected option', async () => {
    const wrapper = mountHarness('<RadioGroup v-model="mode" aria-label="Target" :options="options" />', {
      mode: ref('existing'),
      options: OPTIONS,
    })
    const radios = wrapper.findAll('input[type="radio"]')

    expect((radios[0].element as HTMLInputElement).checked).toBe(true)
    expect((radios[1].element as HTMLInputElement).checked).toBe(false)

    await radios[1].setValue(true)

    expect(wrapper.vm.mode).toBe('new')
    expect((wrapper.findAll('input[type="radio"]')[1].element as HTMLInputElement).checked).toBe(true)
  })

  it('keeps each option id and testid on its native control', () => {
    const wrapper = mountHarness('<RadioGroup v-model="mode" aria-label="Target" :options="withTestids" />', {
      mode: ref('existing'),
      withTestids: [
        { ...OPTIONS[0], testid: 'mode-existing' },
        { ...OPTIONS[1], testid: 'mode-new' },
      ],
    })

    expect(wrapper.get('#radio-existing').attributes('data-testid')).toBe('mode-existing')
    expect(wrapper.get('#radio-new').attributes('data-testid')).toBe('mode-new')
  })

  it('disables a single option and the whole group', () => {
    const wrapper = mountHarness('<RadioGroup v-model="mode" aria-label="Target" :options="options" />', {
      mode: ref('existing'),
      options: [{ ...OPTIONS[0] }, { ...OPTIONS[1], disabled: true }],
    })
    const radios = wrapper.findAll('input[type="radio"]')

    expect(radios[0].attributes('disabled')).toBeUndefined()
    expect(radios[1].attributes('disabled')).toBeDefined()

    const disabledGroup = mountHarness('<RadioGroup v-model="mode" aria-label="Target" disabled :options="options" />', {
      mode: ref('existing'),
      options: OPTIONS,
    })
    for (const radio of disabledGroup.findAll('input[type="radio"]')) {
      expect(radio.attributes('disabled')).toBeDefined()
    }
  })

  it('appends the selected class to the checked option only', () => {
    const wrapper = mountHarness(
      '<RadioGroup v-model="mode" aria-label="Target" :options="options" option-class="rounded border p-3" selected-class="border-primary" />',
      { mode: ref('new'), options: OPTIONS },
    )
    const labels = wrapper.findAll('label')

    expect(labels[0].classes()).toContain('rounded')
    expect(labels[0].classes()).not.toContain('border-primary')
    expect(labels[1].classes()).toContain('border-primary')
  })

  it('renders a description and a badge per option', () => {
    const wrapper = mountHarness('<RadioGroup v-model="mode" aria-label="Target" :options="richOptions" />', {
      mode: ref('ai'),
      richOptions: [
        { value: 'ai', label: 'AI assisted', badge: 'Beta', description: 'Uses a model backend' },
      ],
    })

    expect(wrapper.text()).toContain('AI assisted')
    expect(wrapper.text()).toContain('Beta')
    expect(wrapper.text()).toContain('Uses a model backend')
  })

  it('renders the option-after slot outside the label so a follow-up control does not toggle the radio', async () => {
    const wrapper = mountHarness(
      `<RadioGroup v-model="mode" aria-label="Target" :options="options">
        <template #option-after="{ option }">
          <select v-if="option.value === 'existing'" data-testid="pipeline"><option>p1</option></select>
        </template>
      </RadioGroup>`,
      { mode: ref('existing'), options: OPTIONS },
    )
    const followUp = wrapper.get('[data-testid="pipeline"]')

    expect(followUp.element.closest('label')).toBeNull()

    // Activating the follow-up control must not change the selection.
    await followUp.trigger('click')

    expect(wrapper.vm.mode).toBe('existing')
  })
})
