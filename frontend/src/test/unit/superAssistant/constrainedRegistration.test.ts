import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  constrainedRegistrationState,
  type ConstrainedRegistrationCandidate,
} from '../../../pages/super-assistant/components/assistantPanelUtils.ts'

const server = (
  overrides: Partial<ConstrainedRegistrationCandidate> = {},
): ConstrainedRegistrationCandidate => ({
  constrained: false,
  builtin_key: null,
  last_test_status: 'success',
  tool_manifest: [{ name: 'search_knowledge' }],
  ...overrides,
})

describe('constrainedRegistrationState', () => {
  it('仅成功测试且工具清单非空的非内置 Server 可登记', () => {
    assert.equal(constrainedRegistrationState(server()).eligible, true)
  })

  it('未测试或测试失败时拒绝登记并说明原因', () => {
    for (const status of [null, 'error'] as const) {
      const state = constrainedRegistrationState(server({ last_test_status: status }))
      assert.equal(state.eligible, false)
      assert.match(state.hint ?? '', /测试连接成功/)
    }
  })

  it('工具清单为空时拒绝登记', () => {
    const state = constrainedRegistrationState(server({ tool_manifest: [] }))
    assert.equal(state.eligible, false)
    assert.match(state.hint ?? '', /工具清单为空/)
  })

  it('平台内置 MCP 恒为受约束，不提供手工登记', () => {
    const state = constrainedRegistrationState(server({ builtin_key: 'api_hub' }))
    assert.equal(state.eligible, false)
    assert.match(state.hint ?? '', /内置/)
  })

  it('已登记 Server 保持可撤销，并提示连接或凭据变更后需重新登记', () => {
    const state = constrainedRegistrationState(server({
      constrained: true,
      last_test_status: null,
      tool_manifest: [],
    }))
    assert.equal(state.eligible, true)
    assert.match(state.hint ?? '', /重新测试并登记/)
  })
})
