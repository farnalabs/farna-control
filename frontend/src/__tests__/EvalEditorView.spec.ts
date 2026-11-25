import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('../composables/useApi', () => ({
  useApi: vi.fn(() => ({
    get: vi.fn().mockResolvedValue([]),
    post: vi.fn().mockResolvedValue({}),
    put: vi.fn().mockResolvedValue({}),
    del: vi.fn().mockResolvedValue(undefined),
  })),
}))

vi.mock('../lib/api/client', () => ({
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
  api: {
    GET: vi.fn().mockResolvedValue({ data: { items: [] } }),
    POST: vi.fn().mockResolvedValue({ data: {} }),
    PUT: vi.fn().mockResolvedValue({ data: {} }),
    DELETE: vi.fn().mockResolvedValue({ data: {} }),
  },
}))

vi.mock('vue-i18n', () => ({
  useI18n: () => ({ t: (key: string) => key }),
}))

// A mutable route query lets a test seed ?pipeline_id= before mount; the
// replace spy proves the URL is kept in sync with the selector.
const routerMocks = vi.hoisted(() => ({
  push: vi.fn().mockResolvedValue(undefined),
  replace: vi.fn().mockResolvedValue(undefined),
  query: {} as Record<string, unknown>,
}))

vi.mock('vue-router', async () => {
  const { reactive } = await import('vue')
  const query = reactive<Record<string, unknown>>({})
  routerMocks.query = query
  // Behave like the real router for the one thing this page does: replacing
  // the query also updates the route the component reads back.
  const replace = vi.fn(async (to: { query?: Record<string, unknown> }) => {
    for (const key of Object.keys(query)) delete query[key]
    Object.assign(query, to?.query ?? {})
  })
  routerMocks.replace = replace
  return {
    useRoute: vi.fn(() => ({
      path: '/evals/editor',
      fullPath: '/evals/editor',
      params: {},
      query,
      hash: '',
      matched: [],
      name: 'eval-editor',
      redirectedFrom: undefined,
      meta: {},
    })),
    useRouter: vi.fn(() => ({
      push: routerMocks.push,
      replace,
      resolve: vi.fn(),
      go: vi.fn(),
      back: vi.fn(),
      forward: vi.fn(),
      beforeEach: vi.fn(),
      afterEach: vi.fn(),
      onError: vi.fn(),
      currentRoute: { value: {} },
      getRoutes: vi.fn(() => []),
      addRoute: vi.fn(),
      removeRoute: vi.fn(),
      hasRoute: vi.fn(() => false),
      isReady: vi.fn().mockResolvedValue(undefined),
      install: vi.fn(),
    })),
  }
})

import EvalEditorView from '../views/EvalEditorView.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { api, getAccessToken } from '../lib/api/client'
import PageTabs from '../components/PageTabs.vue'

const apiGET = api.GET as ReturnType<typeof vi.fn>
const apiPOST = api.POST as ReturnType<typeof vi.fn>
const apiPUT = api.PUT as ReturnType<typeof vi.fn>
const apiDELETE = api.DELETE as ReturnType<typeof vi.fn>
const getAccessTokenMock = getAccessToken as ReturnType<typeof vi.fn>

// FAR-2620: the eval-editor delete affordance mirrors TWO backend gates — the
// `eval.definition.delete` route permission (operator floor, resolved live by
// the store) AND an in-body `org_role == "admin"` check. These helpers seed a
// real caller identity so a test can pin exactly which half it is exercising.
const PERMISSIONS_URL = '/api/v1/auth/permissions'
const EVAL_DELETE_PERMISSION = 'eval.definition.delete'

function fakeJwt(orgRole: string): string {
  const segment = (value: unknown) => btoa(JSON.stringify(value))
  return `${segment({ alg: 'none', typ: 'JWT' })}.${segment({
    sub: `${orgRole}-user`,
    org_id: 'org-1',
    org_role: orgRole,
  })}.test-signature`
}

function permissionsOk(permissions: string[]) {
  return { data: { permissions } }
}

function evalItem(over: Record<string, unknown> = {}) {
  return {
    id: 'eval-1',
    pipeline_id: 'p1',
    node_id: null,
    name: 'Existing Eval',
    eval_type: 'regex',
    config_json: { pattern: '.*' },
    pass_threshold: 0.9,
    suite_id: null,
    created_by: 'user-1',
    ...over,
  }
}

const stubs = { FeatureGate: { template: '<div><slot /></div>' } }

async function flush() {
  await flushPromises()
  await nextTick()
}

describe('EvalEditorView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    for (const key of Object.keys(routerMocks.query)) delete routerMocks.query[key]
    getAccessTokenMock.mockReturnValue(fakeJwt('admin'))
    apiGET.mockResolvedValue({ data: { items: [] } })
    apiPOST.mockResolvedValue({ data: {} })
    apiPUT.mockResolvedValue({ data: {} })
    apiDELETE.mockResolvedValue({ data: {} })
  })

  it('renders without crashing', async () => {
    const wrapper = mount(EvalEditorView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('views.EvalEditorView.eval_editor')
  })

  it('renders the three eval-surface tabs, costs-style (FAR-2597)', async () => {
    const wrapper = mount(EvalEditorView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="eval-editor-pipeline"]').exists()).toBe(true)
    })
    // Route-per-tab navigation, not query-state tabs: three destinations that
    // the shared PageTabs renders and highlights.
    const tabs = wrapper.findComponent(PageTabs)
    expect(tabs.exists()).toBe(true)
    expect(tabs.props('tabs')).toEqual([
      { label: 'views.EvalsListView.tabs_all', to: '/evals' },
      { label: 'views.EvalsListView.tabs_editor', to: '/evals/editor' },
      { label: 'views.EvalsListView.tabs_proposals', to: '/evals/proposals' },
    ])
  })

  it('renders a per-type config placeholder on the deterministic JSON textarea', async () => {
    const wrapper = mount(EvalEditorView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    // The typed mechanism renders the builder; the JSON textarea is the
    // deterministic sub-axis (ADR 066 D19), so switch the mechanism first.
    ;(wrapper.vm as any).form.eval_type = 'regex'
    await nextTick()
    const textarea = await vi.waitFor(() => {
      const el = wrapper.find('textarea[data-testid="eval-editor-config"]')
      expect(el.exists()).toBe(true)
      return el
    })
    expect(textarea.attributes('placeholder')).toBe('views.EvalEditorView.configPlaceholder.regex')

    ;(wrapper.vm as any).form.eval_type = 'json_schema'
    await nextTick()
    expect(textarea.attributes('placeholder')).toBe('views.EvalEditorView.configPlaceholder.json_schema')

    ;(wrapper.vm as any).form.eval_type = 'custom_function'
    await nextTick()
    expect(textarea.attributes('placeholder')).toBe('views.EvalEditorView.configPlaceholder.custom_function')

    // An empty type must fall back to the model_judge placeholder, not render
    // a dangling `configPlaceholder.` key.
    ;(wrapper.vm as any).form.eval_type = ''
    await nextTick()
    expect(textarea.attributes('placeholder')).toBe('views.EvalEditorView.configPlaceholder.model_judge')
  })
})

describe('EvalEditorView — FAR-617 pipeline/node/eval CRUD coverage', () => {
  const viewStubs = {
    ...stubs,
    LoadingSpinner: true,
    ErrorAlert: true,
    PageHeader: { template: '<div />' },
  }

  function mountView() {
    return mount(EvalEditorView, {
      global: { stubs: viewStubs, mocks: { $t: (key: string) => key } },
    })
  }

  function selectPipeline(wrapper: Awaited<ReturnType<typeof mountView>>, pipelineId: string) {
    ;(wrapper.vm as unknown as { selectedPipelineId: string }).selectedPipelineId = pipelineId
    return (wrapper.vm as unknown as { onPipelineChange: () => Promise<void> }).onPipelineChange()
  }

  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    for (const key of Object.keys(routerMocks.query)) delete routerMocks.query[key]
    getAccessTokenMock.mockReturnValue(fakeJwt('admin'))
    apiGET.mockResolvedValue({ data: { items: [] } })
    apiPOST.mockResolvedValue({ data: {} })
    apiPUT.mockResolvedValue({ data: {} })
    apiDELETE.mockResolvedValue({ data: {} })
  })

  it('initial load fetches pipelines; save is disabled with no pipeline/name chosen', async () => {
    apiGET.mockResolvedValue({ data: { items: [{ id: 'p1', name: 'P One', description: null }] } })
    const wrapper = mountView()
    await flush()

    expect(apiGET).toHaveBeenCalledWith('/api/v1/pipelines', {
      params: { query: { page_size: 100 } },
    })
    const save = wrapper.find('[data-testid="eval-editor-save"]')
    expect(save.attributes('disabled')).toBeDefined()

    // The select-prompt branch only renders when selectedPipelineId is cleared
    // entirely (it initialises to '__all__', so it is never shown on a fresh mount).
    ;(wrapper.vm as unknown as { selectedPipelineId: string }).selectedPipelineId = ''
    await nextTick()
    expect(wrapper.text()).toContain('views.EvalEditorView.prompt_select_pipeline')
  })

  it('selecting a pipeline loads its graph nodes and evals', async () => {
    apiGET.mockImplementation(async (url: string) => {
      if (url === '/api/v1/pipelines') return { data: { items: [{ id: 'p1', name: 'P One', description: null }] } }
      if (url === '/api/v1/pipelines/{pipeline_id}/graph') {
        return { data: { nodes: [{ id: 'n1', node_type: 'agent', label: 'Writer', agent_id: 'a1', position: { x: 0, y: 0 } }] } }
      }
      if (url === '/api/v1/evals') return { data: { items: [evalItem()] } }
      return { data: { items: [] } }
    })
    const wrapper = mountView()
    await flush()

    await selectPipeline(wrapper, 'p1')
    await flush()

    expect(apiGET).toHaveBeenCalledWith('/api/v1/pipelines/{pipeline_id}/graph', {
      params: { path: { pipeline_id: 'p1' } },
    })
    expect(apiGET).toHaveBeenCalledWith('/api/v1/evals', { params: { query: { pipeline_id: 'p1' } } })
    expect(wrapper.text()).toContain('Existing Eval')
    expect(wrapper.text()).toContain('regex')
    // FAR-2768: the existing-evals list sits on the page surface under its own
    // heading - the eval rows are the only cards, never nested inside an outer one.
    const evalsHeading = wrapper
      .findAll('h2')
      .find((h) => h.text() === 'views.EvalEditorView.existing_evals')
    expect(evalsHeading).toBeDefined()
    expect(evalsHeading!.element.closest('.card')).toBeNull()
  })

  it('falls back to empty collections when pipelines, graph and evals omit data', async () => {
    apiGET.mockImplementation(async () => ({ data: undefined }))
    const wrapper = mountView()
    await flush()

    await selectPipeline(wrapper, 'p1')
    await flush()

    expect((wrapper.vm as unknown as { pipelines: unknown[] }).pipelines).toEqual([])
    expect((wrapper.vm as unknown as { nodes: unknown[] }).nodes).toEqual([])
    expect((wrapper.vm as unknown as { evals: unknown[] }).evals).toEqual([])
  })

  it('graph load failure shows the nodes error without crashing the page', async () => {
    apiGET.mockImplementation(async (url: string) => {
      if (url === '/api/v1/pipelines/{pipeline_id}/graph') throw new Error('graph down')
      return { data: { items: [] } }
    })
    const wrapper = mountView()
    await flush()

    await selectPipeline(wrapper, 'p1')
    await flush()

    // UX conformance STATE-2: API failures render ErrorAlert with a Retry button.
    const nodesAlert = wrapper
      .findAllComponents(ErrorAlert)
      .find((a) => a.props('message') === 'views.EvalEditorView.failed_to_load_nodes')
    expect(nodesAlert?.exists()).toBe(true)
    // Retry reloads the graph.
    apiGET.mockResolvedValue({ data: { nodes: [] } })
    await (nodesAlert!.props('onRetry') as () => Promise<void>)()
    await flush()
    expect(apiGET).toHaveBeenCalledWith('/api/v1/pipelines/{pipeline_id}/graph', {
      params: { path: { pipeline_id: 'p1' } },
    })
  })

  it('evals load failure shows the evals error', async () => {
    apiGET.mockImplementation(async (url: string) => {
      if (url === '/api/v1/evals') throw new Error('evals down')
      return { data: { items: [] } }
    })
    const wrapper = mountView()
    await flush()

    await selectPipeline(wrapper, 'p1')
    await flush()

    // UX conformance STATE-2: API failures render ErrorAlert with a Retry button.
    const evalsAlert = wrapper
      .findAllComponents(ErrorAlert)
      .find((a) => a.props('message') === 'views.EvalEditorView.failed_to_load_evals')
    expect(evalsAlert?.exists()).toBe(true)
    // Retry reloads the evals.
    apiGET.mockResolvedValue({ data: { items: [] } })
    await (evalsAlert!.props('onRetry') as () => Promise<void>)()
    await flush()
    expect(apiGET).toHaveBeenCalledWith('/api/v1/evals', {
      params: { query: { pipeline_id: 'p1' } },
    })
  })

  it('defaults a new model_judge config to a valid typed dialect', async () => {
    const wrapper = mount(EvalEditorView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })

    const vm = wrapper.vm as unknown as {
      form: { eval_type: string }
      buildConfigForSave: () => Record<string, unknown>
    }
    expect(vm.form.eval_type).toBe('model_judge')
    const config = vm.buildConfigForSave()
    // ADR 066 D8: a default create must satisfy the real write validator.
    expect(config.answer_contract).toBe('typed_questions')
    expect(config.field).toBe('output')
    expect(config.backing).toEqual({ kind: 'model_backend' })
    expect((config.questions as Record<string, unknown>[])[0].type).toBe('noul')
    expect(config.pass_rule).toEqual({ kind: 'weighted_score_at_least', threshold: 0.5 })
    expect(config.verdict_kind).toBe('assessment')
    // The config's cut is pass_rule.threshold, never Eval.pass_threshold.
    expect(config).not.toHaveProperty('pass_threshold')
  })

  it('save (create): POST body carries the parsed config, threshold; success message shown', async () => {
    const wrapper = mountView()
    await flush()

    await selectPipeline(wrapper, 'p1')
    await flush()

    await wrapper.find('[data-testid="eval-editor-name"]').setValue('Fresh Eval')
    const typedConfig = (
      wrapper.vm as unknown as { buildConfigForSave: () => Record<string, unknown> }
    ).buildConfigForSave()
    await wrapper.find('[data-testid="eval-editor-pass-threshold"]').setValue('0.55')
    await nextTick()

    const save = wrapper.find('[data-testid="eval-editor-save"]')
    expect(save.attributes('disabled')).toBeUndefined()
    await save.trigger('click')
    await flush()

    expect(apiPOST).toHaveBeenCalledTimes(1)
    const [url, options] = apiPOST.mock.calls[0]
    expect(url).toBe('/api/v1/evals')
    expect(options.body).toEqual({
      pipeline_id: 'p1',
      node_id: null,
      name: 'Fresh Eval',
      eval_type: 'model_judge',
      config_json: typedConfig,
      pass_threshold: 0.55,
    })
    // FAR-631: the success flash now survives the form reset (reset runs
    // first, then the message is set), so the created message renders.
    expect((wrapper.vm as unknown as { formSuccess: string | null }).formSuccess).toBe(
      'views.EvalEditorView.eval_created',
    )
    expect(wrapper.text()).toContain('views.EvalEditorView.eval_created')
  })

  it('save with a specific node selected maps node_id into the body', async () => {
    apiGET.mockImplementation(async (url: string) => {
      if (url === '/api/v1/pipelines/{pipeline_id}/graph') {
        return { data: { nodes: [{ id: 'n1', node_type: 'agent', label: 'Writer', agent_id: 'a1', position: { x: 0, y: 0 } }] } }
      }
      return { data: { items: [] } }
    })
    const wrapper = mountView()
    await flush()

    await selectPipeline(wrapper, 'p1')
    ;(wrapper.vm as unknown as { form: { node_id: string } }).form.node_id = 'n1'
    await nextTick()
    await wrapper.find('[data-testid="eval-editor-name"]').setValue('Node Eval')
    await nextTick()

    await wrapper.find('[data-testid="eval-editor-save"]').trigger('click')
    await flush()

    expect(apiPOST).toHaveBeenCalledTimes(1)
    expect(apiPOST.mock.calls[0][1].body.node_id).toBe('n1')
  })

  it('invalid config JSON disables save and renders the parse error', async () => {
    const wrapper = mountView()
    await flush()

    await selectPipeline(wrapper, 'p1')
    await wrapper.find('[data-testid="eval-editor-name"]').setValue('Broken')
    // The JSON textarea belongs to the deterministic mechanism (ADR 066 D19).
    ;(wrapper.vm as unknown as { onMechanismChange: (value: string) => void }).onMechanismChange(
      'deterministic',
    )
    await nextTick()
    await wrapper.find('textarea[data-testid="eval-editor-config"]').setValue('{not json')
    await nextTick()

    expect(wrapper.find('[data-testid="eval-editor-save"]').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('views.EvalEditorView.invalid_json')
    expect(apiPOST).not.toHaveBeenCalled()
  })

  it('edit flow: startEdit prefills the form, PUT carries the eval id, cancel resets', async () => {
    apiGET.mockImplementation(async (url: string) => {
      if (url === '/api/v1/evals') return { data: { items: [evalItem()] } }
      return { data: { items: [] } }
    })
    const wrapper = mountView()
    await flush()

    await selectPipeline(wrapper, 'p1')
    await flush()

    await wrapper.find('[data-testid="eval-editor-edit"]').trigger('click')
    await nextTick()
    expect((wrapper.find('[data-testid="eval-editor-name"]').element as HTMLInputElement).value).toBe('Existing Eval')
    expect((wrapper.find('[data-testid="eval-editor-config"]').element as HTMLTextAreaElement).value).toContain('"pattern"')
    expect(wrapper.text()).toContain('views.EvalEditorView.edit_eval')

    await wrapper.find('[data-testid="eval-editor-save"]').trigger('click')
    await flush()

    expect(apiPUT).toHaveBeenCalledTimes(1)
    const [url, options] = apiPUT.mock.calls[0]
    expect(url).toBe('/api/v1/evals/{eval_id}')
    expect(options.params.path.eval_id).toBe('eval-1')
    expect(options.body.pass_threshold).toBe(0.9)
    // FAR-631: the updated message now survives the reset and renders (see
    // the create-path note above).
    expect((wrapper.vm as unknown as { formSuccess: string | null }).formSuccess).toBe(
      'views.EvalEditorView.eval_updated',
    )
    expect(wrapper.text()).toContain('views.EvalEditorView.eval_updated')

    // After a successful save the form is reset and editing state cleared.
    expect((wrapper.find('[data-testid="eval-editor-name"]').element as HTMLInputElement).value).toBe('')

    // Cancel button only exists in edit mode; drive resetForm through a fresh edit.
    await wrapper.find('[data-testid="eval-editor-edit"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="eval-editor-cancel"]').trigger('click')
    await nextTick()
    expect((wrapper.find('[data-testid="eval-editor-name"]').element as HTMLInputElement).value).toBe('')
    expect(wrapper.find('[data-testid="eval-editor-cancel"]').exists()).toBe(false)
  })

  it('delete flow: confirm deletes via the API and removes the eval from the list', async () => {
    apiGET.mockImplementation(async (url: string) => {
      if (url === PERMISSIONS_URL) return permissionsOk([EVAL_DELETE_PERMISSION])
      if (url === '/api/v1/evals') return { data: { items: [evalItem()] } }
      return { data: { items: [] } }
    })
    const wrapper = mountView()
    await flush()

    await selectPipeline(wrapper, 'p1')
    await flush()

    await wrapper.find('[data-testid="eval-editor-delete"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="eval-editor-confirm-delete"]').exists()).toBe(true)

    // Cancel first: no DELETE call, back to the single delete button.
    await wrapper.find('[data-testid="eval-editor-cancel-delete"]').trigger('click')
    await nextTick()
    expect(apiDELETE).not.toHaveBeenCalled()

    await wrapper.find('[data-testid="eval-editor-delete"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="eval-editor-confirm-delete"]').trigger('click')
    await flush()

    expect(apiDELETE).toHaveBeenCalledTimes(1)
    const [url, options] = apiDELETE.mock.calls[0]
    expect(url).toBe('/api/v1/evals/{eval_id}')
    expect(options.params.path.eval_id).toBe('eval-1')
    expect(wrapper.text()).toContain('views.EvalEditorView.no_evals_yet')
  })

  it('delete failure with a 404 payload surfaces the already-deleted message', async () => {
    apiGET.mockImplementation(async (url: string) => {
      if (url === PERMISSIONS_URL) return permissionsOk([EVAL_DELETE_PERMISSION])
      if (url === '/api/v1/evals') return { data: { items: [evalItem()] } }
      return { data: { items: [] } }
    })
    apiDELETE.mockRejectedValue(new Error('404 not found'))
    const wrapper = mountView()
    await flush()

    await selectPipeline(wrapper, 'p1')
    await flush()

    await wrapper.find('[data-testid="eval-editor-delete"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="eval-editor-confirm-delete"]').trigger('click')
    await flush()

    expect(wrapper.text()).toContain('views.EvalEditorView.eval_already_deleted')
  })

  it('empty evals list renders the EmptyState after a pipeline is selected', async () => {
    const wrapper = mountView()
    await flush()

    await selectPipeline(wrapper, 'p1')
    await flush()

    expect(wrapper.text()).toContain('views.EvalEditorView.no_evals_yet')
  })

  it('ignores non-string values from the mechanism and type model-value events', async () => {
    const wrapper = mountView()
    await flush()

    const vm = wrapper.vm as unknown as {
      onMechanismChange: (value: unknown) => void
      onDeterministicTypeChange: (value: unknown) => void
      setQuestionType: (question: { type: string }, value: unknown) => void
    }

    expect(() => vm.onMechanismChange(42)).not.toThrow()
    expect(() => vm.onDeterministicTypeChange(42)).not.toThrow()

    vm.onDeterministicTypeChange('not-a-type')
    const question = { type: 'noul' }
    vm.setQuestionType(question, 'not-a-type')
    vm.setQuestionType(question, 42)
    expect(question.type).toBe('noul')
  })

  // FAR-2620: DELETE /api/v1/evals/{eval_id} needs BOTH the
  // `eval.definition.delete` permission AND an admin org role. The editor
  // route itself only requires create/update (operator), so the delete
  // affordance must not inherit the editor's audience.
  function far2620MountView(orgRole: string, permissions: string[]) {
    getAccessTokenMock.mockReturnValue(fakeJwt(orgRole))
    apiGET.mockImplementation(async (url: string) => {
      if (url === PERMISSIONS_URL) return permissionsOk(permissions)
      if (url === '/api/v1/evals') return { data: { items: [evalItem()] } }
      return { data: { items: [] } }
    })
    return mountView()
  }

  async function mountWithRow(orgRole: string, permissions: string[]) {
    const wrapper = far2620MountView(orgRole, permissions)
    await flush()
    await selectPipeline(wrapper, 'p1')
    await flush()
    return wrapper
  }

  it('shows the eval delete affordance for an admin the endpoint accepts (FAR-2620)', async () => {
    const wrapper = await mountWithRow('admin', [EVAL_DELETE_PERMISSION])

    // The row rendered (its edit action proves it), and the delete action is
    // offered because the caller satisfies both endpoint gates.
    expect(wrapper.find('[data-testid="eval-editor-edit"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="eval-editor-delete"]').exists()).toBe(true)
  })

  it('hides the eval delete affordance for an operator the endpoint refuses (FAR-2620)', async () => {
    // An operator holds `eval.definition.delete` (it is an operator-floor
    // permission) and reaches the editor, but the endpoint's in-body
    // `org_role != "admin"` check answers 403 — so the action must be absent,
    // not a button that fails on click.
    const wrapper = await mountWithRow('operator', [EVAL_DELETE_PERMISSION])

    expect(wrapper.find('[data-testid="eval-editor-edit"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="eval-editor-delete"]').exists()).toBe(false)
  })

  it('hides the eval delete affordance from an admin lacking the delete permission (FAR-2620)', async () => {
    // The route-permission half, mirrored: a restricted (scoped) credential can
    // carry an admin org role yet not hold `eval.definition.delete`, and the
    // endpoint refuses that caller at the permission gate. Fail closed.
    const wrapper = await mountWithRow('admin', ['eval.definition.update'])

    expect(wrapper.find('[data-testid="eval-editor-edit"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="eval-editor-delete"]').exists()).toBe(false)
  })
})

describe('EvalEditorView — ?pipeline_id= deep link (FAR-2597)', () => {
  function mountView() {
    return mount(EvalEditorView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
  }

  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    for (const key of Object.keys(routerMocks.query)) delete routerMocks.query[key]
    apiPOST.mockResolvedValue({ data: {} })
    apiPUT.mockResolvedValue({ data: {} })
    apiDELETE.mockResolvedValue({ data: {} })
    apiGET.mockImplementation(async (url: string) => {
      if (url === '/api/v1/pipelines') {
        return { data: { items: [{ id: 'p1', name: 'Ingest', description: null }] } }
      }
      if (url === '/api/v1/pipelines/{pipeline_id}/graph') {
        return { data: { nodes: [{ id: 'n1', node_type: 'agent', label: 'Writer', agent_id: null, position: { x: 0, y: 0 } }] } }
      }
      if (url === '/api/v1/evals') return { data: { items: [evalItem()], total: 1 } }
      if (url === '/api/v1/evals/{eval_id}') return { data: undefined, error: { detail: 'Eval not found' } }
      return { data: { items: [] } }
    })
  })

  it('selects the linked pipeline and loads its nodes and evals', async () => {
    routerMocks.query.pipeline_id = 'p1'
    const wrapper = mountView()
    await flush()

    expect((wrapper.vm as unknown as { selectedPipelineId: string }).selectedPipelineId).toBe('p1')
    expect(apiGET).toHaveBeenCalledWith('/api/v1/evals', { params: { query: { pipeline_id: 'p1' } } })
    expect(wrapper.find('[data-testid="eval-editor-pipeline-link-notice"]').exists()).toBe(false)
    // A resolved link is already in the URL: nothing to rewrite on mount.
    expect(routerMocks.replace).not.toHaveBeenCalled()
  })

  it('falls back to all pipelines with a visible notice for an unknown id', async () => {
    routerMocks.query.pipeline_id = 'deleted-pipeline'
    const wrapper = mountView()
    await flush()

    expect((wrapper.vm as unknown as { selectedPipelineId: string }).selectedPipelineId).toBe('__all__')
    const notice = wrapper.find('[data-testid="eval-editor-pipeline-link-notice"]')
    expect(notice.exists()).toBe(true)
    expect(notice.attributes('role')).toBe('status')
    expect(wrapper.find('[data-testid="eval-editor-pipeline"]').exists()).toBe(true)
    // The stale id is dropped so a reload does not repeat the notice.
    const replacedQuery = (routerMocks.replace.mock.calls.at(-1)?.[0] as { query: Record<string, unknown> }).query
    expect(replacedQuery.pipeline_id).toBeUndefined()
  })

  it('keeps the URL in sync when the pipeline selector changes', async () => {
    const wrapper = mountView()
    await flush()
    const vm = wrapper.vm as unknown as {
      selectedPipelineId: string
      onPipelineChange: () => Promise<void>
    }

    vm.selectedPipelineId = 'p1'
    await vm.onPipelineChange()
    await flush()
    expect((routerMocks.replace.mock.calls.at(-1)?.[0] as { query: Record<string, unknown> }).query.pipeline_id).toBe('p1')

    vm.selectedPipelineId = '__all__'
    await vm.onPipelineChange()
    await flush()
    expect((routerMocks.replace.mock.calls.at(-1)?.[0] as { query: Record<string, unknown> }).query.pipeline_id).toBeUndefined()
  })

  it('reads the first value of an array query param and can write an eval id back to the URL', async () => {
    // A repeated ?pipeline_id= arrives as an array; the first string value wins.
    routerMocks.query.pipeline_id = ['p1']
    const wrapper = mountView()
    await flush()

    const vm = wrapper.vm as unknown as {
      selectedPipelineId: string
      replacePipelineQuery: (pipelineId?: string, evalId?: string) => void
    }
    expect(vm.selectedPipelineId).toBe('p1')

    vm.replacePipelineQuery('p1', 'eval-1')
    const replaced = (routerMocks.replace.mock.calls.at(-1)?.[0] as { query: Record<string, unknown> }).query
    expect(replaced.eval_id).toBe('eval-1')
    wrapper.unmount()
  })

  it('clears the stale-link notice when the pipeline selection changes again', async () => {
    routerMocks.query.pipeline_id = 'deleted-pipeline'
    const wrapper = mountView()
    await flush()
    expect(wrapper.find('[data-testid="eval-editor-pipeline-link-notice"]').exists()).toBe(true)

    const vm = wrapper.vm as unknown as { onPipelineChange: () => Promise<void> }
    await vm.onPipelineChange()
    await flush()

    expect(wrapper.find('[data-testid="eval-editor-pipeline-link-notice"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('preselects the eval named by ?eval_id= once its pipeline has loaded', async () => {
    routerMocks.query.pipeline_id = 'p1'
    routerMocks.query.eval_id = 'eval-1'
    const wrapper = mountView()
    await flush()

    const vm = wrapper.vm as unknown as { editingEvalId: string | null; form: { name: string } }
    expect(vm.editingEvalId).toBe('eval-1')
    expect(vm.form.name).toBe('Existing Eval')
  })

  it('preselects an eval outside the paged list by reading it directly', async () => {
    // The pipeline list endpoint pages at 20 by default, so a pipeline with 21+
    // evals returns a first page that does not contain the linked eval. The deep
    // link must still open it rather than silently drop the selection.
    const pagedEvals = Array.from({ length: 21 }, (_, i) =>
      evalItem({ id: `eval-page-${i}`, name: `Paged eval ${i}` }),
    )
    apiGET.mockImplementation(async (url: string) => {
      if (url === '/api/v1/pipelines') {
        return { data: { items: [{ id: 'p1', name: 'Ingest', description: null }] } }
      }
      if (url === '/api/v1/pipelines/{pipeline_id}/graph') return { data: { nodes: [] } }
      if (url === '/api/v1/evals') return { data: { items: pagedEvals, total: 21 } }
      if (url === '/api/v1/evals/{eval_id}') {
        return { data: evalItem({ id: 'eval-off-page', name: 'Off-page eval' }) }
      }
      return { data: { items: [] } }
    })
    routerMocks.query.pipeline_id = 'p1'
    routerMocks.query.eval_id = 'eval-off-page'
    const wrapper = mountView()
    await flush()

    expect(apiGET).toHaveBeenCalledWith('/api/v1/evals/{eval_id}', {
      params: { path: { eval_id: 'eval-off-page' } },
    })
    const vm = wrapper.vm as unknown as { editingEvalId: string | null; form: { name: string } }
    expect(vm.editingEvalId).toBe('eval-off-page')
    expect(vm.form.name).toBe('Off-page eval')
  })

  it('does not preselect a directly-fetched eval that belongs to another pipeline', async () => {
    apiGET.mockImplementation(async (url: string) => {
      if (url === '/api/v1/pipelines') {
        return { data: { items: [{ id: 'p1', name: 'Ingest', description: null }] } }
      }
      if (url === '/api/v1/pipelines/{pipeline_id}/graph') return { data: { nodes: [] } }
      if (url === '/api/v1/evals') return { data: { items: [], total: 0 } }
      if (url === '/api/v1/evals/{eval_id}') {
        return { data: evalItem({ id: 'eval-other', pipeline_id: 'p2' }) }
      }
      return { data: { items: [] } }
    })
    routerMocks.query.pipeline_id = 'p1'
    routerMocks.query.eval_id = 'eval-other'
    const wrapper = mountView()
    await flush()

    const vm = wrapper.vm as unknown as { editingEvalId: string | null; selectedPipelineId: string }
    expect(vm.selectedPipelineId).toBe('p1')
    expect(vm.editingEvalId).toBeNull()
  })

  it('ignores an eval id that is no longer in the pipeline, keeping the pipeline open', async () => {
    routerMocks.query.pipeline_id = 'p1'
    routerMocks.query.eval_id = 'eval-deleted'
    const wrapper = mountView()
    await flush()

    const vm = wrapper.vm as unknown as { editingEvalId: string | null; selectedPipelineId: string }
    expect(vm.selectedPipelineId).toBe('p1')
    expect(vm.editingEvalId).toBeNull()
    expect(wrapper.find('[data-testid="eval-editor-pipeline-link-notice"]').exists()).toBe(false)
  })

  it('treats a failed pipeline read as a load error, not as a missing pipeline', async () => {
    routerMocks.query.pipeline_id = 'p1'
    let pipelineAttempts = 0
    apiGET.mockImplementation(async (url: string) => {
      if (url === '/api/v1/pipelines') {
        pipelineAttempts += 1
        if (pipelineAttempts === 1) return { data: undefined, error: { detail: 'pipelines unavailable' } }
        return { data: { items: [{ id: 'p1', name: 'Ingest', description: null }] } }
      }
      if (url === '/api/v1/pipelines/{pipeline_id}/graph') return { data: { nodes: [] } }
      if (url === '/api/v1/evals') return { data: { items: [evalItem()], total: 1 } }
      return { data: { items: [] } }
    })
    const wrapper = mountView()
    await flush()

    // A failed read is surfaced as a retryable load error…
    expect(wrapper.find('[data-testid="error-alert-retry"]').exists()).toBe(true)
    // …never as "this pipeline does not exist".
    expect(wrapper.find('[data-testid="eval-editor-pipeline-link-notice"]').exists()).toBe(false)

    await wrapper.find('[data-testid="error-alert-retry"]').trigger('click')
    await flush()

    const vm = wrapper.vm as unknown as { selectedPipelineId: string }
    expect(vm.selectedPipelineId).toBe('p1')
    expect(apiGET).toHaveBeenCalledWith('/api/v1/evals', { params: { query: { pipeline_id: 'p1' } } })
  })
})
