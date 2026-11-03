import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import type { AgentRunSnapshot } from '../../../api/agent.ts'
import {
  absorbSucceededRuns,
  bindRunConversation,
  createRunUiState,
  isRunActiveStatus,
  isRunRetryableStatus,
  latestActiveRun,
  latestRetryableRun,
  mergeOptimisticMessages,
  NEW_CONVERSATION_KEY,
  reduceRunStreamEvent,
  runDisplayContent,
  runConflictCode,
  runsForConversationKey,
  visibleRunsForConversationKey,
  withRunSnapshot,
  type RunUiState,
} from '../../../pages/agent/components/chatRunState.ts'

const snapshot = (overrides: Partial<AgentRunSnapshot> = {}): AgentRunSnapshot => ({
  runId: 'run-1',
  conversationId: 'conv-1',
  status: 'running',
  question: '查询订单数量',
  content: '',
  steps: [],
  citations: [],
  proposals: [],
  lastSeq: null,
  error: null,
  startedAt: '2026-11-03T08:00:00.000Z',
  finishedAt: null,
  retryOf: null,
  ...overrides,
})

const run = (overrides: Partial<AgentRunSnapshot> = {}, localKey?: string): RunUiState =>
  createRunUiState(snapshot(overrides), localKey ? { localKey } : {})

const delta = (seq: number, content: string, reset = false) => ({
  type: 'delta' as const,
  seq,
  content,
  reset,
})

describe('run stream reducer', () => {
  it('delta reset 丢弃上一轮模型调用的 preamble，answer 覆盖为权威文本', () => {
    let state = run()
    state = reduceRunStreamEvent(state, delta(1, '我先规划查询…'))
    state = reduceRunStreamEvent(state, delta(2, '订单共 18 条', true))
    state = reduceRunStreamEvent(state, delta(3, '，主键 order_no'))
    state = reduceRunStreamEvent(state, {
      type: 'answer',
      seq: 4,
      content: '当前共有 18 条订单。',
      citations: [],
      proposals: [],
    })
    state = reduceRunStreamEvent(state, { type: 'done', seq: 5, status: 'succeeded' })
    assert.equal(runDisplayContent(state), '当前共有 18 条订单。')
    assert.equal(state.snapshot.status, 'succeeded')
    assert.equal(state.lastSeq, 5)
    assert.match(state.snapshot.finishedAt ?? '', /^\d{4}-/)
  })

  it('首帧 snapshot(lastSeq=N) 之后重放的旧 delta 不再追加到正文', () => {
    let state = run()
    state = reduceRunStreamEvent(state, {
      type: 'snapshot',
      seq: 1,
      snapshot: snapshot({
        status: 'running',
        content: '快照已有正文',
        lastSeq: 10,
      }),
    })
    state = reduceRunStreamEvent(state, delta(3, '旧事件增量'))
    state = reduceRunStreamEvent(state, delta(11, '新事件增量'))
    assert.equal(runDisplayContent(state), '快照已有正文新事件增量')
    assert.equal(state.lastSeq, 11)
  })

  it('重复/乱序 seq 不重复追加步骤或内容', () => {
    let state = run()
    state = reduceRunStreamEvent(state, {
      type: 'step', seq: 2, step: {
        tool: 'query_objects', arguments: {}, summary: '查询订单', durationMs: 10,
      },
    })
    const afterFirst = state
    state = reduceRunStreamEvent(state, {
      type: 'step', seq: 2, step: {
        tool: 'query_objects', arguments: {}, summary: '重复', durationMs: 10,
      },
    })
    assert.equal(state, afterFirst)
    assert.equal(state.snapshot.steps.length, 1)
  })

  it('meta 回填 conversationId，快照中断保留已有进度并可重试', () => {
    let state = run({ conversationId: null })
    state = reduceRunStreamEvent(state, {
      type: 'step', seq: 1, step: {
        tool: 'query_objects', arguments: {}, summary: '查询订单', durationMs: 10,
      },
    })
    state = reduceRunStreamEvent(state, {
      type: 'delta', seq: 2, content: '已找到 18 条订单', reset: false,
    })
    state = reduceRunStreamEvent(state, {
      type: 'meta', seq: 3, conversationId: 'conv-9',
    })
    state = withRunSnapshot(state, snapshot({
      runId: 'run-1',
      conversationId: 'conv-9',
      status: 'interrupted',
      lastSeq: 4,
      steps: [],
      content: '',
    }), 4)
    assert.equal(state.snapshot.conversationId, 'conv-9')
    assert.equal(state.snapshot.status, 'interrupted')
    assert.equal(state.snapshot.steps.length, 1)
    assert.equal(runDisplayContent(state), '已找到 18 条订单')
    assert.equal(isRunRetryableStatus(state.snapshot.status), true)
  })

  it('服务端较旧 running 快照不得把本地终态降级回运行中', () => {
    const finished = run({ status: 'succeeded', content: '已完成', lastSeq: 10 })
    const next = withRunSnapshot(finished, snapshot({ status: 'running', lastSeq: 4 }))
    assert.equal(next.snapshot.status, 'succeeded')
    assert.equal(next.snapshot.content, '已完成')
  })
})

describe('conversation run isolation', () => {
  it('A→B→A：run 按 localKey 归属，互不串流', () => {
    const runA = run({ runId: 'run-a', conversationId: 'conv-a' })
    const runB = run({ runId: 'run-b', conversationId: 'conv-b' })
    const runs = { 'run-a': runA, 'run-b': runB }
    assert.deepEqual(runsForConversationKey(runs, 'conv-a').map(item => item.snapshot.runId), ['run-a'])
    assert.deepEqual(runsForConversationKey(runs, 'conv-b').map(item => item.snapshot.runId), ['run-b'])
    // A 的流式事件只更新 A；B 仍是 queued/running 原文
    const updatedA = reduceRunStreamEvent(runA, delta(1, 'A 的增量'))
    assert.equal(updatedA.draft, 'A 的增量')
    assert.equal(runB.draft, '')
  })

  it('新建草稿拿到 conversationId 后只迁移该 run 与其归属', () => {
    const pendingA = run({ runId: 'run-a', conversationId: null, question: '新建问题' }, NEW_CONVERSATION_KEY)
    const pendingB = run({ runId: 'run-b', conversationId: null, question: '另一个草稿' }, NEW_CONVERSATION_KEY)
    const bound = bindRunConversation(pendingA, 'conv-a')
    assert.equal(bound.localKey, 'conv-a')
    assert.equal(bound.snapshot.conversationId, 'conv-a')
    assert.equal(pendingB.localKey, NEW_CONVERSATION_KEY)
  })

  it('活跃/可重试选择互不覆盖', () => {
    const failed = run({ runId: 'run-old', status: 'interrupted', finishedAt: '2026-11-03T08:10:00Z' })
    const active = run({ runId: 'run-new', status: 'running' })
    assert.equal(latestActiveRun([failed, active])?.snapshot.runId, 'run-new')
    assert.equal(latestRetryableRun([failed, active])?.snapshot.runId, 'run-old')
    assert.equal(isRunActiveStatus('queued'), true)
  })
})

describe('persisted absorption', () => {
  const succeeded = run({
    status: 'succeeded',
    content: '当前共有 18 条订单。',
    lastSeq: 6,
  })

  it('会话详情已含同一回答时吸收 succeeded run', () => {
    const runs = { 'run-1': succeeded }
    const next = absorbSucceededRuns(runs, 'conv-1', [
      { role: 'user', content: '查询订单数量' },
      { role: 'assistant', content: '当前共有 18 条订单。' },
    ])
    assert.equal(next['run-1'].absorbed, true)
    assert.deepEqual(visibleRunsForConversationKey(next, 'conv-1'), [])
  })

  it('interrupted 不会被历史消息吸收（保留重试入口）', () => {
    const interrupted = run({ status: 'interrupted', content: '部分回答' })
    const runs = { 'run-1': interrupted }
    const next = absorbSucceededRuns(runs, 'conv-1', [
      { role: 'assistant', content: '部分回答' },
    ])
    assert.equal(next['run-1'].absorbed, false)
    assert.equal(visibleRunsForConversationKey(next, 'conv-1').length, 1)
  })

  it('已重试的旧 run 不再展示', () => {
    const runs = {
      'run-1': { ...run({ status: 'interrupted' }), retriedBy: 'run-2' },
      'run-2': run({ runId: 'run-2', status: 'running' }),
    }
    assert.deepEqual(
      visibleRunsForConversationKey(runs, 'conv-1').map(item => item.snapshot.runId),
      ['run-2'],
    )
  })
})

describe('optimistic message merge', () => {
  it('服务端同文提问优先，乐观消息去重', () => {
    const persisted = [
      { id: 'server-user', role: 'user', content: '查询订单' },
    ]
    const optimistic = [
      { id: 'optimistic-1', role: 'user', content: '查询订单' },
      { id: 'optimistic-2', role: 'user', content: '再查一次' },
    ]
    const merged = mergeOptimisticMessages(persisted, optimistic)
    assert.deepEqual(merged.map(item => item.id), ['server-user', 'optimistic-2'])
  })
})

describe('run conflict classification', () => {
  it('真实 409 shape：只看 detail.code，conversation_busy 才入队', () => {
    assert.equal(runConflictCode({
      status: 409,
      detail: { code: 'conversation_busy', message: '该会话已有正在执行或排队中的回合。' },
    }), 'conversation_busy')
    assert.equal(runConflictCode({
      status: 409,
      detail: { code: 'conversation_release_mismatch', message: '发布版本不一致' },
    }), 'conversation_release_mismatch')
    assert.equal(runConflictCode({ status: 409, detail: 'busy' }), null)
    assert.equal(runConflictCode(null), null)
  })
})
