import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import type { KernelRunCallSummary } from '../../../api/superAssistant.ts'
import {
  approvalSummaryView,
  callStatusView,
  callSubtitle,
  callTitle,
} from '../../../pages/super-assistant/components/kernelRunPresentation.ts'

const confirmation = (state: string) => ({
  confirmationId: `conf-${state}`,
  state,
  display: {
    ontology: { id: 'onto-1', name: '供应商域' },
    action: { id: 'action-1', name: '下调供应商等级' },
    target: { id: 'inst-1', label: 'SUP-0042 华南金属' },
    parameters: [],
    release: { id: 'rel-1', version: '7' },
    requiresApproval: true,
  },
  requiresApproval: true,
})

describe('kernel call status view', () => {
  it('renders unknown as 结果待核实 (warning), never as success or waiting-external', () => {
    const view = callStatusView({
      call_id: 'c1', status: 'reconciling', outcome: 'outcome_unknown',
      capability_key: 'ontology.action', confirmation: confirmation('unknown'),
    })
    assert.equal(view.label, '执行结果待核实')
    assert.equal(view.tone, 'warning')
  })

  it('renders compensation_required as 需人工处理 (danger)', () => {
    const view = callStatusView({
      call_id: 'c1', status: 'reconciling', outcome: 'outcome_unknown',
      capability_key: 'ontology.action', confirmation: confirmation('compensation_required'),
    })
    assert.equal(view.label, '外部效果可能已发生，需人工处理')
    assert.equal(view.tone, 'danger')
  })

  it('honors manual_attention / outcome_unknown even without a confirmation object', () => {
    const view = callStatusView({
      call_id: 'c2', status: 'reconciling', outcome: 'outcome_unknown',
      capability_key: 'remote.agent', manual_attention: true,
    })
    assert.equal(view.label, '结果待核实')
    assert.equal(view.tone, 'warning')
  })

  it('maps lifecycle states without masking them as waiting-external', () => {
    const cases: Array<[KernelRunCallSummary, string, string]> = [
      [{ call_id: 'x', status: 'closed', outcome: 'completed', capability_key: 'k', confirmation: confirmation('succeeded') }, '已执行成功', 'success'],
      [{ call_id: 'x', status: 'waiting_external', outcome: 'remote_running', capability_key: 'k', confirmation: confirmation('executing') }, '执行中', 'info'],
      [{ call_id: 'x', status: 'waiting_external', outcome: 'remote_running', capability_key: 'k', confirmation: confirmation('pending_approval') }, '等待审批', 'info'],
      [{ call_id: 'x', status: 'closed', outcome: 'failed', capability_key: 'k', confirmation: confirmation('failed_not_executed') }, '未执行（可安全重试）', 'muted'],
      [{ call_id: 'x', status: 'closed', outcome: 'completed', capability_key: 'k' }, '已完成', 'success'],
      [{ call_id: 'x', status: 'waiting_external', outcome: 'remote_running', capability_key: 'k' }, '等待外部执行', 'info'],
    ]
    for (const [call, label, tone] of cases) {
      const view = callStatusView(call)
      assert.equal(view.label, label, `${call.capability_key}/${call.confirmation?.state ?? call.status}`)
      assert.equal(view.tone, tone, label)
    }
  })
})

describe('kernel call title and subtitle', () => {
  it('prefers the readable action name from server display', () => {
    const call = { call_id: 'c', status: 'closed', outcome: 'completed', capability_key: 'internal.cap.uuid', confirmation: confirmation('succeeded') }
    assert.equal(callTitle(call), '下调供应商等级')
    assert.equal(callSubtitle(call), 'SUP-0042 华南金属 · 供应商域 · v7')
  })

  it('falls back to capability key and hides ids when no confirmation exists', () => {
    const call: KernelRunCallSummary = { call_id: 'c', status: 'running', outcome: 'accepted', capability_key: 'remote.demo' }
    assert.equal(callTitle(call), 'remote.demo')
    assert.equal(callSubtitle(call), '')
  })
})

describe('kernel approval summary view', () => {
  it('exposes server-persisted target/parameter/scope summaries', () => {
    const view = approvalSummaryView({
      approval_id: 'ap-1', status: 'pending', call_id: 'call-1',
      target_summary: '供应商 SUP-0042', parameter_summary: '{"level":"C"}',
      scope_summary: '供应商域 v7 写权限', expires_at: '2026-10-19T00:00:00Z', decided_at: null,
    })
    assert.equal(view.hasDetail, true)
    assert.deepEqual(view.lines, [
      { label: '目标', text: '供应商 SUP-0042' },
      { label: '范围', text: '供应商域 v7 写权限' },
    ])
    assert.equal(view.parameterText, '{"level":"C"}')
  })

  it('marks legacy payloads (no approval object) as hasDetail=false', () => {
    assert.equal(approvalSummaryView(null).hasDetail, false)
    assert.equal(approvalSummaryView(undefined).hasDetail, false)
  })

  it('hides empty parameter summaries', () => {
    const view = approvalSummaryView({
      approval_id: 'ap-2', status: 'pending', call_id: null,
      target_summary: '目标', parameter_summary: '{}', scope_summary: '',
      expires_at: null, decided_at: null,
    })
    assert.equal(view.parameterText, '')
    assert.deepEqual(view.lines, [{ label: '目标', text: '目标' }])
  })
})
