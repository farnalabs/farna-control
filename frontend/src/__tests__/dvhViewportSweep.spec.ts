import { describe, it, expect } from 'vitest'
import appSidebarSource from '../components/AppSidebar.vue?raw'
import assistantOnlySource from '../views/AssistantOnlyView.vue?raw'
import acceptInviteSource from '../views/AcceptInviteView.vue?raw'
import loginSource from '../views/LoginView.vue?raw'

// FAR-2547: the dynamic-viewport-height sweep. Each screen keeps its `vh`
// class as the fallback and adds the matching `dvh` companion; a template that
// drops the companion would silently regress mobile bottom-of-viewport
// reachability, so these assertions pin the class pair on the root elements.
describe('FAR-2547 dynamic viewport height sweep', () => {
  it('AppSidebar keeps h-screen as the fallback and adds h-screen-dvh on both width variants', () => {
    const pairs = appSidebarSource.match(/h-screen h-screen-dvh/g) ?? []
    // Two width variants: the desktop in-flow sidebar and the mobile icon rail.
    expect(pairs.length).toBe(2)
  })

  it('AssistantOnlyView keeps h-screen as the fallback and adds h-screen-dvh', () => {
    expect(assistantOnlySource).toContain('h-screen h-screen-dvh')
  })

  it('AcceptInviteView keeps min-h-screen as the fallback and adds min-h-screen-dvh', () => {
    expect(acceptInviteSource).toContain('min-h-screen min-h-screen-dvh')
  })

  it('LoginView keeps min-h-screen as the fallback and adds min-h-screen-dvh', () => {
    expect(loginSource).toContain('min-h-screen min-h-screen-dvh')
  })
})
