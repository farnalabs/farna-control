import { describe, it, expect, vi, beforeEach } from 'vitest'

const { apiGet } = vi.hoisted(() => ({ apiGet: vi.fn() }))

vi.mock('../../lib/api/client', () => ({
  api: { GET: apiGet },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import { usePipelineNodeLabels } from '../../composables/usePipelineNodeLabels'

interface GraphResponse {
  data?: { nodes?: unknown[] | null } | undefined
  error?: unknown
}

type GraphSpec = GraphResponse | 'defer'

/**
 * Route the single api.GET seam by pipeline id. A 'defer' spec keeps the
 * request pending until the returned resolver is called, so a test can hold
 * two calls in flight at once.
 */
function mockGraphs(map: Record<string, GraphSpec>): Array<() => void> {
  const resolvers: Array<() => void> = []
  apiGet.mockImplementation(
    (path: string, request?: { params?: { path?: { pipeline_id?: string } } }) => {
      if (path !== '/api/v1/pipelines/{pipeline_id}/graph') {
        return Promise.resolve({ data: undefined, error: { detail: `unexpected ${path}` } })
      }
      const pipelineId = request?.params?.path?.pipeline_id ?? ''
      const graph = map[pipelineId]
      if (graph === 'defer') {
        return new Promise(resolve => {
          resolvers.push(() =>
            resolve({ data: { nodes: [{ id: 'n1', label: 'Later' }] }, error: undefined }),
          )
        })
      }
      return Promise.resolve(graph ?? { data: { nodes: [] }, error: undefined })
    },
  )
  return resolvers
}

function graphCalls() {
  return apiGet.mock.calls.filter(call => call[0] === '/api/v1/pipelines/{pipeline_id}/graph')
}

function requestedPipelineIds(): string[] {
  return graphCalls()
    .map(call => (call[1] as { params: { path: { pipeline_id: string } } }).params.path.pipeline_id)
    .sort()
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('usePipelineNodeLabels', () => {
  it('resolves a label, falls back to node_type, and exposes neither as null', async () => {
    mockGraphs({
      p1: {
        data: {
          nodes: [
            { id: 'n1', label: 'Tone reviewer', node_type: 'agent' },
            { id: 'n2', node_type: 'sandbox_agent' },
            { id: 'n3' },
          ],
        },
      },
    })
    const { ensureNodeLabels, nodeLabel, nodeLabelsUnavailable } = usePipelineNodeLabels()

    await ensureNodeLabels(['p1'])

    expect(nodeLabel('p1', 'n1')).toBe('Tone reviewer')
    expect(nodeLabel('p1', 'n2')).toBe('sandbox_agent')
    // Neither label nor node_type resolves to null, so the view shows the short id.
    expect(nodeLabel('p1', 'n3')).toBeNull()
    expect(nodeLabel('p1', 'missing')).toBeNull()
    expect(nodeLabelsUnavailable('p1')).toBe(false)
  })

  it('skips nodes without an id', async () => {
    mockGraphs({
      p1: { data: { nodes: [{ node_type: 'agent' }, { id: '', label: 'blank' }, { id: 'n2', label: 'X' }] } },
    })
    const { ensureNodeLabels, labelsByPipeline, nodeLabel } = usePipelineNodeLabels()

    await ensureNodeLabels(['p1'])

    expect(Object.keys(labelsByPipeline.value.p1)).toEqual(['n2'])
    expect(nodeLabel('p1', 'n2')).toBe('X')
  })

  it('reads each distinct pipeline once and ignores empty ids', async () => {
    mockGraphs({ p1: { data: { nodes: [{ id: 'n1', label: 'One' }] } } })
    const { ensureNodeLabels, nodeLabel } = usePipelineNodeLabels()

    await ensureNodeLabels(['', 'p1', 'p1', ''])

    expect(graphCalls()).toHaveLength(1)
    expect(nodeLabel('p1', 'n1')).toBe('One')
  })

  it('does not re-request a cached or already-failed pipeline', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    mockGraphs({
      p1: { data: { nodes: [{ id: 'n1', label: 'One' }] } },
      p2: { data: undefined, error: { detail: 'down' } },
    })
    const { ensureNodeLabels, nodeLabel, nodeLabelsUnavailable } = usePipelineNodeLabels()

    await ensureNodeLabels(['p1'])
    await ensureNodeLabels(['p1'])
    expect(nodeLabel('p1', 'n1')).toBe('One')
    await ensureNodeLabels(['p2'])
    await ensureNodeLabels(['p2'])

    expect(requestedPipelineIds()).toEqual(['p1', 'p2'])
    expect(nodeLabelsUnavailable('p2')).toBe(true)
    warn.mockRestore()
  })

  it('dedupes concurrent calls for the same pipeline', async () => {
    const resolvers = mockGraphs({ p1: 'defer' })
    const { ensureNodeLabels, nodeLabel } = usePipelineNodeLabels()

    const first = ensureNodeLabels(['p1'])
    const second = ensureNodeLabels(['p1'])
    // The second call joins the in-flight read instead of issuing another.
    expect(graphCalls()).toHaveLength(1)

    resolvers.forEach(resolve => resolve())
    await Promise.all([first, second])

    expect(nodeLabel('p1', 'n1')).toBe('Later')
  })

  it('prunes cached labels when the page set changes but keeps the overlap', async () => {
    mockGraphs({
      p1: { data: { nodes: [{ id: 'n1', label: 'P1' }] } },
      p2: { data: { nodes: [{ id: 'n2', label: 'P2' }] } },
    })
    const { ensureNodeLabels, labelsByPipeline } = usePipelineNodeLabels()

    await ensureNodeLabels(['p1', 'p2'])
    await ensureNodeLabels(['p1'])

    expect(Object.keys(labelsByPipeline.value)).toEqual(['p1'])
    // p1 stays cached and is not re-read; only p1 and p2 were ever fetched.
    expect(graphCalls()).toHaveLength(2)
  })

  it('prunes failed pipelines when the page set changes but keeps the overlap', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    mockGraphs({
      p1: { data: undefined, error: { detail: 'down' } },
      p2: { data: { nodes: [] } },
    })
    const { ensureNodeLabels, nodeLabelsUnavailable } = usePipelineNodeLabels()

    await ensureNodeLabels(['p1'])
    // p1 overlaps, so its failure entry is kept during the prune.
    await ensureNodeLabels(['p1', 'p2'])
    expect(nodeLabelsUnavailable('p1')).toBe(true)

    // p1 is no longer on the page, so its failure entry is dropped.
    await ensureNodeLabels(['p2'])
    expect(nodeLabelsUnavailable('p1')).toBe(false)

    // Neither pipeline is re-read: p1 failed, p2 is cached.
    expect(graphCalls()).toHaveLength(2)
    warn.mockRestore()
  })

  it('treats a graph without a nodes array as an empty set', async () => {
    mockGraphs({
      p1: { data: {} },
      p2: { data: undefined },
      p3: { data: { nodes: null } },
    })
    const { ensureNodeLabels, nodeLabel, nodeLabelsUnavailable } = usePipelineNodeLabels()

    await ensureNodeLabels(['p1', 'p2', 'p3'])

    expect(nodeLabel('p1', 'n1')).toBeNull()
    expect(nodeLabel('p2', 'n1')).toBeNull()
    expect(nodeLabel('p3', 'n1')).toBeNull()
    expect(nodeLabelsUnavailable('p1')).toBe(false)
  })
})
