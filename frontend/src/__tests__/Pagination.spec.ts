import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import Pagination from '../components/shared/Pagination.vue'

function mountPagination(props: Partial<{
  hasPrev: boolean
  hasNext: boolean
  pageLabel: string | null
  prevLabel: string
  nextLabel: string
  prevTestid: string
  nextTestid: string
  statusTestid: string
}> = {}) {
  return mount(Pagination, {
    props: { hasPrev: false, hasNext: false, ...props },
  })
}

describe('Pagination', () => {
  it('disables both controls at each end of the list', () => {
    const wrapper = mountPagination({ hasPrev: false, hasNext: false })

    const prev = wrapper.get('[data-testid="pagination-prev"]').element as HTMLButtonElement
    const next = wrapper.get('[data-testid="pagination-next"]').element as HTMLButtonElement

    expect(prev.disabled).toBe(true)
    expect(next.disabled).toBe(true)
  })

  it('enables a control only when travel in that direction is possible', () => {
    const wrapper = mountPagination({ hasPrev: true, hasNext: false })

    const prev = wrapper.get('[data-testid="pagination-prev"]').element as HTMLButtonElement
    const next = wrapper.get('[data-testid="pagination-next"]').element as HTMLButtonElement

    expect(prev.disabled).toBe(false)
    expect(next.disabled).toBe(true)
  })

  it('emits prev when Previous is clicked', async () => {
    const wrapper = mountPagination({ hasPrev: true })

    await wrapper.get('[data-testid="pagination-prev"]').trigger('click')

    expect(wrapper.emitted('prev')).toHaveLength(1)
    expect(wrapper.emitted('next')).toBeUndefined()
  })

  it('emits next when Next is clicked', async () => {
    const wrapper = mountPagination({ hasNext: true })

    await wrapper.get('[data-testid="pagination-next"]').trigger('click')

    expect(wrapper.emitted('next')).toHaveLength(1)
    expect(wrapper.emitted('prev')).toBeUndefined()
  })

  it('does not emit when a disabled control is clicked', async () => {
    const wrapper = mountPagination({ hasPrev: false, hasNext: false })

    await wrapper.get('[data-testid="pagination-prev"]').trigger('click')
    await wrapper.get('[data-testid="pagination-next"]').trigger('click')

    expect(wrapper.emitted('prev')).toBeUndefined()
    expect(wrapper.emitted('next')).toBeUndefined()
  })

  it('renders the page label in a polite live region', () => {
    const wrapper = mountPagination({ pageLabel: 'Page 3' })

    const status = wrapper.get('[data-testid="pagination-status"]')

    expect(status.text()).toBe('Page 3')
    expect(status.attributes('aria-live')).toBe('polite')
  })

  it('renders no label or live region when the page has no positional label', () => {
    const wrapper = mountPagination({ pageLabel: null })

    expect(wrapper.find('[data-testid="pagination-status"]').exists()).toBe(false)
    expect(wrapper.find('[aria-live="polite"]').exists()).toBe(false)
  })

  it('honours custom labels and testids so an adopting page keeps its own', () => {
    const wrapper = mountPagination({
      hasPrev: true,
      hasNext: true,
      pageLabel: 'Page 2',
      prevLabel: 'Newer',
      nextLabel: 'Older',
      prevTestid: 'runs-list-prev-page',
      nextTestid: 'runs-list-next-page',
      statusTestid: 'runs-list-page-label',
    })

    expect(wrapper.get('[data-testid="runs-list-prev-page"]').text()).toBe('Newer')
    expect(wrapper.get('[data-testid="runs-list-next-page"]').text()).toBe('Older')
    expect(wrapper.get('[data-testid="runs-list-page-label"]').text()).toBe('Page 2')
  })

  it('defaults the labels to the shared Previous/Next strings', () => {
    const wrapper = mountPagination({ hasPrev: true, hasNext: true })

    expect(wrapper.get('[data-testid="pagination-prev"]').text()).toBe('Previous')
    expect(wrapper.get('[data-testid="pagination-next"]').text()).toBe('Next')
  })

  it('renders both controls as plain buttons so they never submit a form', () => {
    const wrapper = mountPagination({ hasPrev: true, hasNext: true })

    const buttons = wrapper.findAll('button')

    expect(buttons).toHaveLength(2)
    expect(buttons[0].attributes('type')).toBe('button')
    expect(buttons[1].attributes('type')).toBe('button')
  })
})
