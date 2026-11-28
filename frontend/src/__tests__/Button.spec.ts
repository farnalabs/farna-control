import { describe, it, expect, vi, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent } from 'vue'
import { readFileSync } from 'fs'
import { resolve } from 'path'
import Aura from '@primevue/themes/aura'
import { toNormalizeVariable, toVariables } from '@primeuix/styled'

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
    ['warning', ['p-button-warn'], 'warn'],
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

  it('pins the warning variant to severity="warn", solid, distinct from primary and danger', () => {
    const warning = mount(Button, { props: { variant: 'warning', label: 'Retry all' } })
    const primary = mount(Button, { props: { variant: 'primary', label: 'Retry all' } })
    const danger = mount(Button, { props: { variant: 'danger', label: 'Retry all' } })

    // The exact flag: severity warn, and nothing that would un-solid it.
    expect(warning.attributes('data-p-severity')).toBe('warn')
    expect(warning.classes()).toContain('p-button-warn')
    expect(warning.classes()).not.toContain('p-button-outlined')
    expect(warning.classes()).not.toContain('p-button-text')
    expect(warning.classes()).not.toContain('p-button-link')

    // Distinct from the two solid variants it can be confused with.
    expect(primary.attributes('data-p-severity')).toBeUndefined()
    expect(danger.attributes('data-p-severity')).toBe('danger')
    expect(warning.attributes('data-p-severity')).not.toBe(primary.attributes('data-p-severity'))
    expect(warning.attributes('data-p-severity')).not.toBe(danger.attributes('data-p-severity'))

    // The variant relies on Aura's warn background/colour token pair: both
    // must resolve in the preset, and neither may be the danger pair.
    const { tokens } = toVariables(Aura as unknown as Parameters<typeof toVariables>[0], { prefix: 'p' })
    const names = new Set((tokens || []).map((token) => `--p-${toNormalizeVariable(token)}`))
    expect(names.has('--p-button-warn-background')).toBe(true)
    expect(names.has('--p-button-warn-color')).toBe(true)
    expect(names.has('--p-button-danger-background')).toBe(true)
  })

  it('supports an outlined warning action through the outlined override', () => {
    const outlinedWarning = mount(Button, { props: { variant: 'warning', outlined: true, label: 'Retry all' } })

    expect(outlinedWarning.attributes('data-p-severity')).toBe('warn')
    expect(outlinedWarning.classes()).toContain('p-button-outlined')
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

  it('maps danger-text to the text flag plus the app destructive-ink class', () => {
    const wrapper = mount(Button, { props: { variant: 'danger-text', label: 'Remove' } })

    // A transparent (text) shape - the destructive ink is the app's own token,
    // so no PrimeVue severity is set and the class is what paints it.
    expect(wrapper.classes()).toContain('p-button-text')
    expect(wrapper.classes()).toContain('ui-button-danger-text')
    expect(wrapper.classes()).not.toContain('p-button-outlined')
    expect(wrapper.attributes('data-p-severity')).toBeUndefined()
  })

  it('gives danger-text an icon-only form that needs no hand-written padding', () => {
    const wrapper = mount(Button, {
      props: { variant: 'danger-text', size: 'xs', ariaLabel: 'Delete pipeline' },
      slots: { icon: '<svg class="icon-glyph" />' },
    })

    expect(wrapper.classes()).toContain('ui-button-danger-text')
    expect(wrapper.classes()).toContain('ui-button-xs')
    // Icon-only comes from the #icon slot, so an SVG-only call site gets the
    // square form (and its pinned 1.75rem padding) without `p-1`/`h-7`/`w-7`.
    expect(wrapper.classes()).toContain('p-button-icon-only')
    expect(wrapper.find('.icon-glyph').exists()).toBe(true)
    expect(wrapper.attributes('aria-label')).toBe('Delete pipeline')
  })

  it('maps plain to text with the default-ink class, distinct from ghost and secondary-text', () => {
    const plain = mount(Button, { props: { variant: 'plain', label: 'Stay' } })
    const ghost = mount(Button, { props: { variant: 'ghost', label: 'Stay' } })
    const secondaryText = mount(Button, { props: { variant: 'secondary-text', label: 'Stay' } })

    expect(plain.classes()).toContain('p-button-text')
    expect(plain.classes()).toContain('ui-button-plain')
    expect(plain.classes()).not.toContain('ui-button-danger-text')

    // All three are text buttons; only `plain` carries the app-ink class, and
    // only `secondary-text` keeps a PrimeVue severity at rest.
    expect(ghost.classes()).not.toContain('ui-button-plain')
    expect(secondaryText.classes()).not.toContain('ui-button-plain')
    expect(secondaryText.attributes('data-p-severity')).toBe('secondary')
    expect(plain.attributes('data-p-severity')).toBeUndefined()
  })

  it('maps size xs to the compact class and PrimeVue small, and pins the toolbar geometry', () => {
    const wrapper = mount(Button, { props: { variant: 'secondary', size: 'xs', label: 'Save' } })

    expect(wrapper.classes()).toContain('p-button-sm')
    expect(wrapper.classes()).toContain('ui-button-xs')
    expect(wrapper.classes()).not.toContain('p-button-icon-only')

    // jsdom does not apply the SFC's scoped CSS, so the visual contract is
    // asserted against the source rule that pins it: the 1.75rem row, the
    // 0.625rem inline padding and the square icon-only form.
    const sfc = readFileSync(resolve(__dirname, '../components/ui/Button.vue'), 'utf8')
    expect(sfc).toContain('.p-button-sm.ui-button-xs {')
    expect(sfc).toContain('height: 1.75rem;')
    expect(sfc).toContain('padding: 0 0.625rem;')
    expect(sfc).toContain('.p-button-sm.ui-button-xs.p-button-icon-only {')
    expect(sfc).toContain('width: 1.75rem;\n  height: 1.75rem;\n  padding: 0;')
    // The compact controls this size replaces faded when disabled; PrimeVue
    // only sets `cursor: default`, so the size carries the state itself.
    expect(sfc).toContain('.p-button-sm.ui-button-xs:disabled {')
    expect(sfc).toContain('cursor: not-allowed;\n  opacity: 0.5;')
  })

  it('leaves md and sm without the compact class', () => {
    const medium = mount(Button, { props: { label: 'Save' } })
    const small = mount(Button, { props: { size: 'sm', label: 'Save' } })

    expect(medium.classes()).not.toContain('ui-button-xs')
    expect(small.classes()).not.toContain('ui-button-xs')
    expect(small.classes()).toContain('p-button-sm')
  })

  it('maps the action variants to their app token classes', () => {
    const action = mount(Button, { props: { variant: 'action', label: 'Run pipeline' } })
    const actionLink = mount(Button, { props: { variant: 'action-link', label: 'Reset' } })

    // Solid, non-severity: the build hue is an app token, not a PrimeVue one.
    expect(action.classes()).toContain('ui-button-action')
    expect(action.classes()).not.toContain('p-button-text')
    expect(action.classes()).not.toContain('p-button-link')
    expect(action.attributes('data-p-severity')).toBeUndefined()

    // Link tone: the link flag (transparent + underlined on hover), in the
    // action hue.
    expect(actionLink.classes()).toContain('ui-button-action-link')
    expect(actionLink.classes()).toContain('p-button-link')
    expect(actionLink.classes()).not.toContain('p-button-text')

    // Both variants paint from the tokens style.css owns, so a renamed/removed
    // token is a test failure rather than an invisible flat button.
    const css = readFileSync(resolve(__dirname, '../style.css'), 'utf8')
    for (const token of ['--action-build:', '--action-build-hover:', '--action-link:', '--action-link-hover:']) {
      expect(css).toContain(token)
    }
  })

  it('exposes the underlying element and a focus() for callers holding a template ref', () => {
    const wrapper = mount(Button, {
      props: { label: 'Delete' },
      attrs: { 'data-testid': 'delete-button' },
      attachTo: document.body,
    })
    const exposed = wrapper.vm as unknown as { el: HTMLElement | null; focus: () => void }

    expect(exposed.el).toBe(wrapper.element)
    expect(exposed.el?.tagName).toBe('BUTTON')

    exposed.focus()
    expect(document.activeElement).toBe(wrapper.element)

    wrapper.unmount()
  })

  it('still forwards a caller class alongside the variant and size classes', () => {
    const wrapper = mount(Button, {
      props: { variant: 'secondary', size: 'xs', label: 'Save' },
      attrs: { class: 'shrink-0 md:hidden' },
    })

    expect(wrapper.classes()).toContain('ui-button-xs')
    expect(wrapper.classes()).toContain('p-button-secondary')
    expect(wrapper.classes()).toContain('shrink-0')
    expect(wrapper.classes()).toContain('md:hidden')
  })
})
