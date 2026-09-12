import { useMemo, useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, ScanEye, ShieldAlert } from 'lucide-react'
import { explorationApi, type OntologyPreviewDisposition } from '@/api/exploration'
import { Badge } from '@/components/ui/Badge'
import { DISPOSITION_LABELS, summarizeOntologyPreview } from './ontologyPreview'

const DISPOSITION_BADGE = {
  add: 'success',
  exists: 'secondary',
  conflict: 'warning',
} as const satisfies Record<OntologyPreviewDisposition, string>

/** 展开态每类集合的渲染上限：超出以「等 X 项」汇总，避免大画布撑爆面板 DOM。 */
const PREVIEW_ITEMS_CAP = 20

/**
 * 本体模型视图顶部的「预览投影」区块（只读）：把当前画布经确定性转换后的
 * 五类集合与绑定版本基线比对，实时展示将新增/跳过/冲突的去向。
 * 版本落库仍走「需求文档 → 生成本体模型」的质量门+人工确认链路，这里不写库。
 * 查询键带 canvasVersion：画布每次改动（SSE canvas 事件 / 会话加载）后失效重取；
 * keepPreviousData 让换版本期间保留上一份数据，避免流式回合内反复闪「计算中」。
 */
export default function OntologyPreviewPanel({ sessionId, canvasVersion }: {
  sessionId: string
  canvasVersion: number
}) {
  const [expanded, setExpanded] = useState(false)
  const { data, isError, isPending } = useQuery({
    queryKey: ['bx-ontology-preview', sessionId, canvasVersion],
    queryFn: () => explorationApi.ontologyPreview(sessionId),
    placeholderData: keepPreviousData,
  })
  const view = useMemo(() => (data ? summarizeOntologyPreview(data) : null), [data])
  const blockingCount = view?.blockingIssues.length ?? 0

  return (
    <div
      data-testid="ontology-preview-panel"
      className="overflow-hidden rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-elevated)]"
    >
      <button
        type="button"
        onClick={() => setExpanded(value => !value)}
        aria-expanded={expanded}
        data-testid="ontology-preview-toggle"
        className="flex w-full items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-[var(--color-bg-hover)]"
      >
        <span className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-md ${blockingCount > 0
          ? 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]'
          : 'bg-brand-soft text-brand-ink'}`}>
          <ScanEye size={13} />
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-1.5 text-xs font-medium text-[var(--color-text-primary)]">
            预览投影
            {view && (
              <span className="flex items-center gap-1" data-testid="ontology-preview-summary">
                {view.totals.add > 0 && <Badge variant="success">新增 {view.totals.add}</Badge>}
                {view.totals.exists > 0 && <Badge variant="secondary">跳过 {view.totals.exists}</Badge>}
                {view.totals.conflict > 0 && <Badge variant="warning">冲突 {view.totals.conflict}</Badge>}
                {view.totals.total === 0 && (
                  <span className="text-[10px] font-normal text-[var(--color-text-tertiary)]">暂无可投影元素</span>
                )}
              </span>
            )}
          </span>
          <span className="mt-0.5 block truncate text-[11px] text-[var(--color-text-tertiary)]">
            {isPending
              ? '正在计算画布的本体投影…'
              : isError
                ? '预览投影加载失败'
                : data?.bound
                  ? '画布 → 绑定版本的实时去向 · 只读不落库'
                  : '未绑定本体版本 · 应用时将新建本体'}
          </span>
        </span>
        {expanded
          ? <ChevronDown size={13} className="shrink-0 text-[var(--color-text-tertiary)]" />
          : <ChevronRight size={13} className="shrink-0 text-[var(--color-text-tertiary)]" />}
      </button>
      {blockingCount > 0 && (
        <div
          data-testid="ontology-preview-blocking"
          className="flex items-start gap-2 border-t border-[var(--color-border)] bg-[var(--color-danger-bg)] px-3 py-2"
        >
          <ShieldAlert size={12} className="mt-0.5 shrink-0 text-[var(--color-danger)]" />
          <span className="text-[11px] leading-5 text-[var(--color-danger)]">
            {blockingCount} 项语义无法无损转换（生成草稿会被质量门拦截）：{view!.blockingIssues[0].message}
            {blockingCount > 1 ? ` 等 ${blockingCount} 项` : ''}
          </span>
        </div>
      )}
      {expanded && view && (
        <div className="space-y-2.5 border-t border-[var(--color-border)] px-3 pb-3 pt-2.5">
          <div className="flex items-center gap-2 text-[11px] text-[var(--color-text-tertiary)]">
            <span>
              质量门 {data!.readiness.gatesPassed}/{data!.readiness.gatesTotal} 通过
              {data!.readiness.blockingCount > 0 && ` · 堵门 ${data!.readiness.blockingCount} 项`}
            </span>
            <span className="ml-auto font-mono text-[10px]" title={`画布指纹 ${data!.canvasFingerprint}`}>
              canvas v{data!.canvasVersion}
            </span>
          </div>
          {view.totals.total === 0 && (
            <p className="text-[11px] leading-5 text-[var(--color-text-tertiary)]">
              画布还没有可投影的对象/行为/规则 —— 随对话澄清，这里会实时显示它们将写成本体的什么。
            </p>
          )}
          {view.collections.filter(coll => coll.total > 0).map(coll => (
            <div key={coll.key} data-testid={`ontology-preview-${coll.key}`}>
              <div className="flex items-center gap-1.5 text-[11px] font-medium text-[var(--color-text-secondary)]">
                {coll.label}
                <span className="rounded bg-muted px-1 py-px text-[10px] text-muted-foreground">{coll.total}</span>
              </div>
              <ul className="mt-1 flex flex-wrap gap-1">
                {coll.items.slice(0, PREVIEW_ITEMS_CAP).map(collItem => (
                  <li key={collItem.key}>
                    <Badge
                      variant={DISPOSITION_BADGE[collItem.disposition]}
                      title={`${collItem.name} · ${DISPOSITION_LABELS[collItem.disposition]}`}
                    >
                      {collItem.displayName}
                      <span className="ml-1 opacity-70">{DISPOSITION_LABELS[collItem.disposition]}</span>
                    </Badge>
                  </li>
                ))}
                {coll.items.length > PREVIEW_ITEMS_CAP && (
                  <li className="inline-flex items-center text-[10px] text-[var(--color-text-tertiary)]">
                    等 {coll.items.length} 项
                  </li>
                )}
              </ul>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
