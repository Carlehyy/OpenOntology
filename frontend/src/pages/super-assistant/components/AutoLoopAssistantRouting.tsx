import { useEffect, useState } from 'react'
import { ArrowLeft, ArrowRight, Bot, RefreshCw } from 'lucide-react'
import { autoloopApi, type AutoLoopEventTrace, type AutoLoopPage } from '@/api/autoloop'
import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { formatDateTime } from '@/utils/datetime'
import { errorText } from './assistantPanelUtils'
import { EVENT_ERROR_LABEL, EVENT_OUTCOME } from './autoloopDisplay'

export default function AutoLoopAssistantRouting({ onOpenEvent, onOpenDecision, onViewAll }: {
  onOpenEvent: (id: string) => void
  onOpenDecision: (id: string) => void
  onViewAll: () => void
}) {
  const [cursors, setCursors] = useState<(string | null)[]>([null])
  const [result, setResult] = useState<AutoLoopPage<AutoLoopEventTrace> | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  const cursor = cursors[cursors.length - 1]

  useEffect(() => {
    let current = true
    setLoading(true)
    setError('')
    void autoloopApi.listEventTraces({ route: 'assistant' }, cursor, 10).then(data => {
      if (current) setResult(data)
    }).catch(err => {
      if (current) setError(errorText(err, '研判记录加载失败，请刷新重试。'))
    }).finally(() => { if (current) setLoading(false) })
    return () => { current = false }
  }, [cursor, revision])

  return (
    <section className="flex min-h-0 min-w-0 flex-1 flex-col" aria-label="未命中事件研判">
      <div className="shrink-0 border-b border-border px-4 py-4 sm:px-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 space-y-1">
            <h2 className="text-base font-semibold">未命中事件研判</h2>
            <p className="text-sm leading-5 text-muted-foreground">助手判断是否需要人工处理。自动派发给 Multica 需要命中已启用的规则。</p>
          </div>
          <div className="flex items-center gap-2">
            <Button type="button" size="icon-sm" variant="ghost" disabled={loading} aria-label="刷新研判记录" title="刷新研判记录" onClick={() => setRevision(value => value + 1)}><RefreshCw size={14} aria-hidden="true" /></Button>
            <Button type="button" size="sm" variant="outline" onClick={onViewAll}>查看未命中流水</Button>
          </div>
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-auto" aria-busy={loading}>
        {error ? <Alert role="alert" variant="danger" className="m-4">{error}</Alert>
          : loading ? <p className="px-4 py-5 text-sm text-muted-foreground sm:px-5" role="status">加载研判记录…</p>
            : result?.items.length ? (
              <ul className="divide-y divide-border">
                {result.items.map(item => {
                  const { label, variant } = EVENT_OUTCOME[item.outcome]
                  return (
                    <li key={item.id} className="flex flex-col gap-3 px-4 py-4 sm:flex-row sm:items-start sm:justify-between sm:px-5">
                      <div className="min-w-0 flex-1 space-y-1.5">
                        <div className="flex flex-wrap items-center gap-2">
                          <h3 className="min-w-0 break-words text-sm font-medium">{item.title?.trim() || '未命名事件'}</h3>
                          <Badge variant={variant}>{label}</Badge>
                        </div>
                        <p className="line-clamp-3 break-words text-sm leading-5 text-muted-foreground">
                          {item.reason ? EVENT_ERROR_LABEL[item.reason] ?? item.reason : item.outcome === 'pending' ? '正在等待研判。' : '查看事件详情了解处理记录。'}
                        </p>
                        <time dateTime={item.receivedAt} className="block font-mono text-xs text-muted-foreground">{formatDateTime(item.receivedAt, { seconds: true, fallback: '时间未知' })}</time>
                      </div>
                      <div className="flex shrink-0 flex-wrap items-center gap-1">
                        <Button type="button" size="sm" variant="ghost" onClick={() => onOpenEvent(item.id)}>查看事件</Button>
                        {item.decisionItemId && <Button type="button" size="sm" variant="outline" onClick={() => onOpenDecision(item.decisionItemId!)}>查看决策事项</Button>}
                      </div>
                    </li>
                  )
                })}
              </ul>
            ) : (
              <div className="flex min-h-full flex-col items-center justify-center gap-2 px-4 py-10 text-center">
                <span className="rounded-xl bg-muted p-2.5 text-muted-foreground"><Bot size={18} aria-hidden="true" /></span>
                <p className="text-sm font-medium">暂无研判记录</p>
                <p className="text-xs text-muted-foreground">未命中规则的事件完成匹配后会出现在这里。</p>
              </div>
            )}
      </div>
      {((result?.hasMore ?? false) || cursors.length > 1) && (
        <div className="flex shrink-0 items-center justify-end gap-2 border-t border-border px-4 py-2 sm:px-5" aria-label="研判记录分页">
          <span className="mr-1 text-xs tabular-nums text-muted-foreground" aria-live="polite">第 {cursors.length} 页</span>
          <Button type="button" size="sm" variant="ghost" disabled={loading || cursors.length === 1} onClick={() => setCursors(value => value.slice(0, -1))}><ArrowLeft size={14} aria-hidden="true" />上一页</Button>
          <Button type="button" size="sm" variant="ghost" disabled={loading || Boolean(error) || !result?.nextCursor} onClick={() => setCursors(value => [...value, result!.nextCursor])}>下一页<ArrowRight size={14} aria-hidden="true" /></Button>
        </div>
      )}
    </section>
  )
}
