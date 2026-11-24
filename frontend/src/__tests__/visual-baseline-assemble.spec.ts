import { afterEach, describe, expect, it } from 'vitest'
import { execFileSync } from 'node:child_process'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

/**
 * Unit + CLI coverage for `tests/visual-baseline/assemble.mjs` (FAR-2734).
 *
 * The assembler turns the per-run PrimeVue capture output into the three
 * committed aggregates under `tests/visual-baseline/v5/`. The aggregates are a
 * retained reference that cannot be re-captured from the current tree, so the
 * one thing that must never happen is a silent, lossy merge: these tests pin
 * the shapes, the fail-loud token checks, the preserved `conflicts` block, the
 * byte-for-byte determinism, and the `--check` drift report.
 */

const SCRIPT = resolve(process.cwd(), 'tests/visual-baseline/assemble.mjs')

interface AssemblerFingerprintEntry {
  url: string
  viewport: { width: number; height: number }
  components: Record<string, Array<Record<string, string>>>
}

interface AssemblerTokensFile {
  note: string
  tokens: Record<string, string>
  conflicts: Record<string, unknown>
}

interface Assembler {
  assembleFingerprints: (captures: Array<{ slug: string; capture: unknown }>) => Record<
    string,
    AssemblerFingerprintEntry
  >
  assembleTokens: (
    captures: Array<{ slug: string; capture: unknown }>,
    options?: { note?: string; conflicts?: Record<string, unknown> },
  ) => AssemblerTokensFile
  assembleDom: (signatures: Record<string, string>) => Record<string, string>
  serialise: (value: unknown) => string
}

// The assembler is plain ESM with no declaration file, and `tsconfig.json` does
// not `allowJs`, so it is loaded with a runtime dynamic `import()` against an
// explicit interface. `createRequire(...)()` cannot load it: that throws
// `ERR_REQUIRE_ESM` before Node 20.19, below the documented Node 20+ floor.
// The specifier is a variable so TypeScript does not try (and fail) to resolve
// the untyped module statically; `@vite-ignore` keeps it a native import. The
// same file is exercised as a CLI below, so the wrapper is not the only thing
// under test.
const ASSEMBLER_URL = pathToFileURL(resolve(process.cwd(), 'tests/visual-baseline/assemble.mjs')).href
const { assembleDom, assembleFingerprints, assembleTokens, serialise } = (await import(
  /* @vite-ignore */ ASSEMBLER_URL
)) as Assembler

const TOKENS: Record<string, string> = {
  '--p-badge-height': '1.25rem',
  '--p-card-background': 'light-dark(#ffffff,hsl(217 27% 10%))',
  '--p-primary-color': '#0bdab4',
}

const COMPONENTS: Record<string, Array<Record<string, string>>> = {
  '.p-button': [{ display: 'flex', 'font-size': '14px', __text: 'Save', __class: 'p-button p-component' }],
  '.p-badge': [],
}

interface SyntheticCapture {
  slug: string
  path: string
  viewport: { width: number; height: number }
  url: string
  title: string
  tokens: Record<string, string>
  components: Record<string, Array<Record<string, string>>>
}

/** A capture payload with the exact shape the Playwright spec writes. */
function makeCapture(options: {
  slug: string
  path: string
  viewport?: { width: number; height: number }
  tokens?: Record<string, string>
  components?: Record<string, Array<Record<string, string>>>
}): SyntheticCapture {
  const viewport = options.viewport ?? { width: 1280, height: 800 }
  return {
    slug: options.slug,
    path: options.path,
    viewport,
    url: `http://localhost:4317${options.path}`,
    title: `Farna Control — ${options.slug}`,
    tokens: structuredClone(options.tokens ?? TOKENS),
    components: structuredClone(options.components ?? COMPONENTS),
  }
}

function captureEntries(...captures: SyntheticCapture[]): Array<{ slug: string; capture: SyntheticCapture }> {
  return captures.map((capture) => ({ slug: capture.slug, capture }))
}

const DOM_SIGNATURES: Record<string, string> = Object.fromEntries(
  Array.from({ length: 16 }, (_, index) => [
    `component${String(index + 1).padStart(2, '0')}.default`,
    `<div class="p-c${index + 1}" data-pc-section="root">C${index + 1}</div>`,
  ]),
)

const tempDirs: string[] = []

function tempDir(): string {
  const dir = mkdtempSync(join(tmpdir(), 'visual-baseline-assemble-'))
  tempDirs.push(dir)
  return dir
}

function writeCaptures(dir: string, ...captures: SyntheticCapture[]): string {
  mkdirSync(dir, { recursive: true })
  for (const capture of captures) {
    writeFileSync(join(dir, `${capture.slug}.json`), JSON.stringify(capture, null, 1))
  }
  return dir
}

function writeDom(dir: string, signatures: Record<string, string> = DOM_SIGNATURES): string {
  mkdirSync(dir, { recursive: true })
  const path = join(dir, 'dom.json')
  writeFileSync(path, JSON.stringify(signatures, null, 1))
  return path
}

interface CliResult {
  status: number
  stdout: string
  stderr: string
}

/** Run the assembler CLI and return its exit status instead of throwing. */
function runCli(args: string[]): CliResult {
  try {
    const stdout = execFileSync(process.execPath, [SCRIPT, ...args], {
      encoding: 'utf8',
      // Pipe stderr so a deliberate CLI failure does not leak into the test
      // run's own output; the caller still gets it from the thrown error.
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    return { status: 0, stdout, stderr: '' }
  } catch (error) {
    const failure = error as { status?: number; stdout?: string; stderr?: string }
    return { status: failure.status ?? -1, stdout: failure.stdout ?? '', stderr: failure.stderr ?? '' }
  }
}

afterEach(() => {
  while (tempDirs.length > 0) {
    const dir = tempDirs.pop()
    if (dir) rmSync(dir, { recursive: true, force: true })
  }
})

describe('assembleFingerprints', () => {
  it('merges captures into { url, viewport, components } keyed by slug, in slug order', () => {
    const desktopBeta = makeCapture({ slug: 'desktop-beta', path: '/beta?tab=two' })
    const mobileAlpha = makeCapture({
      slug: 'mobile-alpha',
      path: '/alpha',
      viewport: { width: 390, height: 844 },
    })

    // Deliberately out of slug order: the assembler must sort.
    const fingerprints = assembleFingerprints(captureEntries(mobileAlpha, desktopBeta))

    expect(Object.keys(fingerprints)).toEqual(['desktop-beta', 'mobile-alpha'])
    expect(Object.keys(fingerprints['desktop-beta'])).toEqual(['url', 'viewport', 'components'])
    expect(fingerprints['desktop-beta'].url).toBe('/beta?tab=two')
    expect(fingerprints['desktop-beta'].viewport).toEqual({ width: 1280, height: 800 })
    expect(fingerprints['desktop-beta'].components).toEqual(COMPONENTS)
    expect(fingerprints['mobile-alpha'].viewport).toEqual({ width: 390, height: 844 })
  })

  it('drops the capture-only fields (title, slug, path, tokens) and the url origin', () => {
    const fingerprints = assembleFingerprints(
      captureEntries(makeCapture({ slug: 'desktop-alpha', path: '/alpha' })),
    )

    const serialised = JSON.stringify(fingerprints)
    expect(serialised).not.toContain('Farna Control —')
    expect(serialised).not.toContain('localhost:4317')
    expect(serialised).not.toContain('--p-badge-height')
    expect(Object.keys(fingerprints['desktop-alpha'])).not.toContain('title')
    expect(Object.keys(fingerprints['desktop-alpha'])).not.toContain('tokens')
    expect(Object.keys(fingerprints['desktop-alpha'])).not.toContain('path')
  })

  it('rejects a capture with no numeric viewport', () => {
    const broken = makeCapture({ slug: 'desktop-alpha', path: '/alpha' })
    // @ts-expect-error -- deliberately malformed input for the guard
    broken.viewport = { width: '1280', height: 800 }

    expect(() => assembleFingerprints(captureEntries(broken))).toThrow(/desktop-alpha.*viewport/)
  })
})

describe('assembleTokens', () => {
  it('unions the identical token set across captures, sorted, with an empty conflicts default', () => {
    const tokens = assembleTokens(
      captureEntries(
        makeCapture({ slug: 'desktop-beta', path: '/beta' }),
        makeCapture({ slug: 'desktop-alpha', path: '/alpha' }),
      ),
    )

    expect(Object.keys(tokens)).toEqual(['note', 'tokens', 'conflicts'])
    expect(Object.keys(tokens.tokens)).toEqual([
      '--p-badge-height',
      '--p-card-background',
      '--p-primary-color',
    ])
    expect(tokens.tokens['--p-badge-height']).toBe('1.25rem')
    expect(tokens.conflicts).toEqual({})
    expect(tokens.note.length).toBeGreaterThan(0)
  })

  it('fails loudly, naming both slugs, when two captures disagree on the token set', () => {
    const truncated = { ...TOKENS }
    delete truncated['--p-badge-height']
    truncated['--p-extra-token'] = '1px'

    const captures = captureEntries(
      makeCapture({ slug: 'desktop-alpha', path: '/alpha' }),
      makeCapture({ slug: 'desktop-beta', path: '/beta', tokens: truncated }),
    )

    expect(() => assembleTokens(captures)).toThrow(
      /"desktop-alpha" and "desktop-beta" disagree on the token set/,
    )
    expect(() => assembleTokens(captures)).toThrow(/--p-badge-height/)
    expect(() => assembleTokens(captures)).toThrow(/--p-extra-token/)
  })

  it('fails loudly, naming both slugs and the token, when a token value disagrees', () => {
    const captures = captureEntries(
      makeCapture({ slug: 'desktop-alpha', path: '/alpha' }),
      makeCapture({
        slug: 'desktop-beta',
        path: '/beta',
        tokens: { ...TOKENS, '--p-badge-height': '9rem' },
      }),
    )

    expect(() => assembleTokens(captures)).toThrow(
      /"desktop-alpha" and "desktop-beta" disagree on the value of "--p-badge-height"/,
    )
    expect(() => assembleTokens(captures)).toThrow(/"1\.25rem" vs "9rem"/)
  })
})

describe('assembleDom', () => {
  it('rejects a signature file with 15 or fewer signatures', () => {
    const tooSmall = Object.fromEntries(
      Object.entries(DOM_SIGNATURES).slice(0, 15),
    )

    expect(() => assembleDom(tooSmall)).toThrow(/expected more than 15 signatures, got 15/)
  })

  it('preserves the signature key order so re-assembly is stable', () => {
    const dom = assembleDom(DOM_SIGNATURES)

    expect(Object.keys(dom)).toEqual(Object.keys(DOM_SIGNATURES))
    expect(dom['component01.default']).toBe(DOM_SIGNATURES['component01.default'])
  })
})

describe('determinism', () => {
  it('serialises two identical assemblies to byte-identical JSON', () => {
    const captures = captureEntries(
      makeCapture({ slug: 'desktop-beta', path: '/beta' }),
      makeCapture({ slug: 'desktop-alpha', path: '/alpha' }),
    )

    const first = [
      serialise(assembleFingerprints(captures)),
      serialise(assembleTokens(captures, { note: 'note' })),
      serialise(assembleDom(DOM_SIGNATURES)),
    ].join('')
    const second = [
      serialise(assembleFingerprints(captures)),
      serialise(assembleTokens(captures, { note: 'note' })),
      serialise(assembleDom(DOM_SIGNATURES)),
    ].join('')

    expect(second).toBe(first)
    expect(first.endsWith('}\n')).toBe(true)
  })

  it('writes byte-identical files on two consecutive CLI runs', () => {
    const root = tempDir()
    const capturesDir = writeCaptures(join(root, 'captures'), makeCapture({ slug: 'desktop-alpha', path: '/alpha' }))
    const domPath = writeDom(root)
    const outA = join(root, 'out-a')
    const outB = join(root, 'out-b')

    expect(runCli(['--captures', capturesDir, '--dom', domPath, '--out', outA]).status).toBe(0)
    expect(runCli(['--captures', capturesDir, '--dom', domPath, '--out', outB]).status).toBe(0)

    for (const name of ['fingerprints.json', 'tokens.json', 'dom.json']) {
      expect(readFileSync(join(outB, name), 'utf8'), name).toBe(readFileSync(join(outA, name), 'utf8'))
    }
  })
})

describe('conflicts preservation', () => {
  it('keeps an existing conflicts block instead of zeroing it', () => {
    const root = tempDir()
    const capturesDir = writeCaptures(join(root, 'captures'), makeCapture({ slug: 'desktop-alpha', path: '/alpha' }))
    const domPath = writeDom(root)
    const outDir = join(root, 'out')
    mkdirSync(outDir, { recursive: true })
    writeFileSync(
      join(outDir, 'tokens.json'),
      `${JSON.stringify(
        { note: 'recorded note', tokens: {}, conflicts: { '--p-badge-height': { v5: '1.25rem', v4: '1.5rem' } } },
        null,
        1,
      )}\n`,
    )

    expect(runCli(['--captures', capturesDir, '--dom', domPath, '--out', outDir]).status).toBe(0)

    const tokens = JSON.parse(readFileSync(join(outDir, 'tokens.json'), 'utf8')) as {
      note: string
      tokens: Record<string, string>
      conflicts: Record<string, unknown>
    }
    expect(tokens.conflicts).toEqual({ '--p-badge-height': { v5: '1.25rem', v4: '1.5rem' } })
    // The note is preserved too, so a re-assembly does not churn the file.
    expect(tokens.note).toBe('recorded note')
    expect(tokens.tokens['--p-primary-color']).toBe('#0bdab4')
  })

  it('replaces the conflicts block when --conflicts is supplied', () => {
    const root = tempDir()
    const capturesDir = writeCaptures(join(root, 'captures'), makeCapture({ slug: 'desktop-alpha', path: '/alpha' }))
    const domPath = writeDom(root)
    const outDir = join(root, 'out')
    const conflictsPath = join(root, 'conflicts.json')
    writeFileSync(conflictsPath, JSON.stringify({ '--p-x': { v5: '1px', v4: '2px' } }, null, 1))

    expect(
      runCli([
        '--captures',
        capturesDir,
        '--dom',
        domPath,
        '--out',
        outDir,
        '--conflicts',
        conflictsPath,
        '--note',
        'supplied note',
      ]).status,
    ).toBe(0)

    const tokens = JSON.parse(readFileSync(join(outDir, 'tokens.json'), 'utf8')) as {
      note: string
      conflicts: Record<string, unknown>
    }
    expect(tokens.conflicts).toEqual({ '--p-x': { v5: '1px', v4: '2px' } })
    expect(tokens.note).toBe('supplied note')
  })
})

describe('--check', () => {
  it('passes when the committed aggregates match a fresh assembly', () => {
    const root = tempDir()
    const capturesDir = writeCaptures(join(root, 'captures'), makeCapture({ slug: 'desktop-alpha', path: '/alpha' }))
    const domPath = writeDom(root)
    const outDir = join(root, 'out')

    expect(runCli(['--captures', capturesDir, '--dom', domPath, '--out', outDir]).status).toBe(0)

    const check = runCli(['--captures', capturesDir, '--dom', domPath, '--out', outDir, '--check'])
    expect(check.status).toBe(0)
    expect(check.stdout).toContain('OK       fingerprints.json')
    expect(check.stdout).toContain('all 3 aggregates match a fresh assembly')
    expect(check.stdout).not.toContain('ADVISORY')
  })

  it('passes with an advisory, not a failure, when content matches but the bytes differ', () => {
    const root = tempDir()
    const capturesDir = writeCaptures(join(root, 'captures'), makeCapture({ slug: 'desktop-alpha', path: '/alpha' }))
    const domPath = writeDom(root)
    const outDir = join(root, 'out')

    expect(runCli(['--captures', capturesDir, '--dom', domPath, '--out', outDir]).status).toBe(0)

    // Rewrite two aggregates in a different key order and indent, the way the
    // committed v5 files were hand-assembled. The CONTENT is unchanged.
    const tokensPath = join(outDir, 'tokens.json')
    const tokens = JSON.parse(readFileSync(tokensPath, 'utf8')) as {
      note: string
      tokens: Record<string, string>
      conflicts: Record<string, unknown>
    }
    writeFileSync(
      tokensPath,
      `${JSON.stringify({ conflicts: tokens.conflicts, tokens: tokens.tokens, note: tokens.note }, null, 2)}\n`,
    )

    const fingerprintsPath = join(outDir, 'fingerprints.json')
    const fingerprints = JSON.parse(readFileSync(fingerprintsPath, 'utf8')) as Record<string, unknown>
    writeFileSync(fingerprintsPath, `${JSON.stringify(Object.fromEntries(Object.entries(fingerprints).reverse()), null, 2)}\n`)

    const check = runCli(['--captures', capturesDir, '--dom', domPath, '--out', outDir, '--check'])

    expect(check.status).toBe(0)
    expect(check.stdout).toContain(
      'ADVISORY tokens.json content matches, formatting/key order differs (the committed file is not assembler-normalised)',
    )
    expect(check.stdout).toContain('ADVISORY fingerprints.json content matches')
    expect(check.stdout).toContain('OK       dom.json')
    expect(check.stdout).toContain('all 3 aggregates match a fresh assembly (2 advisory: content identical, formatting differs)')
    expect(check.stdout).not.toContain('DIFF')
  })

  it('names the concrete content difference and exits non-zero', () => {
    const root = tempDir()
    const capturesDir = writeCaptures(
      join(root, 'captures'),
      makeCapture({ slug: 'desktop-alpha', path: '/alpha' }),
      makeCapture({ slug: 'desktop-beta', path: '/beta' }),
    )
    const domPath = writeDom(root)
    const outDir = join(root, 'out')

    expect(runCli(['--captures', capturesDir, '--dom', domPath, '--out', outDir]).status).toBe(0)

    const tokensPath = join(outDir, 'tokens.json')
    const tokens = JSON.parse(readFileSync(tokensPath, 'utf8')) as {
      tokens: Record<string, string>
    }
    tokens.tokens['--p-badge-height'] = '9rem'
    writeFileSync(tokensPath, serialise(tokens))

    const fingerprintsPath = join(outDir, 'fingerprints.json')
    const fingerprints = JSON.parse(readFileSync(fingerprintsPath, 'utf8')) as Record<string, unknown>
    delete fingerprints['desktop-beta']
    writeFileSync(fingerprintsPath, serialise(fingerprints))

    const check = runCli(['--captures', capturesDir, '--dom', domPath, '--out', outDir, '--check'])

    expect(check.status).not.toBe(0)
    expect(check.stdout).toContain('DIFF     fingerprints.json content differs: desktop-beta: only in the fresh assembly')
    expect(check.stdout).toContain('DIFF     tokens.json content differs: tokens.--p-badge-height')
    expect(check.stdout).toContain('"9rem" (committed) vs "1.25rem" (fresh)')
    expect(check.stdout).toContain('2 of 3 aggregates differ from a fresh assembly')
  })

  it('reports a committed file that is not valid JSON as a content difference', () => {
    const root = tempDir()
    const capturesDir = writeCaptures(join(root, 'captures'), makeCapture({ slug: 'desktop-alpha', path: '/alpha' }))
    const domPath = writeDom(root)
    const outDir = join(root, 'out')

    expect(runCli(['--captures', capturesDir, '--dom', domPath, '--out', outDir]).status).toBe(0)
    writeFileSync(join(outDir, 'dom.json'), '{ not json')

    const check = runCli(['--captures', capturesDir, '--dom', domPath, '--out', outDir, '--check'])

    expect(check.status).not.toBe(0)
    expect(check.stdout).toContain('DIFF     dom.json is not valid JSON')
  })

  it('reports drift per file and exits non-zero without writing anything', () => {
    const root = tempDir()
    const capturesDir = writeCaptures(join(root, 'captures'), makeCapture({ slug: 'desktop-alpha', path: '/alpha' }))
    const domPath = writeDom(root)
    const outDir = join(root, 'out')

    expect(runCli(['--captures', capturesDir, '--dom', domPath, '--out', outDir]).status).toBe(0)

    const domOut = JSON.parse(readFileSync(join(outDir, 'dom.json'), 'utf8')) as Record<string, string>
    domOut['component01.default'] = '<div>tampered</div>'
    writeFileSync(join(outDir, 'dom.json'), serialise(domOut))
    const tampered = readFileSync(join(outDir, 'dom.json'), 'utf8')

    const check = runCli(['--captures', capturesDir, '--dom', domPath, '--out', outDir, '--check'])

    expect(check.status).not.toBe(0)
    expect(check.stdout).toContain('OK       fingerprints.json')
    expect(check.stdout).toContain('DIFF     dom.json')
    expect(check.stdout).toContain('1 of 3 aggregates differ from a fresh assembly')
    // --check is read-only: the tampered file is still there, unchanged.
    expect(readFileSync(join(outDir, 'dom.json'), 'utf8')).toBe(tampered)
  })

  it('reports a missing aggregate as MISSING', () => {
    const root = tempDir()
    const capturesDir = writeCaptures(join(root, 'captures'), makeCapture({ slug: 'desktop-alpha', path: '/alpha' }))
    const domPath = writeDom(root)
    const outDir = join(root, 'out')

    expect(runCli(['--captures', capturesDir, '--dom', domPath, '--out', outDir]).status).toBe(0)
    rmSync(join(outDir, 'fingerprints.json'))

    const check = runCli(['--captures', capturesDir, '--dom', domPath, '--out', outDir, '--check'])

    expect(check.status).not.toBe(0)
    expect(check.stdout).toContain('MISSING  fingerprints.json')
  })
})

describe('CLI guard rails', () => {
  it('exits non-zero with the two slugs when captures disagree on the token set', () => {
    const root = tempDir()
    const truncated = { ...TOKENS }
    delete truncated['--p-badge-height']
    const capturesDir = writeCaptures(
      join(root, 'captures'),
      makeCapture({ slug: 'desktop-alpha', path: '/alpha' }),
      makeCapture({ slug: 'desktop-beta', path: '/beta', tokens: truncated }),
    )
    const domPath = writeDom(root)

    const result = runCli(['--captures', capturesDir, '--dom', domPath, '--out', join(root, 'out')])

    expect(result.status).not.toBe(0)
    expect(result.stderr).toContain('desktop-alpha')
    expect(result.stderr).toContain('desktop-beta')
    expect(result.stderr).toContain('disagree on the token set')
  })

  it('exits non-zero when the --dom file has 15 or fewer signatures', () => {
    const root = tempDir()
    const capturesDir = writeCaptures(join(root, 'captures'), makeCapture({ slug: 'desktop-alpha', path: '/alpha' }))
    const domPath = writeDom(
      root,
      Object.fromEntries(Object.entries(DOM_SIGNATURES).slice(0, 15)),
    )

    const result = runCli(['--captures', capturesDir, '--dom', domPath, '--out', join(root, 'out')])

    expect(result.status).not.toBe(0)
    expect(result.stderr).toContain('expected more than 15 signatures')
  })

  it('exits 2 with usage when a required flag is missing', () => {
    const result = runCli(['--captures', tempDir()])

    expect(result.status).toBe(2)
    expect(result.stderr).toContain('usage: assemble.mjs')
  })
})
