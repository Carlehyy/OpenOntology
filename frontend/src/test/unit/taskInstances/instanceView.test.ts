import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import type { NodeRunView } from '../../../api/taskInstances.ts'
import {
  buildGraph,
  instanceGroup,
  lineDiff,
  waitingNodesOf,
} from '../../../pages/task-instances/instanceView.ts'

const run = (nodeId: string, status: string): NodeRunView => ({
  node_run_id: `r-${nodeId}`, node_id: nodeId, attempt_no: 1, status,
  waiting: status === 'waiting_human' || status === 'waiting_approval',
  error: null, correction_count: 0, rework_count: 0, output: null,
  dispatched_at: null, started_at: null, finished_at: null, created_at: null,
})

describe('task instances instanceView', () => {
  describe('instanceGroup', () => {
    it('routes waiting-human actives to attention group', () => {
      assert.equal(instanceGroup({ status: 'active', needs_attention: true }), 'attention')
      assert.equal(instanceGroup({ status: 'active', needs_attention: false }), 'running')
      assert.equal(instanceGroup({ status: 'cancelling', needs_attention: true }), 'running')
      assert.equal(instanceGroup({ status: 'completed', needs_attention: false }), 'done')
      assert.equal(instanceGroup({ status: 'failed', needs_attention: false }), 'stopped')
      assert.equal(instanceGroup({ status: 'cancelled', needs_attention: false }), 'stopped')
    })
    it('terminal states never land in attention even if flagged', () => {
      assert.equal(instanceGroup({ status: 'completed', needs_attention: true }), 'done')
    })
  })

  describe('buildGraph', () => {
    const spec = {
      api_version: 'openontology.task/v1',
      kind: 'Workflow',
      nodes: {
        analyze: { kind: 'agent' },
        gate: { kind: 'approval' },
        end_ok: { kind: 'terminal' },
      },
      edges: [
        { from: 'analyze.done', to: 'gate' },
        { from: 'gate.rejected', to: 'analyze', rework: true },
        { from: 'gate.approved', to: 'end_ok' },
      ],
    }
    it('builds nodes and normalized edges from spec snapshot', () => {
      const graph = buildGraph(spec, [
        run('analyze', 'completed'), run('gate', 'waiting_approval')])
      assert.deepEqual(graph.nodes.map(node => node.id),
        ['analyze', 'gate', 'end_ok'])
      assert.equal(graph.nodes[0].status, 'completed')
      assert.equal(graph.nodes[1].status, 'waiting_approval')
      assert.equal(graph.nodes[2].status, undefined)
      assert.deepEqual(graph.edges.map(edge => edge.rework),
        [false, true, false])
      assert.deepEqual(graph.edges.map(edge => edge.source),
        ['analyze', 'gate', 'gate'])
    })
    it('handles null spec', () => {
      assert.deepEqual(buildGraph(null), { nodes: [], edges: [] })
    })
  })

  describe('waitingNodesOf', () => {
    it('splits waiting human and approval nodes', () => {
      const instance = {
        id: 'i', name: 'n', goal: 'g', status: 'active', needs_attention: true,
        template_revision_id: 'r', created_by: null, created_at: null,
        finished_at: null, fail_reason: null, cancel_reason: null,
        nodes: [run('review', 'waiting_human'), run('gate', 'waiting_approval'),
          run('agent', 'running')],
        approvals: [], artifacts: [], spec_snapshot: null,
      }
      const waiting = waitingNodesOf(instance)
      assert.deepEqual(waiting.human.map(node => node.node_id), ['review'])
      assert.deepEqual(waiting.approval.map(node => node.node_id), ['gate'])
    })
  })

  describe('lineDiff', () => {
    it('marks insertions and deletions', () => {
      const diff = lineDiff('a\nb\nc', 'a\nx\nc\nd')
      const signs = diff.filter(line => line.sign).map(line => line.sign + line.text)
      assert.deepEqual(signs, ['-b', '+x', '+d'])
    })
    it('identical text yields no signed lines', () => {
      const diff = lineDiff('a\nb', 'a\nb')
      assert.equal(diff.filter(line => line.sign).length, 0)
    })
  })
})
