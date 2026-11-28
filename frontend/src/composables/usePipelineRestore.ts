import { computed, type Ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/schema'
import { formatApiError } from '../lib/api/formatError'
import { withTimeout } from '../lib/requestTimeout'
import {
  readBreakingChanges,
  readChangeClassCounts,
  readImpactedNodes,
  severityLabel as resolveSeverityLabel,
  type SnapshotBreakingChange,
  type SnapshotDiffPayload,
} from '../lib/pipelineSnapshotDiff'

/**
 * FAR-1816: the shared restore (rollback) dialog - banner + timeline.
 *
 * One restore dialog, backed by the existing POST /snapshots/{id}/rollback
 * endpoint. Restore replays the chosen version's graph into the live pipeline
 * and creates a NEW version from it (history is never rewritten), and the
 * server's own guards still refuse a weakening restore. The dialog shows the
 * old-vs-latest change summary from the diff endpoint and names the
 * consequences before the user confirms.
 *
 * Extracted verbatim from PipelineEditorView. The view keeps the dialog STATE
 * (its markup binds those refs, and the component specs read/write them
 * directly) plus the two notice-copy helpers; this module owns the behaviour -
 * the change-summary load, its sequence guard, the refusal copy and the
 * rollback orchestration.
 */

/** The dialog's reactive state, owned by the view that renders the markup. */
export interface RestoreDialogState {
  showRestoreConfirm: Ref<boolean>
  restoreLoading: Ref<boolean>
  restoreError: Ref<string | null>
  restoreTargetId: Ref<string | null>
  restoreTargetVersion: Ref<number | null>
  restoreDiff: Ref<SnapshotDiffPayload>
  restoreDiffLoading: Ref<boolean>
  restoreDiffError: Ref<string | null>
  /** C2: the early-return outcomes (see loadRestoreDiff). */
  restoreDiffIsCurrent: Ref<boolean>
  restoreDiffNoBaseline: Ref<boolean>
}

export interface UsePipelineRestoreOptions {
  /** Pipeline id from the route params, as the view resolved it. */
  pipelineId: string
  /** The version-DESCENDING snapshot catalog; its head is the live config. */
  snapshots: Ref<any[]>
  /** The currently resolved historic snapshot id, if any. */
  resolvedSnapshotId: Ref<string | null>
  /** The snapshot detail the historic view is showing, if any. */
  viewSnapshot: Ref<components['schemas']['SnapshotDetailResponse'] | null>
  /** Whether the live graph has unsaved edits (the shared leave guard). */
  isGraphDirty: Ref<boolean>
  /** The shared unsaved-changes guard; resolves true to proceed. */
  confirmLeaveUnsaved: () => Promise<boolean>
  /** Reload the live graph after a successful rollback. */
  loadGraph: () => Promise<unknown>
  /** Reload the catalog (version chip) after a successful rollback. */
  loadCatalog: () => Promise<unknown>
  /** Leave the historic view and land back in the latest editor. */
  viewLatest: () => void
  /** The timeline component binding, so a new version shows as a rollback. */
  timelineRef: Ref<{ reload: () => Promise<void> } | null>
  /** Show the success notice (the view owns the notice ref and its timer). */
  setRestoreNotice: (message: string) => void
  /** Build the success copy for the versions a restore reported. */
  restoreSuccessNotice: (restored: number | null, created: number | null) => string
  /** The dialog refs, owned by the view. */
  state: RestoreDialogState
}

interface RestoreDiffSummary {
  nodesRestored: number
  nodesRemoved: number
  nodesModified: number
  edgesRestored: number
  edgesRemoved: number
  edgesModified: number
  impacted: number
  breaking: SnapshotBreakingChange[]
  /** True when restoring would change ANYTHING (see the predicate below). */
  hasChanges: boolean
}

export function usePipelineRestore({
  pipelineId,
  snapshots,
  resolvedSnapshotId,
  viewSnapshot,
  isGraphDirty,
  confirmLeaveUnsaved,
  loadGraph,
  loadCatalog,
  viewLatest,
  timelineRef,
  setRestoreNotice,
  restoreSuccessNotice,
  state,
}: UsePipelineRestoreOptions) {
  const { t } = useI18n()

  const {
    showRestoreConfirm,
    restoreLoading,
    restoreError,
    restoreTargetId,
    restoreTargetVersion,
    restoreDiff,
    restoreDiffLoading,
    restoreDiffError,
    restoreDiffIsCurrent,
    restoreDiffNoBaseline,
  } = state

  // M6: sequence guard so an earlier dialog's diff cannot land after a later one.
  let restoreDiffRequestSeq = 0

  const restoreDiffSummary = computed<RestoreDiffSummary | null>(() => {
    const d = restoreDiff.value
    if (!d) return null
    // FAR-1893: the change-class enumeration, the semantic readers and the
    // severity label all come from the shared module. Only this dialog's LAYOUT
    // (the list of counts) stays local.
    const counts = readChangeClassCounts(d)
    const breaking = readBreakingChanges(d)
    const impacted = readImpactedNodes(d).length
    // Deliberately broader than the compare view's `hasStructuralChanges`
    // (structural only): this dialog answers "does restoring this version
    // change anything at all?", so a semantic-only difference (an impacted node
    // or a breaking port change) still means "not identical". Both predicates
    // are expressed from the same shared counts, so neither can drift on the
    // structural half.
    const hasChanges = counts.hasChanges || impacted > 0 || breaking.length > 0
    return {
      nodesRestored: counts.nodesAdded,
      nodesRemoved: counts.nodesRemoved,
      nodesModified: counts.nodesModified,
      edgesRestored: counts.edgesAdded,
      edgesRemoved: counts.edgesRemoved,
      edgesModified: counts.edgesModified,
      impacted,
      breaking,
      hasChanges,
    }
  })

  /** Shared severity rule (neutral namespace), injected with this view's `t`. */
  function restoreSeverityLabel(severity?: string): string {
    return resolveSeverityLabel(severity, t)
  }

  async function loadRestoreDiff(): Promise<void> {
    const seq = ++restoreDiffRequestSeq
    restoreDiff.value = null
    restoreDiffError.value = null
    restoreDiffIsCurrent.value = false
    restoreDiffNoBaseline.value = false
    const target = restoreTargetId.value
    // The newest snapshot is the closest representation of the live/current
    // config. Without it (or when the target IS the current snapshot) there is
    // nothing to compare — but that outcome must still be stated explicitly
    // (C2), never left as an empty box.
    const latest = snapshots.value.at(0)?.id as string | undefined
    if (!target || !latest) {
      restoreDiffNoBaseline.value = true
      return
    }
    if (target === latest) {
      restoreDiffIsCurrent.value = true
      return
    }
    restoreDiffLoading.value = true
    try {
      const { data, error } = await withTimeout((signal) =>
        api.POST('/api/v1/pipelines/{pipeline_id}/snapshots/diff', {
          params: { path: { pipeline_id: pipelineId } },
          body: { snapshot_a_id: latest, snapshot_b_id: target },
          signal,
        }),
      )
      // C2/M6: an earlier dialog's response must not land after a later one.
      if (seq !== restoreDiffRequestSeq) return
      if (error) {
        console.warn('[PipelineEditorView] failed to load restore diff', formatApiError(error))
        restoreDiffError.value = formatApiError(error)
        return
      }
      restoreDiff.value = data ?? null
    } catch (e: unknown) {
      if (seq !== restoreDiffRequestSeq) return
      console.warn('[PipelineEditorView] failed to load restore diff', formatApiError(e))
      restoreDiffError.value = formatApiError(e)
    } finally {
      if (seq === restoreDiffRequestSeq) restoreDiffLoading.value = false
    }
  }

  /**
   * FAR-1816: turn a restore refusal into human language. The server refuses a
   * restore that would weaken a HITL gate, strip a node-bound guardrail, or pin
   * another team's connector — those must read as plain sentences, never the raw
   * server string. Any UNCLASSIFIED refusal gets generic human copy too (the
   * detail is logged for diagnostics); the raw server text is never rendered.
   */
  function describeRestoreRefusal(err: unknown): string {
    const raw = formatApiError(err)
    if (/connector_team_mismatch/i.test(raw)) {
      return t('views.PipelineEditorView.historic_restore_blocked_connector')
    }
    if (/gate weakening denied/i.test(raw)) {
      return t('views.PipelineEditorView.historic_restore_blocked_hitl')
    }
    if (/guardrail/i.test(raw)) {
      return t('views.PipelineEditorView.historic_restore_blocked_guardrail')
    }
    // M8: never surface the raw server string as user-facing copy.
    console.warn('[PipelineEditorView] restore refused', raw)
    return t('views.PipelineEditorView.historic_restore_failed_generic')
  }

  async function openRestoreConfirm(snapshotId?: string, snapshotVersion?: number): Promise<void> {
    // Reuse the same unsaved-changes guard the "view historic version" path
    // runs: restoring replaces the live graph, so unsaved edits must not be
    // silently discarded.
    if (isGraphDirty.value && !(await confirmLeaveUnsaved())) return
    restoreTargetId.value = snapshotId ?? resolvedSnapshotId.value
    restoreTargetVersion.value = snapshotVersion ?? viewSnapshot.value?.snapshot_version ?? null
    restoreError.value = null
    restoreDiff.value = null
    restoreDiffError.value = null
    showRestoreConfirm.value = true
    void loadRestoreDiff()
  }

  async function confirmRestore(): Promise<void> {
    const snapshotId = restoreTargetId.value ?? resolvedSnapshotId.value
    if (!snapshotId) return
    const restoredVersion = restoreTargetVersion.value ?? viewSnapshot.value?.snapshot_version ?? null
    restoreLoading.value = true
    restoreError.value = null
    try {
      const { data, error } = await withTimeout((signal) =>
        api.POST('/api/v1/pipelines/{pipeline_id}/snapshots/{snapshot_id}/rollback', {
          params: { path: { pipeline_id: pipelineId, snapshot_id: snapshotId } },
          signal,
        }),
      )
      if (error) {
        restoreError.value = describeRestoreRefusal(error)
        return
      }
      const createdVersion = (data as { snapshot_version?: number } | null)?.snapshot_version ?? null
      showRestoreConfirm.value = false
      setRestoreNotice(restoreSuccessNotice(restoredVersion, createdVersion))
      // A restore replays this version into the LIVE graph and creates a new
      // version from it — reload the graph, the catalog (for the version chip)
      // and the timeline (so the new version shows as a rollback entry), then
      // land back in the latest editor with the historic banner gone.
      await loadGraph()
      await loadCatalog()
      await timelineRef.value?.reload()
      viewLatest()
    } catch (e: unknown) {
      restoreError.value = describeRestoreRefusal(e)
    } finally {
      restoreLoading.value = false
    }
  }

  return {
    restoreDiffSummary,
    restoreSeverityLabel,
    loadRestoreDiff,
    describeRestoreRefusal,
    openRestoreConfirm,
    confirmRestore,
  }
}
