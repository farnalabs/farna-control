import { ref } from 'vue'
import { api } from '../lib/api/client'

/**
 * FAR-2610: node labels for a page of eval rows.
 *
 * An eval row carries a `node_id`, not the node's human label — labels live in
 * each pipeline's graph. This composable joins them for the rows currently on
 * screen with a BOUNDED read: at most one graph request per distinct pipeline
 * represented on the page, cached per pipeline id and reused across re-renders.
 *
 * It never throws to the caller: a failed graph read leaves that pipeline's rows
 * on their short-id fallback (the view adds a title attribute) and the list
 * stays usable. The cache is pruned only when the pipeline set changes.
 */

interface GraphNode {
  id?: string | null
  node_type?: string | null
  label?: string | null
}

/** A pipeline id mapped to its node labels, keyed by node id. */
type PipelineLabels = Record<string, Record<string, string>>

export function usePipelineNodeLabels() {
  // Reactive: a resolved graph fills the labels in on the already-rendered rows.
  const labelsByPipeline = ref<PipelineLabels>({})
  // Pipelines whose graph read failed, so the view can offer the full id as a
  // title and nobody re-requests the same pipeline within this set.
  const failedPipelines = ref<Record<string, boolean>>({})
  // Non-reactive: dedupes concurrent calls for the same pipeline (a re-render
  // before the first response lands must not add a second request).
  const inFlight = new Set<string>()
  // The pipeline set the cache currently represents.
  let currentPipelineIds: string[] = []

  function setChanged(ids: string[]): boolean {
    if (ids.length !== currentPipelineIds.length) return true
    return ids.some(id => !currentPipelineIds.includes(id))
  }

  /** Drop cached pipelines no longer represented on the page. */
  function pruneTo(ids: string[]): void {
    const keep = new Set(ids)
    const nextLabels: PipelineLabels = {}
    for (const [pipelineId, labels] of Object.entries(labelsByPipeline.value)) {
      if (keep.has(pipelineId)) nextLabels[pipelineId] = labels
    }
    labelsByPipeline.value = nextLabels

    const nextFailed: Record<string, boolean> = {}
    for (const [pipelineId, failed] of Object.entries(failedPipelines.value)) {
      if (keep.has(pipelineId)) nextFailed[pipelineId] = failed
    }
    failedPipelines.value = nextFailed
    currentPipelineIds = ids
  }

  async function loadPipelineLabels(pipelineId: string): Promise<void> {
    if (
      labelsByPipeline.value[pipelineId] !== undefined ||
      failedPipelines.value[pipelineId] === true ||
      inFlight.has(pipelineId)
    ) {
      return
    }
    inFlight.add(pipelineId)
    try {
      const { data, error } = await api.GET('/api/v1/pipelines/{pipeline_id}/graph', {
        params: { path: { pipeline_id: pipelineId } },
      })
      // openapi-fetch resolves an API-level failure as `{ error }` without
      // throwing, so both shapes must degrade the same way.
      if (error) throw error
      const nodes = ((data?.nodes ?? []) as GraphNode[])
      const labels: Record<string, string> = {}
      for (const node of nodes) {
        if (!node?.id) continue
        // label -> node_type -> (the view's short id when this is empty).
        labels[node.id] = node.label || node.node_type || ''
      }
      labelsByPipeline.value = { ...labelsByPipeline.value, [pipelineId]: labels }
    } catch (e) {
      failedPipelines.value = { ...failedPipelines.value, [pipelineId]: true }
      // Best-effort join: the row keeps its short id, but log rather than
      // swallow the failure.
      console.warn(`Failed to load pipeline graph for node labels (${pipelineId}):`, e)
    } finally {
      inFlight.delete(pipelineId)
    }
  }

  /**
   * Ensure the graphs for the given pipeline ids are loaded.
   *
   * One request per DISTINCT pipeline, no request for a pipeline already cached
   * or already failed, and the cache is pruned only when the set changes.
   */
  async function ensureNodeLabels(pipelineIds: string[]): Promise<void> {
    const distinct = Array.from(new Set(pipelineIds.filter(id => id !== '')))
    if (setChanged(distinct)) pruneTo(distinct)
    await Promise.all(distinct.map(loadPipelineLabels))
  }

  /** Resolved label for a node, or null when it is unresolved/unknown. */
  function nodeLabel(pipelineId: string, nodeId: string): string | null {
    const label = labelsByPipeline.value[pipelineId]?.[nodeId]
    return label ? label : null
  }

  /** True when the pipeline's graph could not be read (short id is shown). */
  function nodeLabelsUnavailable(pipelineId: string): boolean {
    return failedPipelines.value[pipelineId] === true
  }

  return {
    labelsByPipeline,
    ensureNodeLabels,
    nodeLabel,
    nodeLabelsUnavailable,
  }
}
