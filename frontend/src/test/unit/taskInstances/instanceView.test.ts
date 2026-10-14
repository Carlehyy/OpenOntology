import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import type { NodeRunView } from '../../../api/taskInstances.ts'
import {
  ATTENTION_REASON_LABELS,
  INSTANCE_STATUS_TONE,
  NODE_STATUS_TONE,
  attentionReasonOf,
  buildGraph,
  dialogNavOf,
  humanizeFailReason,
  instanceCardTimeLabel,
  instanceGroup,
  isTerminalStatus,
  lineDiff,
  summarizeProposal,
  waitingNodesOf,
} from '../../../components/task-instances/instanceView.ts'

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

  describe('isTerminalStatus', () => {
    it('completed/failed/cancelled are terminal', () => {
      assert.equal(isTerminalStatus('completed'), true)
      assert.equal(isTerminalStatus('failed'), true)
      assert.equal(isTerminalStatus('cancelled'), true)
    })
    it('active/cancelling are not terminal', () => {
      assert.equal(isTerminalStatus('active'), false)
      assert.equal(isTerminalStatus('cancelling'), false)
    })
  })

  describe('dialogNavOf', () => {
    it('board and instance detail belong to instances nav', () => {
      assert.equal(dialogNavOf('board'), 'instances')
      assert.equal(dialogNavOf('instance'), 'instances')
    })
    it('templates, activate form and canvas editor belong to templates nav', () => {
      assert.equal(dialogNavOf('templates'), 'templates')
      assert.equal(dialogNavOf('activate'), 'templates')
      assert.equal(dialogNavOf('canvas'), 'templates')
    })
  })

  describe('状态徽章令牌（P0-1 回归）', () => {
    it('所有语义浅底令牌使用 --color-*-bg 正序名', () => {
      const tones = [...Object.values(NODE_STATUS_TONE), ...Object.values(INSTANCE_STATUS_TONE)]
      for (const tone of tones) {
        assert.match(tone, /--color-(info|warning|success|danger)-bg|--color-bg-(hover|sidebar|elevated)/,
          `令牌名疑似反序：${tone}`)
        assert.doesNotMatch(tone, /--color-bg-(info|warning|success|danger)\b/,
          `使用了不存在的反序令牌：${tone}`)
      }
    })
    it('关键状态映射到正确的浅底令牌', () => {
      assert.equal(NODE_STATUS_TONE.failed, 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]')
      assert.equal(NODE_STATUS_TONE.waiting_approval, 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]')
      assert.equal(INSTANCE_STATUS_TONE.active, 'bg-[var(--color-info-bg)] text-[var(--color-text-primary)]')
    })
  })

  describe('attentionReasonOf', () => {
    it('等待审批优先于等待人工', () => {
      assert.equal(attentionReasonOf([{ status: 'waiting_approval' }, { status: 'waiting_human' }]), 'approval')
      assert.equal(attentionReasonOf([{ status: 'waiting_human' }]), 'human')
    })
    it('无等待节点或空列表返回 null', () => {
      assert.equal(attentionReasonOf([{ status: 'running' }]), null)
      assert.equal(attentionReasonOf([]), null)
      assert.equal(attentionReasonOf(null), null)
    })
    it('标签文案为待审批/待人工', () => {
      assert.equal(ATTENTION_REASON_LABELS.approval, '待审批')
      assert.equal(ATTENTION_REASON_LABELS.human, '待人工')
    })
  })

  describe('instanceCardTimeLabel', () => {
    const now = new Date(2026, 9, 12, 12, 0)
    it('运行中实例显示创建于 + 已运行', () => {
      const label = instanceCardTimeLabel({
        status: 'active',
        created_at: new Date(2026, 9, 12, 9, 0).toISOString(),
        finished_at: null,
      }, now)
      assert.equal(label, '创建于 3 小时前 · 已运行 3 小时')
    })
    it('终态实例显示创建于 + 耗时', () => {
      const label = instanceCardTimeLabel({
        status: 'completed',
        created_at: new Date(2026, 9, 12, 10, 0).toISOString(),
        finished_at: new Date(2026, 9, 12, 10, 12).toISOString(),
      }, now)
      assert.equal(label, '创建于 2 小时前 · 耗时 12 分钟')
    })
    it('无时间字段时降级为空串', () => {
      assert.equal(instanceCardTimeLabel({ status: 'failed', created_at: null, finished_at: null }, now), '')
    })
    it('created_at 缺失时不拼出「已运行 —」残缺文案（P1 对抗修复回归）', () => {
      assert.equal(instanceCardTimeLabel({ status: 'active', created_at: null, finished_at: null }, now), '')
      assert.equal(instanceCardTimeLabel({ status: 'completed', created_at: null, finished_at: new Date().toISOString() }, now), '')
    })
  })

  describe('humanizeFailReason', () => {
    it('容器/runner 异常 → 执行环境异常退出', () => {
      assert.equal(
        humanizeFailReason("node:outline:容器执行失败: runner 异常退出 code=None output=''"),
        '执行环境异常退出，任务未能完成')
    })
    it('超时/契约/取消分别映射', () => {
      assert.equal(humanizeFailReason('agent 执行超时（120s）'), '执行超时，任务未能完成')
      assert.equal(humanizeFailReason('contract violated: summary 缺失'), '产出未通过契约校验')
      assert.equal(humanizeFailReason('用户主动取消'), '任务被取消')
    })
    it('未知错误给通用兜底，空值给空串', () => {
      assert.equal(humanizeFailReason('不知所云的错误'), '任务执行失败')
      assert.equal(humanizeFailReason(null), '')
      assert.equal(humanizeFailReason(''), '')
    })
  })

  describe('summarizeProposal', () => {
    it('提取目标、上游产出摘要与产物清单', () => {
      const summary = summarizeProposal({
        taskContext: { goal: '生成发布说明', inputs: {} },
        inputs: {
          draft: {
            summary: '完成',
            artifacts: [
              { name: 'note1.txt', mime_type: 'text/plain' },
              { name: 'note2.txt', mime_type: 'text/plain' },
            ],
          },
        },
      })
      assert.equal(summary?.goal, '生成发布说明')
      assert.deepEqual(summary?.sections, [{ label: 'draft', text: '完成' }])
      assert.deepEqual(summary?.artifacts, [
        { name: 'note1.txt', mime: 'text/plain' },
        { name: 'note2.txt', mime: 'text/plain' },
      ])
    })
    it('仅有 inputs 摘要时 goal 可为 null', () => {
      const summary = summarizeProposal({ inputs: { analyze: { summary: '涉及登录页' } } })
      assert.equal(summary?.goal, null)
      assert.deepEqual(summary?.sections, [{ label: 'analyze', text: '涉及登录页' }])
    })
    it('无可提取字段返回 null（调用方降级为仅原始 JSON）', () => {
      assert.equal(summarizeProposal({}), null)
      assert.equal(summarizeProposal(null), null)
      assert.equal(summarizeProposal({ unknown: 1 }), null)
    })
  })
})
