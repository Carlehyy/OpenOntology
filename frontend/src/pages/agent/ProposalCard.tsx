/**
 * 动作提案卡片 — agent 只提案，人签字（YIYA-36 持久确认协议）。
 *
 * 展示 dry-run 预演结果（参数 + 模拟效果 + 校验错误）；「确认执行」先建立
 * 持久确认请求（prepare），再提交用户二次确认（approve），最后唤醒执行并
 * 展示真实业务状态 — requires_approval 的动作确认后进入 HITL 审批队列。
 *
 * 状态恢复：confirmationId 按用户/本体/会话/提案四元组本地留痕（终态不清除），
 * mount 时经 getProposal 拉服务端权威状态；非终态按退避轮询直至结案。
 * unknown / compensation_required 只如实展示「结果待核实 / 需人工处理」，
 * 绝不自动重执行；恢复失败显示「状态暂不可验证」并禁止重新确认。
 */
import { useEffect, useRef, useState } from 'react'
import {
  Play, ShieldCheck, CheckCircle2, XCircle, Clock3, Loader2, FlaskConical,
} from 'lucide-react'
import {
  agentApi, type AgentActionProposal, type AgentConfirmationState,
} from '@/api/agent'
import { confirmationStateText } from '@/components/ontologyConfirmationModel'
import { useAuthStore } from '@/stores/authStore'
import {
  loadProposalConfirmationRecord,
  nextProposalPollDelayMs,
  proposalConfirmationKey,
  saveProposalConfirmationRecord,
  shouldContinueProposalPolling,
} from './proposalConfirmation'

type BannerTone = 'success' | 'warning' | 'danger' | 'muted'

const bannerClass: Record<BannerTone, string> = {
  success: 'bg-[var(--color-success-bg)] text-[var(--color-success)]',
  warning: 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]',
  danger: 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]',
  muted: 'bg-[var(--color-bg-hover)] text-[var(--color-text-secondary)]',
}

/** 状态横幅视图：文案统一走 confirmationStateText，保证与超级助手/kernel 同一事实源 */
function resultBannerView(result: AgentConfirmationState): { tone: BannerTone; text: string } {
  switch (result.state) {
    case 'pending_approval':
      return { tone: 'warning', text: '已确认并提交审批，等待管理员批准 — 批准后才会真正落库' }
    case 'succeeded':
      return { tone: 'success', text: '已执行成功，变更已写入事实流（可在实例历史中溯源）' }
    case 'compensation_required':
      return {
        tone: 'danger',
        text: `本地执行失败但外部效果可能已发生（${result.failureCode || 'external_applied_local_failed'}）— 请勿重试，需人工核对并补偿`,
      }
    case 'unknown':
      return { tone: 'warning', text: `执行结果待核实（${result.blockReason || 'execution_result_unknown'}）` }
    case 'executing':
    case 'dispatch_queued':
    case 'confirmed':
    case 'recovering':
      return { tone: 'warning', text: '已确认，正在执行…' }
    case 'denied':
      return { tone: 'muted', text: '已拒绝' }
    case 'cancelled':
      return { tone: 'muted', text: '已取消' }
    case 'expired':
      return { tone: 'warning', text: '确认已过期（版本或权限变化），请重新确认' }
    case 'approval_rejected':
      return { tone: 'muted', text: '审批被拒绝，未产生业务变更' }
    case 'failed_not_executed':
      return { tone: 'danger', text: '未执行（可重新确认后重试）' }
    default:
      return { tone: 'danger', text: `未执行（${result.failureCode || confirmationStateText(result.state)}）` }
  }
}

export function ProposalCard({ oid, proposal, conversationId }: {
  oid: string
  proposal: AgentActionProposal
  conversationId: string | null
}) {
  const [executing, setExecuting] = useState(false)
  const [result, setResult] = useState<AgentConfirmationState | null>(null)
  const [error, setError] = useState('')
  /** 恢复通道状态：record 存在但权威状态拉不到时，绝不能退回可重新确认的初始态 */
  const [recoveryFailed, setRecoveryFailed] = useState(false)
  const userId = useAuthStore(state => state.user?.id) ?? ''
  const recoveryKey = proposalConfirmationKey({
    userId,
    ontologyId: oid,
    conversationId: conversationId ?? '',
    proposalId: proposal.proposalId,
  })
  // 轮询循环读取最新权威状态的通道：状态对象每次都是新引用，
  // 用 ref 传递避免「以整个 result 为依赖」导致循环反复重启/双发
  const resultRef = useRef<AgentConfirmationState | null>(null)
  resultRef.current = result

  // 恢复：mount 时按本地留痕拉服务端权威状态（poll/reload 后 banner 不丢）
  useEffect(() => {
    if (!conversationId) return
    const record = loadProposalConfirmationRecord(window.localStorage, recoveryKey)
    if (!record) return
    let alive = true
    setRecoveryFailed(false)
    agentApi.getProposal(oid, record.confirmationId, 'ontology_agent', conversationId)
      .then(state => { if (alive) { setResult(state); setRecoveryFailed(false) } })
      .catch(() => { if (alive) setRecoveryFailed(true) })
    return () => { alive = false }
  }, [recoveryKey, oid, conversationId])

  // 权威状态轮询：单一调度器。effect 只在「换了 confirmationId / 会话 / 本体」时
  // 重建；循环内经 resultRef 读最新状态——终态即停（不清除本地凭证），
  // unknown/compensation_required 可被 reconcile 结案故继续；页面隐藏挂起。
  useEffect(() => {
    const confirmationId = result?.confirmationId
    if (!confirmationId || !conversationId) return
    let alive = true
    let timer: number | undefined
    let attempt = 0
    const run = async () => {
      if (!alive) return
      const current = resultRef.current
      if (!current || current.confirmationId !== confirmationId) return
      if (!shouldContinueProposalPolling(current.state)) return
      if (document.visibilityState !== 'visible') {
        timer = window.setTimeout(run, 1000)
        return
      }
      try {
        const next = await agentApi.getProposal(oid, confirmationId, 'ontology_agent', conversationId)
        if (!alive) return
        attempt += 1
        resultRef.current = next
        setResult(next)
        if (shouldContinueProposalPolling(next.state)) {
          timer = window.setTimeout(run, nextProposalPollDelayMs(attempt))
        }
      } catch {
        if (!alive) return
        attempt += 1
        timer = window.setTimeout(run, nextProposalPollDelayMs(attempt))
      }
    }
    timer = window.setTimeout(run, nextProposalPollDelayMs(0))
    return () => {
      alive = false
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [result?.confirmationId, conversationId, oid])

  const dryRunOk = proposal.status === 'success'
  const params = Object.entries(proposal.parameters || {})
  // prepared 是唯一可（重新）发起确认的状态：幂等 prepare 会回放同一确认请求。
  // 恢复失败（权威状态不可验证）时禁止重新确认，防止对已确认载荷二次决定。
  const canConfirm = (!result || result.state === 'prepared') && !recoveryFailed

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
      // 留痕 confirmationId（best-effort）：写失败不影响后续决定/执行
      saveProposalConfirmationRecord(window.localStorage, recoveryKey, { confirmationId: prepared.confirmationId })
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
      setRecoveryFailed(false)
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
    if (recoveryFailed && !result) {
      return (
        <div className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-medium bg-[var(--color-danger-bg)] text-[var(--color-danger)]">
          <XCircle size={13} /> 该提案已发起过确认，但当前状态暂不可验证（恢复失败）；请刷新重试，不能重新确认
        </div>
      )
    }
    if (!result || result.state === 'prepared') return null
    const view = resultBannerView(result)
    const Icon = view.tone === 'success' ? CheckCircle2 : view.tone === 'danger' ? XCircle : view.tone === 'muted' ? XCircle : Clock3
    return (
      <div className={`flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-medium ${bannerClass[view.tone]}`}>
        <Icon size={13} /> {view.text}
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
        {canConfirm && (
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
