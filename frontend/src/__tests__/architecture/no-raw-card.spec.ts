/**
 * Guard test: the card surface is owned by `components/ui/` (FAR-2697).
 *
 * `.card` (plus `card-base` / `card-header` / `card-content` / `card-footer`)
 * is the ONE card surface in the app. It is emitted by `BaseCard` and reached
 * through its presets (`DataCard`, `TableCard`, `CollapsibleCard`,
 * `CardSkeleton`, `CardHeader`, `CardContent`, `CardTable`, `EmptyState`,
 * `StatCard`, `MetricCard`, `SummaryCard`). A page must not re-implement the
 * surface with a raw `class="card ..."`, because that silently forks the
 * padding/border/hover behaviour and drifts from the primitive.
 *
 * Scope: STATIC `class="..."` attributes in `<template>` blocks of every
 * `*.vue` file under `src/`, excluding the `components/ui/` primitives
 * themselves. Out of scope by design:
 *  - `:class` / `v-bind:class` bindings (dynamic, composed at runtime),
 *  - `<style>` and `<script>` blocks,
 *  - `card-*` PART classes (`card-header`, `card-content`, `card-footer`,
 *    `card-base`) and utility tokens that merely contain the word
 *    (`bg-card`, `cardClass`), which the token boundary excludes.
 *
 * `card-hover` is the one exception: hover styling belongs to `pressable` on
 * BaseCard, so a static `card-hover` without a `card` token in the same
 * attribute is drift too.
 *
 * The allowlist must stay honest: an entry whose file no longer contains a
 * violation fails this spec, so an exception cannot rot into blanket cover.
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { join, relative, extname, sep } from 'node:path'

/** Vitest runs with `frontend/` as the working directory. */
const SRC_DIR = join(process.cwd(), 'src')

/** The primitives that own the card surface. */
const CARD_PRIMITIVES_DIR = 'components/ui/'

interface RawCardSite {
  /** Line number of the offending attribute (1-based). */
  line: number
  /** The whole static class attribute, e.g. `class="card p-4"`. */
  attribute: string
  /** Why it is a violation. */
  why: 'bare-card' | 'card-hover-without-card'
}

/**
 * Files outside `components/ui/` that legitimately carry a raw `card` token in
 * a static class attribute. Each entry must name a real, current exception with
 * a one-line justification; the staleness test below fails an entry that no
 * longer matches.
 */
const ALLOWLIST: ReadonlyArray<{ file: string; reason: string }> = []

function walk(dir: string): string[] {
  const results: string[] = []
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) {
      results.push(...walk(full))
    } else if (entry.isFile() && extname(entry.name) === '.vue') {
      results.push(full)
    }
  }
  return results
}

/** Blank a matched region while preserving newlines (keeps line numbers true). */
function blank(text: string): string {
  return text.replace(/[^\n]/g, ' ')
}

/**
 * Blank everything that is not a static template class attribute: HTML
 * comments, `<style>` blocks and `<script>` blocks.
 */
function templateOnly(source: string): string {
  return source
    .replace(/<!--[\s\S]*?-->/g, blank)
    .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, blank)
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, blank)
}

/**
 * A static `class="..."` attribute: the name is not preceded by a word
 * character or `:` (which would make it `:class` / `v-bind:class`), and its
 * value is a plain quoted string. Dynamic bindings never match.
 */
const STATIC_CLASS_ATTR = /(?<![\w:.-])class=(["'])([^"']*)\1/g

export function findRawCardSites(source: string): RawCardSite[] {
  const template = templateOnly(source)
  const sites: RawCardSite[] = []
  for (const match of template.matchAll(STATIC_CLASS_ATTR)) {
    const tokens = match[2].split(/\s+/).filter(Boolean)
    const hasCard = tokens.includes('card')
    const hasHover = tokens.includes('card-hover')
    if (!hasCard && !hasHover) continue
    const offset = match.index ?? 0
    sites.push({
      line: template.slice(0, offset).split('\n').length,
      attribute: match[0],
      why: hasCard ? 'bare-card' : 'card-hover-without-card',
    })
  }
  return sites
}

interface Violation extends RawCardSite {
  file: string
}

function collectViolations(): Violation[] {
  const violations: Violation[] = []
  for (const path of walk(SRC_DIR)) {
    const relPath = relative(SRC_DIR, path).split(sep).join('/')
    if (relPath.startsWith(CARD_PRIMITIVES_DIR)) continue
    for (const site of findRawCardSites(readFileSync(path, 'utf-8'))) {
      violations.push({ ...site, file: relPath })
    }
  }
  return violations
}

function isAllowlisted(file: string): boolean {
  return ALLOWLIST.some((entry) => entry.file === file)
}

describe('card surface is owned by components/ui (FAR-2697)', () => {
  it('finds the Vue files it is meant to police', () => {
    // A broken walk would make every assertion below pass vacuously.
    expect(walk(SRC_DIR).length).toBeGreaterThan(200)
  })

  it('the matcher itself fails on a raw card surface and ignores parts/bindings', () => {
    // A gate that cannot fail is not a gate: prove the matcher fires.
    expect(findRawCardSites('<template><div class="card p-4">x</div></template>')).toHaveLength(1)

    // The same token in a nested attribute value is still a raw surface.
    expect(findRawCardSites('<template><section class="rounded-lg card">x</section></template>')).toHaveLength(1)

    // `card-hover` alone is drift: hover styling belongs to `pressable`.
    expect(findRawCardSites('<template><div class="card-hover p-4">x</div></template>')).toEqual([
      expect.objectContaining({ why: 'card-hover-without-card' }),
    ])

    // Part classes and lookalike tokens do not match on their own.
    for (const sample of [
      '<template><div class="card-header">x</div></template>',
      '<template><div class="card-content">x</div></template>',
      '<template><div class="card-footer">x</div></template>',
      '<template><div class="card-base">x</div></template>',
      '<template><div class="bg-card p-4">x</div></template>',
      '<template><div class="cardClass">x</div></template>',
    ]) {
      expect(findRawCardSites(sample)).toEqual([])
    }

    // Dynamic bindings are composed at runtime and are not in scope.
    expect(findRawCardSites('<template><div :class="cardClass">x</div></template>')).toEqual([])
    expect(findRawCardSites('<template><div v-bind:class="{ card: on }">x</div></template>')).toEqual([])

    // Styles and scripts may name the class without re-implementing it.
    expect(findRawCardSites('<style>.card { color: red }</style>')).toEqual([])
    expect(findRawCardSites('<script setup lang="ts">const c = \'class="card"\'</script>')).toEqual([])
  })

  it('no page re-implements the card surface', () => {
    const violations = collectViolations()
      .filter((violation) => !isAllowlisted(violation.file))
      .map((violation) => `${violation.file}:${violation.line}: ${violation.attribute} (${violation.why})`)
    expect(violations).toEqual([])
  })

  it('every allowlist entry is justified and still a real exception', () => {
    const violations = collectViolations()
    const stale: string[] = []
    for (const entry of ALLOWLIST) {
      if (entry.reason.trim().length < 10) stale.push(`${entry.file} (reason missing)`)
      if (!violations.some((violation) => violation.file === entry.file)) {
        stale.push(`${entry.file} (no raw card surface any more)`)
      }
    }
    expect(stale).toEqual([])
  })
})
