// FAR-2855: the editor's write controls follow the role each endpoint requires.
// The composable holds a copy of part of the backend permission registry; the
// first block reads the registry itself so the copy cannot drift unnoticed.
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const state = vi.hoisted(() => ({ role: null as string | null }))

vi.mock('../composables/useCurrentUser', async () => {
  const { computed } = await import('vue')
  return { useCurrentUser: () => ({ orgRole: computed(() => state.role) }) }
})

import {
  PIPELINE_EDITOR_PERMISSION_ROLES,
  usePipelineEditorPermissions,
} from '../composables/usePipelineEditorPermissions'

const registrySource = readFileSync(
  resolve(__dirname, '../../../backend/src/farna/auth/permissions.py'),
  'utf-8',
)

const READ_ONLY = {
  canRunPipeline: false,
  canEditGraph: false,
  canUpdatePipeline: false,
  canDeletePipeline: false,
  canCreatePipeline: false,
  canCreateParamSet: false,
  canManageRunnerBindings: false,
}

const EVERYTHING = Object.fromEntries(Object.keys(READ_ONLY).map((key) => [key, true]))

function resolvedPermissions() {
  return Object.fromEntries(
    Object.entries(usePipelineEditorPermissions()).map(([key, value]) => [key, value.value]),
  )
}

describe('usePipelineEditorPermissions', () => {
  beforeEach(() => {
    state.role = null
  })

  it.each(Object.entries(PIPELINE_EDITOR_PERMISSION_ROLES))(
    'requires the same minimum role for %s as the backend registry',
    (permission, role) => {
      const declared = new RegExp(`^\\s*"${permission.replaceAll('.', '\\.')}": "(\\w+)",`, 'm')
      expect(declared.exec(registrySource)?.[1]).toBe(role)
    },
  )

  it.each([
    ['viewer', READ_ONLY],
    ['runner', { ...READ_ONLY, canRunPipeline: true }],
    ['operator', EVERYTHING],
    ['admin', EVERYTHING],
    ['unheard-of', READ_ONLY],
    [null, READ_ONLY],
  ])('resolves the %s role', (role, expected) => {
    state.role = role
    expect(resolvedPermissions()).toStrictEqual(expected)
  })
})
