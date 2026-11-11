import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  MCP_PASSWORD_HEADER,
  MCP_PASSWORD_PLACEHOLDER,
  MCP_USERNAME_HEADER,
  buildMcpClientConfig,
  buildMcpEndpointUrl,
  escapeMcpHeaderValue,
  renderMcpConfigJson,
  sanitizeMcpServerKey,
} from '../../../pages/agent/components/mcpConnectionConfig.ts'

describe('MCP 端点 URL', () => {
  it('同源 origin 与独立 API 基址（__API_BASE_URL__ 注入）统一拼接', () => {
    assert.equal(
      buildMcpEndpointUrl('http://localhost:5173', 'ontology-1'),
      'http://localhost:5173/api/v2/formal/ontologies/ontology-1/mcp',
    )
    assert.equal(
      buildMcpEndpointUrl('https://api.example.test/', 'ontology-1'),
      'https://api.example.test/api/v2/formal/ontologies/ontology-1/mcp',
    )
  })

  it('oid 中的保留字符经 encodeURIComponent 编码', () => {
    assert.equal(
      buildMcpEndpointUrl('https://open.example.com', 'ont ology/1?x'),
      'https://open.example.com/api/v2/formal/ontologies/ont%20ology%2F1%3Fx/mcp',
    )
  })
})

describe('会展开变量的客户端（如 pi）的请求头值转义', () => {
  it('$ 写成 $$，开头 ! 写成 $!；普通值原样（其他客户端无感知）', () => {
    assert.equal(escapeMcpHeaderValue('normal'), 'normal')
    assert.equal(escapeMcpHeaderValue('pa$$word'), 'pa$$$$word')
    assert.equal(escapeMcpHeaderValue('!start'), '$!start')
    assert.equal(escapeMcpHeaderValue('!$mix'), '$!$$mix')
    assert.equal(escapeMcpHeaderValue('$!var'), '$$!var')
    assert.equal(escapeMcpHeaderValue('a!b'), 'a!b')
  })
})

describe('mcpServers server key 净化', () => {
  it('非法字符折叠为 -，首尾 - 去除，空值回落', () => {
    assert.equal(sanitizeMcpServerKey('ontology-1'), 'ontology-1')
    assert.equal(sanitizeMcpServerKey('ont ology/1'), 'ont-ology-1')
    assert.equal(sanitizeMcpServerKey('/ops team/'), 'ops-team')
    assert.equal(sanitizeMcpServerKey('///'), 'ontology')
    assert.equal(sanitizeMcpServerKey(''), 'ontology')
  })

  it('配置 key 使用净化结果', () => {
    const config = buildMcpClientConfig({
      base: 'https://open.example.com', oid: 'ont ology/1',
      ontologyName: '供应链本体', username: 'admin',
    })
    assert.deepEqual(Object.keys(config.mcpServers), ['openontology-ont-ology-1'])
  })
})

describe('mcpServers entry 字段', () => {
  const config = buildMcpClientConfig({
    base: 'https://open.example.com', oid: 'ontology-1',
    ontologyName: '供应链本体', username: 'ad$m!in',
  })
  const entry = config.mcpServers['openontology-ontology-1']

  it('原生 Streamable HTTP：url + exposure direct + description', () => {
    assert.equal(entry.url, 'https://open.example.com/api/v2/formal/ontologies/ontology-1/mcp')
    assert.equal(entry.exposure, 'direct')
    assert.ok(entry.description.includes('供应链本体'))
    assert.ok(entry.description.includes('自动跟随最新发布版本'))
    for (const tool of ['ontology_schema', 'ontology_query', 'ontology_query_page']) {
      assert.ok(entry.description.includes(tool), `description 应包含工具 ${tool}`)
    }
  })

  it('鉴权双请求头，用户名按会展开的客户端规则转义', () => {
    assert.deepEqual(Object.keys(entry.headers).sort(), [MCP_PASSWORD_HEADER, MCP_USERNAME_HEADER].sort())
    assert.equal(entry.headers[MCP_USERNAME_HEADER], 'ad$$m!in')
  })

  it('不内嵌真实密码：仅 ASCII 占位符，无密码入参', () => {
    assert.equal(entry.headers[MCP_PASSWORD_HEADER], MCP_PASSWORD_PLACEHOLDER)
    assert.equal(MCP_PASSWORD_PLACEHOLDER, 'REPLACE_WITH_YOUR_PASSWORD')
    assert.match(MCP_PASSWORD_PLACEHOLDER, /^[\x20-\x7E]+$/)
    // 输入契约里没有 password 字段：整个产物序列化后不含任何“真实密码”位
    const serialized = JSON.stringify(config)
    assert.ok(!serialized.includes('"password"'))
  })

  it('未登录时用户名回落占位符（同样可转义）', () => {
    const anonymous = buildMcpClientConfig({
      base: 'https://open.example.com', oid: 'ontology-1',
      ontologyName: '供应链本体', username: '',
    })
    assert.equal(
      anonymous.mcpServers['openontology-ontology-1'].headers[MCP_USERNAME_HEADER],
      'your-username',
    )
  })
})

describe('JSON 模板序列化', () => {
  it('可解析回等价结构，且以换行结尾方便粘贴', () => {
    const input = { base: 'http://localhost:5173', oid: 'ontology-1', ontologyName: '供应链本体', username: 'admin' }
    const rendered = renderMcpConfigJson(input)
    assert.ok(rendered.endsWith('\n'))
    assert.deepEqual(JSON.parse(rendered), buildMcpClientConfig(input))
  })
})
