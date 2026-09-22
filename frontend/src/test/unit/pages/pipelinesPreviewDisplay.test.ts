import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  displayCellValue,
  isWebhookEchoColumns,
} from '../../../pages/pipelines/previewDisplay.ts'

describe('displayCellValue（试运行输出单元格统一序列化）', () => {
  it('对象序列化为 JSON 文本，不再出现 [object Object]', () => {
    assert.equal(
      displayCellValue({ host: 'n8n', accept: '*/*' }),
      '{"host":"n8n","accept":"*/*"}',
    )
    assert.equal(displayCellValue({ nested: { a: 1 } }), '{"nested":{"a":1}}')
    assert.equal(displayCellValue([1, 2]), '[1,2]')
  })

  it('null / undefined 归一为空串，标量转字符串', () => {
    assert.equal(displayCellValue(null), '')
    assert.equal(displayCellValue(undefined), '')
    assert.equal(displayCellValue(667), '667')
    assert.equal(displayCellValue(true), 'true')
    assert.equal(displayCellValue('production'), 'production')
  })

  it('无法 JSON 序列化的对象回退 String()，不抛错', () => {
    const circular: Record<string, unknown> = {}
    circular.self = circular
    assert.equal(typeof displayCellValue(circular), 'string')
  })
})

describe('isWebhookEchoColumns（n8n 骨架回显识别）', () => {
  const echo = ['headers', 'params', 'query', 'body', 'webhookUrl', 'executionMode']

  it('列集合与 webhook 回显形状完全一致时识别（顺序无关）', () => {
    assert.equal(isWebhookEchoColumns(echo), true)
    assert.equal(isWebhookEchoColumns([...echo].reverse()), true)
  })

  it('缺列 / 多列 / 子集 / 空集合都不识别，避免误伤真实流水线', () => {
    assert.equal(isWebhookEchoColumns(echo.slice(1)), false)
    assert.equal(isWebhookEchoColumns([...echo, 'extra']), false)
    assert.equal(isWebhookEchoColumns(['body', 'headers']), false)
    assert.equal(isWebhookEchoColumns([]), false)
    // 响应体恰好含 body/headers 但整体不是回显形状的真实接口
    assert.equal(isWebhookEchoColumns(['id', 'body', 'amount', 'created_at']), false)
  })
})
