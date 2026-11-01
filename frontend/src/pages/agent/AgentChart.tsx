/**
 * AgentChart — 智能助手回答里的轻量图表
 *
 * 助手在回答中输出一个 ```chart 围栏代码块（JSON spec），这里把它渲染成
 * 主题化的 SVG 图表（柱状 / 折线 / 面积 / 饼图），零第三方依赖、可 hover 查值。
 *
 * spec 结构（与后端系统提示里教给 agent 的格式一致）：
 *   {
 *     type: 'bar' | 'line' | 'area' | 'pie',
 *     title?, xLabel?, yLabel?,
 *     data: [{ label: string, value: number, ...多序列的其它数值键 }],
 *     series?: [{ key: string, name?: string }]   // 多序列时提供
 *   }
 */
import { useRef, useState } from 'react'
import { BarChart3 } from 'lucide-react'

// ---------- 类型 ----------

type ChartType = 'bar' | 'line' | 'area' | 'pie'

interface ChartSeries {
  key: string
  name?: string
}

export interface ChartSpec {
  type: ChartType
  title?: string
  xLabel?: string
  yLabel?: string
  data: Array<Record<string, unknown>>
  series?: ChartSeries[]
}

// ---------- 常量 / 工具 ----------

const PALETTE = ['#059669', '#0284c7', '#f59e0b', '#7c3aed', '#e11d48', '#15803d', '#0891b2', '#db2777']
const AXIS = 'var(--color-text-tertiary)'
const GRID = 'var(--color-border)'

const W = 680
const H = 272
const PAD = { l: 48, r: 18, t: 28, b: 40 }  // t 留白给顶部 y 轴单位标签
const IW = W - PAD.l - PAD.r
const IH = H - PAD.t - PAD.b

const toNum = (v: unknown): number => {
  const n = typeof v === 'number' ? v : parseFloat(String(v))
  return Number.isFinite(n) ? n : 0
}

/** 坐标轴用的紧凑数字：1.2万 / 3.4亿 */
function fmtCompact(n: number): string {
  if (!Number.isFinite(n)) return '—'
  const abs = Math.abs(n)
  if (abs >= 1e8) return `${(n / 1e8).toFixed(abs >= 1e9 ? 0 : 1)}亿`
  if (abs >= 1e4) return `${(n / 1e4).toFixed(abs >= 1e5 ? 0 : 1)}万`
  if (Number.isInteger(n)) return String(n)
  return n.toFixed(Math.abs(n) < 1 ? 2 : 1)
}

/** tooltip 用的完整数字，带千分位 */
const fmtFull = (n: number): string =>
  Number.isFinite(n) ? n.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) : '—'

const truncate = (s: string, max = 6): string => (s.length > max ? `${s.slice(0, max)}…` : s)

/** 生成「好看」的刻度区间（1/2/5 × 10^n） */
function niceScale(min: number, max: number, count = 4): { min: number; max: number; ticks: number[] } {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return { min: 0, max: 1, ticks: [0, 1] }
  if (min === max) { max = min + 1; min = min === 0 ? 0 : min - 1 }
  const niceNum = (range: number, round: boolean) => {
    const exp = Math.floor(Math.log10(range || 1))
    const frac = range / 10 ** exp
    const nf = round
      ? frac < 1.5 ? 1 : frac < 3 ? 2 : frac < 7 ? 5 : 10
      : frac <= 1 ? 1 : frac <= 2 ? 2 : frac <= 5 ? 5 : 10
    return nf * 10 ** exp
  }
  const step = niceNum(niceNum(max - min, false) / Math.max(1, count - 1), true)
  const niceMin = Math.floor(min / step) * step
  const niceMax = Math.ceil(max / step) * step
  const ticks: number[] = []
  for (let v = niceMin; v <= niceMax + step * 0.5; v += step) ticks.push(Number(v.toFixed(6)))
  return { min: niceMin, max: niceMax, ticks }
}

function arcPath(cx: number, cy: number, r: number, a0: number, a1: number): string {
  const large = a1 - a0 > Math.PI ? 1 : 0
  const x0 = cx + r * Math.cos(a0)
  const y0 = cy + r * Math.sin(a0)
  const x1 = cx + r * Math.cos(a1)
  const y1 = cy + r * Math.sin(a1)
  return `M ${cx} ${cy} L ${x0} ${y0} A ${r} ${r} 0 ${large} 1 ${x1} ${y1} Z`
}

// ---------- tooltip ----------

interface Hover {
  x: number
  y: number
  label: string
  rows: Array<{ name: string; value: number; color: string }>
}

function Tooltip({ hover }: { hover: Hover }) {
  return (
    <div
      className="pointer-events-none absolute z-10 min-w-[92px] max-w-[220px] -translate-x-1/2 -translate-y-[calc(100%+10px)] rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-2.5 py-1.5 shadow-lg"
      style={{ left: hover.x, top: hover.y }}
    >
      <div className="mb-1 truncate text-[11px] font-medium text-[var(--color-text-primary)]">{hover.label}</div>
      <div className="space-y-0.5">
        {hover.rows.map((r, i) => (
          <div key={i} className="flex items-center gap-1.5 text-[11px] leading-4">
            <span className="h-2 w-2 shrink-0 rounded-sm" style={{ background: r.color }} />
            {r.name && <span className="text-[var(--color-text-tertiary)]">{r.name}</span>}
            <span className="ml-auto font-mono font-medium text-[var(--color-text-primary)]">{fmtFull(r.value)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ---------- 直角坐标系（柱状 / 折线 / 面积） ----------

function CartesianChart({ spec, series, rows }: {
  spec: ChartSpec
  series: ChartSeries[]
  rows: Array<{ label: string; values: number[] }>
}) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const [hover, setHover] = useState<Hover | null>(null)
  const n = rows.length
  const isBar = spec.type === 'bar'

  const allValues = rows.flatMap(r => r.values)
  const dataMin = Math.min(...allValues)
  const dataMax = Math.max(...allValues)
  // 柱状图基线含 0；折线/面积按数据区间更能体现趋势
  const scale = isBar
    ? niceScale(Math.min(0, dataMin), Math.max(0, dataMax), 5)
    : niceScale(dataMin, dataMax, 5)
  const yFor = (v: number) => PAD.t + IH * (1 - (v - scale.min) / (scale.max - scale.min || 1))
  const baseY = yFor(Math.max(scale.min, Math.min(0, scale.max)))

  const slot = IW / Math.max(1, n)
  // 折线/面积首尾点内缩，避免数据点贴着 y 轴与右边缘、末端标签溢出画布
  const lineInset = Math.max(14, Math.min(48, IW * 0.05))
  const centerX = (i: number) => (
    isBar ? PAD.l + slot * (i + 0.5)
      : n === 1 ? PAD.l + IW / 2
      : PAD.l + lineInset + ((IW - 2 * lineInset) * i) / (n - 1)
  )

  // x 轴标签：过密时抽稀，并按每格宽度自适应截断长度
  const labelStep = n > 16 ? Math.ceil(n / 12) : 1
  const maxLabelChars = Math.max(3, Math.min(12, Math.floor(slot / 7)))

  const showHover = (e: React.MouseEvent, i: number) => {
    const rect = wrapRef.current?.getBoundingClientRect()
    if (!rect) return
    setHover({
      x: e.clientX - rect.left,
      y: e.clientY - rect.top,
      label: rows[i].label,
      rows: series.map((s, si) => ({ name: s.name || '', value: rows[i].values[si], color: PALETTE[si % PALETTE.length] })),
    })
  }

  const groupInnerW = slot * 0.72
  const laneW = groupInnerW / series.length
  const barW = Math.min(64, Math.max(2, laneW * 0.82))  // 上限避免柱子太少时过于粗壮

  return (
    <div ref={wrapRef} className="relative" onMouseLeave={() => setHover(null)}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={{ height: 'auto', display: 'block' }} role="img">
        {/* 网格 + y 刻度 */}
        {scale.ticks.map((t, i) => {
          const y = yFor(t)
          return (
            <g key={i}>
              <line x1={PAD.l} y1={y} x2={W - PAD.r} y2={y} stroke={GRID} strokeWidth={1} opacity={t === 0 ? 0.9 : 0.45} />
              <text x={PAD.l - 8} y={y + 3} textAnchor="end" fontSize={11} fill={AXIS}>{fmtCompact(t)}</text>
            </g>
          )
        })}

        {/* 面积填充 */}
        {spec.type === 'area' && series.map((s, si) => {
          const color = PALETTE[si % PALETTE.length]
          const line = rows.map((r, i) => `${centerX(i)},${yFor(r.values[si])}`).join(' ')
          const area = `M ${centerX(0)},${baseY} L ${rows.map((r, i) => `${centerX(i)},${yFor(r.values[si])}`).join(' L ')} L ${centerX(n - 1)},${baseY} Z`
          return (
            <g key={s.key}>
              <path d={area} fill={color} opacity={0.12} />
              <polyline points={line} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
            </g>
          )
        })}

        {/* 折线 */}
        {spec.type === 'line' && series.map((s, si) => {
          const color = PALETTE[si % PALETTE.length]
          const line = rows.map((r, i) => `${centerX(i)},${yFor(r.values[si])}`).join(' ')
          return <polyline key={s.key} points={line} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
        })}

        {/* 折线/面积的数据点（hover 命中区更大） */}
        {!isBar && rows.map((r, i) => (
          <g key={i}>
            {series.map((s, si) => (
              <circle key={s.key} cx={centerX(i)} cy={yFor(r.values[si])} r={hover?.label === r.label ? 4 : 2.5}
                fill="var(--color-bg-elevated)" stroke={PALETTE[si % PALETTE.length]} strokeWidth={2} />
            ))}
            <rect x={centerX(i) - slot / 2} y={PAD.t} width={slot} height={IH} fill="transparent"
              onMouseMove={e => showHover(e, i)} onMouseEnter={e => showHover(e, i)} />
          </g>
        ))}

        {/* 柱子 */}
        {isBar && rows.map((r, i) => (
          <g key={i}>
            {series.map((s, si) => {
              const v = r.values[si]
              const x = centerX(i) - groupInnerW / 2 + laneW * si + (laneW - barW) / 2
              const y = yFor(Math.max(v, 0))
              const h = Math.abs(yFor(v) - baseY)
              const active = hover?.label === r.label
              return (
                <rect key={s.key} x={x} y={v >= 0 ? y : baseY} width={barW} height={Math.max(1, h)} rx={2}
                  fill={PALETTE[si % PALETTE.length]} opacity={active ? 1 : 0.86}
                  onMouseMove={e => showHover(e, i)} onMouseEnter={e => showHover(e, i)} />
              )
            })}
          </g>
        ))}

        {/* 单序列柱状：柱顶直接标数值，免 hover 即可读 */}
        {isBar && series.length === 1 && n <= 12 && rows.map((r, i) => {
          const v = r.values[0]
          return (
            <text key={i} x={centerX(i)} y={v >= 0 ? yFor(v) - 5 : yFor(v) + 12}
              textAnchor="middle" fontSize={10} fontWeight={600} fill="var(--color-text-secondary)">
              {fmtCompact(v)}
            </text>
          )
        })}

        {/* x 轴标签 */}
        {rows.map((r, i) => (i % labelStep === 0 ? (
          <text key={i} x={centerX(i)} y={H - PAD.b + 18} textAnchor="middle" fontSize={11} fill={AXIS}>
            <title>{r.label}</title>{truncate(r.label, maxLabelChars)}
          </text>
        ) : null))}

        {/* 轴标题：y 单位放到顶部留白区（不与刻度数字打架），x 单位放右下角 */}
        {spec.yLabel && <text x={8} y={14} fontSize={10} fill={AXIS}>{spec.yLabel}</text>}
        {spec.xLabel && <text x={W - PAD.r} y={H - 4} textAnchor="end" fontSize={10} fill={AXIS}>{spec.xLabel}</text>}
      </svg>

      {series.length > 1 && <Legend items={series.map((s, i) => ({ name: s.name || s.key, color: PALETTE[i % PALETTE.length] }))} />}
      {hover && <Tooltip hover={hover} />}
    </div>
  )
}

// ---------- 饼图 ----------

function PieChart({ rows }: {
  rows: Array<{ label: string; values: number[] }>
}) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const [hover, setHover] = useState<Hover | null>(null)
  const slices = rows
    .map((r, i) => ({ label: r.label, value: Math.max(0, r.values[0]), color: PALETTE[i % PALETTE.length] }))
    .filter(s => s.value > 0)
  const total = slices.reduce((sum, s) => sum + s.value, 0)

  const cx = 150
  const cy = H / 2
  const r = 108

  const showHover = (e: React.MouseEvent, label: string, value: number, color: string) => {
    const rect = wrapRef.current?.getBoundingClientRect()
    if (!rect) return
    setHover({ x: e.clientX - rect.left, y: e.clientY - rect.top, label, rows: [{ name: `${((value / total) * 100).toFixed(1)}%`, value, color }] })
  }

  if (total <= 0) return <ChartNote text="图表暂无有效数据" />

  return (
    <div ref={wrapRef} className="relative" onMouseLeave={() => setHover(null)}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={{ height: 'auto', display: 'block' }} role="img">
        {slices.map((s, i) => {
          const frac = s.value / total
          const start = slices.slice(0, i).reduce((sum, x) => sum + x.value, 0)
          const a0 = -Math.PI / 2 + (start / total) * Math.PI * 2
          const a1 = a0 + frac * Math.PI * 2
          const active = hover?.label === s.label
          const mid = (a0 + a1) / 2
          const dx = active ? Math.cos(mid) * 6 : 0
          const dy = active ? Math.sin(mid) * 6 : 0
          // 单一整片时用整圆，避免 arc 退化
          const node = frac >= 0.9999
            ? <circle cx={cx} cy={cy} r={r} fill={s.color} />
            : <path d={arcPath(cx, cy, r, a0, a1)} fill={s.color} />
          const lx = cx + Math.cos(mid) * (r * 0.62)
          const ly = cy + Math.sin(mid) * (r * 0.62)
          return (
            <g key={i} transform={`translate(${dx} ${dy})`} style={{ transition: 'transform .12s', cursor: 'default' }}
              onMouseMove={e => showHover(e, s.label, s.value, s.color)} onMouseEnter={e => showHover(e, s.label, s.value, s.color)}>
              {node}
              {frac >= 0.06 && (
                <text x={lx} y={ly + 3} textAnchor="middle" fontSize={11} fontWeight={600} fill="#fff">
                  {Math.round(frac * 100)}%
                </text>
              )}
            </g>
          )
        })}
      </svg>
      <Legend
        className="absolute right-3 top-1/2 max-h-[92%] -translate-y-1/2 flex-col overflow-auto"
        items={slices.map(s => ({ name: s.label, color: s.color, value: s.value }))}
      />
      {hover && <Tooltip hover={hover} />}
    </div>
  )
}

// ---------- 图例 / 提示 ----------

function Legend({ items, className }: {
  items: Array<{ name: string; color: string; value?: number }>
  className?: string
}) {
  return (
    <div className={`flex flex-wrap items-center gap-x-3 gap-y-1 ${className ?? 'mt-1.5 justify-center'}`}>
      {items.map((it, i) => (
        <div key={i} className="flex items-center gap-1.5 text-[11px] text-[var(--color-text-secondary)]">
          <span className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: it.color }} />
          <span className="truncate max-w-[120px]" title={it.name}>{it.name}</span>
          {typeof it.value === 'number' && <span className="font-mono text-[var(--color-text-tertiary)]">{fmtCompact(it.value)}</span>}
        </div>
      ))}
    </div>
  )
}

function ChartNote({ text }: { text: string }) {
  return <div className="px-3 py-6 text-center text-xs text-[var(--color-text-tertiary)]">{text}</div>
}

// ---------- 主组件 ----------

export function AgentChart({ spec }: { spec: ChartSpec }) {
  const data = Array.isArray(spec.data) ? spec.data : []
  const series: ChartSeries[] = spec.series?.length
    ? spec.series
    : [{ key: 'value', name: spec.yLabel || '' }]
  const rows = data
    .map(d => ({ label: String(d?.label ?? ''), values: series.map(s => toNum(d?.[s.key])) }))
    .filter(r => r.label !== '' || r.values.some(v => v !== 0))

  const body = rows.length === 0
    ? <ChartNote text="图表暂无有效数据" />
    : spec.type === 'pie'
      ? <PieChart rows={rows} />
      : <CartesianChart spec={spec} series={series} rows={rows} />

  return (
    <figure className="my-3 overflow-hidden rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-elevated)]">
      <figcaption className="flex items-center gap-1.5 border-b border-[var(--color-border)] px-3 py-2 text-xs font-semibold text-[var(--color-text-primary)]">
        <BarChart3 size={13} className="text-brand-ink" />
        {spec.title || '数据图表'}
      </figcaption>
      <div className="px-3 py-2.5">{body}</div>
    </figure>
  )
}

// ---------- ```chart 代码块解析（容错，失败则回退原文） ----------

const VALID_TYPES: ChartType[] = ['bar', 'line', 'area', 'pie']

export function ChartBlock({ source }: { source: string }) {
  let spec: ChartSpec | null = null
  try {
    const parsed = JSON.parse(source.trim())
    if (parsed && VALID_TYPES.includes(parsed.type) && Array.isArray(parsed.data)) spec = parsed
  } catch {
    /* 交给下面的回退 */
  }

  if (!spec) {
    return (
      <div className="my-2">
        <pre className="overflow-x-auto rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-3 text-[12px] font-mono text-[var(--color-text-primary)]">{source}</pre>
        <p className="mt-1 text-[11px] text-[var(--color-text-tertiary)]">（图表数据未能解析，已按原文显示）</p>
      </div>
    )
  }
  return <AgentChart spec={spec} />
}
