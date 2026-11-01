/**
 * 动作提案卡片 — agent 只提案，人签字（YIYA-36 持久确认协议）。
 *
 * 展示 dry-run 预演结果（参数 + 模拟效果 + 校验错误）；「确认执行」先建立
 * 持久确认请求（prepare），再提交用户二次确认（approve），最后唤醒执行并
 * 展示真实业务状态 — requires_approval 的动作确认后进入 HITL 审批队列。
 *
 * 状态恢复：confirmationId 按用户/本体/会话/提案四元组本地留痕（终态不清除），
 * mount/切换时经 getProposal 拉取服务端权威状态，未验证前禁止确认；非终态按
 * 退避轮询。unknown / compensation_required / pending_review 只如实展示，
 * 绝不自动重执行。
 */
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  Play, ShieldCheck, CheckCircle2, XCircle, Clock3, Loader2, FlaskConical, Hourglass,
} from 'lucide-react'
import {
  agentApi, type AgentActionProposal, type AgentConfirmationState,
} from '@/api/agent'
import { useAuthStore } from '@/stores/authStore'
import {
  loadProposalConfirmationRecord,
  nextProposalPollDelayMs,
  proposalConfirmationKey,
  proposalEffectsSectionTitle,
  resultBannerView,
  saveProposalConfirmationRecord,
  shouldContinueProposalPolling,
  type ProposalBannerTone,
} from './proposalConfirmation'

const bannerClass: Record<ProposalBannerTone, string> = {
  success: 'bg-[var(--color-success-bg)] text-[var(--color-success)]',
  warning: 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]',
  danger: 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]',
  muted: 'bg-[var(--color-bg-hover)] text-[var(--color-text-secondary)]',
}

const RECOVERY_SYNC_MESSAGE = '确认请求已建立，正在同步服务端权威状态…'


export function ProposalCard({ oid, proposal, conversationId }: {
  oid: string
  proposal: AgentActionProposal
  conversationId: string | null
}) {
  /** 执行态绑定 key + 代次 + runId：跨切换/旧 finally 不得干扰新执行 */
  const [executingToken, setExecutingToken] = useState<{ key: string; generation: number; runId: number } | null>(null)
  const [result, setResult] = useState<AgentConfirmationState | null>(null)
  const [errorState, setErrorState] = useState<{ key: string; message: string } | null>(null)
  const [recoveryFailureKey, setRecoveryFailureKey] = useState<string | null>(null)
  /** 结果绑定完整 key：异键派生为 null */
  const [stateKey, setStateKey] = useState<string | null>(null)
  const keyGenerationRef = useRef(0)
  const executeRunRef = useRef(0)
  const userId = useAuthStore(state => state.user?.id) ?? ''
  const recoveryKey = proposalConfirmationKey({
    userId,
    ontologyId: oid,
    conversationId: conversationId ?? '',
    proposalId: proposal.proposalId,
  })
  const executing = executingToken != null
    && executingToken.key === recoveryKey
    && executingToken.generation === keyGenerationRef.current
  const recoveryFailed = recoveryFailureKey === recoveryKey
  const error = errorState && errorState.key === recoveryKey ? errorState.message : ''
  const stateIsCurrent = stateKey === recoveryKey
  const freshResult = stateIsCurrent ? result : null
  const resultRef = useRef<AgentConfirmationState | null>(null)
  resultRef.current = freshResult
  const recoveryKeyRef = useRef(recoveryKey)
  recoveryKeyRef.current = recoveryKey
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])
  // key 代次递增：切换后旧异步链失效，切回也必须重新验证
  useLayoutEffect(() => {
    keyGenerationRef.current += 1
    setResult(null)
    setStateKey(null)
    setExecutingToken(null)
  }, [recoveryKey])
  /** localStorage 写失败时的内存兜底（只存 confirmationId） */
  const pendingConfirmationIdsRef = useRef(new Map<string, string>())

  // 首帧同步读留痕或内存兜底：权威结果未落位即恢复中；executing 属本次生命周期
  const pendingConfirmationId = pendingConfirmationIdsRef.current.get(recoveryKey) ?? null
  const hasRecoveryRecord = pendingConfirmationId != null
    || loadProposalConfirmationRecord(window.localStorage, recoveryKey) != null
  const recovering = !executing && !recoveryFailed && hasRecoveryRecord
    && !(stateIsCurrent && result != null)

  /** GET 权威状态；apply 守卫卸载、key 与代次竞态 */
  const recoverAuthoritative = (
    key: string, confirmationId: string,
    shouldApply?: () => boolean,
  ): Promise<void> => {
    const generationAtRequest = keyGenerationRef.current
    const apply = shouldApply ?? (() => mountedRef.current
      && recoveryKeyRef.current === key
      && keyGenerationRef.current === generationAtRequest)
    const clearSyncMessage = () => setErrorState(prev => (
      prev?.key === key && prev.message === RECOVERY_SYNC_MESSAGE ? null : prev))
    return agentApi.getProposal(oid, confirmationId, 'ontology_agent', conversationId ?? '')
      .then(state => {
        if (!apply()) return
        setResult(state)
        setStateKey(key)
        setRecoveryFailureKey(null)
        clearSyncMessage()
      })
      .catch(() => {
        if (!apply()) return
        setRecoveryFailureKey(key)
        clearSyncMessage()
      })
  }

  // mount/key 变化时按留痕或内存兜底恢复；无留痕先清旧 key 的恢复失败
  useEffect(() => {
    setRecoveryFailureKey(null)
    if (!conversationId) return
    const record = loadProposalConfirmationRecord(window.localStorage, recoveryKey)
    const confirmationId = record?.confirmationId
      ?? pendingConfirmationIdsRef.current.get(recoveryKey)
    if (!confirmationId) return
    const generationAtEffect = keyGenerationRef.current
    let alive = true
    void recoverAuthoritative(recoveryKey, confirmationId, () => alive
      && recoveryKeyRef.current === recoveryKey
      && keyGenerationRef.current === generationAtEffect)
    return () => { alive = false }
  }, [recoveryKey, oid, conversationId])

  // 权威状态轮询：confirmationId/key 变化即重建；await/catch 后按 key+代次
  // 丢弃迟到响应；终态停止（pending_review 例外），轮询永不触发执行
  useEffect(() => {
    const confirmationId = freshResult?.confirmationId
    if (!confirmationId || !conversationId) return
    const keyAtEffect = recoveryKey
    const generationAtEffect = keyGenerationRef.current
    let alive = true
    let timer: number | undefined
    let attempt = 0
    const run = async () => {
      if (!alive) return
      const current = resultRef.current
      if (!current || current.confirmationId !== confirmationId) return
      if (!shouldContinueProposalPolling(current.state, current.outcome)) return
      if (document.visibilityState !== 'visible') {
        timer = window.setTimeout(run, 1000)
        return
      }
      try {
        const next = await agentApi.getProposal(oid, confirmationId, 'ontology_agent', conversationId)
        if (!alive || recoveryKeyRef.current !== keyAtEffect
          || keyGenerationRef.current !== generationAtEffect) return
        attempt += 1
        resultRef.current = next
        setResult(next)
        setStateKey(keyAtEffect)
        if (shouldContinueProposalPolling(next.state, next.outcome)) {
          timer = window.setTimeout(run, nextProposalPollDelayMs(attempt))
        }
      } catch {
        if (!alive || recoveryKeyRef.current !== keyAtEffect
          || keyGenerationRef.current !== generationAtEffect) return
        attempt += 1
        timer = window.setTimeout(run, nextProposalPollDelayMs(attempt))
      }
    }
    timer = window.setTimeout(run, nextProposalPollDelayMs(0))
    return () => {
      alive = false
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [freshResult?.confirmationId, conversationId, oid, recoveryKey])

  const dryRunOk = proposal.status === 'success'
  const params = Object.entries(proposal.parameters || {})
  // prepared 是唯一可（重新）确认态；恢复中/失败/unknown/compensation 不可确认
  const canConfirm = !recovering && !recoveryFailed
    && (!stateIsCurrent || !freshResult || freshResult.state === 'prepared')

  const execute = async () => {
    if (executing || recovering || recoveryFailed
      || (stateIsCurrent && freshResult != null && freshResult.state !== 'prepared')) {
      return
    }
    if (!conversationId) {
      setErrorState({ key: recoveryKey, message: '缺少会话上下文，无法建立确认请求' })
      return
    }
    // 绑定发起时的 key/代次/runId：跨切换旧链只保留 prepare 记录
    const keyAtStart = recoveryKey
    const generationAtStart = keyGenerationRef.current
    executeRunRef.current += 1
    const runId = executeRunRef.current
    const isStale = () => !mountedRef.current
      || recoveryKeyRef.current !== keyAtStart
      || keyGenerationRef.current !== generationAtStart
    let prepared: AgentConfirmationState | null = null
    setExecutingToken({ key: keyAtStart, generation: generationAtStart, runId })
    setErrorState(null)
    try {
      // 1) 持久准备（不可变载荷；重复提交回放同一确认请求）
      prepared = await agentApi.prepareProposal(oid, {
        sessionKind: 'ontology_agent',
        sessionId: conversationId,
        clientSubmissionId: proposal.proposalId,
        actionId: proposal.actionId,
        releaseId: proposal.releaseId || '',
        targetInstanceId: proposal.targetInstanceId,
        parameters: proposal.parameters || {},
      })
      // 留痕 confirmationId（best-effort）：写失败不影响后续决定/执行
      pendingConfirmationIdsRef.current.set(keyAtStart, prepared.confirmationId)
      saveProposalConfirmationRecord(window.localStorage, keyAtStart, { confirmationId: prepared.confirmationId })
      if (isStale()) return
      // prepare 幂等回放可能带回既有权威状态（已执行/待审/unknown 等）：
      // 直接展示，绝不再次 decide/execute
      if (prepared.state !== 'prepared') {
        setRecoveryFailureKey(null)
        setResult(prepared)
        setStateKey(keyAtStart)
        return
      }
      // 2) 用户二次确认（approve = 同意提交/执行，非成功保证）
      const decisionId = `ui:${prepared.confirmationId}`
      const decided = await agentApi.decideProposal(oid, prepared.confirmationId, {
        decision: 'approve',
        sessionKind: 'ontology_agent',
        sessionId: conversationId,
        decisionId,
        payloadHash: prepared.payloadHash,
      })
      if (isStale()) return
      // 3) 唤醒执行并读取真实状态
      const executed = await agentApi.executeProposal(oid, {
        confirmationId: prepared.confirmationId,
        sessionKind: 'ontology_agent',
        sessionId: conversationId,
      })
      if (isStale()) return
      setRecoveryFailureKey(null)
      setResult(executed.state ? executed : decided)
      setStateKey(keyAtStart)
    } catch (e: unknown) {
      if (isStale()) return
      if (prepared) {
        // 确认已建立但结果未知：转真实 GET，保持 fail-closed
        setErrorState({ key: keyAtStart, message: RECOVERY_SYNC_MESSAGE })
        void recoverAuthoritative(keyAtStart, prepared.confirmationId)
        return
      }
      const detail = (e as { detail?: { message?: string } } | null)?.detail
      const message = (e as { message?: string } | null)?.message
      setErrorState({ key: keyAtStart, message: detail?.message || message || '执行失败' })
    } finally {
      // 只清本次 runId：旧 finally 不得清掉新执行态
      if (mountedRef.current) {
        setExecutingToken(prev => (prev?.runId === runId ? null : prev))
      }
    }
  }

  const resultBanner = () => {
    if (recovering) {
      return (
        <div
          data-testid="proposal-recovery-banner"
          className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-medium bg-[var(--color-bg-hover)] text-[var(--color-text-secondary)]"
        >
          <Hourglass size={13} /> 正在恢复确认状态…（恢复完成前不可确认）
        </div>
      )
    }
    if (recoveryFailed && !freshResult) {
      return (
        <div
          data-testid="proposal-recovery-failed-banner"
          className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-medium bg-[var(--color-danger-bg)] text-[var(--color-danger)]"
        >
          <XCircle size={13} /> 该提案已发起过确认，但当前状态暂不可验证（恢复失败）；请刷新重试，不能重新确认
        </div>
      )
    }
    if (!freshResult || freshResult.state === 'prepared') return null
    const view = resultBannerView(freshResult)
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
            <p
              data-testid="proposal-effects-title"
              className="text-[10px] font-medium text-[var(--color-text-tertiary)] uppercase tracking-wide mb-1.5"
            >
              {proposalEffectsSectionTitle(
                freshResult?.state ?? null, recovering || recoveryFailed || executing)}
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
