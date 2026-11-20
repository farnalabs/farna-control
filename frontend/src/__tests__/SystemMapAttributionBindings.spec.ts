import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import enUS from '../locales/en-US.js'

const mockGet = vi.hoisted(() => vi.fn())

vi.mock('../lib/api/client', () => ({
  api: { GET: mockGet },
  getAccessToken: vi.fn(),
}))

import SystemMapAttributionBindings from '../components/system-maps/SystemMapAttributionBindings.vue'

const i18n = createI18n({ legacy: false, locale: 'en-US', messages: { 'en-US': enUS } })

const nodes = [
  { node_key: 'pipeline-1', node_type: 'pipeline', display_name: 'Pipeline 1' },
  { node_key: 'loop-1', node_type: 'improvement_loop', display_name: 'Loop 1' },
  { node_key: 'agent-1', node_type: 'agent', display_name: 'Agent 1' },
]

const bindings = [
  { id: 'binding-1', binding_kind: 'trigger', node_key: 'pipeline-1', pipeline_id: 'pipe-1', trigger_id: 'trig-1' },
  { id: 'binding-2', binding_kind: 'action_pipeline', node_key: 'loop-1', pipeline_id: 'pipe-2', trigger_id: null },
]

function mountComponent(options: {
  bindings?: unknown[]
  readOnly?: boolean
  createHandler?: (binding: unknown) => Promise<void>
  deleteHandler?: (bindingId: string) => Promise<void>
} = {}) {
  return mount(SystemMapAttributionBindings, {
    props: {
      nodes: nodes as never,
      bindings: (options.bindings ?? []) as never,
      readOnly: options.readOnly,
      createHandler: options.createHandler ?? (async () => {}),
      deleteHandler: options.deleteHandler ?? (async () => {}),
    },
    global: { plugins: [i18n] },
  })
}

type BindingsWrapper = ReturnType<typeof mountComponent>

// The create form's four dropdowns are the design-system AppSelect (PrimeVue
// Select), not native <select> elements: options come from props and a choice
// is an `update:modelValue` emit on the inner Select.
function bindingSelect(wrapper: BindingsWrapper, testid: string) {
  const found = wrapper
    .findAllComponents({ name: 'Select' })
    .find((component) => component.props('inputId') === testid)
  expect(found, `expected an AppSelect with id "${testid}"`).toBeDefined()
  return found!
}

function optionLabels(wrapper: BindingsWrapper, testid: string): string[] {
  return (bindingSelect(wrapper, testid).props('options') as Array<{ label: string }>).map(
    (option) => option.label,
  )
}

async function choose(
  wrapper: BindingsWrapper,
  testid: string,
  value: string,
): Promise<void> {
  bindingSelect(wrapper, testid).vm.$emit('update:modelValue', value)
  await wrapper.vm.$nextTick()
}

function pipelinesResponse() {
  return {
    data: {
      items: [
        { id: 'pipe-1', name: 'Pipeline one' },
        { id: 'pipe-2', name: 'Pipeline two' },
      ],
    },
  }
}

describe('SystemMapAttributionBindings', () => {
  beforeEach(() => {
    mockGet.mockReset()
  })

  it('loads pipelines, shows the spinner, and filters eligible nodes by binding kind', async () => {
    let resolvePipelines: (value: unknown) => void = () => {}
    mockGet.mockReturnValue(new Promise((resolve) => { resolvePipelines = resolve }))

    const wrapper = mountComponent()
    await wrapper.vm.$nextTick()
    expect(wrapper.find('.animate-spin').exists()).toBe(true)

    resolvePipelines(pipelinesResponse())
    await flushPromises()

    expect(mockGet).toHaveBeenCalledWith('/api/v1/pipelines', { params: { query: { page_size: 100 } } })
    expect(wrapper.find('.animate-spin').exists()).toBe(false)
    expect(wrapper.find('[data-testid="system-map-binding-empty"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="system-map-binding-trigger"]').exists()).toBe(true)

    expect(optionLabels(wrapper, 'system-map-binding-node')).toContain('Pipeline 1')
    expect(optionLabels(wrapper, 'system-map-binding-node')).not.toContain('Loop 1')

    await choose(wrapper, 'system-map-binding-kind', 'action_pipeline')

    expect(optionLabels(wrapper, 'system-map-binding-node')).toContain('Loop 1')
    expect(optionLabels(wrapper, 'system-map-binding-node')).not.toContain('Pipeline 1')
    expect(wrapper.find('[data-testid="system-map-binding-trigger"]').exists()).toBe(false)
  })

  it('shows the "Select …" prompt in each closed control while no real choice is made', async () => {
    mockGet.mockResolvedValue(pipelinesResponse())

    const wrapper = mountComponent()
    await flushPromises()

    // A disabled '' sentinel is NOT a valid PrimeVue selection, so without an
    // explicit placeholder the closed control renders blank and the prompt is
    // only visible inside the opened option list.
    for (const [testid, prompt] of [
      ['system-map-binding-node', 'Select a node'],
      ['system-map-binding-pipeline', 'Select a pipeline'],
      ['system-map-binding-trigger', 'Select a trigger'],
    ]) {
      expect(bindingSelect(wrapper, testid).props('modelValue')).toBe('')
      expect(wrapper.get(`[data-testid="${testid}"] .p-select-label`).text()).toBe(prompt)
    }

    // Choosing a real option replaces the prompt; the '' sentinel stays out of
    // the selectable set.
    await choose(wrapper, 'system-map-binding-node', 'pipeline-1')
    expect(wrapper.get('[data-testid="system-map-binding-node"] .p-select-label').text()).toBe('Pipeline 1')
  })

  it('loads triggers for the selected pipeline and clears them when the selection changes', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/pipelines') return Promise.resolve(pipelinesResponse())
      if (path === '/api/v1/triggers') return Promise.resolve({ data: { items: [{ id: 'trig-1', name: 'Nightly' }, { id: 'trig-2', name: null, trigger_type: 'schedule' }, { id: 'trig-3', name: '', trigger_type: null }] } })
      return Promise.resolve({ data: {} })
    })

    const wrapper = mountComponent()
    await flushPromises()

    await choose(wrapper, 'system-map-binding-pipeline', 'pipe-1')
    await flushPromises()

    expect(mockGet).toHaveBeenCalledWith('/api/v1/triggers', { params: { query: { pipeline_id: 'pipe-1', page_size: 100 } } })
    const triggerOptions = optionLabels(wrapper, 'system-map-binding-trigger')
    expect(triggerOptions).toContain('Nightly')
    expect(triggerOptions).toContain('schedule')
    expect(triggerOptions).toContain('trig-3')

    // Changing the binding kind resets the pipeline selection to an empty value.
    await choose(wrapper, 'system-map-binding-kind', 'action_pipeline')
    await flushPromises()
    expect(wrapper.find('[data-testid="system-map-binding-trigger"]').exists()).toBe(false)

    // A pipeline selected while the kind is not "trigger" skips the trigger lookup.
    const callsBefore = mockGet.mock.calls.length
    await choose(wrapper, 'system-map-binding-pipeline', 'pipe-1')
    await flushPromises()
    expect(mockGet.mock.calls.length).toBe(callsBefore)
  })

  it('treats a pipelines lookup without an items array as empty', async () => {
    mockGet.mockResolvedValue({ data: {} })

    const wrapper = mountComponent()
    await flushPromises()

    expect(optionLabels(wrapper, 'system-map-binding-pipeline')).toHaveLength(1)
  })

  it('treats a trigger lookup without an items array as empty', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/pipelines') return Promise.resolve(pipelinesResponse())
      if (path === '/api/v1/triggers') return Promise.resolve({ data: {} })
      return Promise.resolve({ data: {} })
    })

    const wrapper = mountComponent()
    await flushPromises()

    await choose(wrapper, 'system-map-binding-pipeline', 'pipe-1')
    await flushPromises()

    expect(optionLabels(wrapper, 'system-map-binding-trigger')).toHaveLength(1)
  })

  it('submits a trigger binding with the selected trigger id', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/pipelines') return Promise.resolve(pipelinesResponse())
      if (path === '/api/v1/triggers') return Promise.resolve({ data: { items: [{ id: 'trig-1', name: 'Nightly' }, { id: 'trig-2', name: null, trigger_type: 'schedule' }, { id: 'trig-3', name: '', trigger_type: null }] } })
      return Promise.resolve({ data: {} })
    })
    const onCreate = vi.fn(async () => {})

    const wrapper = mountComponent({ createHandler: onCreate })
    await flushPromises()

    await choose(wrapper, 'system-map-binding-node', 'pipeline-1')
    await choose(wrapper, 'system-map-binding-pipeline', 'pipe-1')
    await flushPromises()
    await choose(wrapper, 'system-map-binding-trigger', 'trig-1')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(onCreate).toHaveBeenCalledWith({
      node_key: 'pipeline-1',
      pipeline_id: 'pipe-1',
      trigger_id: 'trig-1',
      binding_kind: 'trigger',
    })
  })

  it('requires a trigger before submitting a trigger binding', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/pipelines') return Promise.resolve(pipelinesResponse())
      if (path === '/api/v1/triggers') return Promise.resolve({ data: { items: [{ id: 'trig-1', name: 'Nightly' }] } })
      return Promise.resolve({ data: {} })
    })
    const onCreate = vi.fn(async () => {})

    const wrapper = mountComponent({ createHandler: onCreate })
    await flushPromises()

    await choose(wrapper, 'system-map-binding-node', 'pipeline-1')
    await choose(wrapper, 'system-map-binding-pipeline', 'pipe-1')
    await flushPromises()

    expect(wrapper.get('[data-testid="system-map-binding-create"]').attributes('disabled')).toBeDefined()

    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(onCreate).not.toHaveBeenCalled()
  })

  it('submits an action-pipeline binding without loading triggers', async () => {
    mockGet.mockResolvedValue(pipelinesResponse())
    const onCreate = vi.fn(async () => {})

    const wrapper = mountComponent({ createHandler: onCreate })
    await flushPromises()

    await choose(wrapper, 'system-map-binding-kind', 'action_pipeline')
    await choose(wrapper, 'system-map-binding-node', 'loop-1')
    await choose(wrapper, 'system-map-binding-pipeline', 'pipe-2')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(mockGet).not.toHaveBeenCalledWith('/api/v1/triggers', expect.anything())
    expect(onCreate).toHaveBeenCalledWith({
      node_key: 'loop-1',
      pipeline_id: 'pipe-2',
      trigger_id: null,
      binding_kind: 'action_pipeline',
    })
  })

  it('renders existing bindings and deletes through the handler', async () => {
    mockGet.mockResolvedValue(pipelinesResponse())
    const onDelete = vi.fn(async () => {})

    const wrapper = mountComponent({ bindings, deleteHandler: onDelete })
    await flushPromises()

    expect(wrapper.get('[data-testid="system-map-binding-list"]').text()).toContain('trigger · pipeline-1 · pipe-1 · trig-1')
    expect(wrapper.get('[data-testid="system-map-binding-list"]').text()).toContain('action_pipeline · loop-1 · pipe-2')

    await wrapper.get('[data-testid="system-map-binding-delete-binding-1"]').trigger('click')
    await flushPromises()

    expect(onDelete).toHaveBeenCalledWith('binding-1')
  })

  it('hides the authoring controls when read-only', async () => {
    mockGet.mockResolvedValue(pipelinesResponse())

    const wrapper = mountComponent({ bindings, readOnly: true })
    await flushPromises()

    expect(wrapper.find('form').exists()).toBe(false)
    expect(wrapper.find('[data-testid="system-map-binding-delete-binding-1"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="system-map-binding-list"]').text()).toContain('trigger · pipeline-1 · pipe-1 · trig-1')
  })

  it('surfaces pipeline load failures and falls back for non-Error rejections', async () => {
    mockGet.mockRejectedValueOnce(new Error('pipelines unavailable'))
    const first = mountComponent()
    await flushPromises()
    expect(first.text()).toContain('pipelines unavailable')

    mockGet.mockRejectedValueOnce('offline')
    const second = mountComponent()
    await flushPromises()
    expect(second.text()).toContain('Could not load pipelines')
  })

  it('surfaces trigger load failures and falls back for non-Error rejections', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/pipelines') return Promise.resolve(pipelinesResponse())
      if (path === '/api/v1/triggers') return Promise.reject(new Error('triggers unavailable'))
      return Promise.resolve({ data: {} })
    })
    const first = mountComponent()
    await flushPromises()

    await choose(first, 'system-map-binding-pipeline', 'pipe-1')
    await flushPromises()
    expect(first.text()).toContain('triggers unavailable')

    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/pipelines') return Promise.resolve(pipelinesResponse())
      if (path === '/api/v1/triggers') return Promise.reject('offline')
      return Promise.resolve({ data: {} })
    })
    const second = mountComponent()
    await flushPromises()

    await choose(second, 'system-map-binding-pipeline', 'pipe-1')
    await flushPromises()
    expect(second.text()).toContain('Could not load triggers')
  })

  async function submitActionPipelineBinding(wrapper: Awaited<ReturnType<typeof mountComponent>>): Promise<void> {
    await choose(wrapper, 'system-map-binding-kind', 'action_pipeline')
    await choose(wrapper, 'system-map-binding-node', 'loop-1')
    await choose(wrapper, 'system-map-binding-pipeline', 'pipe-2')
    await wrapper.get('form').trigger('submit')
  }

  it('surfaces async create and delete handler failures', async () => {
    mockGet.mockResolvedValue(pipelinesResponse())
    const onCreate = vi.fn().mockRejectedValue(new Error('create exploded'))
    const onDelete = vi.fn().mockRejectedValue(new Error('delete exploded'))

    const wrapper = mountComponent({ bindings, createHandler: onCreate, deleteHandler: onDelete })
    await flushPromises()

    await submitActionPipelineBinding(wrapper)
    await flushPromises()
    expect(wrapper.text()).toContain('create exploded')

    await wrapper.get('[data-testid="system-map-binding-delete-binding-1"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('delete exploded')
  })

  it('falls back to generic handler-failure messages for non-Error rejections', async () => {
    mockGet.mockResolvedValue(pipelinesResponse())
    const onCreate = vi.fn().mockRejectedValue('offline')
    const onDelete = vi.fn().mockRejectedValue('offline')

    const wrapper = mountComponent({ bindings, createHandler: onCreate, deleteHandler: onDelete })
    await flushPromises()

    await submitActionPipelineBinding(wrapper)
    await flushPromises()
    expect(wrapper.text()).toContain('Could not create attribution binding')

    await wrapper.get('[data-testid="system-map-binding-delete-binding-1"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Could not delete attribution binding')
  })

  it('keeps saving while an async handler is pending and clears it after the rejection settles', async () => {
    mockGet.mockResolvedValue(pipelinesResponse())
    let rejectCreate: (reason: unknown) => void = () => {}
    const onCreate = vi.fn(() => new Promise<void>((_resolve, reject) => { rejectCreate = reject }))

    const wrapper = mountComponent({ createHandler: onCreate })
    await flushPromises()

    await choose(wrapper, 'system-map-binding-kind', 'action_pipeline')
    await choose(wrapper, 'system-map-binding-node', 'loop-1')
    await choose(wrapper, 'system-map-binding-pipeline', 'pipe-2')
    await wrapper.get('form').trigger('submit')
    await wrapper.vm.$nextTick()

    expect(onCreate).toHaveBeenCalledTimes(1)
    expect(wrapper.get('[data-testid="system-map-binding-create"]').attributes('disabled')).toBeDefined()

    rejectCreate(new Error('create failed after a tick'))
    await flushPromises()

    expect(wrapper.text()).toContain('create failed after a tick')
    expect(wrapper.get('[data-testid="system-map-binding-create"]').attributes('disabled')).toBeUndefined()
  })
})
