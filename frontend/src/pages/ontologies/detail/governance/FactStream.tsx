/* 事实流 →「全量留痕」:每一个变化的出处与因果,追加不修改。
   渲染逻辑与原治理页一致,仅排版融入叙事时间线风格。 */
import { formatDateTime } from '@/utils/datetime'
import { formatDecisionValue, formatFactSource } from '../tabs/governanceFormat'

export interface FactRow {
  id: string
  subjectLabel: string
  propertyName: string
  value: unknown
  present?: boolean
  kind: string
  source: string
  actorId?: string | null
  causedBy?: string | null
  supersedesId?: string | null
  recordedAt: string | null
  ontologyVersion?: string | null
}

export const KIND_META: Record<string, { label: string; cls: string; title: string }> = {
  property: { label: '属性', cls: 'bg-[var(--color-info-bg)] text-[var(--color-info)] border-[color-mix(in_srgb,var(--color-info)_35%,transparent)]', title: '数据源/人工写入的存储属性变化' },
  derived: { label: '派生', cls: 'bg-viz-violet-soft text-viz-violet border-viz-violet-soft', title: '函数自动重算的派生值(可溯源到输入事实)' },
  link: { label: '链接', cls: 'bg-viz-cyan-soft text-viz-cyan border-viz-cyan-soft', title: '关系的建立/解除' },
  object: { label: '存在', cls: 'bg-[var(--color-danger-bg)] text-[var(--color-danger)] border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)]', title: '实例存在性(删除留墓碑)' },
  decision: { label: '决策', cls: 'bg-[var(--color-warning-bg)] text-[var(--color-warning)] border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)]', title: '人的审批决策(批准/拒绝都记录)' },
  absence: { label: '缺席', cls: 'bg-muted text-muted-foreground border-border', title: '查询结果为空/非空的翻转快照——"没有"也有出处' },
}

const fmtTime = (iso?: string | null) => iso
  ? formatDateTime(new Date(iso))
  : '-'
const fmtVal = (v: unknown) => {
  if (v === null || v === undefined) return '∅'
  const s = typeof v === 'object' ? JSON.stringify(v) : String(v)
  return s.length > 30 ? `${s.slice(0, 30)}…` : s
}

export default function FactStream({
  facts,
  kindFilter,
  onKindFilterChange,
}: {
  facts: FactRow[]
  kindFilter: string
  onKindFilterChange: (kind: string) => void
}) {
  return (
    <>
      <div className="mb-3 flex flex-wrap gap-1">
        <button onClick={() => onKindFilterChange('')}
          className={`rounded-full border px-2 py-0.5 text-[10px] ${!kindFilter ? 'border-viz-indigo-soft bg-viz-indigo-soft text-viz-indigo' : 'border-border text-[var(--color-text-tertiary)] hover:text-muted-foreground'}`}>
          全部
        </button>
        {Object.entries(KIND_META).map(([k, m]) => (
          <button key={k} onClick={() => onKindFilterChange(k)} title={m.title}
            className={`rounded-full border px-2 py-0.5 text-[10px] ${kindFilter === k ? m.cls : 'border-border text-[var(--color-text-tertiary)] hover:text-muted-foreground'}`}>
            {m.label}
          </button>
        ))}
      </div>
      {facts.length === 0 ? (
        <p className="py-3 text-center text-xs text-[var(--color-text-tertiary)]">暂无{kindFilter ? `「${KIND_META[kindFilter]?.label}」类` : ''}事实。</p>
      ) : (
        <div className="max-h-96 space-y-0.5 overflow-y-auto">
          {facts.map(f => {
            const decision = f.kind === 'decision' ? formatDecisionValue(f.value) : null
            const rawValue = f.value === null || f.value === undefined
              ? ''
              : typeof f.value === 'object' ? JSON.stringify(f.value) : String(f.value)
            return (
              <div key={f.id} className="flex items-center gap-2 border-b border-border py-1 text-xs last:border-0">
                <span className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] ${KIND_META[f.kind]?.cls ?? KIND_META.property.cls}`}
                  title={KIND_META[f.kind]?.title}>
                  {KIND_META[f.kind]?.label ?? f.kind}
                </span>
                <span className="max-w-[180px] truncate text-muted-foreground" title={f.subjectLabel}>{f.subjectLabel}</span>
                <span className="max-w-[110px] truncate font-mono text-[var(--color-text-tertiary)]">{f.propertyName}</span>
                <span className="text-[var(--color-text-tertiary)]">=</span>
                {decision ? (
                  <span className="flex-1 truncate" title={rawValue}>
                    <span className={decision.decision === 'approved' ? 'font-medium text-[var(--color-success)]' : 'font-medium text-[var(--color-danger)]'}>
                      {decision.decision === 'approved' ? '✓ 批准' : '✗ 拒绝'}
                    </span>
                    {decision.reason && <span className="text-muted-foreground">:{decision.reason}</span>}
                  </span>
                ) : (
                  <span
                    className="flex-1 truncate font-mono text-foreground"
                    title={f.present === false ? '属性已删除' : String(fmtVal(f.value))}
                  >
                    {f.present === false ? '(已删除)' : fmtVal(f.value)}
                  </span>
                )}
                {f.causedBy && (
                  <span className="shrink-0 text-[10px] text-[var(--color-text-tertiary)]"
                    title={`该变化由一次已批准的动作执行引起(指针 ${f.causedBy})`}>因果</span>
                )}
                {f.supersedesId && (
                  <span className="shrink-0 rounded bg-viz-violet-soft px-1 text-[10px] text-viz-violet"
                    title="该事实覆盖了同属性的旧值">覆盖</span>
                )}
                <span className="max-w-[110px] shrink-0 truncate text-[var(--color-text-tertiary)]" title={f.source}>{formatFactSource(f.source)}</span>
                <span className="shrink-0 text-[var(--color-text-tertiary)]">{fmtTime(f.recordedAt)}</span>
              </div>
            )
          })}
        </div>
      )}
    </>
  )
}
