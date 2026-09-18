import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  appendToolStart,
  enqueueMessage,
  normalizeAssistantMarkdown,
  patchToolStep,
  processSummary,
  shiftQueue,
  toolStatusLabel,
} from '../../../pages/super-assistant/components/chatTranscript.ts'

describe('chatTranscript', () => {
  it('strips a wrapping markdown fence that dominates the reply', () => {
    const source = '```markdown\n### 标题\n\n正文\n```\n'
    assert.equal(normalizeAssistantMarkdown(source), '### 标题\n\n正文')
  })

  it('summarizes tool process without inventing counts', () => {
    assert.equal(processSummary([]), '已思考')
    assert.equal(processSummary([], 3), '已思考 · 3 轮')
    assert.equal(processSummary([{ toolName: 'web_search', status: 'success' }]), '1 个工具')
    assert.equal(processSummary([
      { toolName: 'web_search', status: 'success' },
      { toolName: 'memory_search', status: 'success' },
    ]), '2 个工具')
  })

  it('patches parallel tool results by toolRunId instead of always the last step', () => {
    const started = appendToolStart(
      appendToolStart([], { toolName: 'alpha', toolRunId: 'run-a' }),
      { toolName: 'beta', toolRunId: 'run-b' },
    )
    const patched = patchToolStep(started, { toolRunId: 'run-a', status: 'success', preview: 'a-ok' })
    assert.equal(patched[0].status, 'success')
    assert.equal(patched[0].preview, 'a-ok')
    assert.equal(patched[1].status, 'running')
    assert.equal(toolStatusLabel('awaiting_confirmation'), '待确认')
  })

  it('queues and drains follow-up prompts in FIFO order', () => {
    const queued = enqueueMessage(enqueueMessage([], '第一句'), ' 第二句 ')
    assert.deepEqual(queued, ['第一句', '第二句'])
    const first = shiftQueue(queued)
    assert.equal(first.next, '第一句')
    assert.deepEqual(first.rest, ['第二句'])
    const empty = shiftQueue([])
    assert.equal(empty.next, null)
    assert.deepEqual(empty.rest, [])
  })
})
