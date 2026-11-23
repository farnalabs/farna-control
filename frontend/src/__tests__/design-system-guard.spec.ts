/**
 * Architecture gate: the design-system card/header contract cannot drift (FAR-2710).
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
 * It mirrors the backend architecture test
 * `backend/tests/architecture/test_design_system_conformance.py`, which runs on
 * backend-heavy PRs, every push to main and workflow_dispatch. Keep the two in
 * step: they read the SAME checked-in baseline, `frontend/design-system-baseline.json`,
 * and the two scans must produce identical counts. Regenerate the baseline with
 * the snippet in the Python module's docstring.
 *
 * Two rules
 * ---------
 * 1. Absolute rule (no allowlist). A `DataCard` or `SectionCard` opening tag that
 *    carries BOTH a title attribute and `padding="none"` fails. That is the
 *    exact defect FAR-2694 fixed: the card renders its own header, so the
 *    padding override leaves that header flush against the card edge.
 * 2. Shrinking ratchet. Three objective drift fingerprints are counted per file
 *    and compared with the checked-in baseline. A file's count may equal or
 *    fall below its baseline; it may never exceed it. A baseline entry whose
 *    count has fallen to zero must be deleted (the ratchet only shrinks). A
 *    reduction that has not yet reached zero does not fail, but is emitted as a
 *    warning so it is visible in the test output. A file absent from the
 *    baseline has baseline 0.
 *
 * The three ratchet patterns (one-line example each)
 * -------------------------------------------------
 * - `card_container` - a hand-rolled card shell:
 *   `<div class="rounded-lg border border-border p-4">`. Matches a `<div>` whose
 *   class carries the standalone `card` token, or carries a `rounded`/`rounded-*`
 *   token AND a `border`/`border-*` token AND a `p-<n>`/`p-px`/`p-[...]` token.
 * - `card_header` - a hand-rolled CardHeader title:
 *   `<h3 class="text-base font-semibold">`. Matches an `<h2>`/`<h3>` whose class
 *   carries `font-semibold` together with `text-base` or `text-lg`.
 * - `raw_dialog` - a raw PrimeVue dialog import: `import Dialog from 'primevue/dialog'`
 *   (default, named, namespace, aliased or bare import forms all match).
 *
 * Anti-false-positive discipline: these patterns are deliberately objective, not
 * semantic. A legitimate design call that happens to match (canvas nodes, diff
 * tables, auth cards) stays in the baseline unchanged - the ratchet exists to
 * stop NEW drift, not to relitigate the audit. Files under
 * `frontend/src/components/ui` and `frontend/src/components/shared` (the
 * primitives themselves), plus `frontend/src/components/PageTabs.vue` and
 * `frontend/src/__tests__` are out of scope by construction.
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'fs'
import { join, relative, extname, sep, resolve } from 'path'

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

const PATTERNS = ['card_container', 'card_header', 'raw_dialog'] as const
type Pattern = (typeof PATTERNS)[number]

const HTML_COMMENT = /<!--[\s\S]*?-->/g
const CLASS_ATTR = /(?<![-\w])(?::class|class)\s*=\s*(?:"([^"]*)"|'([^']*)')/gs
const CLASS_TOKEN_SEPARATORS = /[\s,]+/
const CLASS_TOKEN_NOISE = /[\[\]'"`{}()]/g
const PADDING_TOKEN = /^p-(?:\d+(?:\.\d+)?|px|\[[^\]]+\])$/
const TITLE_ATTR = /(?<![-\w])title\s*=/
const PADDING_NONE = /(?<![-\w])padding\s*=\s*(?:"none"|'none'|"'none'"|'"none"')/
const RAW_DIALOG_IMPORT = /^\s*import\s+(?:[^\n]*?\bfrom\s+)?["']primevue\/dialog["']/gm

type Counts = Record<Pattern, Record<string, number>>

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

function stripHtmlComments(text: string): string {
  return text.replace(HTML_COMMENT, '')
}

/** Return every `<name ...>` opening tag, quote-aware so `>` in a value is safe. */
function openingTags(text: string, names: readonly string[]): string[] {
  const tags: string[] = []
  const pattern = new RegExp(`<(?:${names.join('|')})(?=[\\s/>])`, 'gi')
  for (const match of text.matchAll(pattern)) {
    let index = (match.index ?? 0) + match[0].length
    let quote: string | null = null
    while (index < text.length) {
      const char = text[index]
      if (quote !== null) {
        if (char === quote) quote = null
      } else if (char === '"' || char === "'") {
        quote = char
      } else if (char === '>') {
        break
      }
      index += 1
    }
    tags.push(text.slice(match.index, index + 1))
  }
  return tags
}

/** Split every static/dynamic `class` value on the tag into bare tokens. */
function classTokens(tag: string): Set<string> {
  const tokens = new Set<string>()
  for (const match of tag.matchAll(CLASS_ATTR)) {
    const raw = match[1] !== undefined ? match[1] : match[2]
    for (const token of raw.replace(CLASS_TOKEN_NOISE, ' ').split(CLASS_TOKEN_SEPARATORS)) {
      if (token) tokens.add(token)
    }
  }
  return tokens
}

function isCardContainer(tag: string): boolean {
  const tokens = classTokens(tag)
  if (tokens.has('card')) return true
  const rounded = [...tokens].some((token) => token === 'rounded' || token.startsWith('rounded-'))
  const bordered = [...tokens].some((token) => token === 'border' || token.startsWith('border-'))
  const padded = [...tokens].some((token) => PADDING_TOKEN.test(token))
  return rounded && bordered && padded
}

function isCardHeader(tag: string): boolean {
  const tokens = classTokens(tag)
  return tokens.has('font-semibold') && (tokens.has('text-base') || tokens.has('text-lg'))
}

/** Count the three ratchet fingerprints in one `.vue` file's source text. */
function countPatterns(text: string): Record<Pattern, number> {
  const stripped = stripHtmlComments(text)
  return {
    card_container: openingTags(stripped, ['div']).filter(isCardContainer).length,
    card_header: openingTags(stripped, ['h2', 'h3']).filter(isCardHeader).length,
    raw_dialog: (stripped.match(RAW_DIALOG_IMPORT) ?? []).length,
  }
}

/** Per-pattern, per-file counts with non-zero entries only (the baseline shape). */
function collectCounts(): Counts {
  const counts: Counts = { card_container: {}, card_header: {}, raw_dialog: {} }
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

function loadBaseline(): Counts {
  const raw: unknown = JSON.parse(readFileSync(BASELINE_PATH, 'utf-8'))
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
    throw new Error(`${BASELINE_NAME} must be a JSON object keyed by pattern`)
  }
  const record = raw as Record<string, unknown>
  const unknownPatterns = Object.keys(record).filter((key) => !(PATTERNS as readonly string[]).includes(key))
  if (unknownPatterns.length) {
    throw new Error(`${BASELINE_NAME} names unknown pattern(s): ${unknownPatterns.join(', ')}; expected ${PATTERNS.join(', ')}`)
  }
  const baseline: Counts = { card_container: {}, card_header: {}, raw_dialog: {} }
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

/** Offending DataCard/SectionCard tags that combine a title with padding="none". */
function titledCardsWithoutPadding(text: string): string[] {
  const offenders: string[] = []
  for (const tag of openingTags(stripHtmlComments(text), ['DataCard', 'SectionCard'])) {
    if (!TITLE_ATTR.test(tag)) continue
    if (!PADDING_NONE.test(tag)) continue
    offenders.push(tag.split(/\s+/).join(' '))
  }
  return offenders
}

function lines(values: string[]): string {
  return values.map((value) => `  ${value}`).join('\n')
}

describe('design-system conformance guard (FAR-2710)', () => {
  it('flags a titled card that sets padding="none", and ignores a commented-out one', () => {
    // Positive control: without these the absolute rule could never fire.
    expect(titledCardsWithoutPadding('<DataCard title="x" padding="none" />')).toHaveLength(1)
    expect(titledCardsWithoutPadding("<SectionCard title='x' :padding=\"'none'\" />")).toHaveLength(1)
    expect(titledCardsWithoutPadding('<!-- <DataCard title="x" padding="none" /> -->')).toHaveLength(0)
    expect(titledCardsWithoutPadding('<DataCard padding="none" />')).toHaveLength(0)
    expect(titledCardsWithoutPadding('<DataCard title="x" padding="sm" />')).toHaveLength(0)
  })

  it('flags every raw primevue/dialog import form, and nothing else', () => {
    expect(countPatterns("import Dialog from 'primevue/dialog'").raw_dialog).toBe(1)
    expect(countPatterns("import * as Dialog from 'primevue/dialog'").raw_dialog).toBe(1)
    expect(countPatterns("import { Dialog } from 'primevue/dialog'").raw_dialog).toBe(1)
    expect(countPatterns("import Dialog from 'primevue/dialog'\nimport X from 'primevue/confirmdialog'").raw_dialog).toBe(1)
    expect(countPatterns("<script>import X from 'primevue/button'</script>").raw_dialog).toBe(0)
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

  it('never exceeds the checked-in baseline, and shrinks resolved entries to deletion', () => {
    const baseline = loadBaseline()
    const actual = collectCounts()

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

    for (const line of reductions) {
      console.warn(
        `Design-system baseline can shrink further (not a failure): lower or remove this entry in ${BASELINE_NAME}:\n  ${line}`,
      )
    }

    const grewTitle = `A file gained more hand-rolled card/header/dialog drift than ${BASELINE_NAME} allows. Use the shared primitives (DataCard/SectionCard/CardHeader and the shared dialog wrapper) instead of raising the baseline:`
    const zeroTitle = `A baseline entry has fallen to zero, so the ratchet must shrink. Delete these entries from ${BASELINE_NAME} (the sweep has landed, or the pattern was renamed - fix the baseline rather than leaving a stale allowance):`
    expect(regressions, `${grewTitle}\n${lines(regressions)}`).toEqual([])
    expect(zeroed, `${zeroTitle}\n${lines(zeroed)}`).toEqual([])
  })
})
