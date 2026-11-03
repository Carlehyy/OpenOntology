import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  AgentProtocolError,
  parseAgentQueryViewResult,
  parseAgentRunSnapshot,
  parseAgentRunStreamEvent,
} from '../../../api/agentProtocol.ts'

const snapshot = {
  runId: 'run-1',
  ontologyId: 'ontology-1',
  releaseId: 'release-1',
  conversationId: 'conv-1',
  status: 'running',
  question: '查询订单',
  content: '',
  steps: [{
    tool: 'query_objects',
    arguments: {},
    summary: '查询订单',
    durationMs: 8,
    result: { rows: [] },
  }],
  citations: [],
  proposals: [],
  lastSeq: 3,
  error: null,
  startedAt: '2026-11-03T08:00:00Z',
  finishedAt: null,
  retryOf: null,
}

describe('run snapshot protocol', () => {
  it('严格解析 camelCase 快照', () => {
    const parsed = parseAgentRunSnapshot(snapshot)
    assert.equal(parsed.runId, 'run-1')
    assert.equal(parsed.status, 'running')
    assert.equal(parsed.steps[0].tool, 'query_objects')
    assert.equal(parsed.lastSeq, 3)
  })

  it('未知 status / snake_case 字段直接报错，不回落 unknown', () => {
    assert.throws(
      () => parseAgentRunSnapshot({ ...snapshot, status: 'done' }),
      AgentProtocolError,
    )
    assert.throws(
      () => parseAgentRunSnapshot({ ...snapshot, run_id: 'run-1', runId: undefined }),
      AgentProtocolError,
    )
  })

  it('SSE 首帧只认 {type:snapshot,snapshot:{...}}；平铺旧形状报错', () => {
    const event = parseAgentRunStreamEvent({ type: 'snapshot', seq: 3, snapshot })
    assert.equal(event.type, 'snapshot')
    assert.throws(
      () => parseAgentRunStreamEvent({ type: 'snapshot', ...snapshot }),
      AgentProtocolError,
    )
  })

  it('delta reset 与 step 事件按固定字段解析', () => {
    const delta = parseAgentRunStreamEvent({
      type: 'delta', seq: 4, content: '订单 18 条', reset: true,
    })
    assert.equal(delta.type, 'delta')
    if (delta.type === 'delta') {
      assert.equal(delta.content, '订单 18 条')
      assert.equal(delta.reset, true)
    }
    const step = parseAgentRunStreamEvent({
      type: 'step',
      seq: 5,
      tool: 'query_objects',
      arguments: {},
      summary: '查询订单',
      durationMs: 8,
    })
    assert.equal(step.type, 'step')
  })

  it('心跳事件可识别，未知事件类型报错', () => {
    assert.equal(parseAgentRunStreamEvent({ type: 'heartbeat' }).type, 'heartbeat')
    assert.throws(
      () => parseAgentRunStreamEvent({ type: 'mystery', seq: 1 }),
      AgentProtocolError,
    )
  })
})

describe('query-view protocol', () => {
  it('解析 columns name/output 与 readable rows，且只认 output 取值', () => {
    const parsed = parseAgentQueryViewResult({
      rows: [{ c0: 'SO-1', c1: 10 }],
      columns: [
        { name: '订单号', output: 'c0', type: 'string', nullable: false },
        { name: '金额', output: 'c1', type: 'number', nullable: true },
      ],
      pageSize: 50,
      hasNext: false,
      cursor: null,
      totalRowCount: 1,
      rowCounting: 'exact',
      observedAt: '2026-11-03T08:00:00Z',
      resolvedWindow: { start: '2026-10-01', end: '2026-10-31' },
      consistency: { sameSnapshot: false, queryFresh: true, mode: 'fresh' },
      querySpec: { shape: 'objects', query: {} },
      querySpecHash: 'hash-1',
      dataMode: 'remote',
      releaseId: 'release-1',
      drilldowns: null,
    })
    assert.equal(parsed.columns[0].key, 'c0')
    assert.equal(parsed.columns[0].label, '订单号')
    assert.equal(parsed.rows[0].c0, 'SO-1')
  })

  it('缺 output / hasNext 非布尔值报错', () => {
    const base = {
      rows: [],
      columns: [{ name: '订单号' }],
      hasNext: false,
      pageSize: 50,
      totalRowCount: 0,
    }
    assert.throws(() => parseAgentQueryViewResult(base), AgentProtocolError)
    assert.throws(
      () => parseAgentQueryViewResult({
        ...base,
        columns: [{ name: '订单号', output: 'c0' }],
        hasNext: 'no',
      }),
      AgentProtocolError,
    )
  })
})
