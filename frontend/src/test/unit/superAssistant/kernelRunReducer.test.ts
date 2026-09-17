import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import type { KernelRunEvent, KernelRunView } from '../../../api/superAssistant.ts'
import { projectionFromSnapshot, reduceKernelRunEvent, type KernelRunProjection } from '../../../pages/super-assistant/components/kernelRunReducer.ts'

const snapshot: KernelRunView = {
  run_id: 'run-1', conversation_id: 'conv-1', status: 'active', wait_reason: null,
  version: 1, execution_version: 'kernel.v1', goal: '整理资料', deadline: null,
  binding_snapshot: {}, current_inbox: [], calls: [], artifacts: [],
}

const reduce = (events: KernelRunEvent[]): KernelRunProjection => events.reduce(reduceKernelRunEvent, projectionFromSnapshot(snapshot))

describe('kernel run event reducer', () => {
  it('projects calls, approvals, inbox and artifacts from event stream', () => {
    const result = reduce([
      { id: 'run-1:1', event: 'call.intent', data: { call_id: 'call-1', capability_key: 'remote.demo' } },
      { id: 'run-1:2', event: 'call.outcome_changed', data: { call_id: 'call-1', status: 'closed', outcome: 'completed' } },
      { id: 'run-1:3', event: 'approval.requested', data: { approval_id: 'approval-1', expires_at: '2026-09-13T01:00:00Z' } },
      { id: 'run-1:4', event: 'artifact.declared', data: { artifact_id: 'artifact-1', kind: 'report', mime_type: 'text/markdown', size: 2, checksum: 'abc' } },
      { id: 'run-1:5', event: 'artifact.completed', data: { artifact_id: 'artifact-1', checksum: 'def', business_status: 'success' } },
    ])
    assert.equal(result.run?.calls[0]?.status, 'closed')
    assert.equal(result.run?.calls[0]?.outcome, 'completed')
    assert.equal(result.run?.current_inbox[0]?.approval_id, 'approval-1')
    assert.equal(result.run?.artifacts[0]?.status, 'complete')
    assert.equal(result.run?.artifacts[0]?.checksum, 'def')
    assert.equal(result.lastSeq, 5)
  })

  it('drops duplicate and out-of-order events while preserving the latest projection', () => {
    const result = reduce([
      { id: 'run-1:2', event: 'run.status_changed', data: { from: 'active', to: 'waiting_input', reason: 'question', version: 2 } },
      { id: 'run-1:2', event: 'run.status_changed', data: { from: 'waiting_input', to: 'failed', reason: 'stale', version: 3 } },
      { id: 'run-1:1', event: 'run.status_changed', data: { from: 'active', to: 'failed', reason: 'stale', version: 9 } },
    ])
    assert.equal(result.run?.status, 'waiting_input')
    assert.equal(result.run?.version, 2)
    assert.equal(result.lastSeq, 2)
  })

  it('removes approval inbox entries after a decision or revocation', () => {
    const result = reduce([
      { id: 'run-1:1', event: 'approval.requested', data: { approval_id: 'approval-1' } },
      { id: 'run-1:2', event: 'approval.decided', data: { approval_id: 'approval-1', decision: 'approved' } },
    ])
    assert.deepEqual(result.run?.current_inbox, [])
  })
})
