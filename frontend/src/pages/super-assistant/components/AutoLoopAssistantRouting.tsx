import { useEffect, useState } from 'react'
import { ArrowLeft, ArrowRight, Pencil, RefreshCw } from 'lucide-react'
import { autoloopApi, type AutoLoopEventTrace, type AutoLoopPage, type AutoLoopRoutingPolicy } from '@/api/autoloop'
import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { EmptyState } from '@/components/ui/LoadingState'
import { formatDateTime } from '@/utils/datetime'
import { errorText } from './assistantPanelUtils'
import { EVENT_ERROR_LABEL, EVENT_OUTCOME, promptProvenance } from './autoloopDisplay'
import { routingPromptLabel } from './RoutingPolicyEditor'

function AssessmentRow({ item, onOpenEvent, onOpenDecision }: {
  item: AutoLoopEventTrace
  onOpenEvent: (id: string) => void
  onOpenDecision: (id: string) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const { label, variant } = EVENT_OUTCOME[item.outcome]
  const reason = item.reason ? EVENT_ERROR_LABEL[item.reason] ?? item.reason : item.outcome === 'pending' ? '等待助手研判。' : '查看事件详情了解处理记录。'
  const long = reason.length > 100 || reason.split('\n').length > 3
  return (
    <li className="flex flex-col gap-2 py-3 lg:flex-row lg:items-start lg:justify-between">
      <div className="min-w-0 flex-1 space-y-1.5">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="min-w-0 break-words text-sm font-medium">{item.title?.trim() || '未命名事件'}</h3>
          <Badge variant={item.action === 'kernel_fallback' ? 'warning' : variant}>{item.action === 'kernel_fallback' ? '研判异常' : label}</Badge>
        </div>
        <div>
          <p className={`whitespace-pre-wrap break-words text-sm leading-5 text-muted-foreground ${!expanded && long ? 'line-clamp-3' : ''}`}>{reason}</p>
          {long && <Button type="button" variant="link" size="sm" className="px-0 text-brand-ink max-md:h-11" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? '收起依据' : '展开依据'}</Button>}
        </div>
        <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
          <time dateTime={item.receivedAt} className="tabular-nums">{formatDateTime(item.receivedAt, { fallback: '时间未知' })}</time>
          {item.outcome !== 'pending' && <span>{promptProvenance(item.routingPromptVersion)}</span>}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-1">
        <Button type="button" size="sm" variant="ghost" className="max-md:h-11" onClick={() => onOpenEvent(item.id)}>查看事件</Button>
        {item.decisionItemId && <Button type="button" size="sm" variant="outline" className="max-md:h-11" onClick={() => onOpenDecision(item.decisionItemId!)}>查看决策事项</Button>}
      </div>
    </li>
  )
}

export default function AutoLoopAssistantRouting({ onOpenEvent, onOpenDecision, onViewAll, policy, policyLoading, policyError, onEditPolicy, onRetryPolicy }: {
  onOpenEvent: (id: string) => void
  onOpenDecision: (id: string) => void
  onViewAll: () => void
  policy: AutoLoopRoutingPolicy | null
  policyLoading: boolean
  policyError: string
  onEditPolicy: () => void
  onRetryPolicy: () => void
}) {
  const [cursors, setCursors] = useState<(string | null)[]>([null])
  const [result, setResult] = useState<AutoLoopPage<AutoLoopEventTrace> | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  const cursor = cursors[cursors.length - 1]
  const reload = () => setRevision(value => value + 1)

  useEffect(() => {
    let current = true
    setLoading(true)
    setError('')
    void autoloopApi.listEventTraces({ route: 'assistant' }, cursor, 10).then(data => {
      if (current) setResult(data)
    }).catch(err => {
      if (current) setError(errorText(err, '研判记录加载失败。'))
    }).finally(() => { if (current) setLoading(false) })
    return () => { current = false }
  }, [cursor, revision])

  return (
    <section className="min-h-0 min-w-0 flex-1 overflow-y-auto p-4 sm:p-5" aria-label="未命中事件研判">
      <div className="space-y-3 border-b border-border pb-4" aria-busy={policyLoading}>
        <div className="flex min-h-12 flex-wrap items-center justify-between gap-3">
          <div className="min-w-0 space-y-1">
            <h2 className="text-sm font-semibold">研判提示词</h2>
            <p className="text-xs leading-5 text-muted-foreground">仅用于未命中启用规则的事件。助手可建议转人工或忽略，不会派发。</p>
          </div>
          <Button type="button" className="max-md:h-11" data-rule-routing-edit-policy disabled={!policy || !!policyError || policyLoading} onClick={onEditPolicy}><Pencil size={16} aria-hidden="true" />编辑研判提示词</Button>
        </div>
        {policyError ? <Alert role="alert" variant="danger">
          {policyError} <Button type="button" size="sm" variant="outline" onClick={onRetryPolicy}>重试加载提示词</Button>
        </Alert> : policy ? <div className="flex flex-wrap items-center gap-2">
          <Badge variant="secondary">{routingPromptLabel(policy.version, policy.promptText)}</Badge>
          {policy.updatedAt && <span className="text-xs text-muted-foreground">更新于 <time dateTime={policy.updatedAt}>{formatDateTime(policy.updatedAt)}</time></span>}
        </div> : <p className="text-xs text-muted-foreground" role="status">正在读取生效版本…</p>}
      </div>
      <div className="flex min-h-12 flex-wrap items-center justify-between gap-2 py-2">
        <h2 className="text-sm font-semibold">未命中事件研判</h2>
        <div className="flex flex-wrap items-center gap-1">
          <Button type="button" size="icon" variant="ghost" className="max-md:size-11" disabled={loading} aria-label="刷新研判记录" title="刷新研判记录" onClick={reload}><RefreshCw size={16} aria-hidden="true" /></Button>
          <Button type="button" variant="outline" className="max-md:h-11" onClick={onViewAll}>查看未命中流水</Button>
        </div>
      </div>
      <div aria-busy={loading}>
        {error ? <Alert role="alert" variant="danger">{error} <Button type="button" size="sm" variant="outline" onClick={reload}>重试加载研判记录</Button></Alert>
          : loading ? <div className="space-y-2" role="status"><span className="sr-only">加载研判记录…</span>{[0, 1, 2].map(row => <div key={row} className="h-14 rounded-md bg-muted" aria-hidden="true" />)}</div>
            : result?.items.length ? (
              <ul className="divide-y divide-border border-t border-border">
                {result.items.map(item => <AssessmentRow key={item.id} item={item} onOpenEvent={onOpenEvent} onOpenDecision={onOpenDecision} />)}
              </ul>
            ) : <EmptyState title="暂无研判记录" description="未命中规则的事件完成匹配后会出现在这里。" />}
      </div>
      {((result?.hasMore ?? false) || cursors.length > 1) && (
        <div className="flex flex-wrap items-center justify-end gap-2 border-t border-border pt-2" aria-label="研判记录分页">
          <span className="mr-1 text-xs tabular-nums text-muted-foreground" aria-live="polite">第 {cursors.length} 页</span>
          <Button type="button" size="sm" variant="ghost" className="max-md:h-11" disabled={loading || cursors.length === 1} onClick={() => setCursors(value => value.slice(0, -1))}><ArrowLeft size={14} aria-hidden="true" />上一页</Button>
          <Button type="button" size="sm" variant="ghost" className="max-md:h-11" disabled={loading || Boolean(error) || !result?.nextCursor} onClick={() => setCursors(value => [...value, result!.nextCursor])}>下一页<ArrowRight size={14} aria-hidden="true" /></Button>
        </div>
      )}
    </section>
  )
}
