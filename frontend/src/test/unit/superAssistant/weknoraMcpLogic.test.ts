import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import type { SuperMcpServer, WeKnoraEndpoint } from '../../../api/superAssistant.ts'
import {
  isWeKnoraManagedServer,
  WEKNORA_READONLY_TOOLS,
  weknoraEndpointCompliance,
  weknoraEndpointConnectBlocked,
  weknoraEndpointStatusView,
  weknoraManagedServerFor,
} from '../../../pages/super-assistant/components/weknoraMcpLogic.ts'

const endpoint = (overrides: Partial<WeKnoraEndpoint> = {}): WeKnoraEndpoint => ({
  id: '0193aaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee',
  name: '知识库检索',
  enabled: true,
  knowledge_base_ids: ['kb-1'],
  knowledge_bases: [{ id: 'kb-1', name: '产品手册' }],
  tools: [...WEKNORA_READONLY_TOOLS],
  readonly: true,
  server_id: null,
  connection_status: 'not_connected',
  message: null,
  ...overrides,
})

describe('weknoraEndpointCompliance', () => {
  it('只读端点且工具全在白名单时合规', () => {
    const view = weknoraEndpointCompliance(endpoint())
    assert.equal(view.compliant, true)
    assert.deepEqual(view.disallowed, [])
    assert.equal(view.notReadonly, false)
  })

  it('包含 ask 等白名单外工具时不合规并给出明细', () => {
    const view = weknoraEndpointCompliance(endpoint({
      tools: ['list_knowledge_bases', 'search_knowledge', 'ask', 'delete_document'],
    }))
    assert.equal(view.compliant, false)
    assert.deepEqual(view.disallowed, ['ask', 'delete_document'])
    assert.equal(view.notReadonly, false)
    assert.deepEqual(view.missingRequired, [])
  })

  it('白名单子集但缺必备工具 list_knowledge_bases 仍不合规', () => {
    // 平台 worker 仅凭端点 Token 调该工具核验真实可见知识库范围，缺失即阻断
    const view = weknoraEndpointCompliance(endpoint({
      tools: ['search_knowledge', 'read_document'],
    }))
    assert.equal(view.compliant, false)
    assert.deepEqual(view.disallowed, [])
    assert.deepEqual(view.missingRequired, ['list_knowledge_bases'])
    assert.match(
      weknoraEndpointConnectBlocked(endpoint({ tools: ['search_knowledge'] }), view) ?? '',
      /缺少必备工具（list_knowledge_bases）/,
    )
  })

  it('端点未声明只读时即使工具全命中白名单也不合规', () => {
    const view = weknoraEndpointCompliance(endpoint({ readonly: false }))
    assert.equal(view.compliant, false)
    assert.equal(view.notReadonly, true)
    assert.deepEqual(view.disallowed, [])
    assert.deepEqual(view.missingRequired, [])
  })
})

describe('weknoraEndpointStatusView', () => {
  it('按连接状态映射中文标签与语义色调', () => {
    assert.deepEqual(weknoraEndpointStatusView(endpoint({ connection_status: 'ready' })), { label: '已连接', tone: 'success' })
    assert.deepEqual(weknoraEndpointStatusView(endpoint({ connection_status: 'needs_verification' })), { label: '待校验', tone: 'warning' })
    assert.deepEqual(weknoraEndpointStatusView(endpoint({ connection_status: 'authorization_required' })), { label: '需要 Token', tone: 'warning' })
    assert.deepEqual(weknoraEndpointStatusView(endpoint({ connection_status: 'disabled' })), { label: '已停用', tone: 'muted' })
    assert.deepEqual(weknoraEndpointStatusView(endpoint({ connection_status: 'unavailable' })), { label: '不可用', tone: 'danger' })
    assert.deepEqual(weknoraEndpointStatusView(endpoint({ connection_status: 'not_connected' })), { label: '未接入', tone: 'muted' })
  })

  it('上游缺失（missing）一律按不可用呈现，覆盖任何连接状态', () => {
    assert.deepEqual(
      weknoraEndpointStatusView(endpoint({ missing: true, connection_status: 'ready' })),
      { label: '不可用', tone: 'danger' },
    )
  })
})

describe('weknoraEndpointConnectBlocked', () => {
  const compliance = weknoraEndpointCompliance(endpoint())

  it('合规且启用的端点允许接入', () => {
    assert.equal(weknoraEndpointConnectBlocked(endpoint(), compliance), null)
  })

  it('上游缺失 / 上游停用时阻断并说明原因', () => {
    assert.match(
      weknoraEndpointConnectBlocked(endpoint({ missing: true }), compliance) ?? '',
      /上游端点已不存在/,
    )
    assert.match(
      weknoraEndpointConnectBlocked(endpoint({ enabled: false }), compliance) ?? '',
      /停用/,
    )
    assert.match(
      weknoraEndpointConnectBlocked(endpoint({ connection_status: 'disabled' }), compliance) ?? '',
      /停用/,
    )
  })

  it('工具不合规或未开只读时阻断并列出不合规工具', () => {
    const nonCompliant = weknoraEndpointCompliance(endpoint({
      tools: ['list_knowledge_bases', 'ask', 'search_knowledge'],
    }))
    assert.match(
      weknoraEndpointConnectBlocked(endpoint(), nonCompliant) ?? '',
      /仅允许接入只读端点.*ask/,
    )
    const notReadonly = weknoraEndpointCompliance(endpoint({ readonly: false }))
    assert.match(
      weknoraEndpointConnectBlocked(endpoint(), notReadonly) ?? '',
      /端点范围或只读工具配置未通过校验/,
    )
  })
})

describe('托管 Server 行识别与查找', () => {
  const server = (overrides: Partial<Pick<SuperMcpServer, 'id' | 'weknora_endpoint_id'>>): Pick<SuperMcpServer, 'id' | 'weknora_endpoint_id'> => ({
    id: 'srv-1',
    weknora_endpoint_id: null,
    ...overrides,
  })

  it('weknora_endpoint_id 回链的行是托管行（缺省视为非托管，兼容旧数据）', () => {
    assert.equal(isWeKnoraManagedServer(server({})), false)
    assert.equal(isWeKnoraManagedServer(server({ weknora_endpoint_id: 'ep-1' })), true)
  })

  it('优先按 server_id 精确匹配，兜底按 weknora_endpoint_id 回链', () => {
    const ep = endpoint({ id: 'ep-1', server_id: 'srv-9' })
    assert.equal(weknoraManagedServerFor(ep, [
      server({ id: 'srv-1', weknora_endpoint_id: 'ep-1' }),
      server({ id: 'srv-9', weknora_endpoint_id: 'ep-1' }),
    ])!.id, 'srv-9')
    // server_id 指向的行不存在（列表非同刻刷新）：按回链兜底命中
    assert.equal(weknoraManagedServerFor(ep, [
      server({ id: 'srv-1', weknora_endpoint_id: 'ep-1' }),
    ])!.id, 'srv-1')
  })

  it('未接入端点找不到托管行时返回 null', () => {
    assert.equal(weknoraManagedServerFor(endpoint(), [server({})]), null)
    assert.equal(weknoraManagedServerFor(endpoint(), []), null)
  })
})
