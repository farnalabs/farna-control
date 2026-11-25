import { describe, it, expect, vi, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent } from 'vue'

import Button from '../components/ui/Button.vue'

afterEach(() => {
  vi.unstubAllEnvs()
  vi.restoreAllMocks()
})

describe('Button', () => {
  it.each([
    ['primary', [], undefined],
    ['secondary', ['p-button-secondary', 'p-button-outlined'], 'secondary'],
    ['secondary-text', ['p-button-secondary', 'p-button-text'], 'secondary'],
    ['ghost', ['p-button-text'], undefined],
    ['link', ['p-button-link'], undefined],
    ['danger', ['p-button-danger'], 'danger'],
  ] as const)('maps variant %s to the PrimeVue severity/outlined/text flags', (variant, classes, severity) => {
    const wrapper = mount(Button, { props: { variant, label: 'Save' } })

    for (const className of classes) expect(wrapper.classes()).toContain(className)
    expect(wrapper.classes()).not.toContain('p-button-icon-only')
    if (severity === undefined) {
      expect(wrapper.attributes('data-p-severity')).toBeUndefined()
    } else {
      expect(wrapper.attributes('data-p-severity')).toBe(severity)
    }
  })

  it('maps secondary-text to the secondary severity at rest rather than the primary one', () => {
    const wrapper = mount(Button, { props: { variant: 'secondary-text', label: 'Cancel' } })

    // The severity is the whole point of the variant: a plain `text` button
    // would render the primary severity at rest.
    expect(wrapper.classes()).toContain('p-button-secondary')
    expect(wrapper.classes()).toContain('p-button-text')
    expect(wrapper.classes()).not.toContain('p-button-outlined')
    expect(wrapper.attributes('data-p-severity')).toBe('secondary')
    expect(wrapper.attributes('data-p')).toContain('text')
  })

  it("maps link to PrimeVue's link flag, distinct from text", () => {
    const link = mount(Button, { props: { variant: 'link', label: 'Learn more' } })
    const text = mount(Button, { props: { variant: 'ghost', label: 'Learn more' } })

    // `link` sets the link flag (transparent hover + underlined label on
    // hover); `text` does not. They must not be the same combination.
    expect(link.classes()).toContain('p-button-link')
    expect(link.classes()).not.toContain('p-button-text')
    expect(link.attributes('data-p')).toContain('link')

    expect(text.classes()).toContain('p-button-text')
    expect(text.classes()).not.toContain('p-button-link')
    expect(text.attributes('data-p')).not.toContain('link')
  })

  it('keeps the variant flags exclusive: primary is solid and ghost is not outlined', () => {
    const primary = mount(Button, { props: { label: 'Save' } })
    expect(primary.classes()).not.toContain('p-button-outlined')
    expect(primary.classes()).not.toContain('p-button-text')

    const ghost = mount(Button, { props: { variant: 'ghost', label: 'Cancel' } })
    expect(ghost.classes()).not.toContain('p-button-outlined')
    expect(ghost.attributes('data-p-severity')).toBeUndefined()
  })

  it('supports the legacy outlined override on secondary and danger', () => {
    const solidSecondary = mount(Button, { props: { variant: 'secondary', outlined: false, label: 'Save' } })
    expect(solidSecondary.classes()).toContain('p-button-secondary')
    expect(solidSecondary.classes()).not.toContain('p-button-outlined')

    const outlinedDanger = mount(Button, { props: { variant: 'danger', outlined: true, label: 'Revoke' } })
    expect(outlinedDanger.classes()).toContain('p-button-danger')
    expect(outlinedDanger.classes()).toContain('p-button-outlined')
  })

  it('maps size md to the default and sm to PrimeVue small', () => {
    const medium = mount(Button, { props: { label: 'Save' } })
    expect(medium.classes()).not.toContain('p-button-sm')
    expect(medium.attributes('data-p')).toBe('')

    const small = mount(Button, { props: { size: 'sm', label: 'Save' } })
    expect(small.classes()).toContain('p-button-sm')
    expect(small.attributes('data-p')).toBe('small')
  })

  it('keeps a loading button disabled and unclickable while rendering its loading icon', async () => {
    const wrapper = mount(Button, { props: { label: 'Save', loading: true } })

    expect(wrapper.attributes('data-p-disabled')).toBe('true')
    expect(wrapper.find('.p-button-loading-icon').exists()).toBe(true)
    // PrimeVue merges incoming attrs AFTER its own `disabled || loading`, so a
    // bound `:disabled="false"` overwrites the computed `true`. The primitive
    // must bind `disabled || loading` itself or the button is only visually
    // disabled: no native attribute, and a click still fires.
    expect(wrapper.attributes('disabled')).toBeDefined()
    expect((wrapper.element as HTMLButtonElement).disabled).toBe(true)

    await wrapper.trigger('click')
    expect(wrapper.emitted('click')).toBeUndefined()
  })

  it('passes disabled through to the underlying button', () => {
    const wrapper = mount(Button, { props: { label: 'Save', disabled: true } })

    expect(wrapper.attributes('data-p-disabled')).toBe('true')
    expect(wrapper.attributes('disabled')).toBeDefined()
  })

  it('takes an explicit accessible name for the icon-only form and renders it', () => {
    const wrapper = mount(Button, { props: { icon: 'pi pi-plus', ariaLabel: 'Add item' } })

    expect(wrapper.classes()).toContain('p-button-icon-only')
    expect(wrapper.attributes('aria-label')).toBe('Add item')
  })

  it('warns (but renders) when an icon-only button has no accessible name', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})

    const wrapper = mount(Button, { props: { icon: 'pi pi-plus' } })

    expect(wrapper.classes()).toContain('p-button-icon-only')
    expect(wrapper.attributes('aria-label')).toBeUndefined()
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('accessible name'))
  })

  it('does not warn for a named icon-only button', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})

    mount(Button, { props: { icon: 'pi pi-plus', ariaLabel: 'Add item' } })

    expect(warn).not.toHaveBeenCalled()
  })

  it('honours an aria-label passed directly by a call site', () => {
    const CallSite = defineComponent({
      components: { UiButton: Button },
      template: '<UiButton label="Copy" aria-label="Copy password" />',
    })

    const wrapper = mount(CallSite)

    expect(wrapper.findComponent(Button).attributes('aria-label')).toBe('Copy password')
  })

  it('does not warn in a production build, even for an unnamed icon-only button', () => {
    vi.stubEnv('DEV', false)
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})

    mount(Button, { props: { icon: 'pi pi-plus' } })

    expect(warn).not.toHaveBeenCalled()
  })

  it('keeps the PrimeVue type behaviour: a plain button is type="button" and submit passes through', () => {
    const plain = mount(Button, { props: { label: 'Save' } })
    expect(plain.attributes('type')).toBe('button')

    const submit = mount(Button, { props: { type: 'submit', label: 'Save' } })
    expect(submit.attributes('type')).toBe('submit')
  })

  it('renders link buttons through the `as`/`href` fall-through without a stray type', () => {
    const wrapper = mount(Button, {
      props: { label: 'Pricing' },
      attrs: { as: 'a', href: 'https://farna.dev', target: '_blank', rel: 'noopener noreferrer' },
    })

    expect(wrapper.element.tagName).toBe('A')
    expect(wrapper.attributes('href')).toBe('https://farna.dev')
    expect(wrapper.attributes('target')).toBe('_blank')
    expect(wrapper.attributes('rel')).toBe('noopener noreferrer')
    expect(wrapper.attributes('type')).toBeUndefined()
  })

  it('renders the label prop and lets the default slot carry the content', () => {
    const labelled = mount(Button, { props: { label: 'Save' } })
    expect(labelled.text()).toBe('Save')

    const slotted = mount(Button, {
      slots: { default: '<span class="slot-child">Custom content</span>' },
    })
    expect(slotted.text()).toBe('Custom content')
    expect(slotted.find('.slot-child').exists()).toBe(true)
  })

  it('falls attributes and data-testid through to the PrimeVue root', () => {
    const wrapper = mount(Button, {
      props: { label: 'Save' },
      attrs: {
        'data-testid': 'save-button',
        class: 'w-full border-primary/30',
        'aria-pressed': 'true',
      },
    })

    expect(wrapper.attributes('data-testid')).toBe('save-button')
    expect(wrapper.classes()).toContain('w-full')
    expect(wrapper.classes()).toContain('border-primary/30')
    expect(wrapper.attributes('aria-pressed')).toBe('true')
  })

  it('forwards the click event', async () => {
    const onClick = vi.fn()
    const wrapper = mount(Button, { props: { label: 'Save', onClick } })

    await wrapper.trigger('click')

    expect(wrapper.emitted('click')).toHaveLength(1)
    expect(onClick).toHaveBeenCalledTimes(1)
  })
})
