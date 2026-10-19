/**
 * kernel run 视图的展示文案派生：call 状态、审批摘要行。
 *
 * 关键语义（YIYA-36）：confirmation.state=unknown → 「结果待核实」、
 * compensation_required → 「需人工处理」——如实呈现保守终态，不伪完成，
 * 也不把这类 call 文案化为「等待外部助手」。本模块为纯函数，
 * 被 Node --experimental-strip-types 单测直接执行（相对路径 import）。
 */
import type { KernelApprovalSummary, KernelRunCallSummary } from '@/api/superAssistant'
import { confirmationStateText } from '../../../components/ontologyConfirmationModel.ts'

export type CallStatusTone = 'success' | 'warning' | 'danger' | 'info' | 'muted'

export interface CallStatusView {
  label: string
  tone: CallStatusTone
}

const settledFailureStates = new Set([
  'failed_not_executed', 'approval_rejected', 'denied', 'cancelled', 'expired',
])

/** 单个 call 的用户可见状态：服务端确认状态优先（更具体），无确认时按 manual_attention/outcome/status */
export function callStatusView(call: KernelRunCallSummary): CallStatusView {
  const state = call.confirmation?.state || null
  if (state === 'compensation_required') return { label: confirmationStateText(state), tone: 'danger' }
  if (state === 'unknown' || state === 'recovering') return { label: confirmationStateText(state), tone: 'warning' }
  if (state === 'succeeded') return { label: '已执行成功', tone: 'success' }
  if (state === 'pending_approval') return { label: '等待审批', tone: 'info' }
  if (state && settledFailureStates.has(state)) return { label: confirmationStateText(state), tone: 'muted' }
  if (state === 'prepared' || state === 'confirmed' || state === 'dispatch_queued' || state === 'executing') {
    return { label: '执行中', tone: 'info' }
  }
  // 无确认对象的 call：服务端保守标记与 outcome_unknown 是「结果待核实」的直接证据
  if (call.manual_attention || call.outcome === 'outcome_unknown') return { label: '结果待核实', tone: 'warning' }
  if (call.status === 'closed' && call.outcome === 'completed') return { label: '已完成', tone: 'success' }
  if (call.status === 'closed') return { label: '未执行', tone: 'muted' }
  if (call.status === 'reconciling') return { label: '结果待核实', tone: 'warning' }
  if (call.status === 'waiting_external') return { label: '等待外部执行', tone: 'info' }
  if (call.status === 'running' || call.status === 'dispatched') return { label: '执行中', tone: 'info' }
  if (call.status === 'cancel_requested') return { label: '取消中', tone: 'muted' }
  if (call.status === 'offered') return { label: '准备中', tone: 'info' }
  return { label: call.status, tone: 'muted' }
}

/** call 行标题：动作确认给用户可读动作名，其余以 capability key 作技术名 */
export function callTitle(call: KernelRunCallSummary): string {
  const actionName = call.confirmation?.display?.action?.name
  if (actionName) return actionName
  return call.capability_key
}

/** call 行副文本：目标 + 发布版本（来自服务端 display；无则不显示内部 ID） */
export function callSubtitle(call: KernelRunCallSummary): string {
  const display = call.confirmation?.display
  if (!display) return ''
  const parts: string[] = []
  if (display.target?.label) parts.push(display.target.label)
  if (display.ontology?.name && display.ontology.name !== display.target?.label) parts.push(display.ontology.name)
  if (display.release?.version) parts.push(`v${display.release.version}`)
  return parts.filter(Boolean).join(' · ')
}

export interface ApprovalSummaryView {
  /** 摘要行：目标 / 范围（服务端持久文本，如实展示） */
  lines: Array<{ label: string; text: string }>
  /** 参数 JSON 文本（服务端 parameter_summary） */
  parameterText: string
  hasDetail: boolean
}

/** 审批摘要视图：旧负载（无 approval 对象）返回 hasDetail=false，仅显示占位 */
export function approvalSummaryView(approval: KernelApprovalSummary | null | undefined): ApprovalSummaryView {
  if (!approval) return { lines: [], parameterText: '', hasDetail: false }
  const lines: Array<{ label: string; text: string }> = []
  if (approval.target_summary) lines.push({ label: '目标', text: approval.target_summary })
  if (approval.scope_summary) lines.push({ label: '范围', text: approval.scope_summary })
  return {
    lines,
    parameterText: approval.parameter_summary && approval.parameter_summary !== '{}' ? approval.parameter_summary : '',
    hasDetail: lines.length > 0 || Boolean(approval.parameter_summary && approval.parameter_summary !== '{}'),
  }
}
