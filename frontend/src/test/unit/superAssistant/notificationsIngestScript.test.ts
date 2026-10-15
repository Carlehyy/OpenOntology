import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  INGEST_ENDPOINT_PATH,
  INGEST_KEY_PLACEHOLDER,
  ingestCurlScript,
} from '../../../pages/super-assistant/components/notificationsIngestScript.ts'

describe('ingestCurlScript', () => {
  it('未注入密钥时输出占位符版（四段结构与转义换行）', () => {
    const script = ingestCurlScript('https://example.com/api/v2/notifications/ingest')
    const lines = script.split('\n')
    assert.equal(lines.length, 4)
    assert.match(lines[0], /^curl -X POST https:\/\/example\.com\/api\/v2\/notifications\/ingest \\$/)
    assert.equal(lines[1], `  -H "X-API-Key: ${INGEST_KEY_PLACEHOLDER}" \\`)
    assert.equal(lines[2], '  -H "Content-Type: application/json" \\')
    assert.match(lines[3], /^ {2}-d '\{"eventId":"order-123".*"priority":"high"\}'$/)
    // body 内换行必须是 JSON 字符串内的字面转义（\n 两个字符），不能是真换行
    assert.ok(lines[3].includes('\\n'))
  })

  it('注入密钥时 X-API-Key 行携带明文，其余行不变', () => {
    const endpoint = 'https://example.com/api/v2/notifications/ingest'
    const withKey = ingestCurlScript(endpoint, 'ob_notif_demo_secret')
    const withoutKey = ingestCurlScript(endpoint)
    assert.equal(withKey.split('\n')[1], '  -H "X-API-Key: ob_notif_demo_secret" \\')
    assert.equal(withKey.split('\n')[0], withoutKey.split('\n')[0])
    assert.equal(withKey.split('\n')[2], withoutKey.split('\n')[2])
    assert.ok(!withKey.includes(INGEST_KEY_PLACEHOLDER))
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
