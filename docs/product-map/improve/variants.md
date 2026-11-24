---
id: feat-variants
prd: N/A
adr: []
code:
  - backend/src/farna/api/routes/variants.py
unit-tests: []
bdd:
  - backend/tests/bdd/features/variants/variant_groups.feature
  - backend/tests/bdd/steps/test_variant_groups.py
depends-on:
  - feat-runs
  - feat-evals
status: covered
---

# Variants

Variant groups – batch comparison on `/variants/compare`. A variant group bundles
weighted variants (optional `run_context_overrides`) and fires one run per
variant; the page hosts an inline variant-group builder (create + batch-fire),
and comparison surfaces eval scores per node, prompt diffs and eval
coverage gaps. A batch-compare flow (`/variants/compare/:batchId`) serves the
batch-compare view directly; it is a community-tier page with no feature flag
(the phantom `variant_batch_compare` flag was removed in FAR-926).

## Behaviors

- [x] A variant group is creatable with weighted variants that fuse
      `run_context_overrides` into each run's input payload
      (`tests/bdd/features/variants/variant_groups.feature`)
      <!-- behaviour: variants-01 | tests: backend/tests/bdd/features/variants/variant_groups.feature -->
- [x] A batch run triggers one run per variant in insertion order with variant names
      carried through the run record
      (`tests/bdd/features/variants/variant_groups.feature`)
      <!-- behaviour: variants-02 | tests: backend/tests/bdd/features/variants/variant_groups.feature -->
- [x] Sequential execution fires one run per variant in variant insertion order,
      driving the real `run_variant_batch` seam (insertion order preserved,
      one shared `batch_id`)
      (`tests/bdd/features/variants/variant_groups.feature`)
      <!-- behaviour: variants-03 | tests: backend/tests/bdd/features/variants/variant_groups.feature -->
- [x] Variant groups can be listed, fetched, updated, deleted and restored
      (ownership asserted per organization, `api/routes/variants.py`)
      <!-- behaviour: variants-04 | tests: none -->
- [x] Comparison surfaces include coverage gaps, prompt diffs and the batch-compare
      summary (`prompt_diffs`, `coverage_gaps`, `batch_compare` endpoints)
      <!-- behaviour: variants-05 | tests: none -->
- [x] Eval coverage gaps report the pipeline eval definitions each variant is
      missing, driving the real `get_coverage_gaps` seam (a variant claiming the
      eval is not flagged)
      (`tests/bdd/features/variants/variant_groups.feature`)
      <!-- behaviour: variants-06 | tests: backend/tests/bdd/features/variants/variant_groups.feature -->
- [x] Per-token breakdown comparison across variants ships: each variant's
      per-node token usage (input/output/total tokens + cost) rides the
      variant-batch detail surface (`_run_to_variant_run` in
      `api/routes/variant_batches.py`) and the `get_batch_compare` surface
      (`BatchRunCompare` / `batch_compare` in `api/routes/variants.py`) via the
      persisted `node_token_usage` union, serialized through the RunResponse
      bounds (`_serialize_node_token_usage` – `model_cost_raw_usd` display
      clamp + newest-N node truncation), and the `/variants/compare/:batchId`
      page renders a per-node token table per expanded variant run
      (`VariantBatchCompareView.vue`, `VariantBatchCompareView.spec.ts`,
      `test_variant_batches.py`, `test_variants.py`)
      <!-- behaviour: variants-07 | tests: backend/tests/unit/api/test_variant_batches.py, backend/tests/unit/api/test_variants.py -->
- [x] The variant batch-compare UI at `/variants/compare/:batchId` is ungated
      (community tier, no feature flag): the phantom `variant_batch_compare`
      flag was removed in FAR-926 and the route serves `VariantBatchCompareView`
      directly (frontend router)
      <!-- behaviour: variants-08 | tests: none -->
- [x] Variant groups are created and batch-fired from the inline builder on
      `/variants/compare` (`components/variants/VariantGroupBuilder.vue`), which
      honors the `pipeline_id` deep-link from a pipeline's "Run as variant" action
      <!-- behaviour: variants-09 | tests: none -->
- [x] A terminal `rejected` run (FAR-1487) aggregates exactly like `cancelled`
      in the server-computed batch status: `_CANCELLED = {"cancelled",
      "rejected"}` in `api/routes/variant_batches.py`, so a batch whose runs are
      all rejected/cancelled reports `cancelled`, and one mixed with `complete`
      reports `partial` (`test_variant_batches.py` `TestComputeBatchStatus`) –
      a HITL rejection lowers a batch's completion ratio rather than raising a
      batch-level `failed`
      <!-- behaviour: variants-10 | tests: backend/tests/unit/api/test_variant_batches.py -->
- [x] `/variants/compare` lists comparison groups in the shared `DataTable`
      (name, pipeline, variants, declared evals, selection strategy, runs,
      degraded evals, actions) with a pipeline select and a name search. It
      requests the endpoint's 100-row page ceiling and prints "Showing N
      comparisons" (plus a capped-list notice at 100) because the endpoint
      returns a bare array with no total; a pipeline-name read failure is
      surfaced rather than silently degrading to ids. A row selects the
      comparison IN PLACE – the per-node comparison table and output diff viewer
      render below it – and never navigates to the batch-detail route; the
      `pipeline_id` deep link still opens the builder, and the page selects the
      first visible row so it never lands empty-handed
      (`VariantCompareView.vue`, `VariantCompareView.spec.ts`,
      `variant-compare-groups.spec.ts`)
      <!-- behaviour: variants-11 | tests: none -->
- [x] User-visible copy on the variants surface standardises on "comparison"
      (table columns, empty state, the inline explainer linking to `/evals`, the
      builder title/subtitle) while "variant group" stays the code and route
      vocabulary. The declared-eval column is labelled "Declared evals" with a
      tooltip: `variants[].eval_definition_ids` are declarations (the UI builder
      writes them empty and nothing scores with them), not the scores a run
      received (`VariantCompareView.vue`, `VariantGroupBuilder.vue`,
      `locales/en-US.js`)
      <!-- behaviour: variants-12 | tests: none -->
- [x] The comparison builder exposes the group-level `degraded_evals` flag
      (default off, operator-controlled) and sends it in
      `CreateVariantGroupRequest`; the backend persists it
      (`variant_groups.degraded_evals`) and, when on, adds
      `_degraded_evals: true` to every run's input payload
      (`_merge_variant_payload`, `db/crud/variant_group.py`). It does NOT skip
      or weaken eval execution – the builder help line and the table badge
      tooltip say so, so a pipeline only reacts to the marker if its templates
      read `input._degraded_evals`. The comparisons table shows the "degraded"
      badge from the group response, so the flag round-trips
      builder → `POST /api/v1/variant-groups` → group response → table
      (`VariantGroupBuilder.vue`, `VariantCompareView.vue`,
      `locales/en-US.js`, `VariantGroupBuilder.spec.ts`,
      `VariantCompareView.spec.ts`)
      <!-- behaviour: variants-13 | tests: none -->

## Known Gaps

None acknowledged: the `@awaiting-implementation` draft scenarios in
`variant_groups.feature` (tracked since the 2026-08-27 walk) are resolved, and
the per-token breakdown comparison the manifest previously parked as the lone
`feat-variants` partial is shipped (2026-09-25 Improve Architecture walk).

## QA History

- 2026-11-21: **FAR-2611**: `degraded_evals` was API-only – `VariantGroupBuilder`
  always sent `false`, so the FAR-2598 comparisons table could never show its
  "Degraded evals" column. The backend consumer was read before writing copy:
  the flag is persisted on `variant_groups` and, when on,
  `_merge_variant_payload` adds `_degraded_evals: true` to every run's input
  payload; nothing in Farna Control skips or weakens eval execution. Added an
  operator-controlled builder checkbox (default off) with help text stating
  exactly that, wired it to `CreateVariantGroupRequest.degraded_evals`, and
  added a tooltip to the existing table badge. Covered by
  `VariantGroupBuilder.spec.ts` (payload round-trip, default off) and
  `VariantCompareView.spec.ts` (badge + tooltip from the group response).
- 2026-11-20: **FAR-2598**: replaced the unlabelled group dropdown on
  `/variants/compare` with a comparison-groups `DataTable` (pipeline filter +
  name search, row selection in place, first-row default) so the list reads as
  comparisons rather than evals, and ran a copy pass standardising user-visible
  terminology on "comparison" with an inline explainer linking to `/evals`.
  Pre-PR QA then corrected two honesty defects: the "Evals scored" column became
  "Declared evals" (the ids are declarations, never scores) and the list asks
  for the 100-row page ceiling with a "Showing N comparisons" count and a
  capped-list notice. Covered by `VariantCompareView.spec.ts` and
  `variant-compare-groups.spec.ts` (390px column hiding + in-place selection).
- 2026-09-25: **Improve Architecture product-map walk**: closed the last
  `feat-variants` partial: per-token breakdown comparison now ships end to end.
  `_run_to_variant_run` (`api/routes/variant_batches.py`) and `get_batch_compare`
  / `batch_compare` (`api/routes/variants.py` + `db/crud/variant_group.py`) carry
  each run's `node_token_usage` union through the RunResponse serialization
  bounds (`_serialize_node_token_usage`), and `VariantBatchCompareView.vue`
  renders a per-node input/output/total-token + cost table per expanded variant
  run (`variant-batch-token-breakdown`, registered in the manifest elements
  inventory). The manifest `feat-variants` status is now `covered`.
- 2026-09-25: **Improve Architecture product-map walk**: reconciled the
  manifest `feat-variants` registry entry with this tracker: per-node eval-score
  comparison (the `[x]` "eval scores per node" behavior above) is now ticked, and
  the unchecked item is narrowed to the genuinely missing per-token breakdown only.
  The tracker's Known Gaps wording ("per-node eval-score / per-token breakdown drafts")
  is clarified by the Behaviors line that already ships per-node eval scores.
- 2026-09-23: **product-map review pass**: removed the stale
  `pipelines/run_variants.feature` "Coverage gaps are reported for a variant
  group" scenario (`@awaiting-implementation`, deselected) and its dead step
  definitions. It was a duplicate draft of the real `get_coverage_gaps` seam
  already locked by `variants/variant_groups.feature` (the `variant_groups.feature`
  eval-coverage scenario drives the same route/seam); the redundant copy and
  `PINNED_AWAITING_IMPLEMENTATION` entry for `pipelines/run_variants.feature`
  were archived. Coverage for this entry is unchanged.

- 2026-09-20: **product-map review pass**: closed the
  "BDD scenarios tagged `@awaiting-implementation`" gap
  (`variant_groups.feature`). The sequential-order scenario now drives the REAL
  `run_variant_batch` seam with the same mock-session machinery as the batch-run
  scenario (runs created sequentially in variant insertion order under one
  `batch_id`), and the eval-coverage draft was re-anchored to the REAL
  `get_coverage_gaps` seam (missing `eval_definition_ids` per variant – a
  variant that claims the eval is not flagged). The two per-node eval-score /
  per-token breakdown comparison drafts were removed: they asserted a wire shape
  the product does not ship (`get_batch_compare` returns per-run
  `eval_pass_rate` / `eval_count` / `total_tokens` / `total_cost_usd` and the
  frozen-snapshot override diff, which the batch-scope comparison scenarios
  already lock) – leaving them tagged would keep false coverage promises in the
  suite. No `@awaiting-implementation` scenarios remain in the feature.

- 2026-09-12: **product-map review pass**: registered the shared
  `JsonViewer` surface (`components/shared/JsonViewer.vue` static testids
  `json-viewer` / `json-viewer-{copy,expand-all,collapse-all,string-expand,string-collapse}`)
  in the manifest `elements:` inventory for `/variants/compare` and
  `/variants/compare/:batchId`: both pages render the compared variant outputs
  inline with `<JsonViewer>` (`VariantCompareView.vue` / `VariantBatchCompareView.vue`),
  so the viewer shipped in the DOM while staying invisible to Assistant's docs indexer /
  `/api/v1/manifest`. The component is now part of both routes' reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`).

- 2026-09-11: **product-map review pass**: extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/variants/compare` and `/variants/compare/:batchId`: the whole-page view(s) `VariantCompareView.vue` and `VariantBatchCompareView.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Assistant's docs indexer /
  `/api/v1/manifest`.

- 2026-09-17: **FAR-936**: retired the AB Test Models page (`/variants/ab-test`).
  Removed route, view, i18n (`views.ABTestModelsView`), manifest entry, elements
  inventory, tests, and sidebar nav entry. The page's variant-builder capability
  moved onto the `/variants/compare` page as an inline builder
  (`components/variants/VariantGroupBuilder.vue`, i18n `views.variantCreator`), so
  the A/B comparison use case stays reachable: the page's "New Comparison" button
  opens the builder, the `pipeline_id` deep-link from PipelineListView pre-selects
  the pipeline, and firing a batch navigates to the batch-compare detail route.

- 2026-08-27: **product-map review pass**: added this behavior-tracker
  for the registered manifest feature `feat-variants`, which previously had no
  `docs/product-map/` entry. Behaviors verified against `api/routes/variants.py` and
  `tests/bdd/features/variants/variant_groups.feature`. Status: covered (with the known
  BDD gaps called out above).
