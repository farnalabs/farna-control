import { describe, expect, it } from 'vitest'
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'

/**
 * Guard test for the PrimeVue visual-baseline reference metadata (FAR-2725).
 *
 * `tests/visual-baseline/v5/` is a RETAINED capture of PrimeVue 5.0.4, kept as
 * the "before" side of the FAR-2205 downgrade measurement. It therefore
 * describes a deliberately DIFFERENT PrimeVue version from the one installed
 * (`package.json` pins PrimeVue 4.5.5) and cannot be regenerated from the
 * current tree.
 *
 * That difference is correct, so the invariant enforced here is NOT
 * `recorded === installed`. It is: the directory name, `versions.json` and the
 * README all agree that the reference describes PrimeVue 5.x, and
 * `versions.json` explicitly declares that it intentionally differs from the
 * installed version. If that intent is ever dropped — most likely by someone
 * "helpfully" rewriting `versions.json` to the installed 4.5.5, silently
 * turning the v5 reference into a v4 one — this guard fails instead of letting
 * the drift stay invisible.
 */

interface Assessment {
  consistent: boolean
  message: string
}

interface VersionAssessmentArgs {
  recorded: string
  installed: string
  /** The major declared by the reference metadata, or null for a live baseline. */
  declaredReferenceMajor: number | null
}

interface VersionsFile {
  primevue: string
  themes: string
  styled: string
  reference: {
    kind: string
    primevue_major: number
    intentionally_differs_from_installed: boolean
  }
}

interface PackageFile {
  dependencies: Record<string, string>
}

/** The major component of a (possibly range-prefixed) semver string, or -1. */
function majorOf(version: string): number {
  const major = Number.parseInt(version.replace(/^[^\d]*/, '').split('.')[0], 10)
  return Number.isNaN(major) ? -1 : major
}

/**
 * Decide whether a recorded baseline version is honestly consistent with its
 * declared intent.
 *
 * - Live baseline (`declaredReferenceMajor === null`): the recorded version is
 *   meant to match the installed one, so any difference is drift and is
 *   reported with BOTH values.
 * - Pinned reference (`declaredReferenceMajor !== null`): the recorded version
 *   is intentionally frozen at a specific major (here, 5) and must carry that
 *   major. The installed version is expected to differ and is not compared.
 */
function assessRecordedVersion(args: VersionAssessmentArgs): Assessment {
  const { recorded, installed, declaredReferenceMajor } = args

  if (declaredReferenceMajor === null) {
    if (recorded === installed) {
      return {
        consistent: true,
        message: `recorded PrimeVue ${recorded} matches installed PrimeVue ${installed}`,
      }
    }
    return {
      consistent: false,
      message: `recorded PrimeVue ${recorded} does not match installed PrimeVue ${installed}`,
    }
  }

  const recordedMajor = majorOf(recorded)
  if (recordedMajor !== declaredReferenceMajor) {
    return {
      consistent: false,
      message:
        `versions.json declares a PrimeVue ${declaredReferenceMajor} reference but records ` +
        `PrimeVue ${recorded} (major ${recordedMajor}); installed is PrimeVue ${installed}`,
    }
  }

  return {
    consistent: true,
    message:
      `recorded PrimeVue ${recorded} is the declared v${declaredReferenceMajor} reference ` +
      `(installed PrimeVue ${installed})`,
  }
}

function baselineRoot(): string {
  return resolve(process.cwd(), 'tests/visual-baseline')
}

function readJson<T>(path: string): T {
  return JSON.parse(readFileSync(path, 'utf8')) as T
}

/**
 * The single recorded-reference directory that holds a `versions.json`.
 * Enumerated (not hard-coded) so a rename to a different major is caught by the
 * asserts below rather than reading a file that no longer exists.
 */
function baselineDir(): { name: string; path: string } {
  const root = baselineRoot()
  const dirs = readdirSync(root, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .filter((entry) => existsSync(resolve(root, entry.name, 'versions.json')))

  expect(dirs.map((entry) => entry.name), 'expected exactly one baseline reference directory').toHaveLength(1)
  const name = dirs[0].name
  return { name, path: resolve(root, name) }
}

function versionsFile(): VersionsFile {
  return readJson<VersionsFile>(resolve(baselineDir().path, 'versions.json'))
}

function installedDependencies(): Record<string, string> {
  return readJson<PackageFile>(resolve(process.cwd(), 'package.json')).dependencies
}

describe('PrimeVue visual-baseline reference metadata guard', () => {
  describe('assessRecordedVersion (proves the comparator is not vacuous)', () => {
    it('flags a live baseline whose recorded version differs from the installed one', () => {
      // The stubbed pair requested by FAR-2725: a comparator that stayed green
      // here would be an assertion about the test's own input, not a check.
      const result = assessRecordedVersion({
        recorded: '9.9.9',
        installed: '4.5.5',
        declaredReferenceMajor: null,
      })
      expect(result.consistent).toBe(false)
      expect(result.message).toContain('9.9.9')
      expect(result.message).toContain('4.5.5')
    })

    it('accepts a live baseline whose recorded version matches the installed one', () => {
      const result = assessRecordedVersion({
        recorded: '4.5.5',
        installed: '4.5.5',
        declaredReferenceMajor: null,
      })
      expect(result.consistent).toBe(true)
    })

    it('flags a declared reference whose recorded version has a different major', () => {
      // This is the exact drift the guard exists to stop: "fixing" versions.json
      // to the installed 4.5.5 while the declared intent still says v5.
      const result = assessRecordedVersion({
        recorded: '4.5.5',
        installed: '4.5.5',
        declaredReferenceMajor: 5,
      })
      expect(result.consistent).toBe(false)
      expect(result.message).toContain('4.5.5')
      expect(result.message).toContain('5')
    })

    it('accepts a declared reference whose recorded version carries that major', () => {
      const result = assessRecordedVersion({
        recorded: '5.0.4',
        installed: '4.5.5',
        declaredReferenceMajor: 5,
      })
      expect(result.consistent).toBe(true)
    })
  })

  describe('the committed reference', () => {
    it('declares itself a PrimeVue 5 reference that intentionally differs from installed', () => {
      const versions = versionsFile()
      expect(versions.reference.kind).toBe('primevue-v5-downgrade-baseline')
      expect(versions.reference.primevue_major).toBe(5)
      expect(versions.reference.intentionally_differs_from_installed).toBe(true)
      expect(majorOf(versions.primevue)).toBe(versions.reference.primevue_major)
    })

    it('records a version consistent with its declared intent', () => {
      const versions = versionsFile()
      const installed = installedDependencies().primevue
      expect(installed).toBeTypeOf('string')
      const result = assessRecordedVersion({
        recorded: versions.primevue,
        installed,
        declaredReferenceMajor: versions.reference.primevue_major,
      })
      expect(result.consistent, result.message).toBe(true)
    })

    it('the installed PrimeVue is genuinely a different major from the reference', () => {
      // The reference exists because the app is on the v4 line. If the installed
      // PrimeVue ever becomes 5.x again, the "v5 reference" is stale and this
      // guard forces a conscious decision rather than leaving a false claim.
      const versions = versionsFile()
      const installedMajor = majorOf(installedDependencies().primevue)
      expect(
        installedMajor,
        `installed PrimeVue major ${installedMajor} must differ from the v${versions.reference.primevue_major} reference; if the app is back on PrimeVue 5, re-evaluate or remove this reference`,
      ).not.toBe(versions.reference.primevue_major)
    })

    it('the directory name encodes the same major as versions.json', () => {
      const { name } = baselineDir()
      const versions = versionsFile()
      const dirMajor = Number.parseInt(name.replace(/^[^\d]*/, ''), 10)
      expect(dirMajor, `directory "${name}" must encode the reference major`).toBe(
        versions.reference.primevue_major,
      )
      expect(majorOf(versions.primevue)).toBe(dirMajor)
    })

    it('the README agrees with versions.json on the recorded version and intent', () => {
      const versions = versionsFile()
      const readme = readFileSync(resolve(baselineRoot(), 'README.md'), 'utf8')
      // Exact recorded version: a re-capture that bumps versions.json without
      // updating the README is drift.
      expect(readme, `README must name the recorded reference version ${versions.primevue}`).toContain(
        versions.primevue,
      )
      // And the README must state that the reference intentionally differs from
      // the installed version (the file's own stated intent, in prose).
      expect(readme.toLowerCase(), 'README must state the reference intentionally differs from the installed version').toContain(
        'intentionally',
      )
    })

    it('the other recorded dependency versions are well-formed v5-era records', () => {
      const versions = versionsFile()
      const dependencies = installedDependencies()

      // `themes` and `styled` record the v5 toolchain captured alongside
      // PrimeVue 5.0.4. They are not comparable to the installed tree:
      //   - `@primeuix/themes` is not an installed dependency at all (the v4 app
      //     uses `@primevue/themes`), so there is no installed value to compare.
      //   - `@primeuix/styled` IS installed, but on the v4-compatible 0.x line;
      //     the reference records the v5 1.x line, another intentional difference.
      // Assert the records stay well-formed package+version strings, and pin the
      // non-checkable fact so the reason stays visible rather than assumed.
      const semverRecord = /^@?[a-z0-9][\w./-]* \d+\.\d+\.\d+$/
      expect(versions.themes).toMatch(semverRecord)
      expect(versions.styled).toMatch(semverRecord)
      expect(dependencies['@primeuix/themes']).toBeUndefined()
    })
  })
})
