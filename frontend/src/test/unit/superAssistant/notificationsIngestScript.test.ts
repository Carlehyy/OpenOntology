import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  INGEST_ENDPOINT_PATH,
  INGEST_KEY_PLACEHOLDER,
  ingestCurlLines,
  ingestCurlScript,
} from '../../../pages/super-assistant/components/notificationsIngestScript.ts'

const ENDPOINT = 'https://example.com/api/v2/notifications/ingest'

describe('ingestCurlScript', () => {
  it('JSON 体按 key 断行（bash 单引号多行串 + JSON 容忍键值间空白，复制即可执行）', () => {
    const lines = ingestCurlScript(ENDPOINT).split('\n')
    assert.equal(lines.length, 9)
    assert.match(lines[0], /^curl -X POST https:\/\/example\.com\/api\/v2\/notifications\/ingest \\$/)
    assert.equal(lines[1], `  -H "X-API-Key: ${INGEST_KEY_PLACEHOLDER}" \\`)
    assert.equal(lines[2], '  -H "Content-Type: application/json" \\')
    assert.equal(lines[3], "  -d '{")
    assert.equal(lines[4], '    "eventId": "order-123",')
    assert.equal(lines[5], '    "title": "订单异常",')
    // body 内换行必须是 JSON 字符串里的字面转义（\n 两个字符），不能是真换行
    assert.equal(lines[6], '    "body": "# 详情\\n金额超限",')
    assert.equal(lines[7], '    "priority": "high"')
    assert.equal(lines[8], "  }'")
    // JSON 尾项无逗号
    assert.ok(!lines[7].endsWith(','))
  })

  it('注入密钥时 X-API-Key 行携带明文，其余行不变', () => {
    const withKey = ingestCurlScript(ENDPOINT, 'ob_notif_demo_secret').split('\n')
    const withoutKey = ingestCurlScript(ENDPOINT).split('\n')
    assert.equal(withKey[1], '  -H "X-API-Key: ob_notif_demo_secret" \\')
    assert.deepEqual(
      withKey.filter((_, i) => i !== 1),
      withoutKey.filter((_, i) => i !== 1),
    )
  })

  it('空白密钥回退占位符（签发响应缺明文时不产出残缺脚本）', () => {
    const script = ingestCurlScript(INGEST_ENDPOINT_PATH, ' '.repeat(3))
    assert.ok(script.includes(INGEST_KEY_PLACEHOLDER))
  })

  it('端点原样透传：相对路径也能拼出可用脚本（同源部署场景）', () => {
    const script = ingestCurlScript(INGEST_ENDPOINT_PATH)
    assert.ok(script.split('\n')[0].includes(INGEST_ENDPOINT_PATH))
  })
})

describe('ingestCurlLines（渲染分段，与复制文本同源）', () => {
  it('渲染 join 后与 ingestCurlScript 输出一致', () => {
    const rendered = ingestCurlLines(ENDPOINT, 'ob_k').map(l => l.map(s => s.text).join('')).join('\n')
    assert.equal(rendered, ingestCurlScript(ENDPOINT, 'ob_k'))
  })

  it('占位符独占 placeholder 分段（「需替换」着色信号）；注入密钥后无 placeholder 段', () => {
    const placeholderSegs = ingestCurlLines(ENDPOINT).flat().filter(s => s.kind === 'placeholder')
    assert.equal(placeholderSegs.length, 1)
    assert.equal(placeholderSegs[0].text, INGEST_KEY_PLACEHOLDER)
    assert.ok(ingestCurlLines(ENDPOINT, 'ob_k').flat().every(s => s.kind !== 'placeholder'))
  })

  it('参数名与 JSON key 标记为弱化层（flag/json-key），命令与 URL 保持默认层', () => {
    const lines = ingestCurlLines(ENDPOINT)
    const kinds = lines.flat().map(s => s.kind)
    assert.ok(kinds.includes('flag'))
    assert.ok(kinds.includes('json-key'))
    assert.equal(lines[0][0].kind, 'cmd')
    assert.equal(lines[0][1].kind, 'url')
  })
})
