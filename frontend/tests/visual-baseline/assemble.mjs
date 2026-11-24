#!/usr/bin/env node
/* eslint-disable no-console -- this is a CLI: its stdout report is the deliverable. */
/**
 * PrimeVue baseline assembler (FAR-2734).
 *
 * The capture spec (`tests/e2e/primevue-visual-baseline.spec.ts`) writes one
 * `<slug>.json` + `<slug>.png` per capture into `PRIMEVUE_BASELINE_OUT`, and
 * `src/__tests__/primevue-component-dom.spec.ts` writes its rendered-DOM
 * signatures to `PRIMEVUE_DOM_OUT`. This script turns that per-run output into
 * the three committed aggregates under `tests/visual-baseline/v5/`:
 *
 *   fingerprints.json  { <slug>: { url, viewport, components } }
 *   tokens.json        { note, tokens, conflicts }
 *   dom.json           { <signature>: string }
 *
 * It replaces the hand-assembly those files were originally produced with, so
 * the aggregates are reproducible from a capture rather than edited by hand.
 *
 * Usage:
 *   node tests/visual-baseline/assemble.mjs \
 *     --captures <dir> --dom <dom.json> --out <dir> \
 *     [--note <text>] [--conflicts <json-file>] [--check]
 *
 * Flags:
 *   --captures <dir>        directory of per-capture `<slug>.json` files
 *                           (the PNGs are ignored)
 *   --dom <dom.json>        the `PRIMEVUE_DOM_OUT` signature file
 *   --out <dir>             directory the three aggregates are written to /
 *                           compared against (e.g. tests/visual-baseline/v5)
 *   --note <text>           override the `tokens.json` note. Without it the
 *                           target file's note is preserved, falling back to a
 *                           generic note for a fresh directory
 *   --conflicts <json-file> replace the `tokens.json` conflicts block. Without
 *                           it the target file's block is preserved verbatim
 *                           (it comes from the v5 toolchain, not from the
 *                           captures, so it must never be silently zeroed)
 *   --check                 assemble in memory and compare the committed
 *                           aggregates by CONTENT (parsed, key-order
 *                           insensitive), not by bytes. Writes nothing. A
 *                           content difference exits non-zero and names it; a
 *                           formatting/key-order-only difference prints one
 *                           `ADVISORY` line and still exits 0
 *
 * Determinism: capture slugs and token names are sorted, each fingerprint entry
 * always emits `url`, `viewport` ({ width, height }) then `components`, and the
 * files are 1-space-indented JSON with a trailing newline. Two consecutive runs
 * over the same input are byte-identical.
 *
 * Fail-loud cases (non-zero exit, explicit message): two captures disagreeing
 * on the token name set or a token value, a malformed capture, a `--dom` file
 * with 15 or fewer signatures, and `--check` finding a content difference.
 */

import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'

/** JSON indent used by the committed aggregates (and by the capture spec). */
const INDENT = 1

/** Fallback `tokens.json` note when the target file has none and no `--note` is given. */
export const DEFAULT_TOKENS_NOTE = 'Union of computed --p-* values across the captured routes'

/** The minimum number of DOM signatures a `--dom` file must carry. */
const MIN_DOM_SIGNATURES = 15

/** Serialise a value exactly as the committed aggregates are formatted. */
export function serialise(value) {
  return `${JSON.stringify(value, null, INDENT)}\n`
}

/**
 * Reduce an absolute capture URL to path + query.
 *
 * The capture runs against a worktree-derived local port, so the origin must not
 * leak into the committed aggregate (it would make every assembly differ). The
 * comparator does the same reduction, so a capture and its aggregate stay
 * comparable.
 */
function pathOf(url) {
  try {
    const parsed = new URL(url)
    return parsed.pathname + parsed.search
  } catch {
    return url
  }
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function sortedKeys(object) {
  return Object.keys(object).sort()
}

/** Format a list of names for an error message, truncated so it stays readable. */
function sample(names, limit = 8) {
  if (names.length <= limit) return names.join(', ')
  return `${names.slice(0, limit).join(', ')} ... (${names.length} total)`
}

/**
 * Structural equality, insensitive to object key order.
 *
 * `--check` uses this instead of a byte comparison: the committed aggregates
 * predate the assembler and are not in its normalised key order, so a byte
 * comparison would fire on cosmetics alone.
 */
function deepEqual(a, b) {
  if (a === b) return true
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false
    return a.every((item, index) => deepEqual(item, b[index]))
  }
  if (isPlainObject(a) && isPlainObject(b)) {
    const aKeys = Object.keys(a)
    if (aKeys.length !== Object.keys(b).length) return false
    for (const key of aKeys) {
      if (!Object.hasOwn(b, key)) return false
      if (!deepEqual(a[key], b[key])) return false
    }
    return true
  }
  return false
}

/** Cap the number of differences a `--check` line spells out. */
const MAX_DIFFERENCES = 3

function renderPath(segments) {
  let out = ''
  for (const segment of segments) {
    if (typeof segment === 'number') {
      out += `[${segment}]`
      continue
    }
    const rendered = /^[-\w.$]+$/.test(segment) ? segment : `["${segment}"]`
    if (out === '') out = rendered
    else out += rendered.startsWith('[') ? rendered : `.${rendered}`
  }
  return out
}

function formatValue(value) {
  const text = JSON.stringify(value) ?? 'undefined'
  return text.length > 60 ? `${text.slice(0, 57)}...` : text
}

/**
 * Collect the concrete content differences between a committed aggregate and a
 * fresh assembly, at most {@link MAX_DIFFERENCES} of them, for the `--check`
 * `DIFF` line.
 */
function collectDifferences(committed, fresh, path, out) {
  if (out.length >= MAX_DIFFERENCES) return

  if (isPlainObject(committed) && isPlainObject(fresh)) {
    for (const key of sortedKeys(committed)) {
      if (out.length >= MAX_DIFFERENCES) return
      if (!Object.hasOwn(fresh, key)) {
        out.push(`${renderPath(path.concat(key))}: absent from the fresh assembly`)
        continue
      }
      collectDifferences(committed[key], fresh[key], path.concat(key), out)
    }
    for (const key of sortedKeys(fresh)) {
      if (out.length >= MAX_DIFFERENCES) return
      if (!Object.hasOwn(committed, key)) out.push(`${renderPath(path.concat(key))}: only in the fresh assembly`)
    }
    return
  }

  if (Array.isArray(committed) && Array.isArray(fresh)) {
    if (committed.length !== fresh.length) {
      out.push(`${renderPath(path)}: ${committed.length} entries (committed) vs ${fresh.length} (fresh)`)
      return
    }
    for (let index = 0; index < committed.length; index++) {
      collectDifferences(committed[index], fresh[index], path.concat(index), out)
    }
    return
  }

  if (committed === fresh) return
  out.push(`${renderPath(path)}: ${formatValue(committed)} (committed) vs ${formatValue(fresh)} (fresh)`)
}

function describeContentDifference(committed, fresh) {
  const out = []
  collectDifferences(committed, fresh, [], out)
  return out.length === 0 ? 'content differs' : out.join('; ')
}

/**
 * Build `fingerprints.json` from per-capture payloads.
 *
 * @param {Array<{ slug: string, capture: object }>} captures
 * @returns {Record<string, { url: string, viewport: { width: number, height: number }, components: object }>}
 */
export function assembleFingerprints(captures) {
  if (!Array.isArray(captures) || captures.length === 0) {
    throw new Error('fingerprints: no captures supplied')
  }

  const bySlug = new Map()
  for (const entry of captures) {
    if (!isPlainObject(entry) || typeof entry.slug !== 'string' || entry.slug.length === 0) {
      throw new Error('fingerprints: every capture needs a non-empty string slug')
    }
    const { slug, capture } = entry
    if (bySlug.has(slug)) {
      throw new Error(`fingerprints: duplicate capture slug "${slug}"`)
    }
    if (!isPlainObject(capture)) {
      throw new Error(`fingerprints: capture "${slug}" is not an object`)
    }
    const { url, viewport, components } = capture
    if (typeof url !== 'string' || url.length === 0) {
      throw new Error(`fingerprints: capture "${slug}" has no url`)
    }
    if (!isPlainObject(viewport) || typeof viewport.width !== 'number' || typeof viewport.height !== 'number') {
      throw new Error(`fingerprints: capture "${slug}" has no numeric viewport width/height`)
    }
    if (!isPlainObject(components)) {
      throw new Error(`fingerprints: capture "${slug}" has no components object`)
    }

    // Fixed key order (url, viewport, components) and a fixed viewport shape, so
    // the entry never inherits the capture payload's own key order.
    bySlug.set(slug, {
      url: pathOf(url),
      viewport: { width: viewport.width, height: viewport.height },
      components,
    })
  }

  const out = {}
  for (const slug of [...bySlug.keys()].sort()) {
    out[slug] = bySlug.get(slug)
  }
  return out
}

/**
 * Build the `tokens` union across captures.
 *
 * Tokens are document-root values: every capture loads the same theme, so two
 * captures that disagree on the token name set (or on a token's value) mean one
 * of them is broken. That is a hard failure, not something to reconcile here.
 */
function unionTokens(captures) {
  if (!Array.isArray(captures) || captures.length === 0) {
    throw new Error('tokens: no captures supplied')
  }

  const ordered = [...captures].sort((a, b) => (a.slug < b.slug ? -1 : a.slug > b.slug ? 1 : 0))
  for (const entry of ordered) {
    if (!isPlainObject(entry) || typeof entry.slug !== 'string' || entry.slug.length === 0) {
      throw new Error('tokens: every capture needs a non-empty string slug')
    }
  }
  const reference = ordered[0]
  if (!isPlainObject(reference.capture) || !isPlainObject(reference.capture.tokens)) {
    throw new Error(`tokens: capture "${reference.slug}" has no tokens object`)
  }
  const referenceNames = sortedKeys(reference.capture.tokens)

  for (const entry of ordered.slice(1)) {
    if (!isPlainObject(entry.capture) || !isPlainObject(entry.capture.tokens)) {
      throw new Error(`tokens: capture "${entry.slug}" has no tokens object`)
    }
    const tokens = entry.capture.tokens
    const names = sortedKeys(tokens)

    const missing = referenceNames.filter((name) => !(name in tokens))
    const extra = names.filter((name) => !(name in reference.capture.tokens))
    if (missing.length > 0 || extra.length > 0) {
      throw new Error(
        `tokens: captures "${reference.slug}" and "${entry.slug}" disagree on the token set ` +
          `("${reference.slug}" has ${missing.length} token(s) "${entry.slug}" lacks` +
          `${missing.length > 0 ? `: ${sample(missing)}` : ''}; ` +
          `"${entry.slug}" has ${extra.length} token(s) "${reference.slug}" lacks` +
          `${extra.length > 0 ? `: ${sample(extra)}` : ''}). ` +
          'Tokens are document-root values, so a differing set means a broken capture.',
      )
    }

    for (const name of referenceNames) {
      if (tokens[name] !== reference.capture.tokens[name]) {
        throw new Error(
          `tokens: captures "${reference.slug}" and "${entry.slug}" disagree on the value of "${name}" ` +
            `(${JSON.stringify(reference.capture.tokens[name])} vs ${JSON.stringify(tokens[name])}). ` +
            'Tokens are document-root values, so a differing value means a broken capture.',
        )
      }
    }
  }

  const out = {}
  for (const name of referenceNames) {
    out[name] = reference.capture.tokens[name]
  }
  return out
}

/**
 * Build `tokens.json`.
 *
 * `note` and `conflicts` are carried in, not derived: `conflicts` was produced
 * by the v5 toolchain (`@primeuix/styled` `toVariables()`), which is not
 * installed, so it must be preserved verbatim rather than recomputed or zeroed.
 *
 * @param {Array<{ slug: string, capture: object }>} captures
 * @param {{ note?: string, conflicts?: object }} [options]
 * @returns {{ note: string, tokens: Record<string, string>, conflicts: object }}
 */
export function assembleTokens(captures, options = {}) {
  const { note, conflicts } = options
  if (note !== undefined && (typeof note !== 'string' || note.length === 0)) {
    throw new Error('tokens: note must be a non-empty string')
  }
  if (conflicts !== undefined && !isPlainObject(conflicts)) {
    throw new Error('tokens: conflicts must be an object')
  }

  return {
    note: note === undefined ? DEFAULT_TOKENS_NOTE : note,
    tokens: unionTokens(captures),
    conflicts: conflicts === undefined ? {} : conflicts,
  }
}

/**
 * Build `dom.json` from the rendered-DOM signature map.
 *
 * The input key order is preserved (it is the component order the DOM spec
 * emits), so re-assembling the same signature file is byte-identical.
 *
 * @param {Record<string, string>} signatures
 * @returns {Record<string, string>}
 */
export function assembleDom(signatures) {
  if (!isPlainObject(signatures)) {
    throw new Error('dom: signature file must contain a JSON object')
  }
  const names = Object.keys(signatures)
  if (names.length <= MIN_DOM_SIGNATURES) {
    throw new Error(
      `dom: expected more than ${MIN_DOM_SIGNATURES} signatures, got ${names.length}; ` +
        'the DOM spec writes 22 — a shorter file means an incomplete run',
    )
  }
  for (const name of names) {
    if (typeof signatures[name] !== 'string' || signatures[name].length === 0) {
      throw new Error(`dom: signature "${name}" is not a non-empty string`)
    }
  }

  const out = {}
  for (const name of names) {
    out[name] = signatures[name]
  }
  return out
}

/**
 * Read every `<slug>.json` capture in a directory.
 *
 * @param {string} dir
 * @returns {Array<{ slug: string, capture: object }>}
 */
export function loadCaptures(dir) {
  if (!existsSync(dir)) {
    throw new Error(`captures: directory not found: ${dir}`)
  }
  const files = readdirSync(dir)
    .filter((file) => file.endsWith('.json'))
    .sort()
  if (files.length === 0) {
    throw new Error(`captures: no "<slug>.json" capture files found in ${dir}`)
  }

  return files.map((file) => {
    const slug = file.slice(0, -'.json'.length)
    const path = join(dir, file)
    let capture
    try {
      capture = JSON.parse(readFileSync(path, 'utf8'))
    } catch (error) {
      throw new Error(`captures: ${path} is not valid JSON: ${error instanceof Error ? error.message : error}`)
    }
    if (isPlainObject(capture) && typeof capture.slug === 'string' && capture.slug !== slug) {
      throw new Error(
        `captures: ${path} declares slug "${capture.slug}" but the filename says "${slug}"; ` +
          'the filename is the aggregate key, so they must agree',
      )
    }
    return { slug, capture }
  })
}

function parseArgs(argv) {
  const args = {}
  for (let i = 2; i < argv.length; i++) {
    const token = argv[i]
    if (!token.startsWith('--')) continue
    const key = token.slice(2)
    const value = argv[i + 1]
    if (value === undefined || value.startsWith('--')) {
      args[key] = true
    } else {
      args[key] = value
      i++
    }
  }
  return args
}

function readJsonFile(path, label) {
  if (!existsSync(path)) {
    throw new Error(`${label}: file not found: ${path}`)
  }
  try {
    return JSON.parse(readFileSync(path, 'utf8'))
  } catch (error) {
    throw new Error(`${label}: ${path} is not valid JSON: ${error instanceof Error ? error.message : error}`)
  }
}

function readJsonIfExists(path) {
  if (!existsSync(path)) return null
  try {
    return JSON.parse(readFileSync(path, 'utf8'))
  } catch {
    // A corrupt target is a --check/reassembly concern, not a reason to abort:
    // treat it as absent so the fresh assembly (and the drift report) wins.
    return null
  }
}

/** `--conflicts` accepts either the conflicts map itself or `{ conflicts: {...} }`. */
function readConflicts(path) {
  const parsed = readJsonFile(path, 'conflicts')
  if (!isPlainObject(parsed)) {
    throw new Error(`conflicts: ${path} must contain a JSON object`)
  }
  if (isPlainObject(parsed.conflicts)) return parsed.conflicts
  return parsed
}

const USAGE =
  'usage: assemble.mjs --captures <dir> --dom <dom.json> --out <dir> ' +
  '[--note <text>] [--conflicts <json-file>] [--check]'

function main(argv) {
  const args = parseArgs(argv)
  if (args.help) {
    console.log(USAGE)
    return 0
  }
  if (!args.captures || !args.dom || !args.out) {
    console.error(USAGE)
    return 2
  }

  const capturesDir = resolve(args.captures)
  const domPath = resolve(args.dom)
  const outDir = resolve(args.out)

  const captures = loadCaptures(capturesDir)
  const signatures = readJsonFile(domPath, 'dom')

  const targetTokensPath = join(outDir, 'tokens.json')
  const targetTokens = readJsonIfExists(targetTokensPath)

  const note =
    typeof args.note === 'string'
      ? args.note
      : isPlainObject(targetTokens) && typeof targetTokens.note === 'string'
        ? targetTokens.note
        : undefined

  const conflicts =
    typeof args.conflicts === 'string'
      ? readConflicts(resolve(args.conflicts))
      : isPlainObject(targetTokens) && isPlainObject(targetTokens.conflicts)
        ? targetTokens.conflicts
        : undefined

  const aggregates = {
    'fingerprints.json': assembleFingerprints(captures),
    'tokens.json': assembleTokens(captures, { note, conflicts }),
    'dom.json': assembleDom(signatures),
  }
  const files = Object.fromEntries(
    Object.entries(aggregates).map(([name, value]) => [name, serialise(value)]),
  )

  if (args.check) {
    console.log(`assemble.mjs --check ${outDir}`)
    let drifted = 0
    let advisory = 0
    const total = Object.keys(aggregates).length

    for (const [name, fresh] of Object.entries(aggregates)) {
      const targetPath = join(outDir, name)
      if (!existsSync(targetPath)) {
        console.log(`  MISSING  ${name}`)
        drifted++
        continue
      }

      const committedText = readFileSync(targetPath, 'utf8')
      if (committedText === files[name]) {
        console.log(`  OK       ${name}`)
        continue
      }

      let committed
      try {
        committed = JSON.parse(committedText)
      } catch (error) {
        console.log(`  DIFF     ${name} is not valid JSON: ${error instanceof Error ? error.message : error}`)
        drifted++
        continue
      }

      // Byte difference, same content: the committed file is simply not in the
      // assembler's normalised key order/formatting. Advisory, not a failure.
      if (deepEqual(committed, fresh)) {
        console.log(
          `  ADVISORY ${name} content matches, formatting/key order differs (the committed file is not assembler-normalised)`,
        )
        advisory++
        continue
      }

      console.log(`  DIFF     ${name} content differs: ${describeContentDifference(committed, fresh)}`)
      drifted++
    }

    if (drifted === 0) {
      console.log(
        advisory === 0
          ? `all ${total} aggregates match a fresh assembly`
          : `all ${total} aggregates match a fresh assembly ` +
              `(${advisory} advisory: content identical, formatting differs)`,
      )
      return 0
    }

    console.log(
      `${drifted} of ${total} aggregates differ from a fresh assembly` +
        (advisory === 0 ? '' : `; ${advisory} advisory`),
    )
    return 1
  }

  mkdirSync(outDir, { recursive: true })
  for (const [name, content] of Object.entries(files)) {
    writeFileSync(join(outDir, name), content)
    console.log(`wrote ${join(outDir, name)} (${content.length} bytes)`)
  }
  return 0
}

// `import.meta.filename` is the module's own path when run by node, and absent
// under a bundler/transformer (e.g. vitest importing it as a library), so the
// CLI only runs when this file is the entry point.
const invokedDirectly =
  typeof import.meta.filename === 'string' && process.argv[1] !== undefined
    ? resolve(process.argv[1]) === import.meta.filename
    : false

if (invokedDirectly) {
  try {
    process.exitCode = main(process.argv)
  } catch (error) {
    console.error(`assemble.mjs: ${error instanceof Error ? error.message : error}`)
    process.exitCode = 1
  }
}
