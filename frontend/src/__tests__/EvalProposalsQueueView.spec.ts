import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const { sampleResponse, mockGet, mockPatch, mockPost, loadingState, mockPlanStore } = vi.hoisted(() => {
  const makeItem = (id: string, feedback_status: string, producing_node_id: string) => ({
    id,
    run_id: 'run-1',
    review_id: 'review',
    rejected_by: null,
    rejection_reason: '',
    rejected_output: {},
    producing_node_id,
    producing_node_name: 'Critic',
    producing_agent_id: null,
    feedback_status,
    feedback_handler_type: 'ai_correction',
    correction_run_id: null,
    eval_gap: false,
    needs_human_review: true,
    pipeline_name: 'Triage',
    created_at: '2026-01-01T00:00:00Z',
  })
  return {
    sampleResponse: {
      items: [
        makeItem('rec-1', 'pending', 'node-1'),
        makeItem('rec-2', 'pending', 'node-2'),
        makeItem('rec-3', 'pending', '11111111-1111-1111-1111-111111111111'),
      ],
      total: 3,
      page: 1,
      page_size: 10,
    },
    mockGet: vi.fn(),
    mockPatch: vi.fn(),
    mockPost: vi.fn(),
    loadingState: { value: false },
    // FAR-2621: the view derives each action's capability from the caller-scoped
    // permission list (the same registry the endpoints enforce). Every existing
    // case below runs as an operator holding all three action permissions; the
    // permission-shape cases override this list.
    mockPlanStore: {
      userPermissions: ['feedback.review', 'gate.create', 'feedback.update'] as string[],
      fetchPermissions: vi.fn(async () => {}),
    },
  }
})

vi.mock('../lib/api/client', () => ({
  api: {
    GET: mockGet,
    PATCH: mockPatch,
    POST: mockPost,
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

vi.mock('vue-i18n', () => ({
  useI18n: () => ({ t: (key: string) => key }),
}))

vi.mock('../stores/planStore', () => ({ usePlanStore: () => mockPlanStore }))

// Mirror AdminUsersView.spec.ts: the writable `proposalsResp` computed is driven
// by useDataFetch's load() so the seeded proposal list is visible to the view.
vi.mock('../composables/useDataFetch', async () => {
  const { ref } = await import('vue')
  return {
    useDataFetch: (
      fetcher: () => Promise<{ data: unknown; error?: unknown }>,
      options?: { initialValue?: unknown },
    ) => {
      const data = ref(options?.initialValue)
      const load = async () => {
        const result = await fetcher()
        ;(data as { value: unknown }).value = result.data ?? options?.initialValue
      }
      void load()
      return { data, loading: ref(loadingState.value), error: ref(''), load }
    },
  }
})

import EvalProposalsQueueView from '../views/EvalProposalsQueueView.vue'
import PageTabs from '../components/PageTabs.vue'

// The PrimeVue Dialog portals to <body>; this stub renders the default and
// footer slots inline so the publish form and its buttons are DOM-reachable.
const dialogStub = { template: '<div><slot /><slot name="footer" /></div>' }

function mountView() {
  return mount(EvalProposalsQueueView, {
    global: {
      stubs: {
        FeatureGate: { template: '<div><slot /></div>' },
        Dialog: dialogStub,
      },
      mocks: { $t: (key: string) => key },
    },
  })
}

async function flush() {
  await flushPromises()
  await nextTick()
}

describe('EvalProposalsQueueView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    loadingState.value = false
    mockPlanStore.userPermissions = ['feedback.review', 'gate.create', 'feedback.update']
    mockGet.mockResolvedValue({ data: sampleResponse, error: undefined })
    mockPatch.mockResolvedValue({ data: {}, error: undefined })
    mockPost.mockResolvedValue({ data: {}, error: undefined })
  })

  it('renders without crashing', async () => {
    const wrapper = mountView()
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('views.EvalProposalsQueueView.title')
  })

  it('renders the three eval-surface tabs, costs-style (FAR-2597)', async () => {
    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="proposal-card-rec-1"]').exists()).toBe(true)
    })
    const tabs = wrapper.findComponent(PageTabs)
    expect(tabs.exists()).toBe(true)
    expect(tabs.props('tabs')).toEqual([
      { label: 'views.EvalsListView.tabs_all', to: '/evals' },
      { label: 'views.EvalsListView.tabs_editor', to: '/evals/editor' },
      { label: 'views.EvalsListView.tabs_proposals', to: '/evals/proposals' },
    ])
  })

  const typedJudgeConfig = {
    field: 'output',
    answer_contract: 'typed_questions',
    backing: { kind: 'model_backend' },
    questions: [
      {
        id: 'verdict',
        type: 'noul',
        instructions: 'Is the output acceptable?',
        criteria: [],
        weight: 1,
        required: true,
        min_pass_confidence: 0.5,
      },
    ],
    pass_rule: { kind: 'weighted_score_at_least', threshold: 0.5 },
    verdict_kind: 'assessment',
  }

  it('seeds a plain JSON config for a non-judge eval type', async () => {
    const wrapper = mountView()
    await flush()
    const vm = wrapper.vm as unknown as { defaultConfigFor: (type: string) => string }

    expect(vm.defaultConfigFor('regex')).toBe('{}')
    expect(vm.defaultConfigFor('model_judge')).toContain('typed_questions')
  })

  it('POSTs the publish endpoint with snake_case body on submit', async () => {
    const wrapper = mountView()
    await flush()

    await wrapper.find('[data-testid="proposal-publish"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="publish-name"]').exists()).toBe(true)

    await wrapper.find('[data-testid="publish-name"]').setValue('Response quality check')
    await wrapper.find('[data-testid="publish-config"]').setValue(JSON.stringify(typedJudgeConfig))
    await wrapper.find('[data-testid="publish-confirm"]').trigger('click')
    await flush()

    // Body fields are snake_case (PublishEvalProposalRequest). node_id is
    // omitted because 'node-1' is not a UUID — the backend resolves it.
    expect(mockPost).toHaveBeenCalledWith('/api/v1/feedback/proposals/{record_id}/publish', {
      params: { path: { record_id: 'rec-1' } },
      body: {
        name: 'Response quality check',
        eval_type: 'model_judge',
        config: typedJudgeConfig,
      },
    })
  })

  it('includes node_id when the producing node id is a UUID', async () => {
    const wrapper = mountView()
    await flush()

    const publishButtons = wrapper.findAll('[data-testid="proposal-publish"]')
    await publishButtons[2].trigger('click')
    await nextTick()

    await wrapper.find('[data-testid="publish-name"]').setValue('Schema check')
    // The dialog now seeds a VALID typed model_judge config by default.
    const seeded = JSON.parse(
      (wrapper.vm as unknown as { publishForm: { config_json: string } }).publishForm.config_json,
    )
    expect(seeded.answer_contract).toBe('typed_questions')
    await wrapper.find('[data-testid="publish-confirm"]').trigger('click')
    await flush()

    expect(mockPost).toHaveBeenCalledWith('/api/v1/feedback/proposals/{record_id}/publish', {
      params: { path: { record_id: 'rec-3' } },
      body: {
        name: 'Schema check',
        eval_type: 'model_judge',
        config: seeded,
        node_id: '11111111-1111-1111-1111-111111111111',
      },
    })
  })

  it('refreshes the list, shows success, and closes the dialog on publish', async () => {
    const wrapper = mountView()
    await flush()
    const getCallsBefore = mockGet.mock.calls.length

    await wrapper.find('[data-testid="proposal-publish"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="publish-name"]').setValue('X')
    await wrapper.find('[data-testid="publish-confirm"]').trigger('click')
    await flush()

    expect(mockGet.mock.calls.length).toBeGreaterThan(getCallsBefore)
    expect(wrapper.text()).toContain('views.EvalProposalsQueueView.publish_success')
    expect(wrapper.find('[data-testid="publish-name"]').exists()).toBe(false)
  })

  it('blocks submit and shows an error for invalid config JSON', async () => {
    const wrapper = mountView()
    await flush()

    await wrapper.find('[data-testid="proposal-publish"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="publish-name"]').setValue('X')
    await wrapper.find('[data-testid="publish-config"]').setValue('{ not json')
    await nextTick()

    expect(wrapper.text()).toContain('views.EvalProposalsQueueView.config_invalid')
    const confirm = wrapper.find('[data-testid="publish-confirm"]')
    expect(confirm.attributes('disabled')).toBeDefined()
    expect(mockPost).not.toHaveBeenCalled()
  })

  it('publishes a proposal with a valid typed judge config', async () => {
    const typedConfig = {
      field: 'output',
      answer_contract: 'typed_questions',
      backing: { kind: 'model_backend' },
      questions: [
        {
          id: 'verdict',
          type: 'noul',
          instructions: 'Is the output acceptable?',
          criteria: [],
          weight: 1,
          required: true,
          min_pass_confidence: 0.5,
        },
      ],
      pass_rule: { kind: 'weighted_score_at_least', threshold: 0.5 },
      verdict_kind: 'assessment',
    }
    const wrapper = mountView()
    await flush()

    await wrapper.find('[data-testid="proposal-publish"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="publish-name"]').setValue('Typed judge')
    await wrapper.find('[data-testid="publish-config"]').setValue(JSON.stringify(typedConfig))
    await nextTick()

    expect(wrapper.text()).not.toContain('views.EvalProposalsQueueView.config_invalid')
    await wrapper.find('[data-testid="publish-confirm"]').trigger('click')
    await flush()

    expect(mockPost).toHaveBeenCalledWith('/api/v1/feedback/proposals/{record_id}/publish', {
      params: { path: { record_id: 'rec-1' } },
      body: { name: 'Typed judge', eval_type: 'model_judge', config: typedConfig },
    })
  })

  it('rejects a config that parses to a non-object', async () => {
    const wrapper = mountView()
    await flush()

    await wrapper.find('[data-testid="proposal-publish"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="publish-name"]').setValue('X')

    // A JSON primitive is not an object.
    await wrapper.find('[data-testid="publish-config"]').setValue('42')
    await nextTick()
    expect(wrapper.text()).toContain('views.EvalProposalsQueueView.config_must_be_object')
    expect(wrapper.find('[data-testid="publish-confirm"]').attributes('disabled')).toBeDefined()

    // Neither is a JSON array, even though typeof reports 'object'.
    await wrapper.find('[data-testid="publish-config"]').setValue('[1, 2]')
    await nextTick()
    expect(wrapper.text()).toContain('views.EvalProposalsQueueView.config_must_be_object')
    expect(wrapper.find('[data-testid="publish-confirm"]').attributes('disabled')).toBeDefined()
    expect(mockPost).not.toHaveBeenCalled()
  })

  it('cancel closes the dialog without publishing', async () => {
    const wrapper = mountView()
    await flush()

    await wrapper.find('[data-testid="proposal-publish"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="publish-name"]').exists()).toBe(true)

    await wrapper.find('[data-testid="publish-cancel"]').trigger('click')
    await nextTick()

    expect(wrapper.find('[data-testid="publish-name"]').exists()).toBe(false)
    expect(mockPost).not.toHaveBeenCalled()
  })

  it('submitPublish is a no-op when no proposal is targeted', async () => {
    const wrapper = mountView()
    await flush()

    await (wrapper.vm as unknown as { submitPublish: () => Promise<void> }).submitPublish()
    await flush()

    expect(mockPost).not.toHaveBeenCalled()
  })

  it('blocks publishing with an empty or whitespace-only name', async () => {
    const wrapper = mountView()
    await flush()

    await wrapper.find('[data-testid="proposal-publish"]').trigger('click')
    await nextTick()

    // Empty name: the confirm button is disabled and the handler refuses
    // before reaching the API, even if the click is dispatched anyway.
    let confirm = wrapper.find('[data-testid="publish-confirm"]')
    expect(confirm.attributes('disabled')).toBeDefined()
    await confirm.trigger('click')
    await flush()
    expect(mockPost).not.toHaveBeenCalled()

    // Whitespace-only names trim to empty and are blocked the same way.
    await wrapper.find('[data-testid="publish-name"]').setValue('   ')
    await nextTick()
    confirm = wrapper.find('[data-testid="publish-confirm"]')
    expect(confirm.attributes('disabled')).toBeDefined()
    await confirm.trigger('click')
    await flush()
    expect(mockPost).not.toHaveBeenCalled()
  })

  it('surfaces a publish failure in the dialog and keeps it open', async () => {
    mockPost.mockResolvedValueOnce({ data: undefined, error: { detail: 'publish exploded' } })
    const wrapper = mountView()
    await flush()

    await wrapper.find('[data-testid="proposal-publish"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="publish-name"]').setValue('X')
    await wrapper.find('[data-testid="publish-confirm"]').trigger('click')
    await flush()

    expect(wrapper.find('[data-testid="publish-dialog-error"]').text()).toContain('publish exploded')
    // The dialog stays open so the reviewer can correct and retry.
    expect(wrapper.find('[data-testid="publish-name"]').exists()).toBe(true)
  })

  it('dismissProposal replaces the whole response through the writable computed (FAR-645)', async () => {
    const wrapper = mountView()
    await flush()

    const before = (wrapper.vm as unknown as { proposalsResp: typeof sampleResponse }).proposalsResp.items
    expect(before.find((x) => x.id === 'rec-2')?.feedback_status).toBe('pending')

    await (wrapper.vm as unknown as { dismissProposal: (id: string) => Promise<void> }).dismissProposal('rec-2')
    await flush()

    expect(mockPatch).toHaveBeenCalledWith('/api/v1/feedback/{record_id}/status', expect.objectContaining({ body: { status: 'dismissed' }, params: { path: { record_id: 'rec-2' } } }))

    const after = (wrapper.vm as unknown as { proposalsResp: typeof sampleResponse }).proposalsResp.items
    expect(after).not.toBe(before)
    expect(after.find((x) => x.id === 'rec-2')?.feedback_status).toBe('dismissed')
    expect(after.find((x) => x.id === 'rec-2')).not.toBe(before.find((x) => x.id === 'rec-2'))
  })

  it('renders an em-dash fallback for an empty rejection reason (STATE-6)', async () => {
    const wrapper = mountView()
    await flush()

    const card = wrapper.find('[data-testid="proposal-card-rec-1"]')
    // The fixtures carry rejection_reason: '' — the field must stay mounted with
    // its label and show a placeholder rather than vanishing or rendering blank.
    expect(card.text()).toContain('views.EvalProposalsQueueView.gap_description')
    expect(card.findAll('p')[1].text()).toBe('—')
  })

  it('renders the rejection reason when it is present (STATE-6)', async () => {
    mockGet.mockResolvedValue({
      data: {
        ...sampleResponse,
        items: sampleResponse.items.map((item) => ({ ...item, rejection_reason: 'Missing refusal check' })),
      },
      error: undefined,
    })

    const wrapper = mountView()
    await flush()

    expect(wrapper.find('[data-testid="proposal-card-rec-1"]').findAll('p')[1].text()).toBe('Missing refusal check')
  })

  it('marks the needs-human-review flag with the AA-safe warning token (A11Y-5)', async () => {
    const wrapper = mountView()
    await flush()

    const marker = wrapper
      .find('[data-testid="proposal-card-rec-1"]')
      .findAll('span')
      .find((span) => span.text() === 'views.EvalProposalsQueueView.needs_human_review')

    // text-amber-500 is a fill hue (~2.1:1 on white); only the -text token clears AA.
    expect(marker?.classes()).toContain('text-warning-text')
    expect(marker?.classes()).not.toContain('text-amber-500')
  })

  it('exposes the loading skeleton as busy for assistive tech (STATE-3)', async () => {
    loadingState.value = true

    const wrapper = mountView()
    await nextTick()

    expect(wrapper.find('[aria-busy="true"]').exists()).toBe(true)
  })

  // FAR-2621: the queue reads with `feedback.list` but each action is gated on
  // exactly what its own endpoint enforces. These cases pin that agreement:
  //   publish -> feedback.review AND gate.create
  //   dismiss -> feedback.update
  describe('action permissions (FAR-2621)', () => {
    it('shows both actions and no read-only notice to a fully privileged caller', async () => {
      const wrapper = mountView()
      await flush()

      expect(wrapper.findAll('[data-testid="proposal-publish"]').length).toBe(3)
      expect(wrapper.findAll('[data-testid="proposal-dismiss"]').length).toBe(3)
      expect(wrapper.find('[data-testid="proposal-readonly-notice"]').exists()).toBe(false)
      // The capability list is resolved from the server, never assumed.
      expect(mockPlanStore.fetchPermissions).toHaveBeenCalled()
    })

    it('renders the queue read-only for a viewer holding only the list permission', async () => {
      mockPlanStore.userPermissions = ['feedback.list']

      const wrapper = mountView()
      await flush()

      // The read itself still fully renders - a viewer keeps the useful part.
      expect(wrapper.findAll('[data-testid^="proposal-card-"]').length).toBe(3)
      expect(wrapper.find('[data-testid="proposal-publish"]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="proposal-dismiss"]').exists()).toBe(false)
      const notice = wrapper.find('[data-testid="proposal-readonly-notice"]')
      expect(notice.exists()).toBe(true)
      expect(notice.text()).toBe('views.EvalProposalsQueueView.readonly_notice')
    })

    it('hides publish without gate.create but keeps dismiss (mirrors each endpoint exactly)', async () => {
      // feedback.review alone admits the route dependency but the publish
      // handler ALSO enforces gate.create, so the button must stay hidden.
      mockPlanStore.userPermissions = ['feedback.list', 'feedback.review', 'feedback.update']

      const wrapper = mountView()
      await flush()

      expect(wrapper.find('[data-testid="proposal-publish"]').exists()).toBe(false)
      expect(wrapper.findAll('[data-testid="proposal-dismiss"]').length).toBe(3)
      expect(wrapper.find('[data-testid="proposal-readonly-notice"]').exists()).toBe(false)
    })

    it('hides dismiss without feedback.update but keeps publish', async () => {
      mockPlanStore.userPermissions = ['feedback.list', 'feedback.review', 'gate.create']

      const wrapper = mountView()
      await flush()

      expect(wrapper.findAll('[data-testid="proposal-publish"]').length).toBe(3)
      expect(wrapper.find('[data-testid="proposal-dismiss"]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="proposal-readonly-notice"]').exists()).toBe(false)
    })

    it('offers no read-only notice when no proposal is actionable', async () => {
      mockPlanStore.userPermissions = ['feedback.list']
      mockGet.mockResolvedValue({
        data: {
          ...sampleResponse,
          items: sampleResponse.items.map((item) => ({ ...item, feedback_status: 'resolved' })),
        },
        error: undefined,
      })

      const wrapper = mountView()
      await flush()

      expect(wrapper.findAll('[data-testid^="proposal-card-"]').length).toBe(3)
      expect(wrapper.find('[data-testid="proposal-readonly-notice"]').exists()).toBe(false)
    })
  })
})

describe('EvalProposalsQueueView — publish dialog open change', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    loadingState.value = false
    mockPlanStore.userPermissions = ['feedback.review', 'gate.create', 'feedback.update']
    mockGet.mockResolvedValue({ data: sampleResponse, error: undefined })
  })

  it('only runs the close teardown when the dialog reports closed', async () => {
    const wrapper = mountView()
    await flush()
    const vm = wrapper.vm as unknown as { onPublishDialogOpenChange: (open: boolean | undefined) => void }

    // Opening (a truthy flag) must not run the close teardown; closing must.
    expect(() => vm.onPublishDialogOpenChange(true)).not.toThrow()
    expect(() => vm.onPublishDialogOpenChange(false)).not.toThrow()
    expect(() => vm.onPublishDialogOpenChange(undefined)).not.toThrow()
  })
})
