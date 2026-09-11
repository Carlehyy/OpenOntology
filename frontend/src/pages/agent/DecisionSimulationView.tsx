import { formatDateTime } from '@/utils/datetime'
import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Activity, AlertTriangle, BarChart3, CheckCircle2, Clock, Database,
  History, Loader2, RefreshCw, Scale, ShieldCheck, Users,
} from 'lucide-react'

import {
  agentApi,
  type DecisionPerspective,
  type DecisionSimulationRun,
  type DecisionSimulationSummary,
} from '@/api/agent'

interface Props {
  oid: string
  releaseId: string
  conversationId: string | null
  activeRunId: string | null
  running: boolean
}

const statusMeta = {
  running: { label: '推演中', className: 'border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-[var(--color-info-bg)] text-[var(--color-info)]' },
  succeeded: { label: '已完成', className: 'border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-[var(--color-success-bg)] text-[var(--color-success)]' },
  failed: { label: '失败', className: 'border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] text-[var(--color-danger)]' },
} as const

const phaseLabel: Record<string, string> = {
  snapshot: '正在固化当前发布版快照',
  planning: '正在编译决策问题与候选方案',
  perspectives: '正在收集独立视角',
  synthesis: '正在汇总分歧与可执行建议',
  complete: '推演完成',
  failed: '推演中断',
}

const confidenceLabel = (value?: string) => (
  value === 'strong' ? '论证较充分' : value === 'moderate' ? '论证中等' : '证据仍偏弱'
)

const disagreementLabel = (value?: string) => (
  value === 'high' ? '分歧较高' : value === 'medium' ? '存在分歧' : '分歧较低'
)

const dateTime = (value?: string | null) => {
  if (!value) return '—'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : formatDateTime(parsed, { seconds: true })
}

function Metric({ label, value, detail }: { label: string; value: string; detail?: string }) {
  return (
    <div className="min-w-0 border-l border-border pl-3 first:border-l-0 first:pl-0">
      <p className="text-[10px] uppercase tracking-[0.08em] text-[var(--color-text-tertiary)]">{label}</p>
      <p className="mt-1 truncate text-sm font-semibold text-foreground">{value}</p>
      {detail && <p className="mt-0.5 truncate text-[10px] text-[var(--color-text-tertiary)]">{detail}</p>}
    </div>
  )
}

function RunningState({ run }: { run?: DecisionSimulationSummary | DecisionSimulationRun }) {
  const diagnostics = run?.diagnostics || {}
  const phase = String(diagnostics.phase || 'snapshot')
  const completed = Number(diagnostics.perspectiveCompleted || 0)
  const total = Number(diagnostics.perspectiveTotal || 4)
  const current = String(diagnostics.perspectiveCurrent || '')
  const progress = phase === 'snapshot' ? 12
    : phase === 'planning' ? 28
      : phase === 'perspectives' ? 35 + Math.round((completed / Math.max(1, total)) * 45)
        : phase === 'synthesis' ? 90 : 100
  return (
    <div className="flex h-full items-center justify-center bg-muted p-8" data-testid="decision-simulation-running">
      <div className="w-full max-w-lg rounded-xl border border-border bg-card p-6 shadow-sm">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-[var(--color-info-bg)] text-[var(--color-info)]">
            <Loader2 size={19} className="animate-spin motion-reduce:animate-none" />
          </div>
          <div className="min-w-0">
            <h4 className="text-sm font-semibold text-foreground">{run?.title || '决策推演正在启动'}</h4>
            <p className="mt-0.5 text-xs text-muted-foreground">{phaseLabel[phase] || '正在处理'}</p>
          </div>
        </div>
        <div className="mt-5 h-1.5 overflow-hidden rounded-full bg-muted" aria-label="推演进度">
          <div className="h-full rounded-full bg-[var(--color-info)] transition-[width] duration-300 motion-reduce:transition-none" style={{ width: `${progress}%` }} />
        </div>
        <div className="mt-3 flex items-center justify-between text-[11px] text-[var(--color-text-tertiary)]">
          <span>{current || '发布版只读快照'}</span>
          <span>{phase === 'perspectives' ? `${completed}/${total} 个视角` : `${progress}%`}</span>
        </div>
        <p className="mt-5 border-t border-border pt-4 text-[11px] leading-5 text-muted-foreground">
          推演运行在隔离快照上。此过程只会写入推演记录，不会修改真实对象、事实、哨兵或执行动作。
        </p>
      </div>
    </div>
  )
}

function EmptyState({ running }: { running: boolean }) {
  if (running) return <RunningState />
  return (
    <div className="flex h-full items-center justify-center bg-muted p-8" data-testid="decision-simulation-empty">
      <div className="max-w-md text-center">
        <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-xl border border-border bg-card text-brand-ink shadow-sm">
          <Scale size={21} />
        </div>
        <h4 className="mt-4 text-sm font-semibold text-foreground">从一个真实决策问题开始</h4>
        <p className="mt-2 text-xs leading-5 text-muted-foreground">
          在左侧告诉助手“推演……”并说明目标、候选方案或时间范围。引擎会锁定当前发布版，收集多个视角并比较方案。
        </p>
        <div className="mt-4 rounded-lg border border-border bg-card px-4 py-3 text-left text-[11px] leading-5 text-muted-foreground">
          示例：推演未来一个月该采取哪种策略，并给出关键假设、风险、早期信号与停止条件。
        </div>
      </div>
    </div>
  )
}

function OptionComparison({ run }: { run: DecisionSimulationRun }) {
  const options = run.evaluation.options || []
  const objectiveLabels = new Map((run.evaluation.objectives || []).map(item => [item.id, item.label]))
  return (
    <section aria-labelledby="decision-options-title">
      <div className="mb-3 flex items-center justify-between">
        <h4 id="decision-options-title" className="flex items-center gap-2 text-xs font-semibold text-foreground">
          <BarChart3 size={14} className="text-brand-ink" />方案比较
        </h4>
        <span className="text-[10px] text-[var(--color-text-tertiary)]">保守分 = 加权均分 − 0.35 × 视角离散度</span>
      </div>
      <div className="overflow-hidden rounded-lg border border-border bg-card">
        {options.map((option, index) => (
          <div key={option.optionId} className={`px-4 py-3 ${index ? 'border-t border-border' : ''}`}>
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className={`flex h-5 w-5 shrink-0 items-center justify-center rounded text-[10px] font-semibold ${option.rank === 1 ? 'bg-brand-soft text-brand-ink' : 'bg-muted text-muted-foreground'}`}>{option.rank}</span>
                  <span className="truncate text-xs font-medium text-foreground">{option.label}</span>
                </div>
                <div className="mt-2.5 h-1.5 overflow-hidden rounded-full bg-muted">
                  <div className={`h-full rounded-full ${option.rank === 1 ? 'bg-brand' : 'bg-accent'}`} style={{ width: `${Math.max(0, Math.min(100, option.robustScore))}%` }} />
                </div>
              </div>
              <div className="shrink-0 text-right">
                <p className="text-base font-semibold tabular-nums text-foreground">{option.robustScore.toFixed(1)}</p>
                <p className="text-[10px] text-[var(--color-text-tertiary)]">保守综合分</p>
              </div>
            </div>
            <div className="mt-2.5 flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-muted-foreground">
              <span>加权均分 {option.meanScore.toFixed(1)}</span>
              <span>分歧跨度 {option.disagreement.toFixed(1)}</span>
              {Object.entries(option.objectiveScores).map(([key, value]) => (
                <span key={key}>{objectiveLabels.get(key) || key} {value.toFixed(1)}</span>
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}

function PerspectiveCard({ perspective, winnerId }: { perspective: DecisionPerspective; winnerId?: string }) {
  const winner = perspective.optionAssessments.find(item => item.optionId === winnerId)
  return (
    <article className="rounded-lg border border-border bg-card p-3.5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h5 className="text-xs font-semibold text-foreground">{perspective.name}</h5>
          <p className="mt-1 line-clamp-2 text-[10px] leading-4 text-[var(--color-text-tertiary)]">{perspective.mission}</p>
        </div>
        <span className="shrink-0 rounded border border-border bg-muted px-1.5 py-0.5 text-[9px] text-muted-foreground">
          证据占比 {Math.round((perspective.evidenceCoverage || 0) * 100)}%
        </span>
      </div>
      <p className="mt-3 text-[11px] leading-5 text-muted-foreground">{perspective.stance || '该视角未提供立场摘要。'}</p>
      {winner?.rationale && (
        <div className="mt-3 border-l-2 border-brand-line pl-2.5 text-[10px] leading-4 text-muted-foreground">
          对推荐方案：{winner.rationale}
        </div>
      )}
      {(perspective.challenges || []).length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {perspective.challenges.slice(0, 3).map(item => (
            <span key={item} className="rounded bg-[var(--color-warning-bg)] px-1.5 py-1 text-[9px] text-[var(--color-warning)]">{item}</span>
          ))}
        </div>
      )}
    </article>
  )
}

function ResultView({ run }: { run: DecisionSimulationRun }) {
  const recommendation = run.recommendation || {}
  const coverage = run.snapshot.coverage || {}
  const winnerId = recommendation.recommendedOptionId
  const scenarios = useMemo(() => {
    const seen = new Set<string>()
    return run.perspectives.flatMap(item => item.scenarioOutlooks || []).filter(item => {
      const key = `${item.name}:${item.trigger}`
      if (seen.has(key)) return false
      seen.add(key)
      return true
    }).slice(0, 6)
  }, [run.perspectives])

  if (run.status === 'running') return <RunningState run={run} />
  if (run.status === 'failed') {
    return (
      <div className="flex h-full items-center justify-center bg-muted p-8">
        <div role="alert" className="max-w-lg rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-card p-5">
          <div className="flex items-center gap-2 text-sm font-semibold text-[var(--color-danger)]"><AlertTriangle size={16} />推演未完成</div>
          <p className="mt-2 text-xs leading-5 text-[var(--color-danger)]">{run.errorMessage || '模型或数据服务返回了不可恢复的错误。'}</p>
        </div>
      </div>
    )
  }

  return (
    <div className="scrollbar-thin h-full overflow-y-auto bg-muted" data-testid="decision-simulation-result">
      <div className="mx-auto max-w-5xl space-y-5 p-4 lg:p-5">
        <section className="rounded-xl border border-border bg-card p-5 shadow-sm">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 text-[10px] font-medium uppercase tracking-[0.12em] text-brand-ink">
                <ShieldCheck size={13} />隔离决策快照
              </div>
              <h3 className="mt-2 text-base font-semibold leading-6 text-foreground">{run.title}</h3>
              <p className="mt-1.5 text-xs leading-5 text-muted-foreground">{run.question}</p>
            </div>
            <span className={`rounded-full border px-2.5 py-1 text-[10px] font-medium ${statusMeta[run.status].className}`}>
              {statusMeta[run.status].label}
            </span>
          </div>
          <div className="mt-5 grid grid-cols-2 gap-y-4 border-t border-border pt-4 sm:grid-cols-4">
            <Metric label="数据截止" value={dateTime(run.snapshot.capturedAt)} detail="冻结后不随线上数据变化" />
            <Metric label="快照范围" value={`${coverage.instanceCount ?? 0} 个实例`} detail={`${coverage.objectTypeCount ?? 0} 类对象 · ${coverage.linkTypeCount ?? 0} 类关系`} />
            <Metric label="独立视角" value={`${run.perspectives.length} 个`} detail={disagreementLabel(run.evaluation.disagreementLevel)} />
            <Metric label="结果强度" value={confidenceLabel(recommendation.confidenceBand)} detail="不是未来概率" />
          </div>
        </section>

        <section className="rounded-xl border border-brand-line bg-brand-soft p-5" aria-labelledby="decision-recommendation-title">
          <div className="flex items-start gap-3">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-card text-brand-ink shadow-sm"><Scale size={16} /></div>
            <div className="min-w-0 flex-1">
              <p className="text-[10px] font-medium uppercase tracking-[0.1em] text-brand-ink">当前建议</p>
              <h4 id="decision-recommendation-title" className="mt-1 text-base font-semibold text-foreground">{recommendation.recommendedOption || '待人工复核'}</h4>
              <p className="mt-2 text-xs leading-5 text-muted-foreground">{recommendation.summary}</p>
              {(recommendation.rationale || []).length > 0 && (
                <ul className="mt-3 space-y-1.5">
                  {recommendation.rationale!.map(item => <li key={item} className="flex gap-2 text-[11px] leading-5 text-muted-foreground"><CheckCircle2 size={12} className="mt-1 shrink-0 text-brand-ink" />{item}</li>)}
                </ul>
              )}
            </div>
          </div>
        </section>

        <OptionComparison run={run} />

        <section aria-labelledby="decision-perspectives-title">
          <div className="mb-3 flex items-center justify-between">
            <h4 id="decision-perspectives-title" className="flex items-center gap-2 text-xs font-semibold text-foreground"><Users size={14} className="text-brand-ink" />多视角审议</h4>
            <span className="text-[10px] text-[var(--color-text-tertiary)]">角色意见相互独立保存，不按票数计算概率</span>
          </div>
          <div className="grid gap-3 xl:grid-cols-2">
            {run.perspectives.map(item => <PerspectiveCard key={item.id} perspective={item} winnerId={winnerId} />)}
          </div>
        </section>

        {scenarios.length > 0 && (
          <section aria-labelledby="decision-scenarios-title">
            <h4 id="decision-scenarios-title" className="mb-3 flex items-center gap-2 text-xs font-semibold text-foreground"><Activity size={14} className="text-brand-ink" />可能情景与早期信号</h4>
            <div className="grid gap-3 lg:grid-cols-3">
              {scenarios.map((scenario, index) => (
                <article key={`${scenario.name}-${index}`} className="rounded-lg border border-border bg-card p-3.5">
                  <div className="flex items-center gap-2"><span className="h-1.5 w-1.5 rounded-full bg-[var(--color-info)]" /><h5 className="text-xs font-medium text-foreground">{scenario.name}</h5></div>
                  <p className="mt-2 text-[10px] leading-4 text-muted-foreground">触发：{scenario.trigger || '尚未定义'}</p>
                  {(scenario.earlySignals || []).slice(0, 3).map(signal => <p key={signal} className="mt-1.5 border-l border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] pl-2 text-[10px] leading-4 text-muted-foreground">{signal}</p>)}
                </article>
              ))}
            </div>
          </section>
        )}

        <div className="grid gap-3 lg:grid-cols-2">
          <section className="rounded-lg border border-border bg-card p-4">
            <h4 className="flex items-center gap-2 text-xs font-semibold text-foreground"><CheckCircle2 size={14} className="text-[var(--color-success)]" />无悔行动</h4>
            <ul className="mt-3 space-y-2">
              {(recommendation.noRegretActions || []).map(item => <li key={item} className="text-[11px] leading-5 text-muted-foreground">{item}</li>)}
              {!recommendation.noRegretActions?.length && <li className="text-[11px] text-[var(--color-text-tertiary)]">暂无</li>}
            </ul>
          </section>
          <section className="rounded-lg border border-border bg-card p-4">
            <h4 className="flex items-center gap-2 text-xs font-semibold text-foreground"><AlertTriangle size={14} className="text-[var(--color-warning)]" />停止条件</h4>
            <ul className="mt-3 space-y-2">
              {(recommendation.stopConditions || []).map(item => <li key={item} className="text-[11px] leading-5 text-muted-foreground">{item}</li>)}
              {!recommendation.stopConditions?.length && <li className="text-[11px] text-[var(--color-text-tertiary)]">暂无</li>}
            </ul>
          </section>
        </div>

        <footer className="rounded-lg border border-border bg-card px-4 py-3 text-[10px] leading-5 text-muted-foreground">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
            <span className="flex items-center gap-1.5"><Database size={11} />发布版 {run.ontologyReleaseId || '未绑定'}</span>
            <span className="flex items-center gap-1.5"><Clock size={11} />{dateTime(run.completedAt)}</span>
            <span>校验值 {(run.snapshot.checksum || '').slice(0, 12) || '—'}</span>
            <span>模型 {run.modelName || '—'}</span>
          </div>
          <p className="mt-1.5 text-[var(--color-text-tertiary)]">{recommendation.disclaimer || '结果用于辅助决策，不代表自动执行指令。'}</p>
        </footer>
      </div>
    </div>
  )
}

export default function DecisionSimulationView({ oid, releaseId, conversationId, activeRunId, running }: Props) {
  const [selectedId, setSelectedId] = useState<string | null>(activeRunId)
  const {
    data: runs = [], isLoading: listLoading, refetch: refetchRuns,
  } = useQuery<DecisionSimulationSummary[]>({
    queryKey: ['decision-simulations', oid, releaseId, conversationId],
    queryFn: () => agentApi.decisionSimulations(oid, {
      releaseId, conversationId: conversationId || undefined, limit: 30,
    }),
    enabled: !!oid && !!releaseId,
    refetchInterval: running ? 1500 : false,
  })

  useEffect(() => {
    if (activeRunId) {
      setSelectedId(activeRunId)
      void refetchRuns()
    }
  }, [activeRunId, refetchRuns])

  useEffect(() => {
    if (!selectedId && runs[0]?.id) setSelectedId(runs[0].id)
  }, [runs, selectedId])

  const effectiveId = selectedId || activeRunId || runs[0]?.id || null
  const { data: run, isLoading: runLoading, refetch: refetchRun } = useQuery<DecisionSimulationRun>({
    queryKey: ['decision-simulation', oid, effectiveId],
    queryFn: () => agentApi.decisionSimulation(oid, effectiveId!),
    enabled: !!oid && !!effectiveId,
    refetchInterval: query => query.state.data?.status === 'running' || running ? 1500 : false,
  })

  if (!oid) return <EmptyState running={false} />

  return (
    <div className="flex h-full min-h-0 flex-col bg-muted">
      <div className="flex h-10 shrink-0 items-center justify-between gap-3 border-b border-border bg-card px-3">
        <div className="flex min-w-0 items-center gap-2 text-[10px] text-muted-foreground">
          <History size={12} />
          <select
            aria-label="选择决策推演记录"
            value={effectiveId || ''}
            onChange={event => setSelectedId(event.target.value || null)}
            disabled={!runs.length}
            className="h-7 max-w-[300px] cursor-pointer rounded border border-border bg-muted px-2 text-[10px] text-muted-foreground outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
          >
            {!runs.length && <option value="">当前会话暂无推演</option>}
            {runs.map(item => <option key={item.id} value={item.id}>{item.title} · {dateTime(item.startedAt)}</option>)}
          </select>
        </div>
        <button
          type="button"
          onClick={() => { void refetchRuns(); if (effectiveId) void refetchRun() }}
          aria-label="刷新决策推演"
          className="flex h-7 w-7 items-center justify-center rounded text-[var(--color-text-tertiary)] transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <RefreshCw size={12} />
        </button>
      </div>
      <div className="min-h-0 flex-1">
        {(listLoading || runLoading) && effectiveId ? (
          <div className="flex h-full items-center justify-center gap-2 text-xs text-muted-foreground"><Loader2 size={14} className="animate-spin motion-reduce:animate-none" />正在读取推演记录…</div>
        ) : run ? <ResultView run={run} /> : runs[0]?.status === 'running' ? <RunningState run={runs[0]} /> : <EmptyState running={running} />}
      </div>
    </div>
  )
}
