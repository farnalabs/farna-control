/**
 * Architecture gate: the design-system contract cannot drift (FAR-2710, FAR-2774).
 *
 * The 2026-11-23 design-system audit found views and components hand-rolling card
 * shells, card headers and raw PrimeVue dialogs instead of composing the shared
 * primitives in `frontend/src/components/ui` and `frontend/src/components/shared`.
 * Prose alone did not stop the drift. This spec gives it the same structural
 * enforcement `appselect-guard.spec.ts` gives native `<select>`, and lives in the
 * frontend unit suite (the "Frontend and WCAG" job and the merge-queue-parity
 * job, both gated on `frontend`) so a frontend-only PR that introduces drift
 * fails CI at PR time.
 *
 * This spec is the SINGLE owner of the guard (FAR-2749). It replaced the
 * duplicate backend architecture test
 * `backend/tests/architecture/test_design_system_conformance.py`, which read the
 * same baseline: keeping two scans in step by hand was itself the drift the
 * guard exists to prevent. The frontend unit suite is the right home - it runs
 * on every frontend-touching PR, and a baseline-only change under `frontend/`
 * triggers that job, whereas the backend architecture suite skips frontend-only
 * changes. The detectors themselves live in one shared module,
 * `frontend/src/test-utils/design-system-detectors.ts`, used by both this guard
 * and the baseline writer below, so the baseline can never be produced by a
 * different scanner from the one that enforces it.
 *
 * Two rules
 * ---------
 * 1. Absolute rule (no allowlist). A `DataCard` or `SectionCard` opening tag that
 *    carries BOTH a title attribute and `padding="none"` fails. That is the
 *    exact defect FAR-2694 fixed: the card renders its own header, so the
 *    padding override leaves that header flush against the card edge.
 * 2. Shrinking ratchet. Six objective drift fingerprints are counted per file
 *    and compared with the checked-in baseline. A file's count may equal or
 *    fall below its baseline; it may never exceed it. A baseline entry whose
 *    count has fallen to zero must be deleted (the ratchet only shrinks). A
 *    reduction that has not yet reached zero does not fail, but is emitted as a
 *    warning so it is visible in the test output. A file absent from the
 *    baseline has baseline 0.
 *
 * The six ratchet patterns (one-line example each)
 * ------------------------------------------------
 * Markup patterns scan the TEMPLATE only: `<script>` and `<style>` blocks and
 * HTML comments are stripped first, so a tag named in a script string, a CSS
 * rule or a commented-out block is not counted.
 * - `card_container` - a hand-rolled card shell:
 *   `<div class="rounded-lg border border-border p-4">`. Matches a `<div>` whose
 *   class carries the standalone `card` token, or carries a `rounded`/`rounded-*`
 *   token AND a `border`/`border-*` token AND a `p-<n>`/`p-px`/`p-[...]` token.
 * - `card_header` - a hand-rolled CardHeader title:
 *   `<h3 class="text-base font-semibold">`. Matches an `<h2>`/`<h3>` whose class
 *   carries `font-semibold` together with `text-base` or `text-lg`.
 * - `raw_button` - a raw HTML button: `<button type="button">Save</button>`.
 *   Matches a literal, case-sensitive `<button` opening tag. `<Button>` (the
 *   PrimeVue component) is not counted.
 * - `raw_input` - a raw HTML input: `<input v-model="value" />`. Matches a
 *   literal, case-sensitive `<input` opening tag. `<InputText>` and AppSelect's
 *   rendered combobox are not counted.
 * Import patterns scan the script block.
 * - `raw_dialog` - a raw PrimeVue dialog import: `import Dialog from 'primevue/dialog'`
 *   (default, named, namespace, aliased or bare import forms all match).
 * - `primevue_badge` - a raw PrimeVue badge import: `import Badge from 'primevue/badge'`
 *   (same import forms; a commented-out import does not match).
 *
 * Anti-false-positive discipline: these patterns are deliberately objective, not
 * semantic. A legitimate design call that happens to match (canvas nodes, diff
 * tables, auth cards) stays in the baseline unchanged - the ratchet exists to
 * stop NEW drift, not to relitigate the audit. Files under
 * `frontend/src/components/ui` and `frontend/src/components/shared` (the
 * primitives themselves), plus `frontend/src/components/PageTabs.vue` and
 * `frontend/src/__tests__` are out of scope by construction - a spec's `stubs`
 * and slot fixtures author raw markup as test doubles, not product surfaces.
 *
 * Regenerating the baseline after a sweep
 * ---------------------------------------
 * `frontend/design-system-baseline.json` is generated from the current tree by
 * this spec's own scanner (`collectCounts()`, which calls the shared
 * `countPatterns()`), with only non-zero entries, sorted deterministically per
 * pattern. After a sweep lowers counts for ANY of the six patterns, regenerate it
 * and delete entries that reached zero (the ratchet fails on a stale zero entry):
 *
 *     cd frontend
 *     DS_GUARD_WRITE_BASELINE=1 pnpm exec vitest run src/__tests__/design-system-guard.spec.ts
 *
 * Run it twice on the same tree: the second run must produce no diff. The writer
 * is the only supported way to change the baseline.
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync, statSync, writeFileSync } from 'fs'
import { join, relative, extname, sep, resolve } from 'path'
import {
  PATTERNS,
  countPatterns,
  emptyCounts,
  stripHtmlComments,
  stripNonTemplateBlocks,
  titledCardsWithoutPadding,
  type Counts,
} from '../test-utils/design-system-detectors'

const SRC_DIR = join(__dirname, '..')
const FRONTEND_ROOT = resolve(SRC_DIR, '..')
const REPO_ROOT = resolve(FRONTEND_ROOT, '..')
const BASELINE_PATH = join(FRONTEND_ROOT, 'design-system-baseline.json')
const BASELINE_NAME = 'frontend/design-system-baseline.json'

const EXCLUDED_DIRS = [
  join(SRC_DIR, 'components', 'ui'),
  join(SRC_DIR, 'components', 'shared'),
  join(SRC_DIR, '__tests__'),
]
const EXCLUDED_FILES = new Set([join(SRC_DIR, 'components', 'PageTabs.vue')])

function walkVue(dir: string): string[] {
  const files: string[] = []
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) {
      files.push(...walkVue(full))
    } else if (extname(full) === '.vue') {
      files.push(full)
    }
  }
  return files
}

function scannedFiles(): string[] {
  return walkVue(SRC_DIR)
    .filter((path) => {
      if (EXCLUDED_FILES.has(path)) return false
      return !EXCLUDED_DIRS.some((dir) => path === dir || path.startsWith(dir + sep))
    })
    .sort()
}

function repoRelative(path: string): string {
  return relative(REPO_ROOT, path).split(sep).join('/')
}

/** Per-pattern, per-file counts with non-zero entries only (the baseline shape). */
function collectCounts(): Counts {
  const counts = emptyCounts()
  for (const path of scannedFiles()) {
    const perFile = countPatterns(readFileSync(path, 'utf-8'))
    for (const pattern of PATTERNS) {
      const count = perFile[pattern]
      if (count) counts[pattern][repoRelative(path)] = count
    }
  }
  for (const pattern of PATTERNS) {
    counts[pattern] = Object.fromEntries(
      Object.entries(counts[pattern]).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)),
    )
  }
  return counts
}

/** Validate a parsed baseline JSON value, rejecting unknown patterns / bad counts. */
function parseBaseline(raw: unknown): Counts {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
    throw new Error(`${BASELINE_NAME} must be a JSON object keyed by pattern`)
  }
  const record = raw as Record<string, unknown>
  const unknownPatterns = Object.keys(record).filter((key) => !(PATTERNS as readonly string[]).includes(key))
  if (unknownPatterns.length) {
    throw new Error(`${BASELINE_NAME} names unknown pattern(s): ${unknownPatterns.join(', ')}; expected ${PATTERNS.join(', ')}`)
  }
  const baseline = emptyCounts()
  for (const pattern of PATTERNS) {
    const entries = record[pattern]
    if (typeof entries !== 'object' || entries === null || Array.isArray(entries)) {
      throw new Error(`${BASELINE_NAME} pattern '${pattern}' must map file paths to counts`)
    }
    for (const [path, count] of Object.entries(entries as Record<string, unknown>)) {
      if (typeof count !== 'number' || !Number.isInteger(count) || count < 1) {
        throw new Error(`${BASELINE_NAME} entry ${pattern}/${path} must be a positive integer, got ${String(count)}`)
      }
      baseline[pattern][path] = count
    }
  }
  return baseline
}

function loadBaseline(): Counts {
  return parseBaseline(JSON.parse(readFileSync(BASELINE_PATH, 'utf-8')))
}

interface RatchetResult {
  regressions: string[]
  reductions: string[]
  zeroed: string[]
}

/**
 * Compare actual per-file counts against the baseline.
 *
 * - a count above its baseline is a regression (new if the baseline is 0, grew
 *   otherwise) and must fail the guard;
 * - a baseline entry whose file now counts zero is `zeroed` and must fail (the
 *   ratchet only shrinks - delete the stale allowance);
 * - a non-zero reduction is only a `reduction`: it warns so it stays visible,
 *   but does not fail.
 */
function evaluateRatchet(baseline: Counts, actual: Counts): RatchetResult {
  const regressions: string[] = []
  const reductions: string[] = []
  const zeroed: string[] = []
  for (const pattern of PATTERNS) {
    for (const [path, count] of Object.entries(actual[pattern])) {
      const allowed = baseline[pattern][path] ?? 0
      if (count > allowed) {
        const kind = allowed === 0 ? 'new' : 'grew'
        regressions.push(`${pattern}: ${path}: ${count} > ${allowed} (${kind})`)
      }
    }
    for (const [path, allowed] of Object.entries(baseline[pattern])) {
      const count = actual[pattern][path] ?? 0
      if (count === 0) {
        zeroed.push(`${pattern}: ${path}: ${allowed} -> 0`)
      } else if (count < allowed) {
        reductions.push(`${pattern}: ${path}: ${allowed} -> ${count}`)
      }
    }
  }
  return { regressions, reductions, zeroed }
}

function lines(values: string[]): string {
  return values.map((value) => `  ${value}`).join('\n')
}

// Regeneration path (replaces the Python generator deleted with the duplicate
// backend guard in FAR-2749): `collectCounts()` already emits the exact baseline
// shape - non-zero entries only, sorted per pattern. The guard and the writer
// share the same detectors (`countPatterns()`), so this cannot drift from what
// the ratchet enforces.
if (process.env.DS_GUARD_WRITE_BASELINE === '1') {
  writeFileSync(BASELINE_PATH, `${JSON.stringify(collectCounts(), null, 2)}\n`)
}

describe('design-system conformance guard (FAR-2710)', () => {
  it('flags a titled DataCard or SectionCard that sets padding="none", and ignores a commented-out one', () => {
    // Positive control: without these the absolute rule could never fire.
    expect(titledCardsWithoutPadding('<DataCard title="x" padding="none" />')).toHaveLength(1)
    expect(titledCardsWithoutPadding('<SectionCard title="x" padding="none" />')).toHaveLength(1)
    expect(titledCardsWithoutPadding("<DataCard title='x' :padding=\"'none'\" />")).toHaveLength(1)
    expect(titledCardsWithoutPadding('<!-- <DataCard title="x" padding="none" /> -->')).toHaveLength(0)
    expect(titledCardsWithoutPadding('<DataCard padding="none" />')).toHaveLength(0)
    expect(titledCardsWithoutPadding('<DataCard title="x" padding="sm" />')).toHaveLength(0)
  })

  it('flags every raw primevue/dialog import form, and nothing else', () => {
    expect(countPatterns("import Dialog from 'primevue/dialog'").raw_dialog).toBe(1)
    expect(countPatterns("import * as Dialog from 'primevue/dialog'").raw_dialog).toBe(1)
    expect(countPatterns("import { Dialog } from 'primevue/dialog'").raw_dialog).toBe(1)
    expect(countPatterns("import 'primevue/dialog'").raw_dialog).toBe(1)
    // A multi-line import is still an import: default, named and namespace forms.
    expect(countPatterns("import Dialog\n  from 'primevue/dialog'").raw_dialog).toBe(1)
    expect(countPatterns("import {\n  Dialog,\n} from 'primevue/dialog'").raw_dialog).toBe(1)
    expect(countPatterns("import *\n  as Dialog from 'primevue/dialog'").raw_dialog).toBe(1)
    expect(countPatterns("import { Dialog as D } from 'primevue/dialog'").raw_dialog).toBe(1)
    // Two adjacent imports stay separately counted.
    expect(countPatterns("import Dialog from 'primevue/dialog'\nimport Dialog2 from 'primevue/dialog'").raw_dialog).toBe(2)
    expect(countPatterns("import Dialog from 'primevue/dialog'\nimport X from 'primevue/confirmdialog'").raw_dialog).toBe(1)
    // A block-commented import is not an import.
    expect(countPatterns("<script>\n/*\nimport Dialog from 'primevue/dialog'\n*/\n</script>").raw_dialog).toBe(0)
    expect(countPatterns("<script>\n/* import Dialog from 'primevue/dialog' */\n</script>").raw_dialog).toBe(0)
    // ...but a real import after a block comment still counts.
    expect(countPatterns("/* header */\nimport Dialog from 'primevue/dialog'").raw_dialog).toBe(1)
    expect(countPatterns("<script>import X from 'primevue/button'</script>").raw_dialog).toBe(0)
  })

  it('flags every raw primevue/badge import form, and nothing else', () => {
    // Positive control: the four import spellings, and the bare side-effect form.
    expect(countPatterns("import Badge from 'primevue/badge'").primevue_badge).toBe(1)
    expect(countPatterns('import Badge from "primevue/badge"').primevue_badge).toBe(1)
    expect(countPatterns("import * as Badge from 'primevue/badge'").primevue_badge).toBe(1)
    expect(countPatterns("import { Badge } from 'primevue/badge'").primevue_badge).toBe(1)
    expect(countPatterns("import { Badge as B } from 'primevue/badge'").primevue_badge).toBe(1)
    expect(countPatterns("import 'primevue/badge'").primevue_badge).toBe(1)
    // A multi-line import is still an import: default, named and namespace forms.
    expect(countPatterns("import Badge\n  from 'primevue/badge'").primevue_badge).toBe(1)
    expect(countPatterns("import {\n  Badge,\n} from 'primevue/badge'").primevue_badge).toBe(1)
    expect(countPatterns("import *\n  as Badge from 'primevue/badge'").primevue_badge).toBe(1)
    // Two adjacent imports stay separately counted.
    expect(countPatterns("import Badge from 'primevue/badge'\nimport Badge2 from 'primevue/badge'").primevue_badge).toBe(2)
    // Negative controls: a sibling module, a commented-out import, and the
    // template mention are not imports.
    expect(countPatterns("import Badge from 'primevue/badgex'").primevue_badge).toBe(0)
    expect(countPatterns("import Badge from 'primevue/message'").primevue_badge).toBe(0)
    // A block-commented import is not an import (single- and multi-line).
    expect(countPatterns("<script>\n/*\nimport Badge from 'primevue/badge'\n*/\n</script>").primevue_badge).toBe(0)
    expect(countPatterns("<script>\n/* import Badge from 'primevue/badge' */\n</script>").primevue_badge).toBe(0)
    // ...but a real import after a block comment still counts.
    expect(countPatterns("/* header */\nimport Badge from 'primevue/badge'").primevue_badge).toBe(1)
    expect(countPatterns("<script>\n// import Badge from 'primevue/badge'\n</script>").primevue_badge).toBe(0)
    expect(countPatterns('<!-- import Badge from \'primevue/badge\' -->').primevue_badge).toBe(0)
    expect(countPatterns('<script>const mod = "primevue/badge"</script>').primevue_badge).toBe(0)
    expect(countPatterns("<style>@import 'primevue/badge';</style>").primevue_badge).toBe(0)
  })

  it('scans card markup in the template only, never in script or style blocks', () => {
    // Positive control: the markup detectors still fire on real template markup.
    expect(countPatterns('<template><div class="card" /></template>').card_container).toBe(1)
    expect(countPatterns('<template><h3 class="text-base font-semibold">T</h3></template>').card_header).toBe(1)
    // A card shell named inside a script string is not template markup.
    expect(
      countPatterns(`<template><div /></template><script>const tpl = '<div class="card" />'</script>`).card_container,
    ).toBe(0)
    expect(
      countPatterns(
        `<template><div /></template><script>const tpl = '<h3 class="text-base font-semibold">T</h3>'</script>`,
      ).card_header,
    ).toBe(0)
    // ...nor is one named inside a CSS rule.
    expect(
      countPatterns(`<template><div /></template><style>.x::after { content: '<div class="card">' }</style>`)
        .card_container,
    ).toBe(0)
    expect(
      countPatterns(`<template><div /></template><style>.x::after { content: '<h3 class="text-base font-semibold">' }</style>`)
        .card_header,
    ).toBe(0)
  })

  it('counts a raw <button> in a template, and ignores comments, scripts, styles and components', () => {
    // Positive control, including multi-line tags and every closing spelling.
    expect(countPatterns('<template><button type="button">Save</button></template>').raw_button).toBe(1)
    expect(countPatterns('<template><button /><button class="x"></button></template>').raw_button).toBe(2)
    expect(countPatterns('<template>\n  <button\n    type="button"\n    @click="go"\n  >\n    Go\n  </button>\n</template>').raw_button).toBe(1)
    // Negative controls: the drift is a raw HTML element, not a component.
    expect(countPatterns('<template><Button label="Save" /></template>').raw_button).toBe(0)
    expect(countPatterns('<template><buttonish /></template>').raw_button).toBe(0)
    // ...and none of these are template markup.
    expect(countPatterns('<template><!-- <button /> --></template>').raw_button).toBe(0)
    expect(countPatterns('<template><div /></template><script>const tpl = \'<button />\'</script>').raw_button).toBe(0)
    expect(countPatterns('<template><div /></template><style>.x::after { content: "<button>" }</style>').raw_button).toBe(0)
  })

  it('counts a raw <input> in a template, and ignores comments, scripts, styles and components', () => {
    // Positive control, in the forms the app authors.
    expect(countPatterns('<template><input v-model="value" /></template>').raw_input).toBe(1)
    expect(countPatterns('<template><input type="checkbox" :checked="on" /><input type="text" /></template>').raw_input).toBe(2)
    // Negative controls: PrimeVue components, its siblings, and non-template text.
    expect(countPatterns('<template><InputText v-model="value" /></template>').raw_input).toBe(0)
    expect(countPatterns('<template><inputmask v-model="value" /></template>').raw_input).toBe(0)
    expect(countPatterns('<template><!-- <input /> --></template>').raw_input).toBe(0)
    expect(countPatterns('<template><div /></template><script>const tpl = \'<input />\'</script>').raw_input).toBe(0)
    expect(countPatterns('<template><div /></template><style>.y::before { content: "<input>" }</style>').raw_input).toBe(0)
  })

  it("does not count AppSelect's internals as a raw input", () => {
    // AppSelect documents that PrimeVue's combobox is not an <input>, and that
    // mention sits inside a template comment, so it is not a raw input...
    const appSelect = join(SRC_DIR, 'components', 'shared', 'AppSelect.vue')
    expect(countPatterns(readFileSync(appSelect, 'utf-8')).raw_input).toBe(0)
    // ...and the shared primitives directory is out of scan scope entirely.
    expect(scannedFiles()).not.toContain(appSelect)
  })

  it('never scans a spec, so a stubbed template is not counted', () => {
    // A spec's `stubs`/slot fixtures author raw markup as test doubles. Prove
    // the fixture really contains one, then that no spec is ever scanned.
    const stubFixture = join(SRC_DIR, '__tests__', 'primevue-component-dom.spec.ts')
    expect(countPatterns(readFileSync(stubFixture, 'utf-8')).raw_button).toBeGreaterThan(0)
    expect(scannedFiles()).not.toContain(stubFixture)
    expect(scannedFiles().some((path) => path.startsWith(join(SRC_DIR, '__tests__') + sep))).toBe(false)
  })

  it('counts all six drift patterns per file', () => {
    // Positive control for the per-file counts the ratchet compares.
    const counts = countPatterns(
      [
        '<template>',
        '<div class="rounded-lg border border-border p-4"></div>',
        '<div class="card"></div>',
        '<h3 class="text-base font-semibold">Title</h3>',
        '<button type="button">Save</button>',
        '<input v-model="value" />',
        '</template>',
        '<script setup lang="ts">',
        "import Dialog from 'primevue/dialog'",
        "import Badge from 'primevue/badge'",
        '</script>',
      ].join('\n'),
    )
    expect(counts.card_container).toBe(2)
    expect(counts.card_header).toBe(1)
    expect(counts.raw_dialog).toBe(1)
    expect(counts.raw_button).toBe(1)
    expect(counts.raw_input).toBe(1)
    expect(counts.primevue_badge).toBe(1)
  })

  it('reads both double- and single-quoted class attributes', () => {
    // Both quote styles are valid SFC markup, so a card shell or header written
    // with single quotes must still be tokenized and counted.
    expect(countPatterns("<template><div class='card'></div></template>").card_container).toBe(1)
    expect(countPatterns("<template><h2 class='text-lg font-semibold'>T</h2></template>").card_header).toBe(1)
  })

  it('strips comments and non-template blocks, optionally preserving line numbers', () => {
    const source = '<template>\n<div />\n</template>\n<!-- note -->\n<style>.a { color: red }</style>'
    // Default drops the stripped regions entirely.
    expect(stripNonTemplateBlocks(source)).not.toContain('note')
    expect(stripNonTemplateBlocks(source)).not.toContain('color: red')
    // preserveLines keeps the newlines so line numbers computed from the result stay true.
    const preserved = stripNonTemplateBlocks(source, { preserveLines: true })
    expect(preserved.split('\n')).toHaveLength(source.split('\n').length)
    expect(preserved).not.toContain('note')
    // stripHtmlComments honours the same preserveLines flag.
    const commented = '<p>a</p>\n<!-- x -->\n<p>b</p>'
    expect(stripHtmlComments(commented)).not.toContain('x')
    expect(stripHtmlComments(commented, true).split('\n')).toHaveLength(commented.split('\n').length)
  })

  it('has no DataCard/SectionCard that combines a title with padding="none"', () => {
    const offenders: string[] = []
    for (const path of scannedFiles()) {
      for (const tag of titledCardsWithoutPadding(readFileSync(path, 'utf-8'))) {
        offenders.push(`${repoRelative(path)}: ${tag}`)
      }
    }
    const title =
      'A DataCard/SectionCard combines a title with padding="none". The card renders its own header, so the override leaves that header flush against the card edge (the FAR-2694 defect). Drop the padding override, or use the primitive\'s own padding scale instead:'
    expect(offenders, title).toEqual([])
  })

  it('rejects a baseline that names an unknown pattern', () => {
    expect(() => parseBaseline({ card_container: {}, not_a_pattern: {} })).toThrow(/unknown pattern/)
  })

  it('rejects a baseline count that is not a positive integer', () => {
    expect(() => parseBaseline({ card_container: { 'frontend/src/X.vue': 0 } })).toThrow(/positive integer/)
    expect(() => parseBaseline({ card_container: { 'frontend/src/X.vue': -2 } })).toThrow(/positive integer/)
  })

  it('fails a file that grows beyond its baseline (new and grew)', () => {
    const baseline = emptyCounts()
    baseline.card_container['frontend/src/Grown.vue'] = 1
    const actual = emptyCounts()
    actual.card_container['frontend/src/Grown.vue'] = 2
    actual.raw_dialog['frontend/src/New.vue'] = 1

    const result = evaluateRatchet(baseline, actual)
    expect(result.regressions.join('\n')).toContain('card_container: frontend/src/Grown.vue: 2 > 1 (grew)')
    expect(result.regressions.join('\n')).toContain('raw_dialog: frontend/src/New.vue: 1 > 0 (new)')
    expect(result.zeroed).toEqual([])
  })

  it('fails a baseline entry whose count reached zero', () => {
    const baseline = emptyCounts()
    baseline.card_header['frontend/src/Resolved.vue'] = 1

    const result = evaluateRatchet(baseline, emptyCounts())
    expect(result.zeroed).toHaveLength(1)
    expect(result.zeroed[0]).toContain('frontend/src/Resolved.vue: 1 -> 0')
    expect(result.regressions).toEqual([])
  })

  it('warns but does not fail on a non-zero reduction', () => {
    const baseline = emptyCounts()
    baseline.card_container['frontend/src/Reduced.vue'] = 3
    const actual = emptyCounts()
    actual.card_container['frontend/src/Reduced.vue'] = 1

    const result = evaluateRatchet(baseline, actual)
    expect(result.reductions).toHaveLength(1)
    expect(result.reductions[0]).toContain('frontend/src/Reduced.vue: 3 -> 1')
    expect(result.regressions).toEqual([])
    expect(result.zeroed).toEqual([])
  })

  it('never exceeds the checked-in baseline, and shrinks resolved entries to deletion', () => {
    const { regressions, reductions, zeroed } = evaluateRatchet(loadBaseline(), collectCounts())

    for (const line of reductions) {
      console.warn(
        `Design-system baseline can shrink further (not a failure): lower or remove this entry in ${BASELINE_NAME}:\n  ${line}`,
      )
    }

    const grewTitle = `A file gained more hand-rolled card/header/dialog/button/input/badge drift than ${BASELINE_NAME} allows. Use the shared primitives (DataCard/SectionCard/CardHeader, the shared dialog wrapper, AppSelect-style wrappers) instead of raising the baseline:`
    const zeroTitle = `A baseline entry has fallen to zero, so the ratchet must shrink. Delete these entries from ${BASELINE_NAME} (the sweep has landed, or the pattern was renamed - fix the baseline rather than leaving a stale allowance):`
    expect(regressions, `${grewTitle}\n${lines(regressions)}`).toEqual([])
    expect(zeroed, `${zeroTitle}\n${lines(zeroed)}`).toEqual([])
  })
})
