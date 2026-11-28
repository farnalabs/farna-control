import { describe, expect, it } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

import FormDialog from '../../components/shared/FormDialog.vue'
import Button from '../../components/ui/Button.vue'

function mountDialog(props: Record<string, unknown> = {}) {
  return mount(FormDialog, {
    props: { open: true, title: 'Confirm action', ...props },
    global: {
      stubs: {
        Dialog: {
          props: ['visible'],
          template: '<div v-if="visible"><slot name="header" /><slot /><slot name="footer" /></div>',
        },
        Button: {
          props: ['disabled', 'loading'],
          template: '<button :disabled="disabled" :data-loading="loading"><slot /></button>',
        },
      },
    },
  })
}

describe('FormDialog', () => {
  it('renders normal confirm text', () => {
    expect(mountDialog({ confirmText: 'Delete' }).findAll('button')[1].text()).toBe('Delete')
  })

  it('renders loading text while confirming', () => {
    const button = mountDialog({ confirmText: 'Delete', confirmLoadingText: 'Deleting...', loading: true }).findAll('button')[1]
    expect(button.text()).toBe('Deleting...')
    expect((button.element as HTMLButtonElement).disabled).toBe(true)
  })

  it('falls back to confirm text when loading text is omitted', () => {
    expect(mountDialog({ confirmText: 'Delete', loading: true }).findAll('button')[1].text()).toBe('Delete')
  })

  it('disables confirmation when explicitly disabled', () => {
    expect((mountDialog({ confirmText: 'Delete', confirmDisabled: true }).findAll('button')[1].element as HTMLButtonElement).disabled).toBe(true)
  })

  it('falls back to the localized confirm label when no confirm text is given', () => {
    expect(mountDialog().findAll('button')[1].text()).toBe('Confirm')
  })

  it('falls back to the localized confirm label while loading when no texts are given', () => {
    expect(mountDialog({ loading: true }).findAll('button')[1].text()).toBe('Confirm')
  })
})

describe('FormDialog footer (shared Button primitive)', () => {
  function mountWithRealButton(props: Record<string, unknown> = {}) {
    return mount(FormDialog, {
      props: { open: true, title: 'Confirm action', ...props },
      global: {
        mocks: { $t: (key: string) => key },
        stubs: {
          Dialog: {
            props: ['visible'],
            template: '<div v-if="visible"><slot name="header" /><slot /><slot name="footer" /></div>',
          },
        },
      },
    })
  }

  it('renders the dialog footer with the shared Button primitive', () => {
    const buttons = mountWithRealButton({ confirmText: 'Delete', confirmSeverity: 'danger' }).findAllComponents(Button)

    expect(buttons).toHaveLength(2)
    // Cancel is the secondary variant, which the primitive renders outlined.
    expect(buttons[0].classes()).toContain('p-button-secondary')
    expect(buttons[0].classes()).toContain('p-button-outlined')
    // Confirm is the danger variant, which the primitive renders solid.
    expect(buttons[1].classes()).toContain('p-button-danger')
  })

  it('keeps a secondary confirm solid as it rendered before the migration', () => {
    const confirm = mountWithRealButton({ confirmText: 'Save', confirmSeverity: 'secondary' }).findAllComponents(Button)[1]

    expect(confirm.classes()).toContain('p-button-secondary')
    expect(confirm.classes()).not.toContain('p-button-outlined')
  })
})

describe('FormDialog accessible name (real PrimeVue Dialog)', () => {
  it('names the dialog after its title via a resolvable aria-labelledby target', async () => {
    const wrapper = mount(FormDialog, {
      attachTo: document.body,
      props: { open: true, title: 'Create Lifecycle Map' },
      global: { mocks: { $t: (key: string) => key } },
    })
    await flushPromises()

    const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]')
    expect(dialog).not.toBeNull()

    const labelledBy = dialog!.getAttribute('aria-labelledby')
    expect(labelledBy).toBeTruthy()

    const label = document.getElementById(labelledBy!)
    expect(label).not.toBeNull()
    expect(label!.textContent).toContain('Create Lifecycle Map')

    wrapper.unmount()
    document.querySelectorAll('.p-dialog-mask').forEach((el) => el.remove())
  })
})
