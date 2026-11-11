import { useCallback, useEffect, useState } from 'react'
import { Check, Loader2, Sparkles, X } from 'lucide-react'

import {
  superAssistantApi,
  type ReflectionCandidate,
} from '@/api/superAssistant'
import { toast } from 'sonner'
import { errorText } from './assistantPanelUtils'
import { candidateActions } from './evolutionLogic'

function ConfidenceTag({ confidence }: { confidence: string }) {
  const tone = confidence === 'high' ? 'bg-brand-soft text-brand-ink'
    : confidence === 'low' ? 'bg-amber-50 text-amber-700' : 'bg-slate-100 text-slate-600'
  return <span className={`rounded px-1.5 py-0.5 text-[10px] ${tone}`}>{confidence}</span>
}

/**
 * 待审批：反思产出的技能进化候选。
 * 旧记忆/冲突候选卡已随 Hindsight 长期记忆上线退役（反思链路不再产出旧记忆，
 * 技能审批保留）；历史遗留的非 skill 候选在此隐藏、不渲染。
 */
export function ApprovalTab({ conversationId }: { conversationId: string | null }) {
  const [candidates, setCandidates] = useState<ReflectionCandidate[]>([])
  const [loading, setLoading] = useState(true)
  // 进行中的审批键 `${candidateId}:${decision}`：仅被点击的按钮转圈，
  // 该候选卡其余按钮在请求期间保持禁用防重复提交，不连坐其它候选卡
  const [busyKey, setBusyKey] = useState<string | null>(null)
  const [reflecting, setReflecting] = useState(false)

  const refresh = useCallback(async () => {
    try {
      setCandidates(await superAssistantApi.reflectionCandidates('pending'))
    } catch (error) {
      toast.error('加载待审批候选失败', { description: errorText(error) })
    } finally {
      setLoading(false)
    }
  }, [toast])

  useEffect(() => {
    setLoading(true)
    refresh()
    const timer = window.setInterval(refresh, 5000)
    return () => window.clearInterval(timer)
  }, [refresh])

  const decide = async (candidate: ReflectionCandidate, decision: string) => {
    if (busyKey) return
    setBusyKey(`${candidate.id}:${decision}`)
    try {
      await superAssistantApi.decideReflectionCandidate(candidate.id, decision)
      toast.success(decision === 'accept' || decision === 'new_supersedes' ? '已接受' : '已处理')
      await refresh()
    } catch (error) {
      toast.error('审批操作失败', { description: errorText(error) })
    } finally {
      setBusyKey(null)
    }
  }

  const runFullReflection = async () => {
    if (!conversationId || reflecting) return
    setReflecting(true)
    try {
      await superAssistantApi.runFullReflection(conversationId)
      toast.success('反思已在后台执行', { description: '产出的候选会稍后出现在这里' })
      window.setTimeout(refresh, 3000)
    } catch (error) {
      toast.error('触发反思失败', { description: errorText(error) })
    } finally {
      setReflecting(false)
    }
  }

  const skillCandidates = candidates.filter(candidate => candidate.kind === 'skill')

  const actions = (candidate: ReflectionCandidate) => (
    <div className="mt-3 flex flex-wrap gap-2">
      {candidateActions(candidate.kind).map(option => {
        const busyHere = busyKey === `${candidate.id}:${option.decision}`
        return (
          <button
            key={option.decision}
            type="button"
            disabled={busyKey?.startsWith(`${candidate.id}:`) ?? false}
            onClick={() => decide(candidate, option.decision)}
            className={option.primary
              ? 'flex min-h-8 items-center gap-1 rounded-lg bg-brand px-3 text-xs font-medium text-white transition-colors hover:bg-brand-deep disabled:opacity-50'
              : 'flex min-h-8 items-center gap-1 rounded-lg border border-[var(--color-border)] px-3 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] disabled:opacity-50'}
          >
            {busyHere ? <Loader2 size={12} className="animate-spin" /> : option.primary ? <Check size={12} /> : <X size={12} />}
            {option.label}
          </button>
        )
      })}
    </div>
  )

  return (
    <div className="grid gap-3" data-testid="approval-tab">
      <button
        type="button"
        onClick={runFullReflection}
        disabled={!conversationId || reflecting}
        className="flex min-h-9 items-center justify-center gap-1.5 rounded-lg border border-brand-line bg-brand-soft text-xs font-medium text-brand-ink transition-colors hover:bg-brand-mist disabled:cursor-not-allowed disabled:opacity-50"
        title={conversationId ? '检查当前会话，整理出可保存的技能' : '请先选择一个会话'}
      >
        {reflecting ? <Loader2 size={13} className="animate-spin" /> : <Sparkles size={13} />}
        检查这次对话，整理可保存的技能
      </button>
      {loading && skillCandidates.length === 0 && (
        <div className="p-10 text-center text-xs text-[var(--color-text-tertiary)]"><Loader2 size={18} className="mx-auto animate-spin" /></div>
      )}
      {!loading && skillCandidates.length === 0 && (
        <div className="rounded-xl border border-dashed border-[var(--color-border)] p-10 text-center text-xs text-[var(--color-text-tertiary)]">
          <Check size={22} className="mx-auto mb-2" />暂无需要你批准的技能
        </div>
      )}
      {skillCandidates.map(candidate => {
        const payload = candidate.payload || {}
        return (
          <article key={candidate.id} data-testid="candidate-skill" className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-4">
            <div className="flex items-center gap-1.5">
              <Sparkles size={14} className="shrink-0 text-brand-ink" />
              <span className="text-xs font-semibold text-[var(--color-text-primary)]">新 Skill</span>
              <ConfidenceTag confidence={candidate.confidence} />
            </div>
            <p className="mt-2 font-mono text-xs font-semibold text-[var(--color-text-primary)]">{String(payload.name || '')}</p>
            <p className="mt-1 text-xs leading-5 text-[var(--color-text-secondary)]">{String(payload.description || '')}</p>
            {payload.skill_md ? (
              <details className="mt-2">
                <summary className="cursor-pointer text-[10px] text-brand-ink hover:underline">预览 SKILL.md</summary>
                <pre className="mt-1 max-h-48 overflow-auto rounded-lg bg-slate-50 p-2 text-[10px] leading-4 whitespace-pre-wrap">{String(payload.skill_md)}</pre>
              </details>
            ) : null}
            {actions(candidate)}
          </article>
        )
      })}
    </div>
  )
}

export function EvolutionPendingBadge() {
  const [count, setCount] = useState(0)
  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const settings = await superAssistantApi.reflectionSettings()
        if (!cancelled) setCount(settings.pending_count)
      } catch { /* 徽标失败静默 */ }
    }
    load()
    const timer = window.setInterval(load, 15000)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [])
  if (!count) return null
  return (
    <span className="rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700" data-testid="pending-badge">
      {count}
    </span>
  )
}
