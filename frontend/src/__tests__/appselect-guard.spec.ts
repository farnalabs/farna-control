/**
 * Guard test: prevents direct imports of primevue/select or primevue/dropdown
 * outside of AppSelect.vue. All usages should go through the shared wrapper
 * to ensure appendTo defaults to 'self' (FAR-851, FAR-869).
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'fs'
import { join, relative, extname, sep } from 'path'

const SRC_DIR = join(__dirname, '..')
const ALLOWED_FILE = 'AppSelect.vue'

/**
 * Test files that must import the raw primitive rather than the wrapper,
 * because their subject IS the primitive. `primevue-component-dom.spec.ts`
 * mounts each component the app uses (including PrimeVue's Select) to pin its
 * rendered DOM across a dependency upgrade (FAR-2205); routing it through
 * AppSelect would test the wrapper instead of the component. Application code
 * is still covered by the scan below.
 */
const ALLOWED_TEST_FILES = new Set(['__tests__/primevue-component-dom.spec.ts'])

function walk(dir: string): string[] {
  const results: string[] = []
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) {
      results.push(...walk(full))
    } else if (extname(full) === '.vue' || extname(full) === '.ts') {
      results.push(full)
    }
  }
  return results
}

describe('No direct primevue/select or primevue/dropdown imports', () => {
  const files = walk(SRC_DIR)
  const violations: string[] = []

  for (const file of files) {
    const relPath = relative(SRC_DIR, file).split(sep).join('/')
    if (relPath.includes(ALLOWED_FILE)) continue
    if (ALLOWED_TEST_FILES.has(relPath)) continue

    const content = readFileSync(file, 'utf-8')
    if (/from\s+['"]primevue\/select['"]/.test(content) || /from\s+['"]primevue\/dropdown['"]/.test(content)) {
      violations.push(relPath)
    }
  }

  it('should have no direct primevue/select or primevue/dropdown imports outside AppSelect.vue', () => {
    expect(violations).toEqual([])
  })
})

/**
 * Guard 2 (FAR-2586): no native `<select>` in a component template.
 *
 * Every user-facing dropdown must be the design-system AppSelect. A native
 * `<select>` renders the browser's own control, which does not match the
 * product's dropdown styling (misaligned chevron, inconsistent focus ring and
 * overlay behaviour) and bypasses the wrapper's label association and
 * overlay-placement guarantees.
 *
 * Only `.vue` files are scanned, and only their template block: a `<select>`
 * inside a spec's `global.stubs` is a test double, not a product surface, so
 * `__tests__`/`tests` directories are skipped. Style and script blocks are
 * stripped before scanning so a comment that names `<select>` is not a
 * violation.
 */
const NATIVE_SELECT_ALLOWLIST: Record<string, string> = {
  // A control that genuinely cannot be an AppSelect (a native multi-select
  // listbox, a size>1 select, a control inside a <noscript> fallback, …) is
  // added here as '<repo-relative path>': '<why the native control is
  // required>'. Keep this list empty unless the reason is unavoidable — the
  // point of the guard is that a new native dropdown cannot slip in silently.
}

/** A `<select>` element opening tag, in any of its authored spellings. */
const NATIVE_SELECT_TAG = /<select(?=[\s>/])/

function stripNonTemplateBlocks(content: string): string {
  return content
    .replace(/<script[\s\S]*?<\/script>/g, '')
    .replace(/<style[\s\S]*?<\/style>/g, '')
    .replace(/<!--[\s\S]*?-->/g, '')
}

/** The template text of a single-file component, with non-markup blocks removed. */
function templateOf(content: string): string {
  return stripNonTemplateBlocks(content)
}

describe('No native <select> in a component template (AppSelect sweep, FAR-2586)', () => {
  it('flags a native select in a template', () => {
    // The detector must actually fire, otherwise the sweep below is vacuous.
    expect(NATIVE_SELECT_TAG.test(templateOf('<template><select v-model="x" /></template>'))).toBe(true)
    expect(NATIVE_SELECT_TAG.test(templateOf('<template><!-- <select> --><div /></template>'))).toBe(false)
    expect(NATIVE_SELECT_TAG.test(templateOf('<template><div /></template><script>// <select></script>'))).toBe(false)
  })

  it('should have no user-facing native <select> outside the allowlist', () => {
    const violations: string[] = []

    for (const file of walk(SRC_DIR)) {
      const relPath = relative(SRC_DIR, file).split(sep).join('/')
      if (!relPath.endsWith('.vue')) continue
      if (relPath.includes('__tests__/')) continue
      if (relPath in NATIVE_SELECT_ALLOWLIST) continue

      if (NATIVE_SELECT_TAG.test(templateOf(readFileSync(file, 'utf-8')))) {
        violations.push(relPath)
      }
    }

    expect(violations).toEqual([])
  })

  it('documents every allowlisted native <select> with a reason', () => {
    for (const [path, reason] of Object.entries(NATIVE_SELECT_ALLOWLIST)) {
      expect(reason.trim().length, `allowlist entry "${path}" needs a documented reason`).toBeGreaterThan(10)
    }
  })
})
