/**
 * Shared helpers for the PrimeVue visual-baseline CLIs (FAR-2734).
 *
 * `assemble.mjs` and `compare.mjs` both parse `--flag value` argv, reduce a
 * capture URL to path + query, and render a single difference value for a
 * human-readable line. Those three pieces live here so the two scripts cannot
 * drift apart — the origin reduction in particular must stay identical, or a
 * capture and its aggregate stop being comparable.
 */

/**
 * Parse `--flag value` / `--flag` arguments into an object.
 *
 * A flag followed by another `--flag` (or by nothing) becomes `true`; the
 * first two argv entries (node + script path) are skipped. Repeated flags keep
 * the last value.
 *
 * @param {string[]} argv
 * @returns {Record<string, string | true>}
 */
export function parseArgs(argv) {
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

/**
 * Reduce an absolute capture URL to path + query.
 *
 * The capture runs against a worktree-derived local port, so the origin must
 * not leak into a committed aggregate (it would make every assembly differ) or
 * a delta report. A non-URL string is returned unchanged.
 *
 * @param {string} url
 * @returns {string}
 */
export function pathOf(url) {
  try {
    const parsed = new URL(url)
    return parsed.pathname + parsed.search
  } catch {
    return url
  }
}

/**
 * Format a value for a one-line, human-readable difference message.
 *
 * `maxLength` is a parameter because the two callers report against different
 * budgets: the assembler's `--check` `DIFF` line is tighter than the
 * comparator's delta table. Long values are truncated with an ellipsis.
 *
 * @param {unknown} value
 * @param {{ maxLength?: number, absent?: string }} [options]
 * @returns {string}
 */
export function formatValue(value, { maxLength = 60, absent = '(absent)' } = {}) {
  if (value === undefined) return absent
  const text = JSON.stringify(value) ?? String(value)
  return text.length > maxLength ? `${text.slice(0, maxLength - 3)}...` : text
}
