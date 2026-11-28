// FAR-2855: a role without `model_backend.binding.manage` still reads an
// agent's runner bindings, with nothing on screen that could change them.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const state = vi.hoisted(() => ({ refused: false }))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(async (url: string) => {
      if (url !== '/api/v1/agents/{agent_id}/bindings') {
        return { data: { items: [{ id: 'mb-1', name: 'Primary backend', provider: 'anthropic' }] } }
      }
      if (state.refused) return { data: undefined, error: { status: 403, detail: 'refused-by-server' } }
      return {
        data: { items: [{ model_backend_id: 'mb-1', target_env_var: 'ANTHROPIC_API_KEY', source_field: 'api_key' }] },
      }
    }),
    PUT: vi.fn(async () => ({ data: { items: [] } })),
  },
}))

import AgentRunnerBindings from '../components/agent/AgentRunnerBindings.vue'
import { api } from '../lib/api/client'

const WRITE_CONTROLS = [
  'pipeline-editor-runner-binding-remove-ANTHROPIC_API_KEY',
  'pipeline-editor-runner-binding-target-input',
  'pipeline-editor-runner-binding-source-input',
  'pipeline-editor-runner-binding-add',
  'pipeline-editor-runner-binding-save',
]

async function mountBindings(readOnly: boolean) {
  const wrapper = mount(AgentRunnerBindings, { props: { agentId: 'agent-1', readOnly } })
  await flushPromises()
  return wrapper
}

describe('AgentRunnerBindings read-only', () => {
  beforeEach(() => {
    state.refused = false
    vi.mocked(api.PUT).mockClear()
  })

  it('lists the bindings with no control that changes them', async () => {
    const wrapper = await mountBindings(true)

    expect(wrapper.find('[data-testid="pipeline-editor-runner-bindings-rows"]').text()).toContain('Primary backend')
    for (const id of WRITE_CONTROLS) expect(wrapper.find(`[data-testid="${id}"]`).exists(), id).toBe(false)
    expect(api.PUT).not.toHaveBeenCalled()
  })

  it('keeps the controls for a role that manages bindings', async () => {
    const wrapper = await mountBindings(false)

    for (const id of WRITE_CONTROLS) expect(wrapper.find(`[data-testid="${id}"]`).exists(), id).toBe(true)
  })

  it('shows a read-only user nothing, not an error, when the list is refused', async () => {
    state.refused = true
    const wrapper = await mountBindings(true)

    expect(wrapper.find('[data-testid="pipeline-editor-runner-bindings"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('refused-by-server')
  })
})
