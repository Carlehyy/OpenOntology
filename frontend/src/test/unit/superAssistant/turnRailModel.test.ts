import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import { deriveTurnRailItems } from '../../../pages/super-assistant/components/turnRailModel.ts'
import type { SuperMessage } from '../../../api/superAssistant.ts'

const makeMessage = (overrides: Partial<SuperMessage>): SuperMessage => ({
  id: 'm-1',
  conversation_id: 'c-1',
  role: 'user',
  content: '',
  status: 'complete',
  steps: [],
  token_usage: {},
  created_at: '2026-10-12T00:00:00Z',
  ...overrides,
})

describe('deriveTurnRailItems', () => {
  it('空会话与纯助手开头段不产生刻度', () => {
    assert.deepEqual(deriveTurnRailItems([]), [])
    const headAssistantOnly = [
      makeMessage({ id: 'a-0', role: 'assistant', content: '开场白' }),
    ]
    assert.deepEqual(deriveTurnRailItems(headAssistantOnly), [])
  })

  it('完整一轮：用户预览剥掉 markdown 语法，助手预览取答复段', () => {
    const items = deriveTurnRailItems([
      makeMessage({ id: 'u-1', role: 'user', content: '**帮我**查一下 `订单` 趋势' }),
      makeMessage({
        id: 'a-1',
        role: 'assistant',
        content: '先看数据结构……\n\n## 结论\n订单**稳步上升**\n```sql\nSELECT 1\n```',
        steps: [{ toolName: 'web_search', status: 'success', arguments: {} }],
      }),
    ])
    assert.equal(items.length, 1)
    assert.equal(items[0].userMessageId, 'u-1')
    assert.equal(items[0].userPreview, '帮我查一下 订单 趋势')
    assert.equal(items[0].assistantPreviewKind, 'text')
    // settled 且带工具轨迹：预览取从首个标题切出的答复段，过程段不进预览
    assert.equal(items[0].assistantPreview, '结论 订单稳步上升 SELECT 1')
    assert.equal(items[0].isRunning, false)
  })

  it('流式中尚无正文标记 running，流出文本后转为 text 且 isRunning 保持', () => {
    const base = [
      makeMessage({ id: 'u-1', role: 'user', content: '第一问' }),
    ]
    const empty = deriveTurnRailItems([
      ...base,
      makeMessage({ id: 'a-1', role: 'assistant', content: '', status: 'streaming' }),
    ])
    assert.equal(empty[0].assistantPreviewKind, 'running')
    assert.equal(empty[0].isRunning, true)

    const partial = deriveTurnRailItems([
      ...base,
      makeMessage({ id: 'a-1', role: 'assistant', content: '正在生成的部分答复', status: 'streaming' }),
    ])
    assert.equal(partial[0].assistantPreviewKind, 'text')
    assert.equal(partial[0].assistantPreview, '正在生成的部分答复')
    assert.equal(partial[0].isRunning, true)
  })

  it('连续多条用户消息（排队）各自成刻度，未回复的轮次为 empty', () => {
    const items = deriveTurnRailItems([
      makeMessage({ id: 'u-1', role: 'user', content: '第一问' }),
      makeMessage({ id: 'u-2', role: 'user', content: '第二问' }),
    ])
    assert.equal(items.length, 2)
    assert.equal(items[0].userMessageId, 'u-1')
    assert.equal(items[0].assistantPreviewKind, 'empty')
    assert.equal(items[1].userMessageId, 'u-2')
    assert.equal(items[1].assistantPreviewKind, 'empty')
  })

  it('同一轮多条助手消息：取最近一段有文本的答复作预览', () => {
    const items = deriveTurnRailItems([
      makeMessage({ id: 'u-1', role: 'user', content: '问' }),
      makeMessage({
        id: 'a-1', role: 'assistant', content: '第一段中间答复',
        steps: [{ toolName: 'web_fetch', status: 'success', arguments: {} }],
      }),
      makeMessage({ id: 'a-2', role: 'assistant', content: '最终结论在这里' }),
    ])
    assert.equal(items[0].assistantPreview, '最终结论在这里')
    assert.equal(items[0].assistantPreviewKind, 'text')
  })

  it('助手未返回文本的轮次保持 empty，预览回退文案由展示层兜底', () => {
    const items = deriveTurnRailItems([
      makeMessage({ id: 'u-1', role: 'user', content: '问' }),
      makeMessage({ id: 'a-1', role: 'assistant', content: '' }),
    ])
    assert.equal(items[0].assistantPreviewKind, 'empty')
    assert.equal(items[0].assistantPreview, '')
  })

  it('超长内容截断到预览上限并带省略号', () => {
    const items = deriveTurnRailItems([
      makeMessage({ id: 'u-1', role: 'user', content: '长'.repeat(300) }),
    ])
    assert.ok(items[0].userPreview.length <= 141)
    assert.ok(items[0].userPreview.endsWith('…'))
  })
})
