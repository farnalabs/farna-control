/**
 * Guard test: every table in the app renders through the ONE canonical table
 * shell (FAR-2584).
 *
 * The shell is `.table-wrapper` (hairline border + radius + horizontal scroll
 * with `overscroll-behavior-x: contain`) plus `.table-header` / `.table-cell` /
 * `.table-cell-numeric` / `.table-empty`. A page may not recreate a bespoke
 * table surface locally.
 *
 * A table is considered canonical when:
 *  - it sits inside a `.table-wrapper`, OR
 *  - its column labels use the shared `.table-header` class, OR
 *  - it is one of the small, documented inline exceptions below.
 *
 * The allowlist must stay honest: an entry that no longer describes a real
 * exception fails this spec, so it cannot rot.
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'fs'
import { join, relative, extname, sep } from 'path'

const SRC_DIR = join(__dirname, '..')
const STYLE_PATH = join(SRC_DIR, 'style.css')

/** The canonical shell primitives themselves. */
const SHELL_PRIMITIVES = new Set([
  'components/ui/data-table/DataTable.vue',
  'components/shared/TableSkeleton.vue',
])

/**
 * Genuinely unique inline tables that are not list tables. Each entry must
 * keep a raw `<table>` that uses neither `.table-wrapper` nor `.table-header`,
 * otherwise the guard fails as stale.
 */
const INLINE_TABLE_ALLOWLIST: Record<string, string> = {
  'components/pipeline/SnapshotLockCard.vue':
    'dense lock-holder list embedded in the snapshot card',
  'views/AgentOutputDiffView.vue':
    'line-level diff grid (monospace code view, not a data table)',
  'views/CostComponentsView.vue':
    'parameter reference list inside the formula disclosure',
  'views/OnboardingWizard.vue':
    'draft-schema field preview inside the wizard form',
  'views/SchemaInferenceView.vue':
    'draft-schema field preview inside the inference form',
  'views/VariantBatchCompareView.vue':
    'dense per-run token breakdown nested in a comparison card',
}

function walk(dir: string): string[] {
  const results: string[] = []
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) {
      results.push(...walk(full))
    } else if (extname(full) === '.vue') {
      results.push(full)
    }
  }
  return results
}

interface TableSite {
  file: string
  line: number
  nearestOpener: string
  region: string
}

/** Blank out HTML/CSS comments while preserving line numbering. */
function stripComments(source: string): string {
  return source.replace(/<!--[\s\S]*?-->/g, (m) => m.replace(/[^\n]/g, ' '))
}

function collectTableSites(): TableSite[] {
  const sites: TableSite[] = []
  for (const file of walk(SRC_DIR)) {
    const relPath = relative(SRC_DIR, file).split(sep).join('/')
    const source = stripComments(readFileSync(file, 'utf-8'))
    const lines = source.split('\n')
    for (let i = 0; i < lines.length; i++) {
      if (!/<table\b/.test(lines[i])) continue
      // nearest enclosing opener tells us whether this table has the shell
      let nearestOpener = ''
      for (let j = i - 1; j >= 0 && j >= i - 12; j--) {
        if (/<(div|section|td|DataCard)\b/.test(lines[j])) {
          nearestOpener = lines[j]
          break
        }
      }
      const close = source.indexOf('</table>', source.indexOf(lines[i], 0))
      const region = close === -1 ? lines[i] : source.slice(source.indexOf(lines[i], 0), close)
      sites.push({ file: relPath, line: i + 1, nearestOpener, region })
    }
  }
  return sites
}

function isCanonical(site: TableSite): boolean {
  if (SHELL_PRIMITIVES.has(site.file)) return true
  if (/table-wrapper/.test(site.nearestOpener)) return true
  if (/table-header/.test(site.region)) return true
  return site.file in INLINE_TABLE_ALLOWLIST
}

describe('canonical table shell (FAR-2584)', () => {
  const sites = collectTableSites()

  it('finds the tables it is meant to police', () => {
    expect(sites.length).toBeGreaterThan(30)
  })

  it('every table uses the shared shell or cells, or is a documented exception', () => {
    const violations = sites
      .filter((site) => !isCanonical(site))
      .map((site) => `${site.file}:${site.line}`)
    expect(violations).toEqual([])
  })

  it('no allowlisted file has become a stale exception', () => {
    const stale: string[] = []
    for (const [file, reason] of Object.entries(INLINE_TABLE_ALLOWLIST)) {
      expect(reason.length).toBeGreaterThan(10)
      const fileSites = sites.filter((site) => site.file === file)
      if (fileSites.length === 0) {
        stale.push(`${file} (no table any more)`)
        continue
      }
      // at least one table in the file must still be a genuine exception
      if (!fileSites.some((site) => !/table-wrapper/.test(site.nearestOpener) && !/table-header/.test(site.region))) {
        stale.push(`${file} (now uses the shell)`)
      }
    }
    expect(stale).toEqual([])
  })

  it('no table is wrapped in the grey DataCard surface', () => {
    const violations = sites
      .filter((site) => /DataCard/.test(site.nearestOpener))
      .map((site) => `${site.file}:${site.line}`)
    expect(violations).toEqual([])
  })

  it('the shell contains only horizontal overscroll (vertical wheel chains to the page)', () => {
    const css = readFileSync(STYLE_PATH, 'utf-8').replace(/\/\*[\s\S]*?\*\//g, '')
    const wrapper = css.match(/\.table-wrapper\s*\{[^}]*\}/)
    expect(wrapper).not.toBeNull()
    expect(wrapper![0]).toContain('overscroll-behavior-x: contain')
    // the both-axes shorthand is the regression this guard exists for
    expect(css).not.toMatch(/overscroll-behavior:\s*contain/)
  })

  it('no horizontally scrolling container traps the vertical wheel', () => {
    const violations: string[] = []
    for (const file of walk(SRC_DIR)) {
      const relPath = relative(SRC_DIR, file).split(sep).join('/')
      const lines = readFileSync(file, 'utf-8').split('\n')
      lines.forEach((line, index) => {
        for (const match of line.matchAll(/class="([^"]*)"/g)) {
          const cls = match[1]
          if (/\boverflow-x-auto\b/.test(cls) && /\boverscroll-contain\b/.test(cls)) {
            violations.push(`${relPath}:${index + 1}`)
          }
        }
      })
    }
    expect(violations).toEqual([])
  })
})
