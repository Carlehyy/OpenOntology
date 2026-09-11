import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { apiClient } from '@/api/client'
import { useEffect, useMemo, useState } from 'react'
import {
  Network, Database, GitBranch, Brain, Activity, Sparkles,
  Workflow, Gauge, ArrowUpRight, ArrowDownRight, RefreshCw, AlertTriangle, CheckCircle2,
  ShieldCheck, Boxes, Target, CircleDot, RadioTower, ClipboardCheck, Zap,
} from 'lucide-react'
import {
  useClock, AnimatedNumber, GlassCard, StatusDot,
  Sparkline, SignalBars, RadialGauge, DataFlowRibbon, WaveSignal,
} from './cockpit'
import { GovernanceHub, type FlowStage } from './flowengine'
import './cockpit.css'

interface Stats {
  ontology_count: number
  entity_count: number
  relation_count?: number
  logic_count: number
  action_count: number
  rule_hits?: number
  recent_ontologies: RecentOntology[]
  domain_counts?: Record<string, number>
  status_counts?: Record<string, number>
}
interface RecentOntology {
  id: number; name: string; domain?: string; status?: string
  entity_count?: number; logic_count?: number; action_count?: number; updated_at?: string
}

const DOMAIN_COLORS = ['#0891b2', '#d97706', '#6366f1', '#059669', '#16a34a', '#e11d48']
const seedSpark = (seed: number, n = 14) =>
  Array.from({ length: n }, (_, i) => 40 + 30 * Math.sin(seed + i * 0.7) + 14 * Math.sin(seed * 2 + i * 1.9))

type TimeWindowKey = '15m' | '24h' | '7d'
type DeltaTone = 'good' | 'warn' | 'bad' | 'neutral'

const TIME_WINDOWS: { key: TimeWindowKey; label: string; short: string; compare: string; ingest: string; decisions: number; multiplier: number }[] = [
  { key: '15m', label: '15分钟', short: '近15m', compare: '较上一15m', ingest: '18.6K', decisions: 18, multiplier: 0.18 },
  { key: '24h', label: '24小时', short: '近24h', compare: '较昨日', ingest: '86K', decisions: 218, multiplier: 1 },
  { key: '7d', label: '7天', short: '近7天', compare: '较上周', ingest: '642K', decisions: 1436, multiplier: 6.4 },
]

function DeltaBadge({ value, tone = 'good', label, className = '' }: { value: string; tone?: DeltaTone; label?: string; className?: string }) {
  const color = tone === 'bad' ? '#e11d48' : tone === 'warn' ? '#d97706' : tone === 'neutral' ? '#8b8ba3' : '#16a34a'
  const Icon = value.trim().startsWith('-') ? ArrowDownRight : ArrowUpRight
  return (
    <span className={`ck-delta ${className}`} style={{ color }}>
      <Icon size={10} />
      {label && <span className="text-[#94a3b8]">{label}</span>}
      <span>{value}</span>
    </span>
  )
}

/* 轻量"实时"遥测：每 2.6s 抖动一次 */
function useTelemetry() {
  const [t, setT] = useState(0)
  useEffect(() => {
    const id = setInterval(() => setT((v) => v + 1), 2600)
    return () => clearInterval(id)
  }, [])
  const jit = (base: number, amp: number, phase = 0) => Math.round(base + amp * Math.sin(t * 1.3 + phase) + amp * 0.4 * Math.sin(t * 2.7 + phase))
  return { ingest: jit(1284, 90), decisions: jit(46, 6, 1), latency: jit(38, 5, 2), tick: t }
}

/* 分区小标题（收进卡片条里，降低对深色背景的突兀感） */
function ZoneHeader({ text, en, color, icon: Icon }: { text: string; en: string; color: string; icon: React.ElementType }) {
  return (
    <div className="ck-panel shrink-0 flex items-center gap-2 px-3 py-1.5">
      <span className="block w-1 h-3 rounded-full" style={{ background: color, boxShadow: `0 0 6px ${color}` }} />
      <Icon size={12} style={{ color }} />
      <span className="text-[11.5px] font-semibold text-[#475569]">{text}</span>
      <span className="ck-mono text-[8px] text-[#a0a8b8] tracking-wider mt-0.5">{en}</span>
    </div>
  )
}

function ProgressRow({ label, value, color, meta, delta, deltaTone = 'good' }: { label: string; value: number; color: string; meta?: string; delta?: string; deltaTone?: DeltaTone }) {
  return (
    <div>
      <div className="flex items-center justify-between gap-1 text-[10.5px] mb-1">
        <span className="text-[#5a5a72] flex items-center gap-1.5 min-w-0 whitespace-nowrap truncate">
          <CircleDot size={9} style={{ color }} />
          {label}
        </span>
        <span className="ck-mono text-[#94a3b8] shrink-0 whitespace-nowrap flex items-center gap-1">
          {meta ?? `${value}%`}
          {delta && <DeltaBadge value={delta} tone={deltaTone} />}
        </span>
      </div>
      <div className="h-1.5 rounded-full overflow-hidden" style={{ background: 'rgba(15,23,42,0.09)' }}>
        <div
          className="h-full rounded-full"
          style={{
            width: `${value}%`,
            background: `linear-gradient(90deg, ${color}66, ${color})`,
            boxShadow: `0 0 8px ${color}80`,
          }}
        />
      </div>
    </div>
  )
}

function QualityRadar({ items }: { items: { label: string; value: number; color: string; delta?: string }[] }) {
  const size = 154
  const cx = size / 2
  const cy = size / 2
  const maxR = 56
  const point = (value: number, i: number, radius = maxR) => {
    const angle = -Math.PI / 2 + (i / items.length) * Math.PI * 2
    const r = radius * (value / 100)
    return { x: cx + Math.cos(angle) * r, y: cy + Math.sin(angle) * r }
  }
  const radar = items.map((it, i) => point(it.value, i)).map((p) => `${p.x},${p.y}`).join(' ')

  return (
    <div className="ck-quality-radar">
      <svg viewBox={`0 0 ${size} ${size}`} className="ck-quality-svg">
        <defs>
          <radialGradient id="qualityRadarFill" cx="50%" cy="50%" r="58%">
            <stop offset="0%" stopColor="#0891b2" stopOpacity="0.3" />
            <stop offset="100%" stopColor="#059669" stopOpacity="0.04" />
          </radialGradient>
          <filter id="qualityRadarGlow" x="-40%" y="-40%" width="180%" height="180%">
            <feGaussianBlur stdDeviation="2.4" result="blur" />
            <feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge>
          </filter>
        </defs>
        {[0.25, 0.5, 0.75, 1].map((r) => (
          <polygon
            key={r}
            points={items.map((_, i) => {
              const p = point(100, i, maxR * r)
              return `${p.x},${p.y}`
            }).join(' ')}
            fill="none"
            stroke="rgba(100,116,139,0.16)"
            strokeWidth="1"
          />
        ))}
        {items.map((it, i) => {
          const p = point(100, i)
          const label = point(118, i)
          return (
            <g key={it.label}>
              <line x1={cx} y1={cy} x2={p.x} y2={p.y} stroke="rgba(100,116,139,0.14)" />
              <circle cx={point(it.value, i).x} cy={point(it.value, i).y} r="2.3" fill={it.color} filter="url(#qualityRadarGlow)" />
              <text x={label.x} y={label.y} textAnchor="middle" dominantBaseline="middle" fontSize="8.5" fill="#5a5a72">{it.label}</text>
            </g>
          )
        })}
        <polygon points={radar} fill="url(#qualityRadarFill)" stroke="#0891b2" strokeWidth="1.4" filter="url(#qualityRadarGlow)" />
        <circle cx={cx} cy={cy} r="3" fill="#1a1a2e" />
      </svg>
      <div className="ck-quality-list">
        {items.map((q) => <ProgressRow key={q.label} label={q.label} value={q.value} color={q.color} meta={`${q.value}%`} delta={q.delta} />)}
      </div>
    </div>
  )
}

export default function OverviewPage() {
  const navigate = useNavigate()
  const clock = useClock()
  const tele = useTelemetry()
  const [timeWindow, setTimeWindow] = useState<TimeWindowKey>('24h')
  const windowMeta = TIME_WINDOWS.find((w) => w.key === timeWindow) ?? TIME_WINDOWS[1]
  const { data, isLoading } = useQuery<Stats>({
    queryKey: ['stats'],
    queryFn: () => apiClient.get('/overview/stats') as any,
  })

  const stats: Stats = data ?? {
    ontology_count: 0, entity_count: 0, relation_count: 0, logic_count: 0,
    action_count: 0, rule_hits: 0, recent_ontologies: [], domain_counts: {}, status_counts: {},
  }

  const entities = stats.entity_count || 0
  const ruleHits = stats.rule_hits ?? Math.max(128, stats.logic_count * 17 + 128)
  const domains = useMemo(() => {
    const dc = stats.domain_counts && Object.keys(stats.domain_counts).length
      ? stats.domain_counts
      : { 供应链: 4, 金融风控: 3, 制造: 2, 医疗: 2 }
    return Object.entries(dc).map(([name, count], i) => ({ name, count, color: DOMAIN_COLORS[i % DOMAIN_COLORS.length] }))
  }, [stats.domain_counts])

  const health = Math.min(99.4, 82 + Math.min(12, stats.ontology_count) + (stats.logic_count > 0 ? 4 : 0)).toFixed(1)
  const hitRate = Math.min(98, 70 + (stats.action_count % 25) + Math.round(stats.logic_count / 3))
  const coverage = Math.min(97, 74 + stats.logic_count * 2 + (entities % 12))
  const pendingApprovals = 4, anomalies = 2
  const decisionsWindow = timeWindow === '15m' ? Math.max(8, Math.round(tele.decisions * 0.38)) : windowMeta.decisions
  const ruleHitsWindow = Math.max(12, Math.round(ruleHits * windowMeta.multiplier))
  const entityDelta = timeWindow === '15m' ? 3 : timeWindow === '24h' ? 28 : 146
  const relationDelta = timeWindow === '15m' ? 5 : timeWindow === '24h' ? 47 : 318
  const ingestSources = [
    { n: 'MySQL · 供应商库', p: 94, c: '#16a34a', m: 'p95 42ms', d: '-6%' },
    { n: 'API · 采购系统', p: 71, c: '#0891b2', m: '2 待映射', d: '+2' },
    { n: 'Kafka · 事件流', p: 88, c: '#059669', m: '1.3K/s', d: '+11%' },
    { n: 'S3 · 归档数据湖', p: 82, c: '#6366f1', m: '批次 18', d: '+4%' },
  ]
  const qualitySignals = [
    { label: '完整', value: 96, color: '#16a34a', delta: '+1.8' },
    { label: '准确', value: 91, color: '#0891b2', delta: '+0.9' },
    { label: '一致', value: 89, color: '#059669', delta: '-0.4' },
    { label: '时效', value: 94, color: '#6366f1', delta: '+2.1' },
    { label: '血缘', value: 87, color: '#d97706', delta: '+3.6' },
  ]
  const decisionQueue = [
    { icon: Zap, label: '自动派单', value: '70%', color: '#16a34a' },
    { icon: ClipboardCheck, label: '人工复核', value: `${pendingApprovals}`, color: '#d97706' },
    { icon: AlertTriangle, label: '需干预', value: `${anomalies}`, color: '#e11d48' },
  ]

  /* 五段流程：左2=数据治理 · 中=本体图谱枢纽 · 右2=业务治理 */
  const flowStages: FlowStage[] = [
    { key: 'collect', zh: '数据采集', en: 'COLLECT', Icon: RadioTower, color: '#0891b2', main: windowMeta.ingest, mainLabel: windowMeta.short, metric: `${windowMeta.compare} +12%`, to: '/data' },
    { key: 'model', zh: '本体建模', en: 'MODEL', Icon: Network, color: '#059669', main: <AnimatedNumber value={stats.ontology_count} />, mainLabel: '本体项目', metric: `${windowMeta.short} +2 版本`, to: '/ontologies' },
    { key: 'graph', zh: '图谱实例', en: 'GRAPH', Icon: Boxes, color: '#6366f1', main: <AnimatedNumber value={entities} />, mainLabel: '实体节点', metric: `净增 ${entityDelta} 节点`, to: '/ontologies' },
    { key: 'analyze', zh: '智能分析', en: 'ANALYZE', Icon: Brain, color: '#d97706', main: <AnimatedNumber value={ruleHitsWindow} />, mainLabel: '规则命中', metric: `${windowMeta.compare} +8%`, to: '/agent' },
    { key: 'decide', zh: '智能决策', en: 'DECIDE', Icon: Target, color: '#e11d48', main: <AnimatedNumber value={tele.decisions} duration={600} />, mainLabel: '决策/分', metric: '自动 70%', to: '/agent' },
  ]

  /* 业务事件流 */
  const activityEvents = [
    { icon: Activity, c: '#d97706', title: '规则命中', detail: '循环担保风险规则触发', time: '刚刚', to: '/agent' },
    { icon: Target, c: '#e11d48', title: '决策执行', detail: '自动审批采购单 #PO-2048', time: '38秒前', to: '/agent' },
    { icon: GitBranch, c: '#6366f1', title: '本体发布', detail: 'v1.2 供应链本体已发布', time: '2分钟前', to: '/ontologies' },
    { icon: ShieldCheck, c: '#16a34a', title: '影子试跑', detail: '3 条规则待人工复核', time: '6分钟前', to: '/agent' },
  ]

  /* 顶部：面向治理的 actionable 指标（看了能行动，而非原始计数） */
  const kpis: { label: string; value: string; suffix?: string; icon: React.ElementType; color: string; trend: string; trendTone?: DeltaTone; tag?: string; to?: string; testId?: string }[] = [
    { label: '治理健康', value: health, suffix: '', icon: ShieldCheck, color: '#16a34a', trend: '+1.2%' },
    { label: '数据质量', value: '92.6', suffix: '%', icon: Database, color: '#0891b2', trend: '+0.8%' },
    { label: '规则覆盖', value: String(coverage), suffix: '%', icon: Workflow, color: '#059669', trend: '+3.1%' },
    { label: '待办审批', value: String(pendingApprovals), suffix: '', icon: CheckCircle2, color: '#d97706', trend: '-2', trendTone: 'good', tag: '待处理', to: '/data/pipelines/steward', testId: 'overview-kpi-approvals' },
    { label: '决策执行', value: String(decisionsWindow), suffix: '', icon: Target, color: '#6366f1', trend: '+18' },
    { label: '异常告警', value: String(anomalies), suffix: '', icon: AlertTriangle, color: '#dc2626', trend: '-2', trendTone: 'good', tag: '去处理', to: '/data' },
  ]

  const focus = stats.recent_ontologies?.[0] ?? { name: '供应链本体', domain: '供应链', entity_count: 42 }

  if (isLoading) return <LoadingCockpit />

  return (
    <div className="ck-root">
      {/* ═══ 顶栏：品牌 + 健康 + 时钟 + KPI ═══ */}
      <GlassCard pad={false} className="shrink-0 overflow-hidden">
        <div className="flex items-center justify-between gap-4 px-4 py-2.5">
          <div className="flex items-center gap-3">
            <div className="relative w-9 h-9 rounded-lg flex items-center justify-center shrink-0"
              style={{ background: 'linear-gradient(135deg, rgba(8,145,178,0.22), rgba(13,148,136,0.07))', border: '1px solid rgba(8,145,178,0.38)', boxShadow: '0 0 18px -6px rgba(8,145,178,0.5)' }}>
              <Network size={18} className="text-[#0891b2]" />
            </div>
            <div>
              <h1 className="text-[16px] font-semibold text-[#1a1a2e] leading-none">本体治理中枢</h1>
              <span className="ck-label text-[9px]" style={{ letterSpacing: '0.2em' }}>ONTOLOGY GOVERNANCE COCKPIT</span>
            </div>
            <div className="hidden md:flex ck-window-switch ml-2">
              {TIME_WINDOWS.map((w) => (
                <button
                  key={w.key}
                  type="button"
                  onClick={() => setTimeWindow(w.key)}
                  className={timeWindow === w.key ? 'is-active' : ''}
                >
                  {w.label}
                </button>
              ))}
            </div>
          </div>
          <div className="flex items-center gap-4">
            <div className="hidden lg:flex items-center gap-2.5 px-3 py-1.5 rounded-full"
              style={{ background: 'rgba(22,163,74,0.1)', border: '1px solid rgba(22,163,74,0.3)' }}>
              <StatusDot color="#16a34a" />
              <span className="text-[12px] text-[#16a34a] font-medium">系统运行中</span>
              <span className="text-[11px] text-[#a0a8b8]">·</span>
              <span className="text-[12px] text-[#475569]">治理健康 <b className="ck-num text-[#1a1a2e]">{health}</b></span>
            </div>
            <div className="flex items-baseline gap-1">
              <span className="ck-mono text-[19px] text-[#1a1a2e] ck-glow-cyan tracking-wider">{clock.time}</span>
              <span className="ck-mono text-[11px] text-[#0e7490] w-5">{clock.ms}</span>
            </div>
            <button onClick={() => navigate(0)} title="刷新"
              className="w-8 h-8 rounded-lg flex items-center justify-center text-[#64748b] hover:text-[#0891b2] transition"
              style={{ background: 'rgba(15,23,42,0.045)', border: '1px solid rgba(15,23,42,0.12)' }}>
              <RefreshCw size={14} />
            </button>
          </div>
        </div>
        <div className="grid grid-cols-3 lg:grid-cols-6 border-t border-[rgba(15,23,42,0.09)]">
          {kpis.map((k, i) => (
            <div key={k.label} data-testid={k.testId} onClick={() => k.to && navigate(k.to)}
              className={`flex items-center gap-2 px-2.5 py-2 ${i > 0 ? 'border-l border-[rgba(15,23,42,0.08)]' : ''} ${k.to ? 'cursor-pointer' : ''} hover:bg-[rgba(13,148,136,0.07)] transition`}>
              <div className="w-7 h-7 rounded-lg flex items-center justify-center shrink-0"
                style={{ background: `${k.color}18`, border: `1px solid ${k.color}33`, boxShadow: `inset 0 1px 0 rgba(255,255,255,0.18), 0 0 12px -6px ${k.color}` }}>
                <k.icon size={14} style={{ color: k.color }} />
              </div>
              <div className="min-w-0 flex-1">
                <div className="text-[10px] text-[#8b8ba3] leading-none whitespace-nowrap truncate">{k.label}</div>
                <div className="text-[18px] font-semibold text-[#1a1a2e] leading-tight ck-num whitespace-nowrap">
                  {k.value}<span className="text-[11px] text-[#8b8ba3] font-normal ml-0.5">{k.suffix}</span>
                </div>
                <div className="mt-0.5 flex items-center gap-1 overflow-hidden text-[8.5px]">
                  <span className="text-[#94a3b8] shrink-0">{windowMeta.short}</span>
                  <DeltaBadge value={k.trend} tone={k.trendTone ?? 'good'} label={windowMeta.compare} />
                </div>
              </div>
              {k.tag ? (
                <span className="hidden 2xl:inline-flex text-[9px] font-medium px-1.5 py-0.5 rounded-full whitespace-nowrap shrink-0"
                  style={{ background: `${k.color}1c`, color: k.color, border: `1px solid ${k.color}44` }}>{k.tag}</span>
              ) : null}
            </div>
          ))}
        </div>
      </GlassCard>

      {/* ═══ 主区：数据治理 · 本体图谱枢纽 · 业务治理 ═══ */}
      <div className="ck-grid grid grid-cols-1 lg:grid-cols-12 gap-3 flex-1 min-h-0 mt-3">

        {/* ── 左：数据治理 ── */}
        <div className="lg:col-span-3 flex flex-col gap-2.5 min-h-0">
          <ZoneHeader text="数据治理" en="DATA GOVERNANCE" color="#0891b2" icon={Database} />

          <GlassCard pad={false} hover className="shrink-0 p-3">
            <div className="flex items-end justify-between">
              <div>
                <div className="text-[10px] text-[#8b8ba3]">{windowMeta.short}接入吞吐</div>
                <div className="flex items-baseline gap-1">
                  <span className="text-[26px] font-semibold text-[#1a1a2e] ck-num leading-none"><AnimatedNumber value={tele.ingest} duration={600} /></span>
                  <span className="text-[10px] text-[#94a3b8]">rec/s</span>
                </div>
                <DeltaBadge value="+12%" label={windowMeta.compare} />
              </div>
              <Sparkline data={seedSpark(9, 16)} color="#0891b2" width={86} height={34} />
            </div>
            <div className="grid grid-cols-3 gap-2 mt-2.5">
              {[{ l: '活跃源', v: 9, c: '#0891b2', d: '+1' }, { l: '接入', v: windowMeta.ingest, c: '#059669', d: '+12%' }, { l: '待映射', v: 2, c: '#d97706', d: '-2' }].map((s) => (
                <div key={s.l} className="rounded-lg px-2 py-1.5" style={{ background: 'rgba(15,23,42,0.035)', border: '1px solid rgba(15,23,42,0.08)' }}>
                  <div className="text-[14px] font-semibold ck-num" style={{ color: s.c }}>{s.v}</div>
                  <div className="flex items-center justify-between gap-0.5">
                    <span className="text-[9px] text-[#94a3b8] truncate">{s.l}</span>
                    <DeltaBadge value={s.d} tone={s.l === '待映射' ? 'good' : 'good'} />
                  </div>
                </div>
              ))}
            </div>
            <div className="mt-2.5 rounded-lg overflow-hidden" style={{ background: 'rgba(15,23,42,0.04)', border: '1px solid rgba(15,23,42,0.07)' }}>
              <DataFlowRibbon height={34} />
            </div>
          </GlassCard>

          <GlassCard pad={false} hover className="flex-1 min-h-0 p-3 flex flex-col">
            <div className="flex items-center justify-between shrink-0">
              <span className="text-[10px] ck-label">采集链路 · ROUTES</span>
              <span className="text-[9px] ck-mono text-[#94a3b8]">{windowMeta.short} · SLA 99.1%</span>
            </div>
            <div className="flex-1 min-h-0 flex flex-col justify-around py-1">
              {ingestSources.map((src) => <ProgressRow key={src.n} label={src.n} value={src.p} color={src.c} meta={src.m} delta={src.d} deltaTone={src.n.includes('API') ? 'warn' : 'good'} />)}
            </div>
          </GlassCard>

          <GlassCard pad={false} hover className="flex-1 min-h-0 p-3 flex flex-col">
            <div className="flex items-center justify-between shrink-0">
              <span className="text-[10px] ck-label">质量雷达 · QUALITY</span>
              <span className="flex items-center gap-1 text-[11px] font-semibold text-[#16a34a]"><ShieldCheck size={11} />92.6 <DeltaBadge value="+1.1" /></span>
            </div>
            <div className="flex-1 min-h-0 flex items-center">
              <QualityRadar items={qualitySignals} />
            </div>
          </GlassCard>
        </div>

        {/* ── 中：本体图谱枢纽 ── */}
        <div className="lg:col-span-6 flex flex-col min-h-0">
          <GlassCard pad={false} hover className="h-full flex flex-col min-h-0">
            <div className="flex items-center justify-between px-4 pt-3 pb-1 shrink-0">
              <div className="flex items-center gap-2 min-w-0">
                <span className="block w-1 h-3.5 rounded-full" style={{ background: '#0891b2', boxShadow: '0 0 8px #0891b2' }} />
                <span className="text-[13px] font-semibold text-[#1a1a2e]">本体图谱治理闭环</span>
                <span className="ck-mono text-[8px] text-[#a0a8b8] tracking-wider mt-0.5 truncate">CLOSED-LOOP · 采集→建模→图谱→分析→决策→反馈</span>
              </div>
              <span className="flex items-center gap-1.5 px-2 py-1 rounded-full text-[10px] font-semibold text-[#0891b2]"
                style={{ background: 'rgba(8,145,178,0.12)', border: '1px solid rgba(8,145,178,0.32)' }}>
                <span className="w-1.5 h-1.5 rounded-full bg-[#0891b2] ck-blink" /> 治理中
              </span>
            </div>
            <div className="flex-1 min-h-0 flex flex-col">
              <div className="flex-1 min-h-0 relative">
                <GovernanceHub stages={flowStages} onNavigate={navigate} />
              </div>
              {/* 治理波形：对应规则命中、推理扫描和决策反馈的实时活跃度 */}
              <div className="shrink-0 px-4 pt-1 pb-1.5 relative">
                <div className="flex items-center justify-between mb-0.5">
                  <span className="ck-label text-[9px]">规则活跃波形 · {windowMeta.short} SIGNAL</span>
                  <div className="flex items-center gap-3 text-[8.5px] ck-mono text-[#a0a8b8]">
                    <span className="flex items-center gap-1"><span className="w-1.5 h-1.5 rounded-full" style={{ background: '#0891b2' }} />采集</span>
                    <span className="flex items-center gap-1"><span className="w-1.5 h-1.5 rounded-full" style={{ background: '#059669' }} />分析</span>
                    <span className="flex items-center gap-1"><span className="w-1.5 h-1.5 rounded-full" style={{ background: '#d97706' }} />决策</span>
                  </div>
                </div>
                <WaveSignal height={96} />
              </div>
            </div>
            <div className="grid grid-cols-5 border-t border-[rgba(15,23,42,0.09)] shrink-0">
              {[
                { l: '实体净增', v: <AnimatedNumber value={entityDelta} />, c: '#059669', s: `${windowMeta.compare} +9%` },
                { l: '关系净增', v: <AnimatedNumber value={relationDelta} />, c: '#6366f1', s: `${windowMeta.compare} +14%` },
                { l: '领域活跃', v: <AnimatedNumber value={domains.length} />, c: '#d97706', s: `${windowMeta.short}` },
                { l: '规则命中', v: <AnimatedNumber value={ruleHitsWindow} />, c: '#16a34a', s: `${windowMeta.compare} +8%` },
                { l: '端到端', v: <span className="ck-num">{(tele.latency / 20).toFixed(1)}s</span>, c: '#0891b2', s: 'p95 -0.3s' },
              ].map((m, i) => (
                <div key={m.l} className={`px-3 py-2 ${i > 0 ? 'border-l border-[rgba(15,23,42,0.08)]' : ''}`}>
                  <div className="text-[15px] font-semibold text-[#1a1a2e] leading-none" style={{ color: m.c }}>{m.v}</div>
                  <div className="text-[9px] text-[#94a3b8] mt-1">{m.l}</div>
                  <div className="text-[8px] ck-mono text-[#a0a8b8] mt-0.5 truncate">{m.s}</div>
                </div>
              ))}
            </div>
          </GlassCard>
        </div>

        {/* ── 右：业务治理 ── */}
        <div className="lg:col-span-3 flex flex-col gap-2.5 min-h-0">
          <ZoneHeader text="业务治理" en="BUSINESS GOVERNANCE" color="#e11d48" icon={Brain} />

          <GlassCard pad={false} hover className="shrink-0 p-3">
            <div className="flex items-center justify-between mb-2">
              <span className="text-[10px] ck-label">智能分析焦点</span>
              <Sparkles size={13} className="text-[#d97706]" />
            </div>
            <div className="rounded-lg p-2.5" style={{ background: 'linear-gradient(135deg, rgba(217,119,6,0.12), rgba(217,119,6,0.04))', border: '1px solid rgba(217,119,6,0.24)' }}>
              <div className="flex items-center justify-between">
                <div className="min-w-0">
                  <div className="text-[13px] font-semibold text-[#1a1a2e] truncate">{focus.name}</div>
                  <div className="text-[10px] text-[#a16207] mt-0.5">{focus.domain ?? '供应链'} · {windowMeta.short} 高中心度</div>
                </div>
                <div className="w-9 h-9 rounded-full flex items-center justify-center shrink-0" style={{ background: 'radial-gradient(circle, rgba(217,119,6,0.4), rgba(217,119,6,0.08))', border: '1px solid rgba(217,119,6,0.4)' }}>
                  <Brain size={16} className="text-[#d97706]" />
                </div>
              </div>
              <div className="flex items-center justify-between gap-2 mt-2.5">
                <span className="min-w-0 truncate text-xs text-[#5a5a72]">置信度 · {windowMeta.compare} +2.4%</span>
                <div className="flex shrink-0 items-center gap-1.5 whitespace-nowrap"><SignalBars level={5} color="#16a34a" /><span className="text-xs text-[#16a34a]">Strong</span></div>
              </div>
            </div>
            <div className="grid grid-cols-3 gap-2 mt-2.5">
              {decisionQueue.map((q) => (
                <div key={q.label} className="rounded-lg px-2 py-1.5" style={{ background: 'rgba(15,23,42,0.035)', border: `1px solid ${q.color}26` }}>
                  <div className="flex items-center justify-between">
                    <q.icon size={11} style={{ color: q.color }} />
                    <span className="text-[14px] font-semibold ck-num" style={{ color: q.color }}>{q.value}</span>
                  </div>
                  <div className="text-[9px] text-[#94a3b8] mt-0.5 truncate">{q.label}</div>
                </div>
              ))}
            </div>
          </GlassCard>

          <GlassCard pad={false} hover className="shrink-0 p-3">
            <div className="flex items-center justify-between mb-1">
              <span className="text-[10px] ck-label">智能决策 · DECISION</span>
              <Gauge size={13} className="text-[#e11d48]" />
            </div>
            <div className="grid grid-cols-2">
              <RadialGauge value={tele.decisions} label="决策吞吐" unit="次/分" color="#0891b2" size={112} sub={`${windowMeta.compare} +18`} />
              <RadialGauge value={hitRate} label="命中率" unit="%" color="#d97706" size={112} sub={`${windowMeta.compare} +3.6%`} />
            </div>
          </GlassCard>

          <GlassCard pad={false} hover className="flex-1 min-h-0 p-2.5 flex flex-col">
            <div className="flex items-center justify-between mb-1 shrink-0">
              <span className="text-[10px] ck-label">{windowMeta.short}事件流 · ACTIVITY</span>
              <span className="flex items-center gap-1 text-[9px] ck-mono text-[#e11d48]"><span className="w-1.5 h-1.5 rounded-full bg-[#e11d48] ck-blink" />REC</span>
            </div>
            <div className="flex-1 min-h-0 overflow-hidden space-y-0.5">
              {activityEvents.map((e, i) => {
                const active = i === tele.tick % activityEvents.length
                return (
                  <div key={e.title} onClick={() => e.to && navigate(e.to)}
                    className="flex items-center gap-1.5 px-1.5 py-1 rounded-lg cursor-pointer transition-all"
                    style={active ? { background: `${e.c}12`, border: `1px solid ${e.c}38` } : { border: '1px solid transparent' }}>
                    <div className="w-5 h-5 rounded-md flex items-center justify-center shrink-0" style={{ background: `${e.c}1c`, border: `1px solid ${e.c}3a` }}>
                      <e.icon size={10} style={{ color: e.c }} />
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-[10.5px] text-[#334155] font-medium truncate">{e.title}</span>
                        <span className="text-[9px] text-[#94a3b8] shrink-0 ck-mono">{active ? '刚刚' : e.time}</span>
                      </div>
                      <div className="text-[9px] text-[#8b8ba3] truncate">{e.detail}</div>
                    </div>
                  </div>
                )
              })}
            </div>
          </GlassCard>
        </div>
      </div>
    </div>
  )
}

/* ── 深色骨架屏 ── */
function LoadingCockpit() {
  return (
    <div className="ck-root">
      <div className="ck-skeleton h-24 shrink-0" />
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-3 flex-1 min-h-0 mt-3">
        <div className="lg:col-span-3 flex flex-col gap-2.5"><div className="ck-skeleton flex-1" /></div>
        <div className="lg:col-span-6"><div className="ck-skeleton h-full" /></div>
        <div className="lg:col-span-3 flex flex-col gap-2.5"><div className="ck-skeleton flex-1" /></div>
      </div>
    </div>
  )
}
