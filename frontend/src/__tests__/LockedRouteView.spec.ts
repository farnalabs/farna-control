import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { useRoute } from 'vue-router'
import LockedRouteView from '../views/LockedRouteView.vue'
import { usePlanStore } from '../stores/planStore'

/**
 * FAR-2664: the locked page a refused deep link lands on. It must NAME the
 * capability the caller tried to open ("Costs requires the Team licence"), not
 * repeat the old silent dashboard bounce, and reuse the entitlement gate for a
 * paid capability.
 */

function routeWith(query: Record<string, string>) {
  return {
    path: '/locked',
    fullPath: '/locked',
    params: {},
    query,
    hash: '',
    matched: [],
    name: null,
    redirectedFrom: undefined,
  }
}

function mountLocked(query: Record<string, string>) {
  vi.mocked(useRoute).mockReturnValue(routeWith(query) as never)
  return mount(LockedRouteView)
}

function seedPlan(tier: string) {
  const planStore = usePlanStore()
  planStore.loaded = true
  planStore.currentTier = tier
  planStore.tierRanks = { community: 0, team: 1, enterprise: 2 }
  planStore.tierLabels = { community: 'Community', team: 'Team', enterprise: 'Enterprise' }
  return planStore
}

describe('LockedRouteView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('names the capability and the licence a below-tier deep link needs', () => {
    seedPlan('community')
    const wrapper = mountLocked({
      from: '/admin/costs',
      capability: 'Costs',
      tier: 'team',
      reason: 'tier',
    })

    expect(wrapper.get('[data-testid="locked-route-header"]').text()).toContain(
      'Costs requires the Team licence',
    )
    // The entitlement lock reuses FeatureGate rather than a second pattern.
    expect(wrapper.find('[data-testid="feature-gate-lock"]').exists()).toBe(true)
    // No "try again" link: a retry cannot help before an upgrade.
    expect(wrapper.find('[data-testid="locked-route-open"]').exists()).toBe(false)
  })

  it('resolves the capability from the sidebar label when the guard passed no name', () => {
    seedPlan('community')
    // A guard-produced link always carries the breadcrumb, but a view that
    // links here directly need not — the nav label still names the page.
    const wrapper = mountLocked({ from: '/settings/sso', tier: 'team', reason: 'tier' })

    expect(wrapper.get('[data-testid="locked-route-header"]').text()).toContain(
      'SSO requires the Team licence',
    )
  })

  it('falls back to the manifest breadcrumb when the page has no sidebar entry', () => {
    seedPlan('community')
    const wrapper = mountLocked({
      from: '/admin/runs/abc/diff',
      capability: 'Output Diff',
      tier: 'team',
      reason: 'tier',
    })

    expect(wrapper.get('[data-testid="locked-route-header"]').text()).toContain('Output Diff')
  })

  it('explains a role refusal without offering a plan upgrade', () => {
    seedPlan('community')
    const wrapper = mountLocked({
      from: '/admin/costs',
      capability: 'Costs',
      tier: 'team',
      reason: 'role',
    })

    expect(wrapper.get('[data-testid="locked-route-header"]').text()).toContain(
      'Your role cannot open Costs',
    )
    expect(wrapper.find('[data-testid="locked-route-closed"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="feature-gate"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="locked-route-dashboard"]').attributes('href')).toBe('/')
  })

  it('explains an instance-admin refusal', () => {
    seedPlan('team')
    const wrapper = mountLocked({
      from: '/admin/run-retention',
      capability: 'Run Retention',
      tier: 'team',
      reason: 'system_admin',
    })

    expect(wrapper.get('[data-testid="locked-route-header"]').text()).toContain(
      'instance administrators',
    )
    expect(wrapper.find('[data-testid="locked-route-closed"]').exists()).toBe(true)
  })

  it('shows the capability as available once the licence covers it', () => {
    seedPlan('team')
    const wrapper = mountLocked({
      from: '/admin/costs',
      capability: 'Costs',
      tier: 'team',
      reason: 'tier',
    })

    expect(wrapper.find('[data-testid="locked-route-available"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="locked-route-open"]').attributes('href')).toBe('/admin/costs')
  })

  it('refuses a protocol-relative `from` so the link cannot be an open redirect', () => {
    // At tier, so the available branch (which carries the link) renders.
    seedPlan('team')
    const wrapper = mountLocked({
      from: '//evil.example/steal',
      capability: 'Costs',
      tier: 'team',
      reason: 'tier',
    })

    expect(wrapper.find('[data-testid="locked-route-available"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="locked-route-open"]').exists()).toBe(false)
  })

  it('refuses a `from` a browser would normalise into a protocol-relative URL', () => {
    seedPlan('team')
    // A URL parser strips the tab before parsing, turning this into "//evil".
    const wrapper = mountLocked({ from: '/\t/evil.example', capability: 'Costs', tier: 'team' })

    expect(wrapper.find('[data-testid="locked-route-open"]').exists()).toBe(false)
  })

  it('falls back to a generic noun when it knows nothing about the attempted page', () => {
    seedPlan('community')
    const wrapper = mountLocked({ reason: 'tier', tier: 'team' })

    expect(wrapper.get('[data-testid="locked-route-header"]').text()).toContain('This page')
    expect(wrapper.get('[data-testid="locked-route-header"]').text()).toContain('Team licence')
  })
})
