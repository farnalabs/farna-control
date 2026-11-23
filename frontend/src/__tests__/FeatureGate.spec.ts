import { describe, it, expect, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { usePlanStore, type PlanEntitlement } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'

/**
 * FAR-2665: the lock copy is decided by the ENTITLEMENT registry in the plan
 * payload, not by a static per-view tier prop. A flag mapped to an ungranted
 * entitlement is a commercial capability (upgrade wording + View Plans); a flag
 * no entitlement covers is behaviour the org switched off (no upgrade can help).
 */
function entitlements(overrides: Partial<PlanEntitlement>[] = []): PlanEntitlement[] {
  const base: PlanEntitlement[] = [
    { key: 'identity', name: 'Identity', description: '', granted: false, flags: ['sso', 'scim', 'team_rbac'] },
    { key: 'audit_evidence', name: 'Audit and evidence', description: '', granted: false, flags: ['audit_viewer', 'admin_run_retention'] },
    { key: 'cost_governance', name: 'Cost governance', description: '', granted: false, flags: ['admin_spend_limits', 'admin_cost_controls', 'admin_cost_breakdown'] },
  ]
  return base.map((entry) => ({ ...entry, ...(overrides.find((o) => o.key === entry.key) ?? {}) }))
}

describe('FeatureGate', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('shows content when feature is enabled', () => {
    const store = usePlanStore()
    store.$patch({ features: { 'test-feature': true } })

    const wrapper = mount(FeatureGate, {
      props: { featureName: 'test-feature' },
      slots: { default: 'Gated Content' },
    })

    expect(wrapper.text()).toContain('Gated Content')
    expect(wrapper.find('[data-testid="feature-gate-lock"]').exists()).toBe(false)
  })

  it('shows the behaviour-off copy when a flag no entitlement covers is disabled', () => {
    const store = usePlanStore()
    store.$patch({ features: { mcp_server: false }, entitlements: entitlements() })

    const wrapper = mount(FeatureGate, {
      props: { featureName: 'mcp_server' },
      slots: { default: 'Gated Content' },
    })

    const lock = wrapper.find('[data-testid="feature-gate-lock"]')
    expect(lock.exists()).toBe(true)
    expect(lock.text()).toContain('Feature not available')
    expect(lock.text()).not.toContain('Team Feature')
    expect(lock.text()).not.toContain('Available on higher plan tier')
    expect(lock.text()).toContain('turned off for your organization')
  })

  it('omits the pricing link for a behaviour-only gate', () => {
    const store = usePlanStore()
    store.$patch({ features: { mcp_server: false }, entitlements: entitlements() })

    const wrapper = mount(FeatureGate, {
      props: { featureName: 'mcp_server' },
      slots: { default: 'Gated Content' },
    })

    expect(wrapper.find('[data-testid="feature-gate-lock"] a').exists()).toBe(false)
  })

  it('renders the commercial copy and entitlement label for an ungranted capability', () => {
    const store = usePlanStore()
    store.$patch({ features: { audit_viewer: false }, entitlements: entitlements() })

    const wrapper = mount(FeatureGate, {
      props: { featureName: 'audit_viewer' },
      slots: { default: 'Gated Content' },
    })

    const lock = wrapper.find('[data-testid="feature-gate-lock"]')
    expect(lock.text()).toContain('Team Feature')
    expect(lock.find('[data-testid="lock-icon"]').attributes('title')).toBe(
      'Available on higher plan tier — Audit and evidence — /settings/license',
    )
    expect(lock.find('a').exists()).toBe(true)
  })

  it('shows the behaviour copy when the entitlement is granted but the flag is switched off', () => {
    // FAR-1283 + FAR-2665: an operator kill switch (feature_flag_catalog
    // is_active=false) turns the flag off while the licence still GRANTS the
    // entitlement. No purchase can re-enable it, so the gate must not claim
    // "Team Feature" / "Available on higher plan tier" or offer View Plans.
    const store = usePlanStore()
    store.$patch({
      features: { team_rbac: false },
      entitlements: entitlements([{ key: 'identity', granted: true }]),
    })

    const wrapper = mount(FeatureGate, {
      props: { featureName: 'team_rbac' },
      slots: { default: 'Gated Content' },
    })

    const lock = wrapper.find('[data-testid="feature-gate-lock"]')
    expect(lock.exists()).toBe(true)
    expect(lock.text()).toContain('Feature not available')
    expect(lock.text()).toContain('turned off for your organization')
    expect(lock.text()).not.toContain('Team Feature')
    expect(lock.text()).not.toContain('Available on higher plan tier')
    expect(lock.find('a').exists()).toBe(false)
  })

  it('locks directly on a requiredEntitlement (no per-capability flag)', () => {
    const store = usePlanStore()
    store.$patch({
      features: {},
      entitlements: entitlements([{ key: 'cost_governance', granted: true }]),
    })

    const lockedWrapper = mount(FeatureGate, {
      props: { requiredEntitlement: 'identity' },
      slots: { default: 'Gated Content' },
    })
    expect(lockedWrapper.find('[data-testid="feature-gate-lock"]').exists()).toBe(true)

    const openWrapper = mount(FeatureGate, {
      props: { requiredEntitlement: 'cost_governance' },
      slots: { default: 'Gated Content' },
    })
    expect(openWrapper.text()).toContain('Gated Content')
  })

  it('renders locked slot when feature is disabled', () => {
    const store = usePlanStore()
    store.$patch({ features: { 'test-feature': false }, entitlements: entitlements() })

    const wrapper = mount(FeatureGate, {
      props: { featureName: 'test-feature' },
      slots: {
        default: 'Gated Content',
        locked: '<span data-testid="custom-locked">Upgrade required</span>',
      },
    })

    expect(wrapper.find('[data-testid="custom-locked"]').exists()).toBe(true)
  })

  it('does not render locked slot when feature is enabled', () => {
    const store = usePlanStore()
    store.$patch({ features: { 'test-feature': true } })

    const wrapper = mount(FeatureGate, {
      props: { featureName: 'test-feature' },
      slots: {
        default: 'Gated Content',
        locked: '<span data-testid="custom-locked">Upgrade required</span>',
      },
    })

    expect(wrapper.find('[data-testid="custom-locked"]').exists()).toBe(false)
  })

  it('keeps the behaviour copy before the plan payload resolves', () => {
    // No entitlement payload yet: the gate must not claim a purchase would fix
    // a flag it cannot classify.
    const store = usePlanStore()
    store.$patch({ features: { audit_viewer: false }, entitlements: [] })

    const wrapper = mount(FeatureGate, {
      props: { featureName: 'audit_viewer' },
      slots: { default: 'Gated Content' },
    })

    const lock = wrapper.find('[data-testid="feature-gate-lock"]')
    expect(lock.text()).toContain('Feature not available')
    expect(lock.find('a').exists()).toBe(false)
  })

  it('applies the behaviour copy in the show-disabled banner too', () => {
    const store = usePlanStore()
    store.$patch({ features: { mcp_server: false }, entitlements: entitlements() })

    const wrapper = mount(FeatureGate, {
      props: { featureName: 'mcp_server', showDisabled: true },
      slots: { default: 'Gated Content' },
    })

    const banner = wrapper.find('[data-testid="feature-gate-disabled"]')
    expect(banner.find('[data-testid="feature-gate-title"]').text()).toBe('Feature not available')
    expect(banner.text()).not.toContain('Team Feature')
    expect(banner.find('a').exists()).toBe(false)
  })
})
