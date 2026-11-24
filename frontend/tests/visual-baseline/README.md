# PrimeVue visual baseline (FAR-2205)

Reference artefacts and the comparison tool used to measure the PrimeVue 5 → 4
downgrade. The standard of equivalence for that change is **no unexplained
difference**: every measured difference is either eliminated or explicitly
justified in the delivery report (`DELTA.md`).

## What this directory is — and what it is not

This directory holds a **retained PrimeVue 5.0.4 reference**: the "before" side
of the downgrade, captured at commit `ec89022af` *before* any dependency change
(see `DELTA.md`). It is **not** a reflection of the currently installed
toolchain.

- **Recorded version:** PrimeVue **5.0.4** with `@primeuix/themes` 3.1.0 and
  `@primeuix/styled` 1.2.0, as recorded in `v5/versions.json`.
- **Installed version:** PrimeVue **4.5.5** (pinned in `frontend/package.json`,
  with `@primevue/themes` 4.5.4 and `@primeuix/styled` 0.7.x).

The recorded and installed versions intentionally differ: they are the two sides
of the measurement, so `v5/versions.json` is **not** expected to match
`package.json`. `src/__tests__/visual-baseline-versions.spec.ts` guards that this
intent stays honest — the directory name (`v5`), `v5/versions.json` and this
README must all agree the reference describes PrimeVue 5.x, and
`v5/versions.json` must keep declaring that it intentionally differs from the
installed version. If the intent is ever dropped (for example, someone "fixes"
`v5/versions.json` to the installed 4.5.5), that guard fails rather than letting
the drift stay silent.

## What is captured

`frontend/tests/e2e/primevue-visual-baseline.spec.ts` (Playwright, local target
only — the mock API is deterministic) captures, for each route and interactive
state:

- a full-page PNG (`fullPage` for routes, viewport for overlays),
- the computed `--p-*` design tokens on `<html>`, and
- a computed-style fingerprint of every PrimeVue component root on the page
  (font size, padding, height, colours, radii, shadow, plus its text).

| Viewport | Captures |
| --- | --- |
| 1280×800 | 13 routes: dashboard, pipelines, pipeline editor, runs, run detail, settings/license, settings/triggers, settings/mcp, schemas, admin/users, admin/connectors, admin/model-backends, admin/run-retention |
| 1280×800 | 7 interactive states: pipelines row `Menu`, folder `Dialog`, runs `Select` open, licence `Dialog`, admin-users `Dialog`, parameter-schemas `Tabs`, dashboard `Tooltip` |
| 390×844 | dashboard, pipelines, runs |

`frontend/src/__tests__/primevue-component-dom.spec.ts` mounts all fifteen
PrimeVue components the app imports (button, dialog, inputtext, card, badge,
menu, selected, tabs + tablist + tab + tabpanels + tabpanel, popover, tooltip)
in their used variants and pins the rendered DOM with `toMatchSnapshot()`. That
snapshot is the **permanent** regression guard; the JSON signature it can emit
(`PRIMEVUE_DOM_OUT`) is the cross-version comparison record.

## Files and how they are produced

| Path | Meaning |
| --- | --- |
| `v5/versions.json` | dependency versions the retained **PrimeVue 5.0.4** reference was captured on (**not** the installed versions) |
| `v5/tokens.json` | union of every computed `--p-*` value across all captures (818 tokens; zero route-to-route conflicts) |
| `v5/fingerprints.json` | per-capture component computed-style fingerprints |
| `v5/dom.json` | per-component rendered-DOM signature (v5) |
| `compare.mjs` | diffs two capture directories into a delta table |

The capture spec writes **one `<slug>.json` + `<slug>.png` per capture** into
`PRIMEVUE_BASELINE_OUT`; it does **not** write the aggregate files committed
here. Those aggregates were assembled outside the repo from the per-capture
output:

- `v5/tokens.json` — union of the computed `--p-*` values across the captures,
  with a `conflicts` map.
- `v5/fingerprints.json` — per-capture component fingerprints keyed by slug
  (`{ url, viewport, components }`): a trimmed merge of the per-capture JSON in
  which the `slug`/`path`/`title`/`tokens` fields are dropped and `url` is
  reduced to path + query.
- `v5/dom.json` — the rendered-DOM signatures emitted by
  `primevue-component-dom.spec.ts` when `PRIMEVUE_DOM_OUT` is set.

> **There is no assembler committed in this repo** that merges the per-capture
> JSON into these aggregate files. The committed aggregates are a retained
> capture and are **partly hand-maintained**: for example FAR-2716 hand-edited
> one recorded `__class` value in `v5/fingerprints.json` when a class was removed
> from the product, because the v5 reference cannot be re-captured from the
> current (v4) tree. Do not hand-write values speculatively; only correct a value
> that is provably wrong against the current product, and record why in the
> commit message.

**Outstanding:** regenerating these files requires temporarily reinstalling the
PrimeVue 5.0.4 toolchain (`primevue` 5.0.4, `@primeuix/themes` 3.1.0,
`@primeuix/styled` 1.2.0), re-running the capture and re-assembling the
aggregates by hand. That cannot be done against the pinned PrimeVue 4.5.5, which
produces the **v4 comparison side** rather than this v5 reference. A reproducible
v5 re-capture (and a committed assembler) is outstanding; the installed-version
re-capture is also outstanding and is not committed here.

PNG screenshots are **not** committed: they are ~1.8 MB per run and are
reproducible from the spec. Capture them with:

```sh
cd frontend
PRIMEVUE_BASELINE_OUT=test-results/primevue-baseline-v5 \
  pnpm exec playwright test tests/e2e/primevue-visual-baseline.spec.ts --workers=1
```

Running this command against the pinned toolchain today captures the **v4** side,
not the v5 reference committed in `v5/`.

## Comparing two captures

`compare.mjs` diffs two **transient capture directories** (the per-slug
`<slug>.json` + `<slug>.png` output of the spec) — not the committed aggregates,
which are a different shape.

```sh
cd frontend
node tests/visual-baseline/compare.mjs \
  --expected test-results/primevue-baseline-v5 \
  --actual   test-results/primevue-baseline-v4 \
  --json     test-results/primevue-delta.json
```

Each page reports the changed `--p-*` tokens, the changed component computed
styles (grouped per selector+property with an instance count) and the
screenshot pixel delta (percentage of pixels differing beyond the per-channel
threshold, maximum channel delta and the bounding box of the change). A
dimension change is reported explicitly rather than silently compared as an
overlap.
