/* 治理工作台 —— 待审批 / 自治等级 / 哨兵 一表汇总。
   以动作为行:左起动作与累计战绩、自治等级路径(含晋升建议)、
   绑定哨兵状态(在线脉冲/影子/停用 + 最近命中)、待审批条目(点击开详情弹窗)、
   近期批准率(含晋升线)、近期执行履历点阵。
   有待审批的行排在最前并以琥珀色标出,先看要裁决的。 */
import { formatDateTime } from '@/utils/datetime'
import {
  ArrowDownCircle, ArrowUpCircle, Bolt, Eye, HandMetal, Rocket,
} from 'lucide-react'
import type {
  OperationsRow, PendingLog, TimelineDot,
} from './storyModel'
import { buildLevelSteps, type AutonomyLevelKey } from './storyModel'
import { buildPromoteCtaReason, readableTargetSummary } from '../tabs/governanceFormat'
import { TruncatedText } from './TruncatedText'

const LEVEL_META: Record<AutonomyLevelKey, { label: string; icon: any; cls: string; desc: string }> = {
  L0: { label: 'L0 影子', icon: Eye, cls: 'bg-muted text-muted-foreground border-border', desc: '哨兵全部静默,只观察不动手' },
  L1: { label: 'L1 人审', icon: HandMetal, cls: 'bg-[var(--color-info-bg)] text-[var(--color-info)] border-[color-mix(in_srgb,var(--color-info)_35%,transparent)]', desc: '每次执行等人批准' },
  L2: { label: 'L2 自动', icon: Bolt, cls: 'bg-[var(--color-success-bg)] text-[var(--color-success)] border-[color-mix(in_srgb,var(--color-success)_35%,transparent)]', desc: '命中即执行' },
}

const DOT_META: Record<string, { cls: string; label: string }> = {
  success: { cls: 'bg-[var(--color-success)]', label: '执行成功' },
  failed: { cls: 'bg-[var(--color-danger)]', label: '执行失败' },
  rejected: { cls: 'bg-accent', label: '被拒绝' },
  pending: { cls: 'bg-[var(--color-warning-bg)]', label: '待审批' },
  executing: { cls: 'bg-[var(--color-info-bg)]', label: '执行中' },
}

const SENTINEL_STATUS_META = {
  online: { text: '在线', cls: 'text-[var(--color-success)]', dot: 'bg-[var(--color-success)]' },
  muted: { text: '影子', cls: 'text-[var(--color-warning)]', dot: 'bg-[var(--color-warning-bg)]' },
  disabled: { text: '停用', cls: 'text-[var(--color-text-tertiary)]', dot: 'bg-accent' },
} as const

const pct = (v: number | null) => (v === null ? '—' : `${Math.round(v * 100)}%`)
const fmtTime = (iso?: string | null) => iso
  ? formatDateTime(new Date(iso))
  : '-'

function LevelStepper({ level }: { level: AutonomyLevelKey }) {
  const steps = buildLevelSteps(level)
  return (
    <div className="flex items-center" aria-label={`当前自治等级 ${LEVEL_META[level].label}`}>
      {steps.map((step, index) => {
        const meta = LEVEL_META[step.key]
        return (
          <div key={step.key} className="flex items-center">
            {index > 0 && (
              <span className={`h-px w-4 ${steps[index - 1].reached && step.reached ? 'bg-brand' : 'bg-[var(--color-bg-active)]'}`} />
            )}
            <span
              title={meta.desc}
              className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium ${
                step.current ? `${meta.cls} gov-level-current` : step.reached ? 'border-brand-line bg-brand-soft text-brand-ink' : 'border-border bg-card text-muted-foreground'
              }`}
            >
              <meta.icon size={10} /> {step.key}
            </span>
          </div>
        )
      })}
    </div>
  )
}

function TimelineDots({ timeline }: { timeline: TimelineDot[] }) {
  if (!timeline.length) {
    return <span className="text-xs text-muted-foreground">还没有执行履历</span>
  }
  return (
    <div className="flex items-center gap-1.5" data-testid="autonomy-timeline">
      {timeline.map((dot, index) => {
        const meta = DOT_META[dot.status] || DOT_META.pending
        const detail = [
          `${meta.label} · ${fmtTime(dot.at)}`,
          dot.durationMs != null ? `耗时 ${dot.durationMs}ms` : '',
          dot.reason ? `原因:${dot.reason}` : '',
          dot.error ? `错误:${dot.error}` : '',
        ].filter(Boolean).join('\n')
        return (
          <span
            key={dot.id}
            className={`gov-dot-pop h-2.5 w-2.5 rounded-full ${meta.cls} ${dot.status === 'pending' || dot.status === 'executing' ? 'ring-2 ring-[var(--color-warning)]' : ''}`}
            style={{ animationDelay: `${index * 45}ms` }}
            title={detail}
            aria-label={detail}
          />
        )
      })}
      <span className="ml-1 text-xs text-muted-foreground">新 → 旧</span>
    </div>
  )
}


function PromoteCta({
  actionId,
  recommendation,
  recentCount,
  recentApprovalRate,
  promoteMinDecisions,
  promoteRate,
  onGoVersions,
}: {
  actionId: string
  recommendation: 'promote' | 'demote' | 'observe' | null
  recentCount: number
  recentApprovalRate: number | null
  promoteMinDecisions: number
  promoteRate: number
  onGoVersions: () => void
}) {
  const promoteEnabled = recommendation === 'promote'
  const promoteReason = buildPromoteCtaReason({
    recommendation,
    recentCount,
    recentApprovalRate,
    promoteMinDecisions,
    promoteRate,
  })
  const hintId = `promote-hint-${actionId}`
  return (
    <span className="inline-flex flex-col gap-0.5">
      {/* aria-disabled 保留悬停/聚焦，避免 HTML disabled 吞掉 title */}
      <button
        type="button"
        onClick={() => { if (!promoteEnabled) return; onGoVersions() }}
        aria-disabled={!promoteEnabled}
        aria-describedby={hintId}
        title={promoteReason}
        className={`inline-flex items-center gap-1 rounded-lg border px-2 py-1 text-xs ${
          promoteEnabled
            ? 'border-[var(--color-success)] bg-[var(--color-success)] text-[var(--color-text-inverse)] hover:bg-[var(--color-success)]'
            : 'cursor-not-allowed border-border text-muted-foreground opacity-70'
        }`}
      >
        <ArrowUpCircle size={12} /> 去草稿提升自治等级
      </button>
      <p id={hintId} className={`text-xs ${promoteEnabled ? 'text-[var(--color-success)]' : 'text-muted-foreground'}`}>
        {promoteReason}
      </p>
    </span>
  )
}

const GRID_COLS = 'xl:grid-cols-[minmax(0,1.25fr)_minmax(0,1.4fr)_minmax(0,1.1fr)_minmax(0,1.6fr)_minmax(0,1fr)_minmax(0,1.15fr)]'

function CellLabel({ children }: { children: React.ReactNode }) {
  return <p className="mb-1 text-xs font-medium text-muted-foreground xl:hidden">{children}</p>
}

export default function OperationsBoard({
  rows,
  timelines,
  onOpenPending,
  onGoVersions,
}: {
  rows: OperationsRow[]
  timelines: Record<string, TimelineDot[]>
  onOpenPending: (log: PendingLog) => void
  onGoVersions: () => void
}) {
  if (rows.length === 0) {
    return (
      <div className="py-3 text-center">
        <p className="text-xs text-muted-foreground">还没有动作。请在版本草稿中创建动作并绑定哨兵，发布后再在这里管理放权与裁决。</p>
        <button onClick={onGoVersions}
          className="mt-2 inline-flex items-center gap-1 text-xs text-[var(--color-warning)] hover:underline">
          去版本草稿创建动作
        </button>
      </div>
    )
  }

  return (
    <div data-testid="governance-operations-board">
      {/* 表头(宽屏可见;窄屏每行折叠为卡片,字段带小标签) */}
      <div className={`hidden gap-3 border-b border-border pb-2 text-xs font-medium text-muted-foreground xl:grid ${GRID_COLS}`}>
        <span>动作</span>
        <span>自治等级</span>
        <span>绑定哨兵</span>
        <span>待审批</span>
        <span>近期批准率</span>
        <span>近期执行履历</span>
      </div>

      <div className="divide-y border-border">
        {rows.map(row => {
          const { stat } = row
          const rate = stat.decisions.recentApprovalRate
          const hasPending = row.pendings.length > 0
          return (
            <div
              key={stat.actionId}
              className={`grid gap-3 py-3 xl:items-center ${GRID_COLS} ${
                hasPending ? '-mx-3 gov-row-pending rounded-lg border-l-2 border-[var(--color-warning)] px-3' : ''
              }`}
            >
              {/* 动作 */}
              <div>
                <CellLabel>动作</CellLabel>
                <p className="flex items-center gap-1.5 text-sm font-semibold text-foreground">
                  <Rocket size={13} className="shrink-0 text-[var(--color-warning)]" />
                  {stat.actionName}
                </p>
                <p className="mt-1 text-xs text-muted-foreground">
                  累计 批准 {stat.decisions.approved} · 拒绝 {stat.decisions.rejected} · 自动执行 {stat.autoRuns.total}
                  {stat.autoRuns.failed > 0 && <span className="text-[var(--color-danger)]">(失败 {stat.autoRuns.failed})</span>}
                </p>
              </div>

              {/* 自治等级 + 晋升建议 */}
              <div className="space-y-1.5">
                <CellLabel>自治等级</CellLabel>
                <div className="flex flex-wrap items-center gap-2">
                  <LevelStepper level={stat.level} />
                  {stat.level === 'L1' && (
                    <PromoteCta
                      actionId={stat.actionId}
                      recommendation={stat.recommendation}
                      recentCount={stat.decisions.recentCount}
                      recentApprovalRate={rate}
                      promoteMinDecisions={stat.thresholds.promoteMinDecisions}
                      promoteRate={stat.thresholds.promoteRate}
                      onGoVersions={onGoVersions}
                    />
                  )}
                  {stat.level === 'L2' && (
                    <button type="button" onClick={onGoVersions}
                      title="去草稿降低/调整自治等级"
                      className="inline-flex items-center gap-1 rounded-lg border border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] px-2 py-1 text-xs text-[var(--color-info)] hover:bg-[var(--color-info-bg)]">
                      <ArrowDownCircle size={12} /> 去草稿降低/调整自治等级
                    </button>
                  )}
                  {stat.level === 'L0' && <span className="text-xs text-muted-foreground">影子观察中</span>}
                </div>
                {stat.recommendationReason && stat.level !== 'L1' ? (
                  <p className={`text-xs ${
                    stat.recommendation === 'demote' ? 'text-[var(--color-danger)]' : 'text-muted-foreground'
                  }`}>{stat.recommendationReason}</p>
                ) : null}
              </div>

              {/* 绑定哨兵 */}
              <div className="space-y-1">
                <CellLabel>绑定哨兵</CellLabel>
                {row.sentinelViews.length === 0 ? (
                  <span className="text-xs text-muted-foreground">未绑定哨兵</span>
                ) : row.sentinelViews.map(sn => {
                  const meta = SENTINEL_STATUS_META[sn.status]
                  return (
                    <p key={sn.id} className="flex flex-wrap items-center gap-1.5 text-xs">
                      <span className={`relative h-1.5 w-1.5 rounded-full ${meta.dot} ${sn.status === 'online' ? 'gov-pulse' : ''}`} />
                      <span className="font-medium text-foreground">{sn.name}</span>
                      <span className={meta.cls}>{meta.text}</span>
                      {sn.recentHits > 0 && <span className="text-viz-rose">命中 {sn.recentHits}</span>}
                    </p>
                  )
                })}
              </div>

              {/* 待审批 */}
              <div className="space-y-1">
                <CellLabel>待审批</CellLabel>
                {row.pendings.length === 0 ? (
                  <span className="text-xs text-muted-foreground">—</span>
                ) : row.pendings.map(log => (
                  <button
                    key={log.id}
                    type="button"
                    onClick={() => onOpenPending(log)}
                    aria-label={`查看待审批详情:${log.actionName || log.actionId}`}
                    className="group flex w-full items-center gap-1.5 rounded-lg border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-card px-2 py-1 text-left transition hover:border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] hover:bg-[var(--color-warning-bg)]"
                  >
                    <HandMetal size={11} className="shrink-0 text-[var(--color-warning)]" />
                    <TruncatedText
                      className="min-w-0 flex-1 text-xs font-medium text-foreground"
                      text={log.objectInstanceId ? readableTargetSummary(log, `${log.objectInstanceId.slice(0, 10)}…`) : (log.actionName || log.actionId)}
                    />
                    <span className="shrink-0 text-xs text-muted-foreground">{fmtTime(log.executedAt)}</span>
                    <span className="shrink-0 text-xs text-brand-ink opacity-0 transition group-hover:opacity-100">查看详情</span>
                  </button>
                ))}
              </div>

              {/* 近期批准率 */}
              <div>
                <CellLabel>近期批准率</CellLabel>
                <div className="flex items-center gap-2">
                  <div className="relative h-1.5 w-full min-w-16 rounded-full bg-muted">
                    <div className={`h-full rounded-full transition-all duration-700 ${
                      rate !== null && rate >= stat.thresholds.promoteRate ? 'bg-[var(--color-success-bg)]'
                      : rate !== null && rate >= 0.5 ? 'bg-[var(--color-warning-bg)]' : 'bg-[var(--color-danger-bg)]'
                    }`} style={{ width: `${Math.round((rate ?? 0) * 100)}%` }} />
                    <span
                      className="absolute inset-y-0 w-px bg-accent"
                      style={{ left: `${stat.thresholds.promoteRate * 100}%` }}
                      title={`晋升线 ${Math.round(stat.thresholds.promoteRate * 100)}%`}
                    />
                  </div>
                  <span className="shrink-0 font-mono text-xs text-muted-foreground">{pct(rate)}</span>
                </div>
                <p className="mt-1 text-xs text-muted-foreground">({stat.decisions.recentCount}/{stat.thresholds.promoteMinDecisions} 次)</p>
              </div>

              {/* 近期执行履历 */}
              <div>
                <CellLabel>近期执行履历</CellLabel>
                <TimelineDots timeline={timelines[stat.actionId] || []} />
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
