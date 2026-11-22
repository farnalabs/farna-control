import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import DbCapacityBanner from '../components/DbCapacityBanner.vue'
import { fetchDbCapacity, type DbCapacityInfo } from '../lib/api/dbCapacity'
import { getAccessToken } from '../lib/api/client'

/**
 * FAR-2664: the capacity banner's remediation links point at SYSTEM-group
 * pages. The system-admin (and role) gates still redirect, so a caller the gate
 * refuses used to follow a link and land silently back on the Dashboard while
 * the banner kept warning. Each link now goes either to its page (the caller
 * passes the non-tier gates) or to the locked page naming the refusal.
 */
vi.mock('../lib/api/dbCapacity', () => ({ fetchDbCapacity: vi.fn() }))
vi.mock('../lib/api/client', () => ({ getAccessToken: vi.fn() }))

// FAR-2664: a locked item without a tier must still build a valid locked link.
// The real manifest gives both remediation pages a tier, so substitute the nav
// only for the defensive no-tier case.
const navState = vi.hoisted(() => ({ groups: null as null | unknown[] }))
vi.mock('../config/navigation', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../config/navigation')>()
  return { ...actual, getNavGroups: () => navState.groups ?? actual.getNavGroups() }
})

const mockedFetch = vi.mocked(fetchDbCapacity)

function jwt(claims: Record<string, unknown>): string {
  const payload = btoa(JSON.stringify(claims))
  return `header.${payload}.signature`
}

function capacity(): DbCapacityInfo {
  return {
    capacity_percent: 92,
    mode: 'fixed',
    alert_level: 'critical',
    used_bytes: 9_000_000_000,
    capacity_bytes: 10_000_000_000,
  }
}

async function mountBanner(): Promise<ReturnType<typeof mount>> {
  mockedFetch.mockResolvedValue(capacity())
  const wrapper = mount(DbCapacityBanner)
  await nextTick()
  await nextTick()
  return wrapper
}

describe('DbCapacityBanner remediation links (FAR-2664)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    navState.groups = null
  })

  it('links an instance administrator straight at both remediation pages', async () => {
    vi.mocked(getAccessToken).mockReturnValue(jwt({ is_system_admin: true, org_role: 'admin' }))
    const wrapper = await mountBanner()

    expect(wrapper.get('[data-testid="db-capacity-run-retention-link"]').attributes('href')).toBe(
      '/admin/run-retention',
    )
    expect(wrapper.get('[data-testid="db-capacity-housekeeping-link"]').attributes('href')).toBe(
      '/admin/housekeeping',
    )
  })

  it('sends an organisation admin who is not an instance admin to the honest lock', async () => {
    vi.mocked(getAccessToken).mockReturnValue(jwt({ is_system_admin: false, org_role: 'admin' }))
    const wrapper = await mountBanner()

    const retention = wrapper.get('[data-testid="db-capacity-run-retention-link"]').attributes('href')
    expect(retention).toContain('/locked?')
    expect(retention).toContain('from=%2Fadmin%2Frun-retention')
    expect(retention).toContain('reason=system_admin')
    expect(retention).not.toBe('/')

    const housekeeping = wrapper.get('[data-testid="db-capacity-housekeeping-link"]').attributes('href')
    expect(housekeeping).toContain('/locked?')
    expect(housekeeping).toContain('reason=system_admin')
  })

  it('names the role refusal when a system admin lacks the run-retention role', async () => {
    vi.mocked(getAccessToken).mockReturnValue(jwt({ is_system_admin: true, org_role: 'viewer' }))
    const wrapper = await mountBanner()

    const retention = wrapper.get('[data-testid="db-capacity-run-retention-link"]').attributes('href')
    expect(retention).toContain('/locked?')
    expect(retention).toContain('reason=role')
    // Housekeeping carries no role requirement, so a system admin reaches it.
    expect(wrapper.get('[data-testid="db-capacity-housekeeping-link"]').attributes('href')).toBe(
      '/admin/housekeeping',
    )
  })

  it('keeps the plain destinations when the token is unreadable', async () => {
    // An opaque/absent token cannot render this banner inside the authenticated
    // shell; guessing a refusal would only hide the operator's remediation
    // path, so the pre-change destination is kept.
    vi.mocked(getAccessToken).mockReturnValue('opaque-token')
    const wrapper = await mountBanner()

    expect(wrapper.get('[data-testid="db-capacity-run-retention-link"]').attributes('href')).toBe(
      '/admin/run-retention',
    )
  })

  it('treats an absent org role as holding none, sending a system admin to the role lock', async () => {
    vi.mocked(getAccessToken).mockReturnValue(jwt({ is_system_admin: true }))
    const wrapper = await mountBanner()

    const retention = wrapper.get('[data-testid="db-capacity-run-retention-link"]').attributes('href')
    expect(retention).toContain('/locked?')
    expect(retention).toContain('reason=role')
  })

  it('omits the tier on a locked item that carries none', async () => {
    navState.groups = [
      {
        id: 'test',
        label: 'Test',
        defaultCollapsed: false,
        items: [
          {
            to: '/admin/run-retention',
            icon: 'Clock',
            label: 'Run Retention',
            labelKey: 'k',
            systemAdminOnly: true,
            requiredRoles: ['admin'],
          },
        ],
      },
    ]
    vi.mocked(getAccessToken).mockReturnValue(jwt({ is_system_admin: false, org_role: 'admin' }))
    const wrapper = await mountBanner()

    const retention = wrapper.get('[data-testid="db-capacity-run-retention-link"]').attributes('href')
    expect(retention).toContain('reason=system_admin')
    expect(retention).not.toContain('tier=')
  })
})
