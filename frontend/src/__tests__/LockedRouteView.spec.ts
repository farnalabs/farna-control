import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { useRoute } from 'vue-router'
import LockedRouteView from '../views/LockedRouteView.vue'
import { usePlanStore, type PlanEntitlement } from '../stores/planStore'

/**
 * FAR-2664 / FAR-2665: the locked page a refused deep link lands on. It must
 * NAME the capability the caller tried to open ("Costs requires the Cost
 * governance capability"), not repeat the old silent dashboard bounce, and
 * reuse the entitlement gate for a commercial capability. The capability is an
 * entry in the plan payload's entitlement registry — never a tier string.
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

const ALL_ENTITLEMENTS: PlanEntitlement[] = [
  { key: 'identity', name: 'Identity', description: '', granted: false, flags: ['sso', 'scim', 'team_rbac'] },
  { key: 'audit_evidence', name: 'Audit and evidence', description: '', granted: false, flags: ['audit_viewer', 'admin_run_retention'] },
  { key: 'cost_governance', name: 'Cost governance', description: '', granted: false, flags: ['admin_spend_limits', 'admin_cost_controls', 'admin_cost_breakdown'] },
]

function seedPlan(tier: string, granted: string[] = []) {
  const planStore = usePlanStore()
  planStore.loaded = true
  planStore.currentTier = tier
  planStore.tierRanks = { community: 0, team: 1, enterprise: 2 }
  planStore.tierLabels = { community: 'Community', team: 'Team', enterprise: 'Enterprise' }
  planStore.entitlements = ALL_ENTITLEMENTS.map((e) => ({ ...e, granted: granted.includes(e.key) }))
  return planStore
}

describe('LockedRouteView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('names the capability and the entitlement an unentitled deep link needs', () => {
    seedPlan('community')
    const wrapper = mountLocked({
      from: '/admin/costs',
      capability: 'Costs',
      entitlement: 'cost_governance',
      reason: 'entitlement',
    })

    expect(wrapper.get('[data-testid="locked-route-header"]').text()).toContain(
      'Costs requires the Cost governance capability',
    )
    // The entitlement lock reuses FeatureGate rather than a second pattern.
    expect(wrapper.find('[data-testid^="feature-gate-lock-"]').exists()).toBe(true)
    // No "try again" link: a retry cannot help before an upgrade.
    expect(wrapper.find('[data-testid="locked-route-open"]').exists()).toBe(false)
    // But the caller is never stranded: the lock branch still offers the way
    // back to the Dashboard.
    expect(wrapper.get('[data-testid="locked-route-dashboard"]').attributes('href')).toBe('/')
  })

  it('resolves the capability from the sidebar label when the guard passed no name', () => {
    seedPlan('community')
    // A guard-produced link always carries the breadcrumb, but a view that
    // links here directly need not — the nav label still names the page.
    const wrapper = mountLocked({ from: '/settings/sso', entitlement: 'identity', reason: 'entitlement' })

    expect(wrapper.get('[data-testid="locked-route-header"]').text()).toContain(
      'SSO requires the Identity capability',
    )
  })

  it('falls back to the manifest breadcrumb when the page has no sidebar entry', () => {
    seedPlan('community')
    const wrapper = mountLocked({
      from: '/admin/runs/abc/diff',
      capability: 'Output Diff',
      entitlement: 'audit_evidence',
      reason: 'entitlement',
    })

    expect(wrapper.get('[data-testid="locked-route-header"]').text()).toContain('Output Diff')
  })

  it('explains a role refusal without offering a plan upgrade', () => {
    seedPlan('community')
    const wrapper = mountLocked({
      from: '/admin/costs',
      capability: 'Costs',
      entitlement: 'cost_governance',
      reason: 'role',
    })

    expect(wrapper.get('[data-testid="locked-route-header"]').text()).toContain(
      'Your role cannot open Costs',
    )
    const closed = wrapper.get('[data-testid="locked-route-closed"]')
    expect(closed.text()).toContain('needs a role change from your organisation administrator')
    expect(wrapper.find('[data-testid^="feature-gate-"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="locked-route-dashboard"]').attributes('href')).toBe('/')
  })

  it('names the role that would open the page, and how to ask for it (FAR-2687)', () => {
    seedPlan('community')
    const wrapper = mountLocked({
      from: '/lifecycle-maps',
      capability: 'Lifecycle Maps',
      role: 'operator,admin',
      reason: 'role',
    })

    const closed = wrapper.get('[data-testid="locked-route-closed"]')
    // An any-of role floor names every role the manifest lists, translated —
    // "ask for the role that includes it" left the caller to guess.
    expect(closed.text()).toContain('This page requires one of these roles: Operator, Admin.')
    expect(closed.text()).toContain('ask an organisation administrator to give you one of them')
    // A role lock never offers a plan upgrade: no purchase changes a role.
    expect(wrapper.find('[data-testid^="feature-gate-"]').exists()).toBe(false)
    // And it offers no "open the capability" link back into the route it just
    // refused — that link only exists once the licence covers it, and a link
    // into a denied route is the shape that could bounce the caller straight
    // back here (a loop).
    expect(wrapper.find('[data-testid="locked-route-open"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="locked-route-dashboard"]').attributes('href')).toBe('/')
  })

  it('names a single required role naturally, translating it (FAR-2687)', () => {
    seedPlan('community')
    const wrapper = mountLocked({ from: '/admin/users', capability: 'Users', role: 'admin', reason: 'role' })

    // One role reads as "requires the Admin role", not "one of these roles: Admin".
    expect(wrapper.get('[data-testid="locked-route-closed"]').text()).toContain(
      'This page requires the Admin role.',
    )
  })

  it('shows an unrecognised role key verbatim rather than dropping it (FAR-2687)', () => {
    seedPlan('community')
    const wrapper = mountLocked({ from: '/x', capability: 'X', role: 'auditor', reason: 'role' })

    // A role added to the manifest before its locale label exists still reads
    // as the role, not as an empty "requires the  role".
    expect(wrapper.get('[data-testid="locked-route-closed"]').text()).toContain(
      'This page requires the auditor role.',
    )
  })

  it('falls back to the generic role wording when the guard named no role', () => {
    seedPlan('community')
    const wrapper = mountLocked({ from: '/admin/costs', capability: 'Costs', reason: 'role' })

    expect(wrapper.get('[data-testid="locked-route-closed"]').text()).toContain(
      'it needs a role change from your organisation administrator',
    )
  })

  it('explains an instance-admin refusal', () => {
    seedPlan('team', ['audit_evidence'])
    const wrapper = mountLocked({
      from: '/admin/run-retention',
      capability: 'Run Retention',
      entitlement: 'audit_evidence',
      reason: 'system_admin',
    })

    expect(wrapper.get('[data-testid="locked-route-header"]').text()).toContain(
      'instance administrators',
    )
    const closed = wrapper.get('[data-testid="locked-route-closed"]')
    // An instance-level restriction names the system administrator, not the
    // organisation administrator (who cannot grant it).
    expect(closed.text()).toContain('system administrator can lift')
    expect(closed.text()).not.toContain('needs a role change')
    expect(closed.text()).toContain('organisation administrator cannot grant it')
    // FAR-2688: the system-admin branch must offer no link back into the route
    // that was just refused. The only "open" affordance lives inside the
    // tier-lock branch; here the sole way out is the Dashboard, so a denied
    // navigation can never be re-entered from this page (no redirect loop).
    expect(wrapper.find('[data-testid="locked-route-open"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="locked-route-dashboard"]').attributes('href')).toBe('/')
  })

  it('shows the capability as available once the licence grants it', () => {
    seedPlan('team', ['cost_governance'])
    const wrapper = mountLocked({
      from: '/admin/costs',
      capability: 'Costs',
      entitlement: 'cost_governance',
      reason: 'entitlement',
    })

    expect(wrapper.find('[data-testid="locked-route-available"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="locked-route-open"]').attributes('href')).toBe('/admin/costs')
  })

  it('refuses a protocol-relative `from` so the link cannot be an open redirect', () => {
    // Granted, so the available branch (which carries the link) renders.
    seedPlan('team', ['cost_governance'])
    const wrapper = mountLocked({
      from: '//evil.example/steal',
      capability: 'Costs',
      entitlement: 'cost_governance',
      reason: 'entitlement',
    })

    expect(wrapper.find('[data-testid="locked-route-available"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="locked-route-open"]').exists()).toBe(false)
  })

  it('refuses a `from` a browser would normalise into a protocol-relative URL', () => {
    seedPlan('team', ['cost_governance'])
    // A URL parser strips the tab before parsing, turning this into "//evil".
    const wrapper = mountLocked({ from: '/\t/evil.example', capability: 'Costs', entitlement: 'cost_governance' })

    expect(wrapper.find('[data-testid="locked-route-open"]').exists()).toBe(false)
  })

  it('falls back to a generic noun when it knows nothing about the attempted page', () => {
    seedPlan('community')
    const wrapper = mountLocked({ reason: 'entitlement', entitlement: 'identity' })

    expect(wrapper.get('[data-testid="locked-route-header"]').text()).toContain('This page')
    expect(wrapper.get('[data-testid="locked-route-header"]').text()).toContain('Identity capability')
  })

  it('uses the unknown-entitlement wording when the guard supplied no entitlement', () => {
    // The router always carries an entitlement for a commercial lock, but a view
    // that links here directly (or a manifest entry with no entitlement) may
    // not — the page must still name the capability rather than render an empty
    // capability name.
    seedPlan('community')
    const wrapper = mountLocked({ from: '/admin/costs', capability: 'Costs', reason: 'entitlement' })

    expect(wrapper.get('[data-testid="locked-route-header"]').text()).toContain(
      'needs a higher plan tier',
    )
    expect(wrapper.get('[data-testid="locked-route-header"]').text()).toContain('Costs')
  })
})
