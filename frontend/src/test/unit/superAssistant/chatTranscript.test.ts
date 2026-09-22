import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  appendToolStart,
  BUILTIN_TOOL_FRIENDLY_NAMES,
  enqueueMessage,
  groupConsecutiveSteps,
  mergeServerMessages,
  normalizeAssistantMarkdown,
  patchToolStep,
  plainSnippet,
  processSummary,
  sameConversationList,
  sameMessageList,
  shiftQueue,
  splitProcessAndAnswer,
  toolGroupStatus,
  toolStatusLabel,
} from '../../../pages/super-assistant/components/chatTranscript.ts'
import type { SuperMessage } from '../../../api/superAssistant.ts'

const makeMessage = (overrides: Partial<SuperMessage>): SuperMessage => ({
  id: 'm-1',
  conversation_id: 'c-1',
  role: 'assistant',
  content: '',
  status: 'complete',
  steps: [],
  token_usage: {},
  created_at: '2026-09-22T00:00:00Z',
  ...overrides,
})

describe('chatTranscript', () => {
  it('strips a wrapping markdown fence that dominates the reply', () => {
    const source = '```markdown\n### 标题\n\n正文\n```\n'
    assert.equal(normalizeAssistantMarkdown(source), '### 标题\n\n正文')
  })

  it('summarizes tool process without inventing counts', () => {
    assert.equal(processSummary([]), '已思考')
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

  it('folds pre-heading agent chatter and keeps the report as the answer', () => {
    const source = [
      '好的，我来打开 B 站首页并抓取网络请求。页面已经打开。信息已经足够了。',
      '',
      '# B 站首页推荐视频的接口来源',
      '',
      '打开首页后定位到推荐位接口。',
    ].join('\n')
    const split = splitProcessAndAnswer(source)
    assert.match(split.process, /我来打开 B 站首页/)
    assert.match(split.answer, /^# B 站首页推荐视频的接口来源/)
    assert.equal(split.answer.includes('我来打开'), false)
  })

  it('does not split when the reply already starts with a heading', () => {
    const source = '# 结论\n\n正文'
    assert.deepEqual(splitProcessAndAnswer(source), { process: '', answer: source })
  })

  it('ignores headings inside fenced code', () => {
    const source = '过程说明。\n\n```md\n# 不是结论\n```\n\n# 真正结论\n\n正文'
    const split = splitProcessAndAnswer(source)
    assert.equal(split.process, '过程说明。\n\n```md\n# 不是结论\n```')
    assert.equal(split.answer, '# 真正结论\n\n正文')
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

  it('adopts server messages verbatim when the page is not streaming', () => {
    const server = [
      makeMessage({ id: 'u-1', role: 'user', content: '你好' }),
      makeMessage({ id: 'a-1', content: '你好！', status: 'complete' }),
    ]
    const merged = mergeServerMessages(server, undefined)
    assert.ok(merged)
    assert.equal(merged.boundMessageId, null)
    assert.deepEqual(merged.messages, server)
  })

  it('overlays the local stream buffer onto the persisted streaming placeholder', () => {
    const server = [
      makeMessage({ id: 'u-1', role: 'user', content: '你好' }),
      makeMessage({ id: 'a-real', status: 'streaming' }),
    ]
    const overlay = {
      messageId: 'assistant-temp',
      content: '已生成的部分',
      steps: [{ toolName: 'web_search', status: 'running' }],
      status: 'streaming' as const,
      tokenUsage: {},
      thinkingRound: null,
    }
    const merged = mergeServerMessages(server, overlay)
    assert.ok(merged)
    assert.equal(merged.boundMessageId, 'a-real')
    assert.equal(merged.messages[1].id, 'a-real')
    assert.equal(merged.messages[1].content, '已生成的部分')
    assert.equal(merged.messages[1].steps.length, 1)
    assert.equal(merged.messages[0].content, '你好')
  })

  it('keeps the local optimistic view when the placeholder is not persisted yet', () => {
    const server = [makeMessage({ id: 'u-1', role: 'user', content: '你好' })]
    const overlay = {
      messageId: 'assistant-temp',
      content: '',
      steps: [],
      status: 'streaming' as const,
      tokenUsage: {},
      thinkingRound: null,
    }
    assert.equal(mergeServerMessages(server, overlay), null)
  })

  it('treats equivalent message lists as unchanged to skip re-render', () => {
    const left = [
      makeMessage({ id: 'u-1', role: 'user', content: '你好' }),
      makeMessage({ id: 'a-1', content: '回复', steps: [{ toolName: 't', status: 'success' }] }),
    ]
    const right = left.map(item => ({ ...item }))
    assert.equal(sameMessageList(left, right), true)
    assert.equal(sameMessageList(left, []), false)
    assert.equal(sameMessageList(left, [right[0], { ...right[1], content: '变了' }]), false)
    assert.equal(sameMessageList(left, [right[0], { ...right[1], status: 'streaming' }]), false)
  })

  it('detects conversation list changes for cross-client sync', () => {
    const base = { id: 'c-1', title: '新会话', status: 'active', updated_at: '2026-09-22T00:00:00Z' }
    assert.equal(sameConversationList([base], [{ ...base }]), true)
    assert.equal(sameConversationList([base], [{ ...base, title: '你好，同步自测' }]), false)
    assert.equal(sameConversationList([base], [{ ...base, updated_at: '2026-09-22T00:01:00Z' }]), false)
    assert.equal(sameConversationList([], [base]), false)
  })

  it('folds only consecutive same-name tool steps into groups', () => {
    const steps = [
      { toolName: 'todo_write', status: 'success' },
      { toolName: 'browser_network_requests', status: 'success' },
      { toolName: 'browser_network_requests', status: 'success' },
      { toolName: 'browser_network_requests', status: 'success' },
      { toolName: 'todo_write', status: 'success' },
    ]
    const groups = groupConsecutiveSteps(steps)
    assert.deepEqual(groups.map(group => [group.toolName, group.steps.length]), [
      ['todo_write', 1],
      ['browser_network_requests', 3],
      ['todo_write', 1],
    ])
    assert.equal(groups[0].startIndex, 0)
    assert.equal(groups[1].startIndex, 1)
    assert.equal(groups[2].startIndex, 4)
  })

  it('aggregates group status with awaiting/running outranking errors', () => {
    assert.equal(toolGroupStatus([{ toolName: 't', status: 'success' }]), 'success')
    assert.equal(toolGroupStatus([
      { toolName: 't', status: 'success' },
      { toolName: 't', status: 'error' },
    ]), 'error')
    assert.equal(toolGroupStatus([
      { toolName: 't', status: 'success' },
      { toolName: 't', status: 'running' },
    ]), 'running')
    assert.equal(toolGroupStatus([
      { toolName: 't', status: 'cancelled' },
      { toolName: 't', status: 'awaiting_confirmation' },
    ]), 'awaiting_confirmation')
    assert.equal(toolGroupStatus([
      { toolName: 't', status: 'success' },
      { toolName: 't', status: 'cancelled' },
    ]), 'cancelled')
  })

  it('strips markdown syntax in search snippets and windows around the keyword', () => {
    const raw = '好的，我来抓取请求。```json\n{"code":0}\n```\n# 滚动到底部后的接口确认\n浏览器已经实际**滚动到底部**并触发了 `x/web-show/res/locs` 接口。'
    const snippet = plainSnippet(raw, '滚动到底部')
    assert.equal(snippet.includes('```'), false)
    assert.equal(snippet.includes('# 滚动'), false)
    assert.equal(snippet.includes('**'), false)
    assert.equal(snippet.includes('`x/web-show'), false)
    assert.ok(snippet.includes('滚动到底部'))
    // 命中词两侧超出窗口半径时带省略号，窗口内保留关键词本体
    const windowed = plainSnippet(`${'前'.repeat(60)}命中词${'后'.repeat(60)}`, '命中词')
    assert.ok(windowed.startsWith('…'))
    assert.ok(windowed.endsWith('…'))
    assert.ok(windowed.includes('命中词'))
    // 关键词未命中：退化为截断展示，语法符号仍被剥离
    const fallback = plainSnippet(raw, '不存在的关键词')
    assert.equal(fallback.includes('**'), false)
    assert.ok(fallback.endsWith('…'))
    assert.ok(fallback.length <= 65)
    // 不把 snake_case 的下划线当强调剥掉
    assert.equal(plainSnippet('调用了 browser_network_requests 接口', '接口').includes('browser_network_requests'), true)
  })
})

describe('BUILTIN_TOOL_FRIENDLY_NAMES', () => {
  it('仅覆盖内置工具：已知名命中、MCP/未知名不命中', () => {
    assert.equal(BUILTIN_TOOL_FRIENDLY_NAMES.browser_navigate, '打开网页')
    assert.equal(BUILTIN_TOOL_FRIENDLY_NAMES.web_fetch, '抓取网页')
    assert.equal(BUILTIN_TOOL_FRIENDLY_NAMES.todo_write, '写入步骤清单')
    // MCP 工具与未知名一律不翻译（直出原名）
    assert.equal(BUILTIN_TOOL_FRIENDLY_NAMES['mcp__platform_api_hub__create_interface'], undefined)
    assert.equal(BUILTIN_TOOL_FRIENDLY_NAMES.unknown_tool, undefined)
  })
})
