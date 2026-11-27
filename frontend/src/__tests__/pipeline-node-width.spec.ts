import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import {
  NODE_REGISTRY_ORDER,
  PIPELINE_NODE_MAX_WIDTH_CLASS,
} from '../components/pipeline/nodes/NodeRegistry'
import AgentNode from '../components/pipeline/nodes/AgentNode.vue'
import ConnectorNode from '../components/pipeline/nodes/ConnectorNode.vue'
import DispatchNode from '../components/pipeline/nodes/DispatchNode.vue'
import HitlNode from '../components/pipeline/nodes/HitlNode.vue'
import ManualNode from '../components/pipeline/nodes/ManualNode.vue'
import RouterNode from '../components/pipeline/nodes/RouterNode.vue'
import { layoutNodes } from '../utils/graph-layout'

const CARDS = {
  manual: ManualNode,
  agent: AgentNode,
  router: RouterNode,
  hitl: HitlNode,
  dispatch: DispatchNode,
  connector: ConnectorNode,
}

const LONG_DESCRIPTION = 'Classifies the PR by type (feature, bugfix, refactor, docs) and severity.'

describe('pipeline node card width bound', () => {
  it.each(NODE_REGISTRY_ORDER)('bounds the %s card and clamps its description to two lines', (nodeType) => {
    const wrapper = mount(CARDS[nodeType], {
      props: { id: nodeType, data: { node_type: nodeType, label: 'Classify PR', description: LONG_DESCRIPTION } },
    })
    expect(wrapper.classes()).toContain(PIPELINE_NODE_MAX_WIDTH_CLASS)
    expect(wrapper.classes()).toContain('break-words')
    const description = wrapper.findAll('div').find((div) => div.text() === LONG_DESCRIPTION)!
    expect(description.classes()).toContain('line-clamp-2')
    wrapper.unmount()
  })

  it('spaces unmeasured cards further apart than the widest card', () => {
    // Tailwind spacing scale: one step is 0.25rem, 4px at the 16px root size.
    const maxWidthPx = Number(PIPELINE_NODE_MAX_WIDTH_CLASS.replace('max-w-', '')) * 4
    const [source, target] = layoutNodes(
      [{ id: 'a' }, { id: 'b' }],
      [{ source: 'a', target: 'b' }],
    )
    expect(target.position.x - source.position.x).toBeGreaterThan(maxWidthPx)
  })
})
