import { useEffect, useRef, useState } from 'react'
import { Bot, Check, Sparkles, X } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Alert } from '@/components/ui/Alert'
import operationsReportsApi, { operationsApiErrorText, type OperationsDatasetColumn } from '@/api/v2/operations-reports'
import { diffReportPlanBlocks, normalizeReportPlanCandidate, planBlockLabel, PLAN_AGG_LABELS, PLAN_BUCKET_LABELS, PLAN_CHART_LABELS, PLAN_FILTER_TYPE_LABELS, PLAN_QUERY_LIMIT_DEFAULT, type PlanBlock, type ReportPlan } from '../reportPlanModel'
import ReportPlanEditor from './ReportPlanEditor'
import ReportPlanPreview from './ReportPlanPreview'

function blockDescription(block: PlanBlock | null): string {
  if (!block) return '无'
  if (block.type === 'text') return block.text
  if (block.type === 'chart') return `${PLAN_CHART_LABELS[block.chart]} · ${block.query} · ${[block.x, ...(block.y ?? [])].filter(Boolean).join('、') || '全部输出列'}`
  const metrics = block.columns.map(column => `${column.field || '全部记录'}${column.agg ? ` · ${PLAN_AGG_LABELS[column.agg]}` : ''}${column.alias ? ` → ${column.alias}` : ''}`)
  if (block.time_bucket) metrics.push(`${block.time_bucket.field} · ${PLAN_BUCKET_LABELS[block.time_bucket.unit]}${block.time_bucket.alias ? ` → ${block.time_bucket.alias}` : ''}`)
  if (block.sort?.length) metrics.push(`排序：${block.sort.map(key => `${key.field} ${key.dir === 'asc' ? '升序' : '降序'}`).join('、')}`)
  metrics.push(`最多 ${block.limit ?? PLAN_QUERY_LIMIT_DEFAULT} 行`)
  return metrics.join('；')
}

export default function ReportAIPanel({ reportId, datasetId, datasetName, columns, plan, onAccept }: {
  reportId: string
  datasetId: string
  datasetName: string
  columns: OperationsDatasetColumn[]
  plan: ReportPlan
  onAccept: (candidate: ReportPlan) => void
}) {
  const [brief, setBrief] = useState('')
  const [candidate, setCandidate] = useState<{ plan: ReportPlan; baseline: string; explanation: string } | null>(null)
  const [tested, setTested] = useState(false)
  const [error, setError] = useState('')
  const [running, setRunning] = useState(false)
  const requestToken = useRef(0)
  const fingerprint = JSON.stringify(plan)
  const stale = !!candidate && candidate.baseline !== fingerprint

  useEffect(() => () => { requestToken.current += 1 }, [])

  const design = async () => {
    if (!brief.trim()) return
    const token = ++requestToken.current
    setRunning(true)
    setError('')
    setCandidate(null)
    setTested(false)
    try {
      const result = await operationsReportsApi.designPlan(reportId, {
        dataset_id: datasetId,
        brief: brief.trim(),
        current_plan: plan,
      })
      if (requestToken.current !== token) return
      const next = normalizeReportPlanCandidate(result.plan)
      if (!next) throw new Error('AI 返回的计划无法识别')
      setCandidate({ plan: next, baseline: fingerprint, explanation: result.explanation })
    } catch (nextError) {
      if (requestToken.current === token) {
        setError(operationsApiErrorText(nextError, 'AI 设计暂时不可用，请检查模型配置或稍后重试'))
      }
    } finally {
      if (requestToken.current === token) setRunning(false)
    }
  }

  return (
    <section className="space-y-3 rounded-xl border border-border bg-card p-4" data-testid="report-ai-panel">
      <div className="flex items-center gap-2">
        <Bot size={17} className="text-brand-ink" />
        <div>
          <p className="text-sm font-medium text-foreground">AI 设计助手</p>
          <p className="text-xs text-muted-foreground">描述业务目标，试跑并接受候选后，再确认保存。</p>
        </div>
      </div>
      <textarea
        className="min-h-24 w-full resize-y rounded-lg border border-border bg-card px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        value={brief}
        maxLength={4000}
        onChange={event => setBrief(event.target.value)}
        placeholder="例如：按区域比较订单量与金额，用柱状图展示分布，并保留区域筛选。"
        aria-label="AI 报告目标"
      />
      <Button size="sm" onClick={() => void design()} loading={running} disabled={!brief.trim()} data-testid="report-ai-generate">
        <Sparkles size={13} /> {candidate ? '重新生成候选' : '生成候选'}
      </Button>
      {error && <Alert variant="warning" role="alert">{error}</Alert>}
      {candidate && (
        <div className="space-y-3 border-t border-border pt-3" data-testid="report-ai-candidate">
          <p className="text-sm text-foreground">{candidate.explanation}</p>
          <p className="text-xs text-muted-foreground">以下变化尚未写入草稿。接受将替换当前计划的内容与筛选器。</p>
          <ul className="space-y-1 text-xs text-muted-foreground" aria-label="候选内容差异">
            {diffReportPlanBlocks(plan.blocks, candidate.plan.blocks).filter(diff => diff.status !== 'unchanged').map(diff => (
              <li key={diff.index} className="rounded-lg border border-border p-2">
                <p className="font-medium text-foreground">内容 {diff.index + 1}：{diff.current ? planBlockLabel(diff.current) : '空'} → {diff.candidate ? planBlockLabel(diff.candidate) : '删除'}</p>
                <div className="mt-1 grid gap-2 sm:grid-cols-2">
                  <p className="whitespace-pre-wrap break-words"><span className="font-medium">当前：</span>{blockDescription(diff.current)}</p>
                  <p className="whitespace-pre-wrap break-words"><span className="font-medium">候选：</span>{blockDescription(diff.candidate)}</p>
                </div>
              </li>
            ))}
            {JSON.stringify(plan.filters) !== JSON.stringify(candidate.plan.filters) && (
              <li className="rounded-lg border border-border p-2">
                <p className="font-medium text-foreground">筛选器变化</p>
                <div className="mt-1 grid gap-2 sm:grid-cols-2">
                  <p className="break-words">当前：{plan.filters.map(filter => `${filter.name} · ${filter.field || filter.name} · ${PLAN_FILTER_TYPE_LABELS[filter.type]}`).join('；') || '无'}</p>
                  <p className="break-words">候选：{candidate.plan.filters.map(filter => `${filter.name} · ${filter.field || filter.name} · ${PLAN_FILTER_TYPE_LABELS[filter.type]}`).join('；') || '无'}</p>
                </div>
              </li>
            )}
          </ul>
          <details>
            <summary className="cursor-pointer text-xs text-muted-foreground">核对候选字段、聚合与图表</summary>
            <div className="mt-3"><ReportPlanEditor plan={candidate.plan} columns={columns} onChange={() => {}} readOnly /></div>
          </details>
          {stale && <Alert variant="warning" role="alert">当前草稿在候选生成后有变化，请重新生成候选。</Alert>}
          <ReportPlanPreview
            reportId={reportId}
            datasetId={datasetId}
            datasetName={datasetName}
            plan={candidate.plan}
            onSuccess={() => setTested(true)}
            onInvalidate={() => setTested(false)}
          />
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" disabled={!tested || stale} onClick={() => { onAccept(candidate.plan); setCandidate(null) }} data-testid="report-ai-accept">
              <Check size={13} /> 接受候选
            </Button>
            <Button size="sm" variant="ghost" onClick={() => { setCandidate(null); setTested(false) }} data-testid="report-ai-discard">
              <X size={13} /> 丢弃
            </Button>
            {!tested && <span className="text-xs text-muted-foreground">请先点击预览中的“查询”核对真实结果。</span>}
          </div>
        </div>
      )}
    </section>
  )
}
