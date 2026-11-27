import { afterEach, describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import Aura from '@primevue/themes/aura'
import { Theme } from '@primeuix/styled'

/**
 * Cascade contract for PrimeVue 4 styled mode on top of Tailwind v4 (FAR-2817).
 *
 * The bug this pins: PrimeVue emits its component CSS UNLAYERED unless it is
 * given `cssLayer`. Unlayered CSS beats every `@layer`, so Tailwind utilities
 * (which Tailwind v4 puts in `@layer utilities`) written on a PrimeVue
 * component were silently dropped - `py-2`/`px-3`/`rounded-lg`/`focus:ring-2`
 * on `Input` did nothing, and SearchInput had to escape its icon padding with
 * `pl-9!`. `main.ts` now configures `cssLayer`, which makes PrimeVue wrap every
 * rule in `@layer primevue` and inject a layer-order statement that places
 * `primevue` BELOW `utilities`.
 *
 * Why this is asserted here and not with a computed style: `vitest` runs in
 * jsdom, which does not implement cascade layers at all (a layered declaration
 * is dropped, so a computed-style assertion in this suite cannot see the real
 * cascade). The real-browser evidence for the fix was measured with Playwright
 * against the local mock stack (recorded on FAR-2817): with the option,
 * `.p-inputtext` padding goes 6px -> 8px for a `py-2` control and the
 * `focus:ring-2` box-shadow appears; without it, both stay inert.
 *
 * What IS pinned mechanically, and would fail without the fix:
 *
 *  1. `main.ts` configures `cssLayer` with every layer Tailwind v4 declares,
 *     with `primevue` immediately below `utilities` (read from the installed
 *     `tailwindcss` package, never a hand-copied list - a new upstream layer
 *     fails this test instead of silently falling below `primevue`).
 *  2. The option the app passes is the one PrimeVue actually consumes: it
 *     makes PrimeVue emit `@layer <order>` and wrap component CSS in
 *     `@layer primevue`. With the option off, the same calls emit UNLAYERED
 *     CSS - the exact shape of the bug.
 */

/** The cascade layers Tailwind v4 declares, read from the installed package. */
function tailwindLayers(): string[] {
  const css = readFileSync(resolve(__dirname, '..', '..', 'node_modules', 'tailwindcss', 'index.css'), 'utf8')
  const statement = css.match(/^@layer\s+([^;{]+);/m)
  if (!statement) throw new Error('tailwindcss/index.css declares no layer-order statement')
  return statement[1].split(',').map((layer) => layer.trim()).filter(Boolean)
}

/** The `cssLayer` option `main.ts` passes to `app.use(PrimeVue, ...)`. */
function mainCssLayerOptions(): { name: string; order: string } {
  const source = readFileSync(resolve(__dirname, '..', 'main.ts'), 'utf8')
  // The block holds two string properties, so `[^}]*` is exact - no nesting.
  const block = source.match(/cssLayer:\s*\{([^}]*)\}/)
  if (!block) throw new Error('main.ts configures no PrimeVue theme cssLayer')
  const name = block[1].match(/name:\s*['"]([^'"]+)['"]/)
  const order = block[1].match(/order:\s*['"]([^'"]+)['"]/)
  if (!name || !order) throw new Error('main.ts cssLayer is missing its name or order')
  return { name: name[1], order: order[1] }
}

function orderOf(order: string): string[] {
  return order.split(',').map((layer) => layer.trim()).filter(Boolean)
}

/** The theme as it was before this spec touched the (module-global) singleton. */
const themeBeforeSpec = Theme.getTheme()

afterEach(() => {
  // `Theme` is a module-level singleton; restore it so a later spec that reads
  // the preset/options sees the same state it would without this file.
  Theme.setTheme(themeBeforeSpec)
})

describe('PrimeVue cssLayer cascade contract (FAR-2817)', () => {
  it('places the primevue layer immediately below Tailwind utilities', () => {
    const { order } = mainCssLayerOptions()

    const tailwind = tailwindLayers()
    const expected = [...tailwind.slice(0, tailwind.indexOf('utilities')), 'primevue', 'utilities']

    // Exact order, not just "somewhere before utilities": `utilities` must stay
    // last so a plain utility still beats PrimeVue, and every other Tailwind
    // layer must keep its position (base/preflight stays BELOW PrimeVue, so
    // PrimeVue component defaults are not clobbered by preflight).
    expect(orderOf(order)).toEqual(expected)
  })

  it('only names a layer that the order also declares', () => {
    const { name, order } = mainCssLayerOptions()

    // A `name` missing from `order` would leave the layer undeclared, and a
    // layer first encountered at the end of the order gets appended AFTER
    // `utilities` - silently reinstating the bug this ticket fixed.
    expect(orderOf(order)).toContain(name)
  })

  it('makes PrimeVue emit layered CSS with the app order', () => {
    const { name, order } = mainCssLayerOptions()

    Theme.setTheme({ preset: Aura, options: { cssLayer: { name, order } } })

    expect(Theme.getLayerOrderCSS('inputtext')).toBe(`@layer ${order}`)
    expect(Theme.transformCSS('inputtext', 'a{color:red}', 'style')).toBe(`@layer ${name}{a{color:red}}`)
  })

  it('leaves PrimeVue CSS unlayered without the option (the shape of the bug)', () => {
    // Control: proves the assertions above discriminate. This is what the app
    // shipped before FAR-2817, and why ordinary utilities were ignored.
    Theme.setTheme({ preset: Aura, options: { cssLayer: false } })

    expect(Theme.getLayerOrderCSS('inputtext')).toBe('')
    expect(Theme.transformCSS('inputtext', 'a{color:red}', 'style')).toBe('a{color:red}')
  })
})
