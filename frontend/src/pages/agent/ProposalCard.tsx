/**
 * 动作提案卡片 — agent 只提案，人签字（YIYA-36 持久确认协议）。
 *
 * 展示 dry-run 预演结果（参数 + 模拟效果 + 校验错误）；「确认执行」先建立
 * 持久确认请求（prepare），再提交用户二次确认（approve），最后唤醒执行并
 * 展示真实业务状态 — requires_approval 的动作确认后进入 HITL 审批队列。
 */
import { useState } from 'react'
import {
  Play, ShieldCheck, CheckCircle2, XCircle, Clock3, Loader2, FlaskConical,
} from 'lucide-react'
import {
  agentApi, type AgentActionProposal, type AgentConfirmationState,
} from '@/api/agent'

export function ProposalCard({ oid, proposal, conversationId }: {
  oid: string
  proposal: AgentActionProposal
  conversationId: string | null
}) {
  const [executing, setExecuting] = useState(false)
  const [result, setResult] = useState<AgentConfirmationState | null>(null)
  const [error, setError] = useState('')

  const dryRunOk = proposal.status === 'success'
  const params = Object.entries(proposal.parameters || {})

  const execute = async () => {
    if (!conversationId) {
      setError('缺少会话上下文，无法建立确认请求')
      return
    }
    setExecuting(true)
    setError('')
    try {
      // 1) 持久准备（不可变载荷；重复提交回放同一确认请求）
      const prepared = await agentApi.prepareProposal(oid, {
        sessionKind: 'ontology_agent',
        sessionId: conversationId,
        clientSubmissionId: proposal.proposalId,
        actionId: proposal.actionId,
        releaseId: proposal.releaseId || '',
        targetInstanceId: proposal.targetInstanceId,
        parameters: proposal.parameters || {},
      })
      // 2) 用户二次确认（approve = 同意提交/执行，非成功保证）
      const decisionId = `ui:${prepared.confirmationId}`
      const decided = await agentApi.decideProposal(oid, prepared.confirmationId, {
        decision: 'approve',
        sessionKind: 'ontology_agent',
        sessionId: conversationId,
        decisionId,
        payloadHash: prepared.payloadHash,
      })
      // 3) 唤醒执行并读取真实状态
      const executed = await agentApi.executeProposal(oid, {
        confirmationId: prepared.confirmationId,
        sessionKind: 'ontology_agent',
        sessionId: conversationId,
      })
      setResult(executed.state ? executed : decided)
    } catch (e: unknown) {
      const detail = (e as { detail?: { message?: string } } | null)?.detail
      const message = (e as { message?: string } | null)?.message
      setError(detail?.message || message || '执行失败')
    } finally {
      setExecuting(false)
    }
  }

  const resultBanner = () => {
    if (!result) return null
    if (result.state === 'pending_approval') {
      return (
        <div className="flex items-center gap-1.5 px-3 py-2 rounded-lg bg-[var(--color-warning-bg)] text-xs font-medium text-[var(--color-warning)]">
          <Clock3 size={13} /> 已确认并提交审批，等待管理员批准 — 批准后才会真正落库
        </div>
      )
    }
    if (result.state === 'succeeded') {
      return (
        <div className="flex items-center gap-1.5 px-3 py-2 rounded-lg bg-[var(--color-success-bg)] text-xs font-medium text-[var(--color-success)]">
          <CheckCircle2 size={13} /> 已执行成功，变更已写入事实流（可在实例历史中溯源）
        </div>
      )
    }
    if (result.state === 'unknown') {
      return (
        <div className="flex items-center gap-1.5 px-3 py-2 rounded-lg bg-[var(--color-warning-bg)] text-xs font-medium text-[var(--color-warning)]">
          <Clock3 size={13} /> 执行结果待核实（{result.blockReason || 'execution_result_unknown'}）
        </div>
      )
    }
    if (result.state === 'executing' || result.state === 'dispatch_queued') {
      return (
        <div className="flex items-center gap-1.5 px-3 py-2 rounded-lg bg-[var(--color-warning-bg)] text-xs font-medium text-[var(--color-warning)]">
          <Clock3 size={13} /> 已确认，正在执行…
        </div>
      )
    }
    const label = result.state === 'denied' ? '已拒绝'
      : result.state === 'cancelled' ? '已取消'
        : result.state === 'expired' ? '确认已过期（版本或权限变化），请重新准备'
          : result.state === 'approval_rejected' ? '审批被拒绝，未产生业务变更'
            : `未执行（${result.failureCode || result.state}）`
    return (
      <div className="flex items-center gap-1.5 px-3 py-2 rounded-lg bg-[var(--color-danger-bg)] text-xs font-medium text-[var(--color-danger)]">
        <XCircle size={13} /> {label}
      </div>
    )
  }

  return (
    <div className="mt-3 rounded-xl border border-viz-indigo-soft bg-gradient-to-b from-viz-indigo-soft to-transparent overflow-hidden">
      {/* 头部 */}
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2 px-4 py-2.5 border-b border-viz-indigo-soft">
        <div className="flex flex-wrap items-center gap-2 min-w-0">
          <div className="w-6 h-6 rounded-lg bg-viz-indigo-soft flex items-center justify-center shrink-0">
            <FlaskConical size={12} className="text-viz-indigo" />
          </div>
          <span className="text-sm font-semibold text-[var(--color-text-primary)] truncate">
            {proposal.actionName}
          </span>
          <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md text-[10px] font-medium shrink-0 ${dryRunOk
            ? 'bg-[var(--color-success-bg)] text-[var(--color-success)]' : 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]'}`}>
            预演{dryRunOk ? '通过' : '未通过'}
          </span>
          {proposal.requiresApproval && (
            <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md text-[10px] font-medium bg-[var(--color-warning-bg)] text-[var(--color-warning)] shrink-0">
              <ShieldCheck size={9} />需审批
            </span>
          )}
        </div>
        {!result && (
          <button onClick={execute} disabled={!dryRunOk || executing}
            className="inline-flex items-center gap-1.5 h-7 px-3 rounded-lg bg-[var(--color-primary)] text-[var(--color-primary-foreground)] text-xs font-medium hover:opacity-90 disabled:opacity-30 disabled:cursor-not-allowed transition-all shrink-0">
            {executing ? <Loader2 size={11} className="animate-spin" /> : <Play size={11} />}
            确认执行
          </button>
        )}
      </div>

      <div className="px-4 py-3 space-y-2.5">
        {params.length > 0 && (
          <div className="space-y-1 text-xs">
            {params.map(([k, v]) => (
              <div key={k} className="flex gap-3">
                <span className="text-[var(--color-text-tertiary)] shrink-0 min-w-[64px]">{k}</span>
                <span className="text-[var(--color-text-secondary)] break-all">
                  {typeof v === 'object' ? JSON.stringify(v) : String(v)}
                </span>
              </div>
            ))}
          </div>
        )}

        {proposal.effects.length > 0 && (
          <div>
            <p className="text-[10px] font-medium text-[var(--color-text-tertiary)] uppercase tracking-wide mb-1.5">
              预演效果 · 尚未落库
            </p>
            <div className="space-y-1">
              {proposal.effects.map((e, i) => (
                <div key={i} className="flex items-start gap-2 text-xs text-[var(--color-text-secondary)]">
                  <span className="mt-1.5 w-1 h-1 rounded-full bg-viz-indigo shrink-0" />
                  {e.description || e.type}
                </div>
              ))}
            </div>
          </div>
        )}

        {proposal.validationErrors.length > 0 && (
          <div className="text-xs text-[var(--color-danger)] space-y-0.5">
            {proposal.validationErrors.map((e, i) => <div key={i}>· {e}</div>)}
          </div>
        )}

        {resultBanner()}
        {error && (
          <div className="flex items-center gap-1.5 px-3 py-2 rounded-lg bg-[var(--color-danger-bg)] text-xs font-medium text-[var(--color-danger)]">
            <XCircle size={13} />{error}
          </div>
        )}
      </div>
    </div>
  )
}
