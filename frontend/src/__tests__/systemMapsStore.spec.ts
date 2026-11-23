import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const mockGet = vi.hoisted(() => vi.fn())
const mockGetAccessToken = vi.hoisted(() => vi.fn())
const mockIsOperator = vi.hoisted(() => ({ value: false }))
const mockIsSystemAdmin = vi.hoisted(() => ({ value: false }))
const mockFlag = vi.hoisted(() => ({ systemMaps: false }))
const mockPlanStore = vi.hoisted(() => ({
  loaded: false,
  currentTier: 'community',
  userPermissions: [] as string[],
  fetchPlan: vi.fn(),
  fetchPermissions: vi.fn(),
  featureEnabled: (name: string) => (name === 'system_maps' ? mockFlag.systemMaps : false),
}))

vi.mock('../lib/api/client', () => ({
  api: { GET: mockGet },
  getAccessToken: mockGetAccessToken,
}))
vi.mock('../stores/planStore', () => ({ usePlanStore: () => mockPlanStore }))
vi.mock('../composables/useCurrentUser', () => ({
  useCurrentUser: () => ({ isOperator: mockIsOperator, isSystemAdmin: mockIsSystemAdmin }),
}))

import { isReleaseGatedVisibility, useSystemMapsStore } from '../stores/systemMaps'

function jwtFor(sub: string, claims: Record<string, unknown> = {}): string {
  const payload = btoa(JSON.stringify({ sub, ...claims }))
  return `header.${payload}.signature`
}

function configureAuthorizedAccess(): void {
  mockIsOperator.value = true
  mockPlanStore.loaded = true
  mockFlag.systemMaps = true
  mockPlanStore.userPermissions = ['system_maps_access']
}

describe('useSystemMapsStore access', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    mockGet.mockReset()
    mockGetAccessToken.mockReturnValue(jwtFor('operator-1'))
    mockIsOperator.value = false
    mockIsSystemAdmin.value = false
    mockPlanStore.loaded = false
    mockPlanStore.currentTier = 'community'
    mockFlag.systemMaps = false
    mockPlanStore.userPermissions = []
    mockPlanStore.fetchPlan.mockReset()
    mockPlanStore.fetchPlan.mockResolvedValue(undefined)
    mockPlanStore.fetchPermissions.mockReset()
    mockPlanStore.fetchPermissions.mockResolvedValue(undefined)
  })

  it('enables authoring when the plan flag is on and role plus grant are present', async () => {
    configureAuthorizedAccess()

    const store = useSystemMapsStore()
    await store.fetchAccess()

    expect(store.systemMapsEnabled).toBe(true)
    expect(store.hasAccess).toBe(true)
    expect(store.accessLoaded).toBe(true)
  })

  it('never probes the system-admin feature-flag endpoint', async () => {
    configureAuthorizedAccess()
    mockPlanStore.userPermissions = ['system_maps_access', 'system.config.manage']

    const store = useSystemMapsStore()
    await store.fetchAccess()

    expect(mockGet).not.toHaveBeenCalled()
  })

  it('denies authoring when the system_maps flag is off', async () => {
    configureAuthorizedAccess()
    mockFlag.systemMaps = false

    const store = useSystemMapsStore()
    await store.fetchAccess()

    expect(store.systemMapsEnabled).toBe(false)
    expect(store.hasAccess).toBe(false)
  })

  it('denies authoring without the system_maps_access grant', async () => {
    configureAuthorizedAccess()
    mockPlanStore.userPermissions = []

    const store = useSystemMapsStore()
    await store.fetchAccess()

    expect(store.systemMapsEnabled).toBe(false)
    expect(store.hasAccess).toBe(false)
  })

  it('allows a system admin with no org role to author', async () => {
    mockIsSystemAdmin.value = true
    mockPlanStore.loaded = true
    mockFlag.systemMaps = true
    mockPlanStore.userPermissions = ['system_maps_access']

    const store = useSystemMapsStore()
    await store.fetchAccess()

    expect(store.systemMapsEnabled).toBe(true)
    expect(store.hasAccess).toBe(true)
  })

  it('keeps the published read model enabled for a viewer', async () => {
    mockPlanStore.loaded = true
    mockPlanStore.userPermissions = ['system_maps.read']

    const store = useSystemMapsStore()
    await store.fetchAccess()

    expect(store.systemMapsEnabled).toBe(true)
    expect(store.hasReadAccess).toBe(true)
    expect(store.hasAccess).toBe(false)
  })

  it('does not repeat an access probe after access has settled', async () => {
    configureAuthorizedAccess()
    const store = useSystemMapsStore()

    await store.fetchAccess()
    await store.fetchAccess()

    expect(mockPlanStore.fetchPermissions).toHaveBeenCalledTimes(1)
  })

  it('resets access state when the token principal changes', async () => {
    configureAuthorizedAccess()
    const store = useSystemMapsStore()

    await store.fetchAccess()
    expect(store.accessLoaded).toBe(true)

    mockGetAccessToken.mockReturnValue(jwtFor('operator-2'))
    mockPlanStore.userPermissions = []
    await store.fetchAccess()

    expect(store.accessLoaded).toBe(true)
    expect(store.systemMapsEnabled).toBe(false)
  })

  it('ignores a stale authorization result after a same-subject token demotion', async () => {
    configureAuthorizedAccess()
    let resolveOldPermissions: ((value: unknown) => void) | undefined
    mockPlanStore.fetchPermissions.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOldPermissions = resolve
        }),
    )
    const store = useSystemMapsStore()

    const oldRequest = store.fetchAccess()
    await vi.waitFor(() => expect(mockPlanStore.fetchPermissions).toHaveBeenCalledTimes(1))

    mockGetAccessToken.mockReturnValue(jwtFor('operator-1', { org_role: 'viewer' }))
    mockIsOperator.value = false
    mockPlanStore.userPermissions = []
    await store.fetchAccess()

    resolveOldPermissions?.(undefined)
    await oldRequest

    expect(store.systemMapsEnabled).toBe(false)
    expect(store.accessLoaded).toBe(true)
    expect(store.error).toBeNull()
  })

  it('resets access state when reset is called', async () => {
    configureAuthorizedAccess()
    const store = useSystemMapsStore()

    await store.fetchAccess()
    store.reset()

    expect(store.accessLoaded).toBe(false)
    expect(store.systemMapsEnabled).toBe(false)
    expect(store.error).toBeNull()
    expect(store.loaded).toBe(false)
  })

  it('fails closed when no access token is present', async () => {
    mockGetAccessToken.mockReturnValue(null)
    const store = useSystemMapsStore()

    await store.fetchAccess()

    expect(mockGet).not.toHaveBeenCalled()
    expect(store.systemMapsEnabled).toBe(false)
    expect(store.accessLoaded).toBe(true)
  })

  it('deduplicates concurrent access probes', async () => {
    configureAuthorizedAccess()
    let resolvePermissions: ((value: unknown) => void) | undefined
    mockPlanStore.fetchPermissions.mockImplementation(() => new Promise((resolve) => { resolvePermissions = resolve }))
    const store = useSystemMapsStore()

    const first = store.fetchAccess()
    const second = store.fetchAccess()
    await vi.waitFor(() => expect(mockPlanStore.fetchPermissions).toHaveBeenCalledTimes(1))

    resolvePermissions?.(undefined)
    await Promise.all([first, second])

    expect(mockPlanStore.fetchPermissions).toHaveBeenCalledTimes(1)
  })

  it('fetches the plan before permissions for an unloaded community-tier store', async () => {
    mockIsOperator.value = true
    mockPlanStore.currentTier = 'community'

    const store = useSystemMapsStore()
    await store.fetchAccess()

    expect(mockPlanStore.fetchPlan).toHaveBeenCalledTimes(1)
    expect(mockPlanStore.fetchPermissions).toHaveBeenCalledTimes(1)
  })

  it('does not fetch the plan for a known non-community tier', async () => {
    mockIsOperator.value = true
    mockPlanStore.currentTier = 'team'

    const store = useSystemMapsStore()
    await store.fetchAccess()

    expect(mockPlanStore.fetchPlan).not.toHaveBeenCalled()
    expect(mockPlanStore.fetchPermissions).toHaveBeenCalledTimes(1)
  })

  it('records an Error rejection and remains disabled', async () => {
    configureAuthorizedAccess()
    mockPlanStore.loaded = false
    mockPlanStore.fetchPlan.mockRejectedValue(new Error('plan unavailable'))

    const store = useSystemMapsStore()
    await store.fetchAccess()

    expect(store.error).toBe('plan unavailable')
    expect(store.systemMapsEnabled).toBe(false)
    expect(store.accessLoaded).toBe(true)
  })

  it('uses a stable message for non-Error failures', async () => {
    configureAuthorizedAccess()
    mockPlanStore.loaded = false
    mockPlanStore.fetchPlan.mockRejectedValue('not an Error instance')

    const store = useSystemMapsStore()
    await store.fetchAccess()

    expect(store.error).toBe('Could not read System Maps feature flags')
    expect(store.systemMapsEnabled).toBe(false)
  })

  it('stays disabled when the flag is off for every permission shape', async () => {
    const store = useSystemMapsStore()

    for (const permissions of [[], ['system_maps_access'], ['system_maps.read']]) {
      mockIsOperator.value = true
      mockPlanStore.loaded = true
      mockFlag.systemMaps = false
      mockPlanStore.userPermissions = permissions
      store.reset()
      await store.fetchAccess()
      expect(store.hasAccess).toBe(false)
    }
  })

  it('keeps discovery fail-closed until its endpoint exists', async () => {
    const store = useSystemMapsStore()

    await store.fetchDiscovery()

    expect(store.loaded).toBe(true)
  })
})

describe('useSystemMapsStore localized errors', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    mockGet.mockReset()
  })

  it('uses the localized fallback when listing maps fails with a non-Error', async () => {
    mockGet.mockRejectedValueOnce('offline')

    const store = useSystemMapsStore()
    await store.fetchMaps()

    expect(store.error).toBe('Could not load system maps')
  })

  it('uses the localized fallback when loading a map fails with a non-Error', async () => {
    mockGet.mockRejectedValueOnce('offline')

    const store = useSystemMapsStore()
    await store.loadMap('map-1')

    expect(store.error).toBe('Could not load system map')
  })
})

function overviewPayload(overrides: Record<string, unknown> = {}) {
  return {
    system_map_id: 'map',
    revision_id: 'revision',
    revision_number: 1,
    generated_at: '2026-01-08T00:00:00Z',
    window_days: 7,
    window_start: '2026-01-01T00:00:00Z',
    window_end: '2026-01-08T00:00:00Z',
    nodes: [],
    loop_configs: [],
    bottlenecks: [],
    ...overrides,
  }
}

function mapItem(id: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    organisation_id: 'org',
    name: id,
    description: null,
    owner_team_id: null,
    visibility: 'org',
    default_map: false,
    published_revision_id: null,
    account_id: 'account',
    updated_by: null,
    deleted_at: null,
    deleted_by: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function listPayload(
  items: ReturnType<typeof mapItem>[],
  overrides: { total?: number; page?: number; page_size?: number } = {},
) {
  return { items, total: items.length, page: 1, page_size: 20, ...overrides }
}

describe('useSystemMapsStore overview', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    mockGet.mockReset()
    mockGetAccessToken.mockReturnValue(jwtFor('operator-1'))
  })

  it('loads the published default map, never a draft', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/system-maps') {
        return Promise.resolve({
          data: listPayload([
            mapItem('draft-default', { default_map: true, published_revision_id: null }),
            mapItem('published-plain', { published_revision_id: 'rev-plain' }),
            mapItem('published-default', { default_map: true, published_revision_id: 'rev-default' }),
          ]),
        })
      }
      return Promise.resolve({ data: overviewPayload({ system_map_id: 'published-default' }) })
    })
    const store = useSystemMapsStore()

    await store.fetchDefaultOverview()

    expect(mockGet).toHaveBeenCalledWith(
      '/api/v1/system-maps/{map_id}/overview',
      expect.objectContaining({
        params: { path: { map_id: 'published-default' }, query: { window_days: 7 } },
      }),
    )
    expect(store.overview?.system_map_id).toBe('published-default')
    expect(store.overviewError).toBeNull()
    expect(store.overviewEmpty).toBe(false)
    expect(store.overviewLoading).toBe(false)
  })

  it('finds a published map after the first page', async () => {
    const firstPage = Array.from({ length: 20 }, (_, index) => mapItem(`draft-${index}`))
    const published = mapItem('published-late', { published_revision_id: 'rev-late' })
    mockGet.mockImplementation((url: string, init?: { params?: { query?: { page?: number } } }) => {
      if (url === '/api/v1/system-maps') {
        const page = init?.params?.query?.page
        return Promise.resolve({
          data:
            page === 1
              ? listPayload(firstPage, { total: 21 })
              : listPayload([published], { total: 21, page: 2 }),
        })
      }
      return Promise.resolve({ data: overviewPayload({ system_map_id: 'published-late' }) })
    })
    const store = useSystemMapsStore()

    await store.fetchDefaultOverview()

    expect(mockGet).toHaveBeenCalledWith(
      '/api/v1/system-maps',
      expect.objectContaining({ params: { query: { page: 2, page_size: 20 } } }),
    )
    expect(store.overview?.system_map_id).toBe('published-late')
  })

  it('falls back to any published map when no published map is the default', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/system-maps') {
        return Promise.resolve({
          data: listPayload([
            mapItem('draft-default', { default_map: true }),
            mapItem('published-plain', { published_revision_id: 'rev-plain' }),
          ]),
        })
      }
      return Promise.resolve({ data: overviewPayload({ system_map_id: 'published-plain' }) })
    })
    const store = useSystemMapsStore()

    await store.fetchDefaultOverview()

    expect(store.overview?.system_map_id).toBe('published-plain')
  })

  it('settles to the empty state when nothing is published', async () => {
    mockGet.mockResolvedValue({
      data: listPayload([mapItem('draft', { default_map: true })]),
    })
    const store = useSystemMapsStore()

    await store.fetchDefaultOverview()

    expect(store.overview).toBeNull()
    expect(store.overviewEmpty).toBe(true)
    expect(store.overviewError).toBeNull()
    expect(store.overviewLoading).toBe(false)
    expect(mockGet).toHaveBeenCalledTimes(1)
  })

  it('settles to the empty state for an empty map list', async () => {
    mockGet.mockResolvedValue({ data: listPayload([]) })
    const store = useSystemMapsStore()

    await store.fetchDefaultOverview()

    expect(store.overviewEmpty).toBe(true)
    expect(store.overviewError).toBeNull()
  })

  it('uses the fallback for an empty API error detail', async () => {
    mockGet.mockResolvedValue({ error: { detail: '' }, data: undefined })
    const store = useSystemMapsStore()

    await store.fetchDefaultOverview()

    expect(store.overviewError).toBe('System map list request failed')
  })

  it('ignores a stale map-list response after the token changes', async () => {
    let resolveOldRequest: ((value: unknown) => void) | undefined
    mockGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOldRequest = resolve
        }),
    )
    const store = useSystemMapsStore()

    const request = store.fetchDefaultOverview()
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1))

    mockGetAccessToken.mockReturnValue(jwtFor('operator-2'))
    resolveOldRequest?.({ data: listPayload([mapItem('stale', { published_revision_id: 'rev' })]) })
    await request

    expect(store.overview).toBeNull()
    expect(store.overviewError).toBeNull()
    expect(store.overviewLoading).toBe(true)
  })

  it('ignores a stale map-list error after the token changes', async () => {
    let rejectOldRequest: ((reason?: unknown) => void) | undefined
    mockGet.mockImplementationOnce(
      () =>
        new Promise((_resolve, reject) => {
          rejectOldRequest = reject
        }),
    )
    const store = useSystemMapsStore()

    const request = store.fetchDefaultOverview()
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1))

    mockGetAccessToken.mockReturnValue(jwtFor('operator-2'))
    rejectOldRequest?.(new Error('stale map-list failure'))
    await request

    expect(store.overview).toBeNull()
    expect(store.overviewError).toBeNull()
    expect(store.overviewLoading).toBe(true)
  })

  it('reports a list failure as an error, not an empty state', async () => {
    mockGet.mockResolvedValue({ error: { detail: 'map list exploded' }, data: undefined })
    const store = useSystemMapsStore()

    await store.fetchDefaultOverview()

    expect(store.overviewEmpty).toBe(false)
    expect(store.overviewError).toBe('map list exploded')
    expect(store.overviewLoading).toBe(false)
  })

  it('reports access denial on the map list', async () => {
    mockGet.mockResolvedValue({ error: { detail: 'Forbidden' }, data: undefined })
    const store = useSystemMapsStore()

    await store.fetchDefaultOverview()

    expect(store.overviewError).toBe('Forbidden')
    expect(store.overview).toBeNull()
  })

  it('uses a stable message for a non-Error overview rejection', async () => {
    mockGet.mockRejectedValue('not an Error instance')
    const store = useSystemMapsStore()

    await store.fetchDefaultOverview()

    expect(store.overviewError).toBe('System map list request failed')
    expect(store.overviewLoading).toBe(false)
  })

  it('sends an abort signal with the list and overview requests', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/system-maps') {
        return Promise.resolve({
          data: listPayload([mapItem('published', { published_revision_id: 'rev' })]),
        })
      }
      return Promise.resolve({ data: overviewPayload() })
    })
    const store = useSystemMapsStore()

    await store.fetchDefaultOverview()

    const listInit = mockGet.mock.calls[0][1] as { signal?: unknown }
    const overviewInit = mockGet.mock.calls[1][1] as { signal?: unknown }
    expect(listInit.signal).toBeInstanceOf(AbortSignal)
    expect(overviewInit.signal).toBeInstanceOf(AbortSignal)
  })

  it('loads an overview for an explicit map id with the selected window', async () => {
    mockGet.mockResolvedValue({ data: overviewPayload({ system_map_id: 'map-x', window_days: 30 }) })
    const store = useSystemMapsStore()

    await store.fetchOverview('map-x', 30)

    expect(mockGet).toHaveBeenCalledWith(
      '/api/v1/system-maps/{map_id}/overview',
      expect.objectContaining({
        params: { path: { map_id: 'map-x' }, query: { window_days: 30 } },
      }),
    )
    expect(store.overview?.system_map_id).toBe('map-x')
    expect(store.overview?.window_days).toBe(30)
    expect(store.overviewError).toBeNull()
    expect(store.overviewLoading).toBe(false)
  })

  it('reports an overview failure response', async () => {
    mockGet.mockResolvedValue({ error: { detail: 'overview denied' }, data: undefined })
    const store = useSystemMapsStore()

    await store.fetchOverview('map-x')

    expect(store.overview).toBeNull()
    expect(store.overviewError).toBe('overview denied')
    expect(store.overviewLoading).toBe(false)
  })

  it('reports an unknown/null overview payload as an error', async () => {
    mockGet.mockResolvedValue({ data: undefined })
    const store = useSystemMapsStore()

    await store.fetchOverview('map-x')

    expect(store.overviewError).toBe('System overview request failed')
    expect(store.overview).toBeNull()
  })

  it('reports an overview access denial without dropping the loading flag', async () => {
    mockGet.mockResolvedValue({ error: { detail: 'Forbidden' }, data: undefined })
    const store = useSystemMapsStore()

    await store.fetchOverview('map-x')

    expect(store.overviewError).toBe('Forbidden')
    expect(store.overviewLoading).toBe(false)
  })

  it('replaces the overview on refresh', async () => {
    mockGet
      .mockResolvedValueOnce({ data: overviewPayload({ revision_number: 1 }) })
      .mockResolvedValueOnce({ data: overviewPayload({ revision_number: 2 }) })
    const store = useSystemMapsStore()

    await store.fetchOverview('map-x')
    await store.fetchOverview('map-x')

    expect(store.overview?.revision_number).toBe(2)
  })

  it('clears a previous error on a successful refresh', async () => {
    mockGet
      .mockResolvedValueOnce({ error: { detail: 'temporary' }, data: undefined })
      .mockResolvedValueOnce({ data: overviewPayload() })
    const store = useSystemMapsStore()

    await store.fetchOverview('map-x')
    expect(store.overviewError).toBe('temporary')

    await store.fetchOverview('map-x')
    expect(store.overviewError).toBeNull()
    expect(store.overview).not.toBeNull()
  })

  it('rejects a response when the token changes without an explicit reset', async () => {
    let resolveOldRequest: ((value: unknown) => void) | undefined
    mockGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOldRequest = resolve
        }),
    )
    const store = useSystemMapsStore()

    const request = store.fetchOverview('map-x')
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1))

    mockGetAccessToken.mockReturnValue(jwtFor('operator-2'))
    resolveOldRequest?.({ data: overviewPayload({ system_map_id: 'stale-principal' }) })
    await request

    expect(store.overview).toBeNull()
    expect(store.overviewError).toBeNull()
    expect(store.overviewLoading).toBe(true)
  })

  it('rejects an error when the token changes without an explicit reset', async () => {
    let rejectOldRequest: ((reason?: unknown) => void) | undefined
    mockGet.mockImplementationOnce(
      () =>
        new Promise((_resolve, reject) => {
          rejectOldRequest = reject
        }),
    )
    const store = useSystemMapsStore()

    const request = store.fetchOverview('map-x')
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1))

    mockGetAccessToken.mockReturnValue(jwtFor('operator-2'))
    rejectOldRequest?.(new Error('stale principal failed'))
    await request

    expect(store.overview).toBeNull()
    expect(store.overviewError).toBeNull()
    expect(store.overviewLoading).toBe(true)
  })

  it('reports a pagination safety-limit failure', async () => {
    mockGet.mockImplementation((_url: string, init?: { params?: { query?: { page?: number } } }) => {
      const page = init?.params?.query?.page ?? 1
      return Promise.resolve({
        data: listPayload(
          Array.from({ length: 20 }, (_, index) => mapItem(`draft-${page}-${index}`)),
          { total: 20001, page },
        ),
      })
    })
    const store = useSystemMapsStore()

    await store.fetchDefaultOverview()

    expect(store.overview).toBeNull()
    expect(store.overviewError).toBe('System map list pagination exceeded its safety limit')
    expect(mockGet).toHaveBeenCalledTimes(1000)
  })

  it('rejects a stale principal response after reset', async () => {
    let resolveOldRequest: ((value: unknown) => void) | undefined
    mockGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOldRequest = resolve
        }),
    )
    const store = useSystemMapsStore()

    const request = store.fetchOverview('map-x')
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1))

    mockGetAccessToken.mockReturnValue(jwtFor('operator-2'))
    store.reset()
    resolveOldRequest?.({ data: overviewPayload({ system_map_id: 'leaked' }) })
    await request

    expect(store.overview).toBeNull()
    expect(store.overviewError).toBeNull()
    expect(store.overviewLoading).toBe(false)
  })

  it('lets the newest request win over an older in-flight response', async () => {
    let resolveOldRequest: ((value: unknown) => void) | undefined
    mockGet
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveOldRequest = resolve
          }),
      )
      .mockResolvedValueOnce({ data: overviewPayload({ system_map_id: 'newer' }) })
    const store = useSystemMapsStore()

    const oldRequest = store.fetchOverview('map-old')
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1))
    await store.fetchOverview('map-new')

    resolveOldRequest?.({ data: overviewPayload({ system_map_id: 'older' }) })
    await oldRequest

    expect(store.overview?.system_map_id).toBe('newer')
    expect(store.overviewLoading).toBe(false)
  })

  it('times out a stalled overview request instead of loading forever', async () => {
    vi.useFakeTimers()
    try {
      mockGet.mockImplementation(() => new Promise(() => {}))
      const store = useSystemMapsStore()

      const request = store.fetchOverview('map-x')
      await vi.advanceTimersByTimeAsync(15000)
      await request

      expect(store.overview).toBeNull()
      expect(store.overviewError).toBe('Request timed out after 15000ms')
      expect(store.overviewLoading).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('times out a stalled map-list request instead of loading forever', async () => {
    vi.useFakeTimers()
    try {
      mockGet.mockImplementation(() => new Promise(() => {}))
      const store = useSystemMapsStore()

      const request = store.fetchDefaultOverview()
      await vi.advanceTimersByTimeAsync(15000)
      await request

      expect(store.overviewError).toBe('Request timed out after 15000ms')
      expect(store.overviewLoading).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('clears overview state on reset', async () => {
    mockGet.mockResolvedValue({ data: overviewPayload() })
    const store = useSystemMapsStore()

    await store.fetchOverview('map-x')
    store.reset()

    expect(store.overview).toBeNull()
    expect(store.overviewError).toBeNull()
    expect(store.overviewEmpty).toBe(false)
    expect(store.overviewLoading).toBe(false)
  })
})

describe('useSystemMapsStore feature entitlement (402)', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    mockGet.mockReset()
    mockGetAccessToken.mockReturnValue(jwtFor('operator-1'))
  })

  it('settles the default overview to unavailable when the list is refused', async () => {
    mockGet.mockResolvedValue({ error: { status: 402 }, data: undefined })

    const store = useSystemMapsStore()
    await store.fetchDefaultOverview()

    expect(store.overview).toBeNull()
    expect(store.overviewEmpty).toBe(false)
    expect(store.overviewError).toBeNull()
    expect(store.overviewUnavailable).toBe(true)
    expect(store.overviewLoading).toBe(false)
  })

  it('settles the overview to unavailable when the aggregate is refused', async () => {
    mockGet.mockResolvedValue({ error: { type: 'urn:problem:modulo:feature_required' }, data: undefined })

    const store = useSystemMapsStore()
    await store.fetchOverview('map-x')

    expect(store.overview).toBeNull()
    expect(store.overviewEmpty).toBe(false)
    expect(store.overviewError).toBeNull()
    expect(store.overviewUnavailable).toBe(true)
    expect(store.overviewLoading).toBe(false)
  })

  it('marks the store unavailable when loading a revision is refused', async () => {
    mockGet.mockResolvedValue({ error: { status: 402 }, data: undefined, response: { status: 402 } })

    const store = useSystemMapsStore()
    await store.loadRevision('map-x', 'rev-1')

    expect(store.unavailable).toBe(true)
    expect(store.error).toBeNull()
    expect(store.activeRevision).toBeNull()
    expect(store.graph).toBeNull()
  })

  it('marks the store unavailable when loading the current revision is refused', async () => {
    mockGet.mockResolvedValue({ error: { status: 402 }, data: undefined })

    const store = useSystemMapsStore()
    await store.loadCurrentRevision('map-x')

    expect(store.unavailable).toBe(true)
    expect(store.error).toBeNull()
    expect(store.activeRevision).toBeNull()
  })

  it('marks the store unavailable when loading a revision by time is refused', async () => {
    mockGet.mockResolvedValue({ error: { status: 402 }, data: undefined, response: { status: 402 } })

    const store = useSystemMapsStore()
    await store.loadRevisionByTime('map-x', '2026-01-01T00:00:00Z')

    expect(store.unavailable).toBe(true)
    expect(store.error).toBeNull()
    expect(store.activeRevision).toBeNull()
  })
})

describe('isReleaseGatedVisibility (FAR-2638)', () => {
  it('fails closed when the manifest route entry is missing', () => {
    expect(isReleaseGatedVisibility(undefined)).toBe(true)
    expect(isReleaseGatedVisibility(null)).toBe(true)
  })

  it('gates private_preview and in_dev routes', () => {
    expect(isReleaseGatedVisibility({ visibility: 'private_preview' })).toBe(true)
    expect(isReleaseGatedVisibility({ visibility: 'in_dev' })).toBe(true)
  })

  it('does not gate a public route or one with no visibility', () => {
    expect(isReleaseGatedVisibility({ visibility: 'public' })).toBe(false)
    expect(isReleaseGatedVisibility({})).toBe(false)
    expect(isReleaseGatedVisibility({ visibility: null })).toBe(false)
  })
})
