import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const mockAvailableEvents = [
  'hitl_awaiting',
  'run_failed',
  'run_stalled',
  'claim_expired',
  'hitl_overdue',
  'hitl_deadline_warning',
  'budget_exceeded',
  'circuit_breaker_tripped',
  'trigger_deactivated',
  'trigger_streak_alert',
]

/** One configured outbound endpoint — the default "configured" state. */
const mockEndpoints = [{ id: 'ep-1', url: 'https://example.com/hook' }]

function defaultGet(url: string): Promise<{ data: unknown; error: unknown }> {
  if (url === '/api/v1/admin/notifications/available-events') {
    return Promise.resolve({ data: mockAvailableEvents, error: undefined })
  }
  if (url === '/api/v1/notifications') {
    return Promise.resolve({ data: mockEndpoints, error: undefined })
  }
  return Promise.resolve({ data: { items: [], total: 0, next_cursor: null }, error: undefined })
}

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((url: string) => defaultGet(url)),
    POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AdminNotificationDeliveryLogView from '../views/AdminNotificationDeliveryLogView.vue'
import AppSelect from '../components/shared/AppSelect.vue'
import { api } from '../lib/api/client'
import { usePlanStore } from '../stores/planStore'

/** The delivery-log GET calls only — excludes the registry and endpoint reads. */
function deliveryCalls(): Array<[string, Record<string, unknown>]> {
  const getMock = api.GET as unknown as ReturnType<typeof vi.fn>
  return getMock.mock.calls.filter(
    (call: unknown[]) => call[0] === '/api/v1/admin/notifications/deliveries',
  ) as Array<[string, Record<string, unknown>]>
}

function mockGet(): ReturnType<typeof vi.fn> {
  return api.GET as unknown as ReturnType<typeof vi.fn>
}

/**
 * RouterLink is globally registered in the app, so it must be stubbed here
 * (the house pattern) — otherwise the link renders as an unknown element and
 * the boundary/CTA assertions would not be testing the shipped markup.
 */
function mountView() {
  return mount(AdminNotificationDeliveryLogView, {
    global: {
      stubs: {
        RouterLink: {
          props: ['to'],
          template: '<a :href="to"><slot /></a>',
        },
      },
    },
  })
}

/** Serve an endpoint list body (or a deliberately malformed one) for the banner. */
function withEndpoints(body: unknown, error?: unknown) {
  mockGet().mockImplementation((url: string) => {
    if (url === '/api/v1/admin/notifications/available-events') {
      return Promise.resolve({ data: mockAvailableEvents, error: undefined })
    }
    if (url === '/api/v1/notifications') {
      return Promise.resolve({ data: body, error })
    }
    return Promise.resolve({ data: { items: [], total: 0, next_cursor: null }, error: undefined })
  })
}

describe('AdminNotificationDeliveryLogView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation(
      (url: string) => defaultGet(url),
    )
  })

  it('renders the Webhook Deliveries title and subtitle (FAR-2596)', async () => {
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Webhook Deliveries')
    expect(wrapper.text()).toContain(
      'See whether each notification Farna Control sent to your webhooks was delivered, and retry the ones that failed.',
    )
    expect(wrapper.text()).not.toContain('Webhook Notifications')
  })

  it('explains the page boundary and links inbound trigger webhooks out (FAR-2596)', async () => {
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    const boundary = wrapper.find('[data-testid="admin-notification-log-boundary"]')
    expect(boundary.exists()).toBe(true)
    expect(boundary.text()).toContain('outbound delivery attempt')
    // Historical deliveries to a since-deleted webhook stay visible...
    expect(boundary.text()).toContain('including webhooks you have since deleted')
    // ...while in-app error alerts (same table, status 'in_app') do not.
    expect(boundary.text()).toContain('In-app error alerts are not shown here')
    expect(boundary.text()).toContain('Inbound trigger webhooks are recorded on the')

    // Inbound webhooks are not on this page — the explainer points at the
    // Trigger Event Log, the surface that actually records them.
    const link = boundary.find('a')
    expect(link.exists()).toBe(true)
    expect(link.attributes('href')).toBe('/admin/trigger-events')
    expect(link.text()).toBe('Trigger Event Log')
    expect(boundary.text()).toContain('instead.')
  })

  it('offers only status filters the delivery log can actually satisfy (FAR-2596)', async () => {
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    // The status Select is the first AppSelect in the filter bar.
    const statusSelect = wrapper.findAllComponents(AppSelect).at(0)
    expect(statusSelect).toBeDefined()
    const values = (statusSelect!.props('options') as Array<{ value: string }>).map((o) => o.value)

    expect(values).toContain('__all__')
    expect(values).toContain('delivered')
    expect(values).toContain('failed')
    expect(values).toContain('dead_lettered')
    // 'in_app' rows are excluded server-side and 'pending' is not in the
    // delivery-log status vocabulary (the CHECK constraint allows only
    // delivered / failed / dead_lettered / in_app), so either filter could
    // only ever return nothing.
    expect(values).not.toContain('in_app')
    expect(values).not.toContain('pending')
  })

  it('renders the "no deliveries yet" state when endpoints exist and nothing has fired', async () => {
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="admin-notification-log-no-endpoints"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="admin-notification-log-empty"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('No deliveries yet')
    expect(wrapper.text()).toContain(
      'Your webhooks are configured. A row appears here each time a subscribed event fires.',
    )
    // No filters are applied, so there is nothing to reset.
    expect(wrapper.find('[data-testid="admin-notification-log-empty-reset"]').exists()).toBe(false)
  })

  it('warns when no outbound webhook endpoints are configured (FAR-2596)', async () => {
    withEndpoints([])
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    const banner = wrapper.find('[data-testid="admin-notification-log-no-endpoints"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('No outbound notification webhooks are configured')
    expect(banner.text()).toContain('Nothing will appear here until you add one')
    expect(banner.text()).toContain('run failures, HITL deadlines, budget alerts or capacity warnings')

    // The warning is announced, not merely rendered: the live region is
    // mounted unconditionally so the appearing banner is picked up.
    expect(banner.element.parentElement?.getAttribute('aria-live')).toBe('polite')

    // The "your webhooks are configured" empty state would contradict the
    // banner, so it is withheld rather than shown underneath it.
    expect(wrapper.find('[data-testid="admin-notification-log-empty"]').exists()).toBe(false)
  })

  it('links the banner CTA to /settings/teams when team_rbac is active (FAR-2596)', async () => {
    withEndpoints([])
    usePlanStore().features['team_rbac'] = true
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    const cta = wrapper.find('[data-testid="admin-notification-log-add-endpoint"]')
    expect(cta.exists()).toBe(true)
    expect(cta.attributes('href')).toBe('/settings/teams')
    expect(cta.text()).toBe('Add a webhook endpoint')
  })

  it('withholds the banner CTA when team_rbac is inactive, rather than linking into a disabled gate (FAR-2596)', async () => {
    withEndpoints([])
    // Default plan store: no team_rbac feature and no tier catalogue, which is
    // exactly the state where FeatureGate would render the locked settings page.
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="admin-notification-log-no-endpoints"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="admin-notification-log-add-endpoint"]').exists()).toBe(false)
  })

  it('fails open when the endpoint list request errors (never a false warning)', async () => {
    withEndpoints(undefined, { detail: 'boom' })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="admin-notification-log-no-endpoints"]').exists()).toBe(false)
    // A failed best-effort endpoint read must not break the page it decorates.
    expect(wrapper.text()).not.toContain('boom')
    const empty = wrapper.find('[data-testid="admin-notification-log-empty"]')
    expect(empty.exists()).toBe(true)
    // ...and it must not assert a configuration it could not verify.
    expect(empty.text()).toContain('A row appears here each time a subscribed event fires.')
    expect(empty.text()).not.toContain('Your webhooks are configured')
  })

  it('fails open when the endpoint list body is not an array', async () => {
    withEndpoints({ unexpected: 'shape' })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="admin-notification-log-no-endpoints"]').exists()).toBe(false)
    const empty = wrapper.find('[data-testid="admin-notification-log-empty"]')
    expect(empty.exists()).toBe(true)
    expect(empty.text()).not.toContain('Your webhooks are configured')
  })

  it('splits the filters empty state and resets the filters from it (FAR-2596)', async () => {
    vi.useFakeTimers()
    try {
      vi.setSystemTime(new Date(2026, 10, 4, 12, 0, 0))
      const wrapper = mountView()
      await flushPromises()
      await nextTick()

      const vm = wrapper.vm as unknown as { filterStatus: string }
      vm.filterStatus = 'failed'

      await wrapper.find('[data-testid="admin-notification-log-apply"]').trigger('click')
      await flushPromises()
      await nextTick()

      const filtered = wrapper.find('[data-testid="admin-notification-log-empty-filters"]')
      expect(filtered.exists()).toBe(true)
      expect(filtered.text()).toContain('No deliveries match these filters')
      expect(filtered.text()).toContain(
        'Widen the date range, or clear the event type and status filters.',
      )

      const emptyReset = filtered.find('[data-testid="admin-notification-log-empty-reset"]')
      expect(emptyReset.exists()).toBe(true)
      expect(emptyReset.text()).toBe('Reset filters')
      expect(emptyReset.attributes('aria-label')).toBe('Reset all filters')

      await emptyReset.trigger('click')
      await flushPromises()
      expect(deliveryCalls()).toContainEqual([
        '/api/v1/admin/notifications/deliveries',
        { params: { query: { limit: 50, from: '2026-10-06', to: '2026-11-04' } } },
      ])
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('renders filter controls', async () => {
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="admin-notification-log-status"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="admin-notification-log-event-type"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="admin-notification-log-date-from"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="admin-notification-log-date-to"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="admin-notification-log-apply"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="admin-notification-log-reset"]').exists()).toBe(true)
    expect((wrapper.find('[data-testid="admin-notification-log-date-from"]').element as HTMLInputElement).value).toMatch(/^\d{4}-\d{2}-\d{2}$/)
    expect((wrapper.find('[data-testid="admin-notification-log-date-to"]').element as HTMLInputElement).value).toMatch(/^\d{4}-\d{2}-\d{2}$/)
  })

  it('sends the last-30-days default range on first load', async () => {
    vi.useFakeTimers()
    try {
      vi.setSystemTime(new Date(2026, 10, 4, 12, 0, 0))
      const wrapper = mountView()
      await flushPromises()
      await nextTick()
      const calls = deliveryCalls()
      expect(calls.length).toBeGreaterThan(0)
      expect(calls[0][1]).toEqual({
        params: { query: { limit: 50, from: '2026-10-06', to: '2026-11-04' } },
      })
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('reset restores the default range and clears the other filters', async () => {
    vi.useFakeTimers()
    try {
      vi.setSystemTime(new Date(2026, 10, 4, 12, 0, 0))
      const wrapper = mountView()
      await flushPromises()
      await nextTick()
      const vm = wrapper.vm as unknown as {
        filterStatus: string
        filterEventType: string
        filterDateFrom: string
        filterDateTo: string
      }
      vm.filterStatus = 'failed'
      vm.filterEventType = 'run_failed'
      vm.filterDateFrom = '2026-01-01'
      vm.filterDateTo = '2026-01-31'

      await wrapper.find('[data-testid="admin-notification-log-reset"]').trigger('click')
      await flushPromises()

      const calls = deliveryCalls()
      expect(calls[calls.length - 1][1]).toEqual({
        params: { query: { limit: 50, from: '2026-10-06', to: '2026-11-04' } },
      })
      expect(vm.filterStatus).toBe('__all__')
      expect(vm.filterEventType).toBe('__all__')
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('omits the date bounds when no date is selected', async () => {
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    const vm = wrapper.vm as unknown as {
      filterDateFrom: string
      filterDateTo: string
    }
    vm.filterDateFrom = ''
    vm.filterDateTo = ''

    await wrapper.find('[data-testid="admin-notification-log-apply"]').trigger('click')
    await flushPromises()

    const calls = deliveryCalls()
    expect(calls[calls.length - 1][1]).toEqual({ params: { query: { limit: 50 } } })
  })

  it('renders pagination controls when items exist', async () => {
    mockGet().mockResolvedValue({
      data: {
        items: [
          {
            id: '1',
            event_type: 'run_failed',
            status: 'failed',
            attempt_count: 3,
            response_code: 500,
            last_error: 'Internal server error',
            response_body: null,
            endpoint_url: 'https://example.com/hook',
            endpoint_id: 'ep-1',
            created_at: '2025-06-30T12:00:00Z',
          },
        ],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="admin-notification-log-previous"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="admin-notification-log-next"]').exists()).toBe(true)
  })

  it('renders event types and statuses with their human labels, never raw snake_case (FAR-2596)', async () => {
    mockGet().mockResolvedValue({
      data: {
        items: [
          {
            id: '1',
            event_type: 'run_failed',
            status: 'dead_lettered',
            attempt_count: 4,
            response_code: 500,
            last_error: 'Internal server error',
            response_body: null,
            endpoint_url: 'https://example.com/hook',
            endpoint_id: 'ep-1',
            created_at: '2025-06-30T12:00:00Z',
          },
        ],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    const row = wrapper.find('tbody tr')
    expect(row.exists()).toBe(true)
    expect(row.text()).toContain('Run Failed')
    expect(row.text()).toContain('Dead Lettered')
    expect(row.text()).not.toContain('run_failed')
    expect(row.text()).not.toContain('dead_lettered')
  })

  it('explains what Dead Lettered means under the table (FAR-2596)', async () => {
    mockGet().mockResolvedValue({
      data: {
        items: [
          {
            id: '1',
            event_type: 'run_failed',
            status: 'dead_lettered',
            attempt_count: 4,
            response_code: 500,
            last_error: 'Internal server error',
            response_body: null,
            endpoint_url: 'https://example.com/hook',
            endpoint_id: 'ep-1',
            created_at: '2025-06-30T12:00:00Z',
          },
        ],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    const help = wrapper.find('[data-testid="admin-notification-log-dead-lettered-help"]')
    expect(help.exists()).toBe(true)
    expect(help.text()).toBe(
      'Dead Lettered means the delivery gave up after the final attempt; retrying starts a fresh attempt.',
    )
  })

  it('loads the next page and jumps to the dead-letter queue', async () => {
    const deadLetteredEntry = {
      id: '1',
      event_type: 'run_failed',
      status: 'dead_lettered',
      attempt_count: 3,
      response_code: 500,
      last_error: 'Internal server error',
      response_body: null,
      endpoint_url: 'https://example.com/hook',
      endpoint_id: 'ep-1',
      created_at: '2025-06-30T12:00:00Z',
    }
    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation(
      (url: string) => {
        if (url === '/api/v1/admin/notifications/available-events') {
          return Promise.resolve({ data: mockAvailableEvents, error: undefined })
        }
        if (url === '/api/v1/notifications') {
          return Promise.resolve({ data: mockEndpoints, error: undefined })
        }
        return Promise.resolve({
          data: { items: [deadLetteredEntry], total: 1, next_cursor: 'cursor-2' },
          error: undefined,
        })
      },
    )

    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="admin-notification-log-next"]').trigger('click')
    await flushPromises()
    expect(deliveryCalls().at(-1)![1]).toMatchObject({
      params: { query: expect.objectContaining({ cursor: 'cursor-2' }) },
    })

    await wrapper.find('[data-testid="admin-notification-log-dlq-filter"]').trigger('click')
    await flushPromises()
    expect(deliveryCalls().at(-1)![1]).toMatchObject({
      params: { query: expect.objectContaining({ status: 'dead_lettered' }) },
    })
    wrapper.unmount()
  })

  it('expands a delivery row via keyboard (Enter and Space) for a11y', async () => {
    mockGet().mockResolvedValue({
      data: {
        items: [
          {
            id: '1',
            event_type: 'run_failed',
            status: 'failed',
            attempt_count: 3,
            response_code: 500,
            last_error: 'Internal server error',
            response_body: null,
            endpoint_url: 'https://example.com/hook',
            endpoint_id: 'ep-1',
            created_at: '2025-06-30T12:00:00Z',
          },
        ],
        total: 1,
        next_cursor: null,
      },
      error: undefined,
    })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    const row = wrapper.find('tr[tabindex="0"]')
    expect(row.exists()).toBe(true)

    // Enter toggles the row expansion open (detail row with colspan=8).
    await row.trigger('keydown', { key: 'Enter' })
    await nextTick()
    expect(wrapper.find('td[colspan="8"]').exists()).toBe(true)

    // Enter again collapses it.
    await row.trigger('keydown', { key: 'Enter' })
    await nextTick()
    expect(wrapper.find('td[colspan="8"]').exists()).toBe(false)

    // Space re-opens the expansion (keyboard-only equivalent of the click).
    await row.trigger('keydown', { key: ' ' })
    await nextTick()
    expect(wrapper.find('td[colspan="8"]').exists()).toBe(true)
  })

  it('offers every registry event in the event-type filter (FAR-1319)', async () => {
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    // The filter's event list is fetched from the server-side registry, not
    // hardcoded — hitl_deadline_warning and the other later-registered events
    // must be selectable without a frontend edit.
    expect(api.GET).toHaveBeenCalledWith('/api/v1/admin/notifications/available-events')

    // The event-type Select is the second AppSelect in the filter bar.
    const eventSelect = wrapper.findAllComponents(AppSelect).at(1)
    expect(eventSelect).toBeDefined()
    const options = eventSelect!.props('options') as Array<{ value: string; label: string }>
    const values = options.map((o) => o.value)
    expect(values).toContain('__all__')
    expect(values).toContain('hitl_deadline_warning')
    expect(values).toContain('circuit_breaker_tripped')

    // Every registry event renders a human-readable label, never its raw
    // snake_case name: the later-registered events used to fall through to
    // the raw name for want of an en-US key (FAR-1319 review).
    const labelFor = (value: string) => options.find((o) => o.value === value)?.label
    expect(labelFor('run_stalled')).toBe('Run Stalled')
    expect(labelFor('budget_exceeded')).toBe('Budget Exceeded')
    expect(labelFor('circuit_breaker_tripped')).toBe('Circuit Breaker Tripped')
    expect(labelFor('trigger_deactivated')).toBe('Trigger Deactivated')
    expect(labelFor('trigger_streak_alert')).toBe('Trigger Streak Alert')
    expect(labelFor('hitl_deadline_warning')).toBe('HITL Deadline Warning')
  })

  it('falls back to the raw event name when no i18n label exists (FAR-1319)', async () => {
    const getMock = vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>)
    getMock.mockImplementation((url: string) => {
      if (url === '/api/v1/admin/notifications/available-events') {
        return Promise.resolve({ data: ['brand_new_event'], error: undefined })
      }
      if (url === '/api/v1/notifications') {
        return Promise.resolve({ data: mockEndpoints, error: undefined })
      }
      return Promise.resolve({ data: { items: [], total: 0, next_cursor: null }, error: undefined })
    })

    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    // An event registered on the server before its en-US label ships must
    // still be selectable — it degrades to its raw snake_case name rather than
    // rendering "undefined" or dropping out of the filter.
    const eventSelect = wrapper.findAllComponents(AppSelect).at(1)
    const options = eventSelect!.props('options') as Array<{ value: string; label: string }>
    const option = options.find((o) => o.value === 'brand_new_event')
    expect(option).toBeDefined()
    expect(option!.label).toBe('brand_new_event')
  })

  it('surfaces an inline retry when the registry fetch fails (FAR-1319)', async () => {
    const getMock = vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>)
    let registryFails = true
    getMock.mockImplementation((url: string) => {
      if (url === '/api/v1/admin/notifications/available-events') {
        return registryFails
          ? Promise.resolve({ data: undefined, error: { detail: 'boom' } })
          : Promise.resolve({ data: mockAvailableEvents, error: undefined })
      }
      if (url === '/api/v1/notifications') {
        return Promise.resolve({ data: mockEndpoints, error: undefined })
      }
      return Promise.resolve({ data: { items: [], total: 0, next_cursor: null }, error: undefined })
    })

    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    // A failed registry fetch degrades the filter to "All types" but is no
    // longer silent: a small inline retry mirrors the team picker.
    const msg = wrapper.find('[data-testid="admin-notification-log-events-unavailable"]')
    expect(msg.exists()).toBe(true)
    const retry = wrapper.find('[data-testid="admin-notification-log-events-retry"]')
    expect(retry.exists()).toBe(true)

    registryFails = false
    await retry.trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="admin-notification-log-events-unavailable"]').exists()).toBe(false)
  })
})
