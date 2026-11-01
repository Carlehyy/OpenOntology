import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  structureProjectionStatusLabel,
  structureProjectionSummary,
  structureProjectionTone,
} from '../../../pages/ontology-model/network/structureProjectionModel.ts'

describe('structureProjectionStatusLabel (structure state, not instance projection)', () => {
  it('maps every contract enum and never relabels failure as ready', () => {
    assert.equal(structureProjectionStatusLabel('empty'), '未构建')
    assert.equal(structureProjectionStatusLabel('building'), '构建中')
    assert.equal(structureProjectionStatusLabel('ready'), '已就绪')
    assert.equal(structureProjectionStatusLabel('stale'), '已漂移')
    assert.equal(structureProjectionStatusLabel('failed'), '构建失败')
    assert.equal(structureProjectionStatusLabel('unavailable'), '暂不可用')
  })

  it('passes unknown status values through verbatim (no invented fallback)', () => {
    assert.equal(structureProjectionStatusLabel('mystery'), 'mystery')
  })
})

describe('structureProjectionTone', () => {
  it('aligns semantic tones: ready=success, stale=warning, failed=danger', () => {
    assert.equal(structureProjectionTone('ready'), 'success')
    assert.equal(structureProjectionTone('building'), 'info')
    assert.equal(structureProjectionTone('stale'), 'warning')
    assert.equal(structureProjectionTone('unavailable'), 'warning')
    assert.equal(structureProjectionTone('failed'), 'danger')
    assert.equal(structureProjectionTone('empty'), 'muted')
    assert.equal(structureProjectionTone('mystery'), 'muted')
  })
})

describe('structureProjectionSummary (server facts only, no fabrication)', () => {
  it('joins release version, counts and drift into one honest line', () => {
    const line = structureProjectionSummary({
      status: 'stale', releaseId: 'rel-1', version: 'v3', snapshotHash: 'h1',
      drift: true, error: null, nodeCount: 12, edgeCount: 9,
    })
    assert.match(line, /v3/)
    assert.match(line, /12 节点 \/ 9 边/)
    assert.match(line, /漂移/)
    assert.ok(!line.includes('null'))
  })

  it('surfaces the server error verbatim and omits missing optional parts', () => {
    const line = structureProjectionSummary({
      status: 'failed', releaseId: null, version: null, snapshotHash: null,
      drift: false, error: 'Neo4j 写入中断', nodeCount: 0, edgeCount: 0,
    })
    assert.match(line, /Neo4j 写入中断/)
    assert.ok(!line.includes('发布'))
  })
})
describe('unavailable (read-side only, never fake ready)', () => {
  it('surfaces the server reason verbatim with counts as served, no ready wording', () => {
    const line = structureProjectionSummary({
      status: 'unavailable', releaseId: 'rel-1', version: 'v2', snapshotHash: 'h1',
      drift: false, error: 'Neo4j 不可达', nodeCount: 0, edgeCount: 0,
    })
    assert.match(line, /v2/)
    assert.match(line, /Neo4j 不可达/)
    assert.ok(!line.includes('已就绪'))
  })
})
