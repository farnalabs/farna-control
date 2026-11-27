import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { usePlanStore } from '../stores/planStore'
import { api } from '../lib/api/client'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: {
            period: 'month',
            group_by: 'team',
            items: [
              { entity_id: 'team-1', entity_name: 'Alpha', total_spend_usd: 250.0, total_runs: 10 },
              { entity_id: 'team-2', entity_name: 'Beta', total_spend_usd: 150.0, total_runs: 5 },
            ],
          },
          error: undefined,
        })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.resolve({
          data: [
            { id: 'anomaly-1', anomaly_date: '2026-06-28', pipeline_id: null, amount: 150.0, baseline: 50.0, percent_above: 200.0, dismissed: false },
            { id: 'anomaly-2', anomaly_date: '2026-06-25', pipeline_id: null, amount: 90.0, baseline: 45.0, percent_above: 100.0, dismissed: true },
          ],
          error: undefined,
        })
      }
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({
          data: {
            license: { tier: 'team', has_license_key: true, is_valid: true },
            flags: [{ name: 'admin_cost_breakdown', description: '', tier: 'team', currently_active: true, depends_on: null }],
            would_activate: [],
          },
          error: undefined,
        })
      }
      if (path.startsWith('/api/v1/admin/costs/anomalies/dismiss/')) {
        return Promise.resolve({ data: null, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    }),
    PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    POST: vi.fn().mockImplementation((path: string) => {
      if (path.startsWith('/api/v1/admin/costs/anomalies/dismiss/')) {
        return Promise.resolve({ data: null, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AdminCostBreakdownView from '../views/AdminCostBreakdownView.vue'
import StatCard from '../components/ui/StatCard.vue'

describe('AdminCostBreakdownView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [createPinia()] },
    })

    await nextTick()
    await nextTick()
    await nextTick()

    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Cost Breakdown')
    expect(wrapper.find('header.page-header + .page-tabs-wrapper').exists()).toBe(true)
    expect(wrapper.findAll('header.page-header + .page-tabs-wrapper .page-tab')).toHaveLength(4)
  })

  it('uses the shared page rhythm and keeps Costs headings readable and spaced', async () => {
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [createPinia()] },
    })
    await flushPromises()

    // Header and tab bar are direct children of the shared `.page-wide`
    // container, so the existing adjacent-sibling rhythm spaces them; the
    // FeatureGate is the next sibling and owns the gaps between content cards.
    // This replaces the old ad-hoc nested `div.space-y-6` wrapper.
    const page = wrapper.get('.page-wide')
    expect(page.find('.page-wide > header.page-header').exists()).toBe(true)
    expect(page.find('.page-wide > .page-tabs-wrapper').exists()).toBe(true)
    const gate = page.get('.page-wide > [data-testid^="feature-gate-"]')
    expect(gate.classes()).toContain('space-y-6')
    expect(gate.find('.space-y-6').exists()).toBe(false)

    expect(page.get('h1[data-page-heading]').text()).toBe('Cost Breakdown')

    const teamHeading = page.get('h2')
    expect(teamHeading.text()).toContain('Per-Team Cost Breakdown')
    const teamHeader = teamHeading.element.closest('.card-header')
    expect(teamHeader).not.toBeNull()
    // The per-team card uses the shared DataCard padding like the anomaly card
    // below it; it no longer relies on one-off px-6/pt-6 header styling with an
    // unpadded content area (FAR-2394).
    expect(Array.from(teamHeader!.classList)).not.toEqual(expect.arrayContaining(['px-6', 'pt-6']))
    const teamDescription = teamHeader!.querySelector('p')
    expect(teamDescription?.textContent).toContain('Monthly spend, billable run count, and average cost per billable run')
    expect(teamDescription?.parentElement).toBe(teamHeading.element.parentElement)
    const anomalyHeader = page.findAll('.card-header')[1]
    expect(anomalyHeader.classes()).not.toEqual(expect.arrayContaining(['px-6', 'pt-6']))

    const teamCard = page.get('[data-testid="cost-team-table"]').element.closest('.card-base')
    const anomalyCard = anomalyHeader.element.closest('.card-base')
    expect(teamCard?.classList.contains('p-4')).toBe(true)
    expect(teamCard?.classList.contains('p-0')).toBe(false)
    expect(anomalyCard?.classList.contains('p-4')).toBe(true)
  })

  it('dismisses an anomaly via POST', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    const dismissButton = wrapper.find('[data-testid="cost-anomaly-dismiss-anomaly-1"]')
    expect(dismissButton.exists()).toBe(true)
    await dismissButton.trigger('click')
    await nextTick()
    await nextTick()

    expect(api.POST).toHaveBeenCalledWith('/api/v1/admin/costs/anomalies/dismiss/{anomaly_id}', {
      params: { path: { anomaly_id: 'anomaly-1' } },
    })
  })

  it('renders summary cards from org_total/org_run_count when items is empty', async () => {
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: {
            period: 'month',
            group_by: 'team',
            items: [],
            org_total: '90.138792',
            org_run_count: 42,
          },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    expect(wrapper.find('[data-testid="cost-total-spend"]').text()).toContain('90.14')
    expect(wrapper.find('[data-testid="cost-total-runs"]').text()).toContain('42')
  })

  it('renders the successful-run count and cost per successful run from the costs response', async () => {
    // FAR-2824: the costs response is the single source. $400 total spend, 15
    // total runs, 13 successful runs → cost per successful run = 400 / 13 ≈ 30.77.
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: {
            period: 'month',
            group_by: 'team',
            items: [
              { entity_id: 'team-1', entity_name: 'Alpha', total_spend_usd: 250.0, total_runs: 10 },
              { entity_id: 'team-2', entity_name: 'Beta', total_spend_usd: 150.0, total_runs: 5 },
            ],
            org_total: '400.000000',
            org_run_count: 15,
            org_successful_run_count: 13,
          },
          error: undefined,
        })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.resolve({ data: [], error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    const card = wrapper.find('[data-testid="cost-per-successful-run"]')
    expect(card.exists()).toBe(true)
    // successfulRuns = 13 (from the costs response) → 400 / 13 ≈ 30.77
    expect(card.text()).toContain('30.77')

    const countEl = wrapper.find('[data-testid="cost-successful-runs-count"]')
    expect(countEl.exists()).toBe(true)
    expect(countEl.text()).toContain('13')
  })

  it('reads total runs, successful runs and cost per successful run from one costs response (FAR-2820 / FAR-2824)', async () => {
    // The report returns the successful count from the SAME facts window as
    // Total Runs, so the two tiles and the cost-per-successful-run figure must
    // all agree - there is no second source left to straddle a month boundary.
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: {
            period: 'month',
            group_by: 'team',
            items: [{ entity_id: 'team-1', entity_name: 'Alpha', total_spend_usd: 226.0, total_runs: 0 }],
            org_total: '226.000000',
            org_run_count: 226,
            org_successful_run_count: 200,
          },
          error: undefined,
        })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.resolve({ data: [], error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    const countEl = wrapper.find('[data-testid="cost-successful-runs-count"]')
    expect(countEl.text()).toContain('200')

    const totalRuns = wrapper.find('[data-testid="cost-total-runs"]')
    expect(totalRuns.text()).toContain('226')

    // 226 / 200 = 1.13 — the pair from the single costs response.
    expect(wrapper.find('[data-testid="cost-per-successful-run"]').text()).toContain('1.13')
  })

  it('shows a dash and No successful runs when the costs response reports zero successes', async () => {
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: {
            period: 'month',
            group_by: 'team',
            items: [
              { entity_id: 'team-1', entity_name: 'Alpha', total_spend_usd: 100.0, total_runs: 5 },
            ],
            org_total: '100.000000',
            org_run_count: 5,
            org_successful_run_count: 0,
          },
          error: undefined,
        })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.resolve({ data: [], error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    const card = wrapper.find('[data-testid="cost-per-successful-run"]')
    expect(card.exists()).toBe(true)
    // Zero successful runs → dash, not Infinity/NaN
    expect(card.text()).toContain('—')

    const countEl = wrapper.find('[data-testid="cost-successful-runs-count"]')
    expect(countEl.exists()).toBe(true)
    expect(countEl.text()).toContain('No successful runs')
  })

  it('shows a dash when the costs response omits org_successful_run_count (graceful degradation)', async () => {
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: {
            period: 'month',
            group_by: 'team',
            items: [
              { entity_id: 'team-1', entity_name: 'Alpha', total_spend_usd: 100.0, total_runs: 5 },
            ],
            org_total: '100.000000',
            org_run_count: 5,
          },
          error: undefined,
        })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.resolve({ data: [], error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    const card = wrapper.find('[data-testid="cost-per-successful-run"]')
    expect(card.exists()).toBe(true)
    // No successful count in the costs response → degrade to 0 → dash.
    expect(card.text()).toContain('—')

    const countEl = wrapper.find('[data-testid="cost-successful-runs-count"]')
    expect(countEl.exists()).toBe(true)
    expect(countEl.text()).toContain('No successful runs')
  })

  it('issues no analytics request for the summary tiles (FAR-2824)', async () => {
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: {
            period: 'month',
            group_by: 'team',
            items: [
              { entity_id: 'team-1', entity_name: 'Alpha', total_spend_usd: 100.0, total_runs: 5 },
            ],
            org_total: '100.000000',
            org_run_count: 5,
            org_successful_run_count: 4,
          },
          error: undefined,
        })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.resolve({ data: [], error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    // Every figure comes from the costs response: 100 / 4 = 25.00, count 4.
    expect(wrapper.find('[data-testid="cost-per-successful-run"]').text()).toContain('25.00')
    expect(wrapper.find('[data-testid="cost-successful-runs-count"]').text()).toContain('4')

    // The removed fallback used to hit this endpoint; it must never be queried.
    const analyticsCalls = (api.GET as any).mock.calls.filter(
      (args: unknown[]) => args[0] === '/api/v1/analytics/query',
    )
    expect(analyticsCalls).toHaveLength(0)
  })

  it('shows an error alert when the anomalies request fails', async () => {
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: {
            period: 'month',
            group_by: 'team',
            items: [
              { entity_id: 'team-1', entity_name: 'Alpha', total_spend_usd: 100.0, total_runs: 5 },
            ],
          },
          error: undefined,
        })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.resolve({ data: null, error: { status: 500, message: 'Internal Server Error' } })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    const alert = wrapper.find('[data-testid="cost-anomalies-error"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('Failed to load anomalies')
  })

  it('renders the anomaly list and omits the plus sign for a non-positive percent_above', async () => {
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: {
            period: 'month',
            group_by: 'team',
            items: [
              { entity_id: 'team-1', entity_name: 'Alpha', total_spend_usd: 100.0, total_runs: 5 },
            ],
          },
          error: undefined,
        })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.resolve({
          data: [
            { id: 'anomaly-flat', anomaly_date: '2026-06-20', pipeline_id: null, amount: 50.0, baseline: 50.0, percent_above: 0, dismissed: false },
          ],
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    expect(wrapper.find('[data-testid="cost-team-table"]').exists()).toBe(true)
    const list = wrapper.find('[data-testid="cost-anomalies-list"]')
    expect(list.exists()).toBe(true)
    expect(list.text()).toContain('0% above')
  })

  it('degrades to an empty list instead of throwing when anomalies arrive in a paged envelope', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: { period: 'month', group_by: 'team', items: [] },
          error: undefined,
        })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        // The contract is a bare array; this is the paged envelope shape that
        // used to make `anomalies.value.filter` throw (FAR-2614).
        return Promise.resolve({
          data: {
            items: [
              { id: 'anomaly-1', anomaly_date: '2026-06-28', pipeline_id: null, amount: 150.0, baseline: 50.0, percent_above: 200.0, dismissed: false },
            ],
            total: 1,
          },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    // Rendering completes without a TypeError, the anomaly card shows its
    // empty state, and the shape surprise is surfaced rather than swallowed.
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.find('[data-testid="cost-anomalies-empty"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="cost-anomalies-list"]').exists()).toBe(false)
    expect(warnSpy).toHaveBeenCalled()

    warnSpy.mockRestore()
  })

  it('renders the anomalies when the payload is a bare array', async () => {
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: { period: 'month', group_by: 'team', items: [] },
          error: undefined,
        })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.resolve({
          data: [
            { id: 'anomaly-active', anomaly_date: '2026-06-28', pipeline_id: null, amount: 150.0, baseline: 50.0, percent_above: 200.0, dismissed: false },
            { id: 'anomaly-dismissed', anomaly_date: '2026-06-25', pipeline_id: null, amount: 90.0, baseline: 45.0, percent_above: 100.0, dismissed: true },
          ],
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    const list = wrapper.find('[data-testid="cost-anomalies-list"]')
    expect(list.exists()).toBe(true)
    expect(wrapper.find('[data-testid="cost-anomaly-anomaly-active"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="cost-anomaly-anomaly-dismissed"]').exists()).toBe(false)
    expect(list.text()).toContain('1 dismissed')
  })

  it('renders the empty states when there are no team rows and no anomalies', async () => {
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: { period: 'month', group_by: 'team', items: [] },
          error: undefined,
        })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.resolve({ data: [], error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    expect(wrapper.find('[data-testid="cost-team-empty"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="cost-anomalies-empty"]').exists()).toBe(true)
  })

  it('renders refused/clamped annotations and falls back to 0 for a zero-run team', async () => {
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: {
            period: 'month',
            group_by: 'team',
            items: [
              { entity_id: 'team-1', entity_name: 'Alpha', total_spend_usd: 10.0, total_runs: 0, annotations: { refused_total_usd: 12.5, clamped_total_usd: null } },
              { entity_id: 'team-2', entity_name: 'Beta', total_spend_usd: 20.0, total_runs: 4, annotations: { refused_total_usd: null, clamped_total_usd: 3.5 } },
            ],
          },
          error: undefined,
        })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.resolve({ data: [], error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    const refused = wrapper.find('[data-testid="cost-annotation-refused"]')
    expect(refused.exists()).toBe(true)
    expect(refused.text()).toContain('12.50')
    expect(wrapper.find('[data-testid="cost-annotation-clamped"]').exists()).toBe(true)
    // A team with zero runs uses the 0 fallback rather than dividing by zero.
    expect(wrapper.find('[data-testid="cost-team-table"]').text()).toContain('0.00')
  })

  it('sources the successful count from the costs response even when org_total is absent', async () => {
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: {
            period: 'month',
            group_by: 'team',
            items: [
              { entity_id: 'team-1', entity_name: 'Alpha', total_spend_usd: 400.0, total_runs: 20 },
            ],
            org_run_count: 20,
            org_successful_run_count: 16,
          },
          error: undefined,
        })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.resolve({ data: [], error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    // Without org_total the spend falls back to the item sum (400); the
    // successful count still comes from the costs response (16) → 400 / 16 = 25.00.
    const card = wrapper.find('[data-testid="cost-per-successful-run"]')
    expect(card.exists()).toBe(true)
    expect(card.text()).toContain('25.00')
    expect(wrapper.find('[data-testid="cost-successful-runs-count"]').text()).toContain('16')
  })

  it('shows an error alert when the anomalies request throws', async () => {
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: { period: 'month', group_by: 'team', items: [] },
          error: undefined,
        })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.reject(new Error('anomalies unavailable'))
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    const alert = wrapper.find('[data-testid="cost-anomalies-error"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('Failed to load anomalies')
  })

  it('shows an error alert when dismissing an anomaly fails', async () => {
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: { period: 'month', group_by: 'team', items: [] },
          error: undefined,
        })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.resolve({
          data: [
            { id: 'anomaly-1', anomaly_date: '2026-06-28', pipeline_id: null, amount: 150.0, baseline: 50.0, percent_above: 200.0, dismissed: false },
          ],
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    ;(api.POST as any).mockRejectedValueOnce(new Error('dismiss failed'))

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    const dismissButton = wrapper.find('[data-testid="cost-anomaly-dismiss-anomaly-1"]')
    expect(dismissButton.exists()).toBe(true)
    await dismissButton.trigger('click')
    await flushPromises()

    const alert = wrapper.find('[data-testid="cost-anomalies-error"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('Failed to dismiss anomaly')
  })

  it('keeps ordinary Costs typography on the product font while technical values stay scoped', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    const page = wrapper.get('.page-wide')
    expect(page.classes()).not.toContain('font-sans')
    expect(page.get('h1[data-page-heading]').classes()).not.toContain('font-mono')
    expect(page.find('.font-mono').exists()).toBe(false)
  })

  it('uses the shared dashboard metric-label typography for the summary headings', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    // FAR-2175: the Costs metric headings are deliberately the same
    // second-level label style as the Dashboard StatCard label - not the
    // larger section-heading style. Pin the contract so a future heading
    // sweep cannot silently diverge the two surfaces.
    const dashboardLabelClasses = mount(StatCard, {
      props: { label: 'Dashboard metric', value: 1 },
    }).get('p').classes()

    const metricLabels: Array<[string, string]> = [
      ['cost-total-spend', 'Total Spend (This Month)'],
      ['cost-avg-per-run', 'Avg Cost per Run'],
      ['cost-per-successful-run', 'Cost per Successful Run'],
      ['cost-total-runs', 'Total Runs'],
    ]

    for (const [testId, labelText] of metricLabels) {
      // The tile's data-testid sits on its root element, with the shared card
      // surface either being that root or a descendant of it.
      const tile = wrapper.get(`[data-testid="${testId}"]`).element
      const card = tile.classList.contains('card-base') ? tile : tile.querySelector('.card-base')
      expect(card).not.toBeNull()
      const label = Array.from(card!.querySelectorAll('p, span')).find(
        (element) => element.textContent?.trim() === labelText,
      )
      expect(label, `missing metric heading ${labelText}`).toBeTruthy()
      expect(Array.from(label!.classList).sort()).toEqual([...dashboardLabelClasses].sort())
    }
  })

  it('requests the team grouping and discloses spend with no team attribution', async () => {
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: {
            period: 'month',
            group_by: 'team',
            items: [
              { entity_id: 'team-1', entity_name: 'Alpha', total_spend_usd: 250.0, total_runs: 10 },
            ],
            org_unassigned_components: '5.5',
            legacy_total: '99.99',
            org_total: '355.49',
          },
          error: undefined,
        })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.resolve({ data: [], error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    expect(api.GET).toHaveBeenCalledWith('/api/v1/admin/costs', {
      params: { query: { group_by: 'team', period: 'month' } },
    })
    expect(wrapper.text()).toContain('team-attributed runs')
    const table = wrapper.find('[data-testid="cost-team-table"]')
    expect(table.text()).toContain('Alpha')
    expect(table.text()).toContain('250.00')

    const unattributed = wrapper.find('[data-testid="cost-team-unattributed"]')
    expect(unattributed.exists()).toBe(true)
    expect(unattributed.text()).toContain('$5.50')
    expect(unattributed.text()).toContain('unassigned component spend')
    // The per-team card uses the shared DataCard padding like the anomaly card
    // below it, so the disclosure line needs no one-off inset (FAR-2394).
    const teamCard = table.element.closest('.card-base')
    expect(teamCard?.classList.contains('p-4')).toBe(true)
    expect(unattributed.classes()).not.toEqual(expect.arrayContaining(['px-6', 'pb-6']))
    // legacy_total is NOT excluded from team rows (team-owned legacy spend is
    // written to the owner team's ledger row), so it must never be disclosed here.
    expect(unattributed.text()).not.toContain('99.99')
    expect(unattributed.text()).not.toContain('legacy')
  })

  it('omits the unattributed line when the reporting amounts are absent or unparseable', async () => {
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: {
            period: 'month',
            group_by: 'team',
            items: [
              { entity_id: 'team-1', entity_name: 'Alpha', total_spend_usd: 10.0, total_runs: 1 },
            ],
            org_unassigned_components: 'not-a-number',
          },
          error: undefined,
        })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.resolve({ data: [], error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    expect(wrapper.find('[data-testid="cost-team-unattributed"]').exists()).toBe(false)
    const unavailable = wrapper.find('[data-testid="cost-team-unattributed-unavailable"]')
    expect(unavailable.exists()).toBe(true)
    expect(unavailable.text()).toContain('could not be loaded')
  })

  it('shows neither disclosure line when unassigned spend is a genuine zero', async () => {
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: {
            period: 'month',
            group_by: 'team',
            items: [
              { entity_id: 'team-1', entity_name: 'Alpha', total_spend_usd: 10.0, total_runs: 1 },
            ],
            org_unassigned_components: '0',
          },
          error: undefined,
        })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.resolve({ data: [], error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    expect(wrapper.find('[data-testid="cost-team-unattributed"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="cost-team-unattributed-unavailable"]').exists()).toBe(false)
  })

  it('discloses unassigned spend even when there are no team rows', async () => {
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/costs') {
        return Promise.resolve({
          data: {
            period: 'month',
            group_by: 'team',
            items: [],
            org_unassigned_components: '12',
          },
          error: undefined,
        })
      }
      if (path === '/api/v1/admin/costs/anomalies') {
        return Promise.resolve({ data: [], error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { admin_cost_breakdown: true } })

    const wrapper = mount(AdminCostBreakdownView, {
      global: { plugins: [pinia] },
    })

    await flushPromises()
    await flushPromises()

    expect(wrapper.find('[data-testid="cost-team-empty"]').exists()).toBe(true)
    const unattributed = wrapper.find('[data-testid="cost-team-unattributed"]')
    expect(unattributed.exists()).toBe(true)
    expect(unattributed.text()).toContain('$12.00')
  })
})
