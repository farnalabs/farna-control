import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

const { apiGet } = vi.hoisted(() => ({ apiGet: vi.fn() }))

vi.mock('@/lib/api/client', () => ({
  getAccessToken: vi.fn(() => 'mock-token'),
  getAuthHeaders: vi.fn(() => ({ Authorization: 'Bearer mock-token' })),
  api: {
    GET: apiGet,
    POST: vi.fn(),
    PATCH: vi.fn(),
    PUT: vi.fn(),
    DELETE: vi.fn(),
  },
}))

vi.mock('../components/assistant/AssistantChat.vue', () => ({
  default: { name: 'AssistantChat', template: '<div data-testid="assistant-chat-stub" />' },
}))
vi.mock('../components/assistant/AssistantSessionDrawer.vue', () => ({
  default: { name: 'AssistantSessionDrawer', emits: ['close', 'select-session'], template: '<div />' },
}))
vi.mock('../components/assistant/AssistantSkillManager.vue', () => ({
  default: { name: 'AssistantSkillManager', template: '<div />' },
}))
vi.mock('../components/assistant/AssistantContextSources.vue', () => ({
  default: { name: 'AssistantContextSources', template: '<div />' },
}))

import AssistantPanel from '../components/assistant/AssistantPanel.vue'
import { useAssistantStore } from '../composables/useAssistantStore'

async function mountDocked(): Promise<ReturnType<typeof mount>> {
  const store = useAssistantStore()
  store.panelState = 'docked'
  apiGet.mockResolvedValue({ data: { items: [] }, error: undefined })
  const wrapper = mount(AssistantPanel)
  await flushPromises()
  return wrapper
}

// FAR-2547: the docked panel fills the viewport with 100dvh where supported so
// its bottom is not hidden behind mobile browser chrome, and falls back to
// 100vh where dvh is unavailable.
describe('AssistantPanel docked height (FAR-2547)', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    vi.clearAllMocks()
    // jsdom does not implement CSS.supports; ensure a spy-able function exists.
    if (typeof (CSS as { supports?: unknown }).supports !== 'function') {
      ;(CSS as Record<string, unknown>).supports = () => true
    }
  })

  afterEach(() => {
    vi.restoreAllMocks()
    localStorage.clear()
  })

  it('uses 100dvh for the docked panel when the browser supports dvh', async () => {
    vi.spyOn(CSS, 'supports').mockReturnValue(true)
    const wrapper = await mountDocked()
    const panel = wrapper.find('.assistant-panel')
    expect(panel.exists()).toBe(true)
    expect((panel.element as HTMLElement).style.height).toBe('100dvh')
  })

  it('falls back to 100vh for the docked panel when dvh is unsupported', async () => {
    vi.spyOn(CSS, 'supports').mockReturnValue(false)
    const wrapper = await mountDocked()
    const panel = wrapper.find('.assistant-panel')
    expect(panel.exists()).toBe(true)
    expect((panel.element as HTMLElement).style.height).toBe('100vh')
  })
})
