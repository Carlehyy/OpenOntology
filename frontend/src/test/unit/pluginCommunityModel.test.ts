import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  counterText,
  endpointText,
  filterMcpServers,
  mcpStats,
  serverTitle,
  statusKeyOf,
  testToast,
  transportLabel,
} from '../../pages/community/pluginCommunityModel.ts'
import type { SuperMcpServer } from '../../api/superAssistant.ts'

const now = '2026-09-22T08:00:00+00:00'

const mk = (overrides: Partial<SuperMcpServer> = {}): SuperMcpServer => ({
  id: 'srv-1',
  name: 'weather_tools',
  display_name: '天气工具集',
  description: '提供城市天气查询能力',
  builtin_key: null,
  dev_project_id: null,
  transport: 'streamable_http',
  url: 'https://mcp.example.com/mcp',
  header_names: [],
  command: null,
  args: [],
  env_names: [],
  enabled: false,
  require_confirmation: true,
  tool_manifest: [],
  last_test_status: null,
  last_test_message: null,
  last_tested_at: null,
  created_at: now,
  updated_at: now,
  ...overrides,
})

describe('statusKeyOf', () => {
  it('success/error 映射为同名状态，其余（含 null）归为未测试', () => {
    assert.equal(statusKeyOf(mk({ last_test_status: 'success' })), 'success')
    assert.equal(statusKeyOf(mk({ last_test_status: 'error' })), 'error')
    assert.equal(statusKeyOf(mk({ last_test_status: null })), 'untested')
  })
})

describe('filterMcpServers', () => {
  const servers = [
    mk({ id: 'a', name: 'weather_tools', display_name: '天气工具集', last_test_status: 'success' }),
    mk({
      id: 'b',
      name: 'worldbank_mcp',
      display_name: '世界银行',
      description: '世界银行开放数据',
      transport: 'stdio',
      url: '',
      command: 'npx',
      args: ['-y', 'worldbank-mcp'],
      last_test_status: 'error',
    }),
    mk({
      id: 'c',
      name: 'tavily',
      display_name: 'Tavily',
      description: '联网搜索工具',
      url: 'https://tavily.example.com/mcp',
      last_test_status: null,
    }),
  ]

  it('关键词可命中名称（忽略大小写）、显示名、描述与工具名', () => {
    assert.deepEqual(filterMcpServers(servers, 'WEATHER', []).map(s => s.id), ['a'])
    assert.deepEqual(filterMcpServers(servers, '世界', []).map(s => s.id), ['b'])
    assert.deepEqual(filterMcpServers(servers, '天气查询', []).map(s => s.id), ['a'])
    assert.deepEqual(filterMcpServers(servers, 'mcp.example.com', []).map(s => s.id), ['a'])
    assert.deepEqual(
      filterMcpServers([mk({ id: 't', tool_manifest: [{ name: 'get_forecast', description: '读取城市天气预报', input_schema: { type: 'object' } }] })], 'forecast', []).map(s => s.id),
      ['t'],
    )
  })

  it('stdio 服务器可用命令行命中；无结果时返回空数组', () => {
    assert.deepEqual(filterMcpServers(servers, 'worldbank-mcp', []).map(s => s.id), ['b'])
    assert.deepEqual(filterMcpServers(servers, 'zzqqxx', []), [])
  })

  it('状态筛选为多选并集，未勾选视为不过滤', () => {
    assert.deepEqual(filterMcpServers(servers, '', []).map(s => s.id), ['a', 'b', 'c'])
    assert.deepEqual(filterMcpServers(servers, '', ['success']).map(s => s.id), ['a'])
    assert.deepEqual(filterMcpServers(servers, '', ['success', 'untested']).map(s => s.id), ['a', 'c'])
    // 关键词与状态叠加：先关键词再状态
    assert.deepEqual(filterMcpServers(servers, 'mcp', ['error']).map(s => s.id), ['b'])
  })
})

describe('mcpStats', () => {
  it('统计总数、已通过数与工具总数', () => {
    const servers = [
      mk({ last_test_status: 'success', tool_manifest: [{ name: 't1', description: '', input_schema: {} }, { name: 't2', description: '', input_schema: {} }] }),
      mk({ last_test_status: 'error', tool_manifest: [{ name: 't3', description: '', input_schema: {} }] }),
      mk(),
    ]
    assert.deepEqual(mcpStats(servers), { total: 3, healthy: 1, tools: 3 })
    assert.deepEqual(mcpStats([]), { total: 0, healthy: 0, tools: 0 })
  })
})

describe('counterText', () => {
  it('加载期显示占位文案而非 0 / 0，避免“空平台”误读', () => {
    assert.equal(counterText(0, 0, true), '正在加载…')
    assert.equal(counterText(2, 3, false), '显示 2 / 3 项')
  })
})

describe('testToast', () => {
  it('测试成功且未启用：标题带服务器名并建议就地启用', () => {
    const info = testToast(mk({ enabled: false }), { ok: true, message: '连接成功，发现 12 个工具' })
    assert.equal(info.ok, true)
    assert.equal(info.title, '「天气工具集」连接成功')
    assert.equal(info.suggestEnable, true)
    assert.match(info.description, /发现 12 个工具/)
    assert.match(info.description, /尚未对超级助手启用/)
  })

  it('测试成功且已启用：不提示启用', () => {
    const info = testToast(mk({ enabled: true }), { ok: true, message: '连接成功，发现 2 个工具' })
    assert.equal(info.suggestEnable, false)
    assert.equal(info.description, '连接成功，发现 2 个工具')
  })

  it('测试失败：标题为连接失败，不提示启用', () => {
    const info = testToast(mk(), { ok: false, message: '连接超时' })
    assert.equal(info.title, '「天气工具集」连接失败')
    assert.equal(info.suggestEnable, false)
    assert.equal(info.description, '连接超时')
  })

  it('未配置显示名时标题回退到标识', () => {
    const info = testToast(mk({ display_name: '' }), { ok: true, message: '' })
    assert.equal(info.title, '「weather_tools」连接成功')
  })
})

describe('serverTitle / transportLabel / endpointText', () => {
  it('显示名缺失回退标识；developed 与 stdio 的标签和地址各有约定', () => {
    assert.equal(serverTitle(mk({ display_name: '' })), 'weather_tools')
    assert.equal(transportLabel(mk({ transport: 'developed' })), '自研')
    assert.equal(transportLabel(mk({ transport: 'streamable_http' })), 'Streamable HTTP')
    assert.equal(endpointText(mk({ transport: 'developed' })), '平台进程内执行')
    assert.equal(endpointText(mk({ transport: 'stdio', command: 'npx', args: ['-y', 'x'] })), 'npx -y x')
    assert.equal(endpointText(mk()), 'https://mcp.example.com/mcp')
  })
})
