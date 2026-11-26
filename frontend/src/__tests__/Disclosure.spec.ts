import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, ref } from 'vue'

import Disclosure from '../components/ui/Disclosure.vue'

afterEach(() => {
  vi.unstubAllEnvs()
  vi.restoreAllMocks()
})

describe('Disclosure', () => {
  it('wires aria-expanded and aria-controls to a real button and its region', async () => {
    const wrapper = mount(Disclosure, {
      props: { open: false, label: 'Stacktrace', 'data-testid': 'toggle' },
      slots: { default: '<pre class="region-body">trace</pre>' },
    })

    const trigger = wrapper.get('[data-testid="toggle"]')
    expect(trigger.element.tagName).toBe('BUTTON')
    expect(trigger.attributes('type')).toBe('button')
    expect(trigger.attributes('aria-expanded')).toBe('false')

    // aria-controls names the primitive's own region, which is absent while
    // closed and rendered with that exact id while open.
    const controls = trigger.attributes('aria-controls')
    expect(controls).toBeTruthy()
    expect(wrapper.find(`#${controls}`).exists()).toBe(false)

    await wrapper.setProps({ open: true })

    expect(trigger.attributes('aria-expanded')).toBe('true')
    const rendered = wrapper.get(`#${controls}`)
    expect(rendered.find('.region-body').exists()).toBe(true)
  })

  it('gives every generated region a distinct id across instances', async () => {
    const wrapper = mount({
      components: { Disclosure },
      data: () => ({ open: true }),
      template: `
        <div>
          <Disclosure :open="open" label="Stacktrace">trace</Disclosure>
          <Disclosure :open="open" label="Context">context</Disclosure>
        </div>
      `,
    })

    const triggers = wrapper.findAll('button')
    expect(triggers).toHaveLength(2)
    const first = triggers[0].attributes('aria-controls')
    const second = triggers[1].attributes('aria-controls')
    expect(first).toBeTruthy()
    expect(second).toBeTruthy()
    expect(first).not.toBe(second)

    // Each trigger resolves to its own rendered region, so the ids are unique.
    expect(wrapper.findAll(`#${first}`)).toHaveLength(1)
    expect(wrapper.findAll(`#${second}`)).toHaveLength(1)
  })

  it('emits update:open with the toggled value on click', async () => {
    const wrapper = mount(Disclosure, { props: { open: false, label: 'Details' } })

    await wrapper.get('button').trigger('click')
    expect(wrapper.emitted('update:open')).toEqual([[true]])

    await wrapper.setProps({ open: true })
    await wrapper.get('button').trigger('click')
    expect(wrapper.emitted('update:open')).toEqual([[true], [false]])
  })

  it('supports v-model:open from the caller', async () => {
    const Host = defineComponent({
      components: { Disclosure },
      setup() {
        const open = ref(false)
        return { open }
      },
      template: '<Disclosure v-model:open="open" label="More">Body</Disclosure>',
    })

    const wrapper = mount(Host)
    expect(wrapper.get('button').attributes('aria-expanded')).toBe('false')

    await wrapper.get('button').trigger('click')

    expect(wrapper.get('button').attributes('aria-expanded')).toBe('true')
    expect(wrapper.text()).toContain('Body')
  })

  it('rotates its built-in chevron when open, for both resting directions', async () => {
    const right = mount(Disclosure, { props: { open: true, label: 'More' } })
    expect(right.get('button svg').classes()).toContain('rotate-90')

    const down = mount(Disclosure, { props: { open: true, label: 'More', chevron: 'down' } })
    const downChevron = down.get('button svg')
    expect(downChevron.classes()).toContain('rotate-180')
    expect(downChevron.classes()).not.toContain('rotate-90')

    const closed = mount(Disclosure, { props: { open: false, label: 'More' } })
    expect(closed.get('button svg').classes()).not.toContain('rotate-90')
  })

  it('renders a row-shaped trigger through the scoped slot with the primitive rotation', () => {
    const Host = defineComponent({
      components: { Disclosure },
      data: () => ({ open: true }),
      template: `
        <Disclosure :open="open">
          <template #trigger="{ open, chevronClass, rotationClass }">
            <span class="row-name" :data-open="String(open)">Team A</span>
            <svg class="slot-chevron" :class="[chevronClass, open && rotationClass]" />
          </template>
        </Disclosure>
      `,
    })

    const wrapper = mount(Host)
    const trigger = wrapper.get('button')
    expect(trigger.find('.row-name').text()).toBe('Team A')
    expect(trigger.find('.row-name').attributes('data-open')).toBe('true')
    expect(trigger.get('.slot-chevron').classes()).toContain('rotate-90')
  })

  it('points aria-controls at a caller-owned region instead of rendering one', () => {
    const Host = defineComponent({
      components: { Disclosure },
      data: () => ({ open: true }),
      template: `
        <div>
          <Disclosure :open="open" controls="outside-region" label="Row" />
          <div id="outside-region">Outside</div>
        </div>
      `,
    })

    const wrapper = mount(Host)
    const trigger = wrapper.get('button')
    expect(trigger.attributes('aria-controls')).toBe('outside-region')
    expect(wrapper.get('#outside-region').text()).toBe('Outside')
    // The caller owns the region, so the primitive renders no second one.
    expect(wrapper.findAll('#outside-region')).toHaveLength(1)
  })

  it('stops the trigger click from bubbling only when asked', async () => {
    const onParentClick = vi.fn()

    const bubbling = mount({
      components: { Disclosure },
      template: '<div @click="onClick"><Disclosure label="Inline" /></div>',
      setup: () => ({ onClick: onParentClick }),
    })
    await bubbling.get('button').trigger('click')
    expect(onParentClick).toHaveBeenCalledTimes(1)

    const stopped = mount({
      components: { Disclosure },
      template: '<div @click="onClick"><Disclosure label="Row" stop-propagation /></div>',
      setup: () => ({ onClick: onParentClick }),
    })
    onParentClick.mockClear()
    await stopped.get('button').trigger('click')
    expect(onParentClick).not.toHaveBeenCalled()
  })

  it('keeps a disabled trigger inert and marks the state natively', async () => {
    const wrapper = mount(Disclosure, { props: { open: false, label: 'Locked', disabled: true } })

    expect(wrapper.get('button').attributes('disabled')).toBeDefined()
    await wrapper.get('button').trigger('click')
    expect(wrapper.emitted('update:open')).toBeUndefined()
  })

  it('lets listener attributes such as @click.stop land on the trigger', async () => {
    const onClick = vi.fn()
    const wrapper = mount({
      components: { Disclosure },
      template: '<Disclosure label="Row" @click.stop="onClick" />',
      setup: () => ({ onClick }),
    })

    await wrapper.get('button').trigger('click')
    expect(onClick).toHaveBeenCalledTimes(1)
    expect(wrapper.emitted('update:open')).toBeUndefined()
  })

  it('does not warn in a production build when no region can be resolved', () => {
    vi.stubEnv('DEV', false)
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})

    mount(Disclosure, { props: { label: 'Details' } })

    expect(warn).not.toHaveBeenCalled()
  })
})
