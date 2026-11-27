/**
 * Shared harness for the ParameterSchemasView specs.
 *
 * `ParameterSchemasView.spec.ts` and `ParameterSchemasView-branches.spec.ts`
 * carried byte-identical copies of the API double, the schema/parameter
 * fixtures, the mount-and-settle helpers and (in the branches spec) four
 * copies of the same `setItem` literal. All of that lives here now; each spec
 * keeps its own `vi.mock` registration — Vitest hoists those per test file —
 * and all of its own cases.
 */
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { expect } from 'vitest'
import type { Mock } from 'vitest'

import ParameterSchemasView from '../../views/ParameterSchemasView.vue'
import { api } from '../../lib/api/client'

/** Settle promises AND the microtask/tick boundary the view's watchers need. */
export async function flush() {
  await flushPromises()
  await nextTick()
  await flushPromises()
}

export const param = (over: Record<string, unknown> = {}) => ({
  name: 'region',
  label: 'Region',
  description: 'AWS region',
  type: 'string',
  required: true,
  default_value: 'us-east-1',
  multiline: false,
  options: undefined,
  minimum: undefined,
  maximum: undefined,
  placeholder: undefined,
  ...over,
})

export const schemaItem = (over: Record<string, unknown> = {}) => ({
  id: 'ps-1',
  organisation_id: 'org-1',
  name: 'Deployment Schema',
  description: 'Deployment parameters',
  version: 3,
  parameters: [param()],
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-02T00:00:00Z',
  ...over,
})

export const setItem = (over: Record<string, unknown> = {}) => ({
  id: 'set-1',
  parameter_schema_id: 'ps-1',
  name: 'Prod Values',
  description: 'prod',
  version: 2,
  schema_version: 3,
  values: { region: 'eu-west-1' },
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
  ...over,
})

export const listPayload = (items: unknown[] = [schemaItem()]) => ({
  data: { items, total: items.length, page: 1, page_size: 100 },
  error: undefined,
})

/** Install the default `api.GET` implementation (or a per-test override). */
export function mockGet(impl?: (url: string, opts?: unknown) => unknown) {
  ;(api.GET as Mock).mockImplementation(async (url: string, opts?: unknown) => {
    if (impl) return impl(url, opts)
    if (url === '/api/v1/parameter-schemas') return listPayload()
    if (url === '/api/v1/model-backends') return { data: { items: [{ id: 'mb-1', name: 'Stub Backend' }] }, error: undefined }
    if (url === '/api/v1/schemas') return listPayload()
    if (url === '/api/v1/parameter-schemas/{schema_id}/sets') return { data: [], error: undefined }
    if (url === '/api/v1/parameter-schemas/{schema_id}/references') return { data: { agents: [], sets: [] }, error: undefined }
    return { data: undefined, error: { detail: 'not found' } }
  })
}

export function mountView() {
  return mount(ParameterSchemasView)
}

export async function mountWithSchemas(items: unknown[] = [schemaItem()]) {
  mockGet(() => listPayload(items))
  const wrapper = mountView()
  await flush()
  return wrapper
}

/** Opens the editor for the first listed schema. */
export async function openEditor() {
  const wrapper = await mountWithSchemas()
  await wrapper.find('tbody tr').trigger('click')
  await flush()
  return wrapper
}

/**
 * Switches editor tabs by clicking the real PrimeVue tab buttons — this is
 * what triggers the view's watch that lazy-loads sets/references.
 */
export async function switchTab(wrapper: Awaited<ReturnType<typeof openEditor>>, label: string) {
  const tab = wrapper.findAll('[role="tab"]').find((t) => t.text() === label)
  expect(tab, `tab ${label} not found`).toBeDefined()
  await tab!.trigger('click')
  await flush()
  return wrapper
}

/** Opens the editor with the parameter-sets tab active. */
export async function openEditorAtSetsTab(sets: unknown[] = [setItem()]) {
  mockGet((url) => {
    if (url === '/api/v1/parameter-schemas/{schema_id}/sets') return { data: sets, error: undefined }
    if (url === '/api/v1/parameter-schemas') return listPayload()
    if (url === '/api/v1/schemas') return listPayload()
    if (url === '/api/v1/model-backends') return { data: { items: [{ id: 'mb-1', name: 'Stub Backend' }] }, error: undefined }
    return { data: undefined, error: { detail: 'not found' } }
  })
  const wrapper = mountView()
  await flush()
  await wrapper.find('tbody tr').trigger('click')
  await flush()
  await switchTab(wrapper, 'Parameter Sets')
  return wrapper
}
