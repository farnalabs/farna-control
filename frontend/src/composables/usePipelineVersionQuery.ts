import { computed, ref, watch, type Ref } from 'vue'
import { useI18n } from 'vue-i18n'
import type { RouteLocationNormalizedLoaded } from 'vue-router'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/schema'
import { formatApiError } from '../lib/api/formatError'
import { withTimeout } from '../lib/requestTimeout'
import { SNAPSHOT_PAGE_SIZE } from '../lib/pipelineVersion'

/**
 * FAR-1812/1818: everything the editor derives from the version QUERY STRING,
 * plus the `?version=<n>` alias resolution.
 *
 * The route query selects the mode: `?snapshot=<uuid>` is the canonical deep
 * link and `?version=<n>` an alias resolved against the server's
 * version-DESCENDING snapshot list by resolveVersionAlias()'s bounded search
 * (snapshot wins when both are present). `?compare=<uuid>` is the FAR-1818
 * compare key on top of the historic view.
 *
 * Extracted verbatim from PipelineEditorView so the query/lookup contract has
 * one home. The view still owns the frozen graph and the detail load; this
 * composable only reads the query, resolves the alias and reports failures on
 * the view's `pageError`.
 */

function readQueryValue(value: unknown): string | null {
  const raw = Array.isArray(value) ? value.at(0) : value
  return typeof raw === 'string' && raw.length > 0 ? raw : null
}

// --- `?version=<n>` alias resolution ---------------------------------------
// The alias resolves against the server's version-DESCENDING snapshot list
// (`GET /{pipeline_id}/snapshots`), which is PAGINATED (page_size caps at
// 100, default 20). Resolving only the first loaded page made a deep link
// like `?version=3` on a long-lived pipeline report "This version is no
// longer available" for a version that exists — a false statement on the
// exact promise the feature exists to keep. The search here is BOUNDED by
// construction:
//
//   * page 1 at SNAPSHOT_PAGE_SIZE — a single request covers the top
//     100 versions (every pipeline up to 100 versions), and
//   * for deeper versions, a binary search over the remaining pages — at
//     most log2(pageCount) further requests, hard-capped at
//     SNAPSHOT_LOOKUP_MAX_PROBES (12 probes = 409,600 versions; exhausting
//     the budget throws rather than guessing "missing").
//
// The `?snapshot=<uuid>` path never enters this code — no extra request, no
// latency change for the canonical link. Nothing depends on loadCatalog's
// page-1 list, so a partial or failed catalog load cannot make an existing
// deep version read as missing either.
//
// FAR-1816/1817: SNAPSHOT_PAGE_SIZE is the shared window this lookup and the
// timeline fetch (lib/pipelineVersion). The chip no longer uses it (FAR-1895:
// it resolves through a server-filtered query), so the number is NOT a
// chip/timeline parity guarantee — that parity is structural (see
// currentVersionSnapshot in the view and PipelineSnapshotTimeline's
// groupHistory).
const SNAPSHOT_LOOKUP_MAX_PROBES = 12

type VersionLookupStatus = 'idle' | 'loading' | 'found' | 'absent' | 'failed'

export interface UsePipelineVersionQueryOptions {
  /** The view's live route (its query is the mode selector). */
  route: RouteLocationNormalizedLoaded
  /** Pipeline id from the route params, as the view resolved it. */
  pipelineId: string
  /** The version-DESCENDING snapshot catalog the view loaded. */
  snapshots: Ref<any[]>
  /** The view's fatal page-error ref; a failed alias search lands here. */
  pageError: Ref<string | null>
}

export function usePipelineVersionQuery({
  route,
  pipelineId,
  snapshots,
  pageError,
}: UsePipelineVersionQueryOptions) {
  const { t } = useI18n()

  const snapshotParam = computed(() => readQueryValue(route.query.snapshot))
  const versionParam = computed(() => readQueryValue(route.query.version))
  const historicRequested = computed(() => snapshotParam.value !== null || versionParam.value !== null)

  // FAR-1818: version compare mode. `?compare=<uuid>` — the KEY's presence is
  // what selects the mode, and an empty/absent id means "compare against the
  // latest version". The base side is the resolved `?snapshot=` version (or its
  // `?version=` alias), so the canonical deep link is
  // `?snapshot=<base>&compare=<other>`. Kept separate from the historic-view
  // refs: entering compare never hydrates the editable graph.
  const compareRequested = computed(() => route.query.compare !== undefined)
  const compareParam = computed(() => readQueryValue(route.query.compare))

  // The latest saved version (the snapshot list is version-DESCENDING and
  // loadCatalog filters version 0) — the default "compare against" target.
  const latestSnapshotId = computed<string | null>(() => snapshots.value.at(0)?.id ?? null)

  /** `?version=<n>`: the positive integer, -1 when malformed, null when absent. */
  const requestedVersion = computed(() => {
    const raw = versionParam.value
    if (raw === null) return null
    return /^\d+$/.test(raw) ? Number(raw) : -1
  })

  const versionLookupStatus = ref<VersionLookupStatus>('idle')
  const versionLookupId = ref<string | null>(null)
  // Monotonic token: a search superseded by a newer query change (or a page
  // reload) must never write its result over the newer one.
  let versionLookupToken = 0

  async function fetchSnapshotLookupPage(
    page: number,
  ): Promise<{ items: components['schemas']['SnapshotResponse'][]; total: number }> {
    const { data, error } = await withTimeout((signal) =>
      api.GET('/api/v1/pipelines/{pipeline_id}/snapshots', {
        params: { path: { pipeline_id: pipelineId }, query: { page, page_size: SNAPSHOT_PAGE_SIZE } },
        signal,
      }),
    )
    if (error || !data) throw new Error(`snapshot list page ${page} could not be loaded`)
    const items = data.items ?? []
    // `total` is always sent by the server; fall back to the page's own length
    // so a response without it degrades to "one page", never to NaN bounds.
    const total = Number.isFinite(data.total) ? data.total : items.length
    return { items, total }
  }

  /**
   * Find `version` in the server's version-DESC snapshot ordering.
   *
   * Returns the snapshot id, or null when the version is PROVABLY absent
   * (it lies outside the existing version range, or the page whose range
   * contains it has no such row — the list is sorted, so absence on that page
   * is absence everywhere). Throws when the search could not complete: a
   * failed search must never masquerade as "this version is no longer
   * available".
   */
  async function findSnapshotIdByVersion(version: number): Promise<string | null> {
    const find = (items: components['schemas']['SnapshotResponse'][]) =>
      items.find((s) => s.snapshot_version === version) ?? null
    const pageOne = await fetchSnapshotLookupPage(1)
    const onFirst = find(pageOne.items)
    if (onFirst) return onFirst.id
    if (pageOne.items.length === 0) return null // no snapshots at all
    // Sorted version-DESC: page 1 spans [newest … oldest-on-page-1].
    if (version > pageOne.items.at(0)!.snapshot_version) return null // newer than the newest
    if (version >= pageOne.items.at(-1)!.snapshot_version) {
      return null // inside page 1's range, not present — absent
    }
    // Below page 1: binary search the remaining pages by the range each spans.
    let lo = 2
    let hi = Math.ceil(pageOne.total / SNAPSHOT_PAGE_SIZE)
    let probes = 0
    while (lo <= hi) {
      if (probes >= SNAPSHOT_LOOKUP_MAX_PROBES) {
        throw new Error('version alias search exceeded its probe budget')
      }
      probes += 1
      const mid = Math.floor((lo + hi) / 2)
      const page = await fetchSnapshotLookupPage(mid)
      // Versions saved while this search runs push every older version DOWN
      // the ranking — refresh `hi` from each page's live total so a stale
      // bound can never truncate the search below the target's new rank (a
      // false "no longer available" of exactly the kind this search exists to
      // prevent). Termination is unaffected: `probes` still caps the loop.
      hi = Math.max(hi, Math.ceil(page.total / SNAPSHOT_PAGE_SIZE))
      if (page.items.length === 0) {
        hi = mid - 1 // past the end (list shrank) — search earlier
        continue
      }
      const hit = find(page.items)
      if (hit) return hit.id
      if (version > page.items.at(0)!.snapshot_version) {
        hi = mid - 1 // belongs above this page
        continue
      }
      if (version < page.items.at(-1)!.snapshot_version) {
        lo = mid + 1 // belongs below this page
        continue
      }
      return null // inside this page's range, not present — absent
    }
    return null
  }

  /**
   * Resolve the current `?version=<n>` alias. No-op for the canonical
   * `?snapshot=` link (which needs no list at all), for an absent alias, and
   * for a malformed one (versionMissing derives that case directly, with no
   * request). A completed search records found/absent; a FAILED one lands on
   * pageError — an accurate load error — never on the graceful miss, because
   * "no longer available" would be false when the search itself failed.
   */
  async function resolveVersionAlias(): Promise<void> {
    const token = ++versionLookupToken
    const version = requestedVersion.value
    if (snapshotParam.value !== null || versionParam.value === null || version === null || version <= 0) {
      versionLookupId.value = null
      versionLookupStatus.value = 'idle'
      return
    }
    versionLookupId.value = null
    versionLookupStatus.value = 'loading'
    try {
      const id = await findSnapshotIdByVersion(version)
      if (token !== versionLookupToken) return
      versionLookupId.value = id
      versionLookupStatus.value = id ? 'found' : 'absent'
    } catch (e: unknown) {
      if (token !== versionLookupToken) return
      console.warn('Failed to resolve the ?version= alias', e)
      versionLookupStatus.value = 'failed'
      pageError.value = t('views.PipelineEditorView.version_load_failed', { error: formatApiError(e) })
    }
  }

  // Re-resolve whenever the alias (or its precedence against `?snapshot=`)
  // changes: entering a version, switching versions, or leaving to latest.
  // NOT immediate — mount is covered by the view's fetcher, which AWAITS this
  // same routine, so `loading` spans the search and the spinner never gaps.
  // reloadPage (ErrorAlert's retry) re-runs it for the same reason: a failed
  // search must be retryable from the error it produced.
  watch([snapshotParam, versionParam], () => {
    void resolveVersionAlias()
  })

  const resolvedSnapshotId = computed<string | null>(() => {
    if (snapshotParam.value !== null) return snapshotParam.value
    if (versionLookupStatus.value !== 'found') return null
    return versionLookupId.value
  })

  return {
    snapshotParam,
    versionParam,
    historicRequested,
    compareRequested,
    compareParam,
    latestSnapshotId,
    requestedVersion,
    versionLookupStatus,
    versionLookupId,
    resolveVersionAlias,
    resolvedSnapshotId,
  }
}
