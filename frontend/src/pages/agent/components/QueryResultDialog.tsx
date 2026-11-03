/**
 * 查询明细弹窗：工具 step 只携带 querySpec/统计元信息；打开后由 Web
 * 重新调用 query-view 生成 Web-owned 真实 cursor 的第一页。
 *
 * 契约要点：
 * - 后续翻页携带 {conversationId, cursor, querySpec}，不重复 pageSize；
 * - 中间页保留句柄，终页/换组/卸载用 DELETE 释放（已展示页保留本地缓存）；
 * - Web 重启返回 410/reprepareRequired 时，页面提供重新查询入口；
 * - drilldown {token, description} 直接回传 POST，不回到 LLM 规划。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Database, RefreshCw, RotateCcw } from 'lucide-react'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import {
  agentApi,
  type AgentQuerySpec,
  type AgentQueryViewResult,
} from '@/api/agent'
import { parseAgentQueryViewResult } from '@/api/agentProtocol'
import { ResultTable } from './ResultTable'
import {
  applyQuerySort,
  buildQueryViewBody,
  canSortQuerySpec,
  consistencyNotice,
  drilldownForResultRow,
  formatConsistency,
  queryPageSize,
  releaseBody,
  type StepQuerySource,
} from './queryResult'

interface PageEntry {
  result: AgentQueryViewResult
  cursor: string | null
  /** 该页句柄所属会话（切换会话后旧响应用原会话释放）。 */
  conversationId: string | null
}

interface QueryViewSnapshot {
  pages: Record<number, PageEntry>
  page: number
  querySpec: AgentQuerySpec | null
  drilldown: Record<string, unknown> | null
  fallbackDrilldown: Record<string, unknown> | null
  sort: { key: string; direction: 'asc' | 'desc' } | null
  title: string
}

export interface QueryResultDialogProps {
  open: boolean
  onClose: () => void
  oid: string
  conversationId: string | null
  source: StepQuerySource | null
}

function localResult(source: StepQuerySource): AgentQueryViewResult {
  return {
    rows: source.rows,
    columns: source.columns,
    pageSize: source.rows.length,
    hasNext: false,
    cursor: null,
    totalRowCount: source.totalRowCount,
    observedAt: source.observedAt,
    resolvedWindow: source.resolvedWindow,
    consistency: source.consistency,
    querySpec: source.querySpec,
    querySpecHash: null,
    rowCounting: null,
    releaseId: null,
    dataMode: source.dataMode,
    drilldowns: null,
  }
}

function drilldownTitle(
  drilldown: Record<string, unknown>,
  row: Record<string, unknown>,
): string {
  const groupDisplay = row.groupDisplay
  if (typeof groupDisplay === 'string' && groupDisplay.trim()) {
    return `明细 · ${groupDisplay}`
  }
  const description = drilldown.description
  if (description && typeof description === 'object' && !Array.isArray(description)) {
    const record = description as Record<string, unknown>
    const groupBy = Array.isArray(record.groupBy) ? record.groupBy : []
    const groupValues = Array.isArray(record.groupValues) ? record.groupValues : []
    const pairs = groupBy.map((label, index) =>
      `${String(label)}=${String(groupValues[index] ?? '（空）')}`)
    if (pairs.length > 0) return `明细 · ${pairs.join(' | ')}`
  }
  return '分组明细'
}

function isReprepareError(cause: any): boolean {
  if (cause?.status === 410) return true
  const detail = cause?.detail
  return detail?.reprepareRequired === true
    || /reprepare/i.test(String(detail?.code || detail?.message || ''))
}

export function QueryResultDialog({
  open,
  onClose,
  oid,
  conversationId,
  source,
}: QueryResultDialogProps) {
  const [pages, setPages] = useState<Record<number, PageEntry>>({})
  const [page, setPage] = useState(0)
  const [querySpec, setQuerySpec] = useState<AgentQuerySpec | null>(null)
  const [drilldown, setDrilldown] = useState<Record<string, unknown> | null>(null)
  const [fallbackDrilldown, setFallbackDrilldown] = useState<Record<string, unknown> | null>(null)
  const [sort, setSort] = useState<{ key: string; direction: 'asc' | 'desc' } | null>(null)
  const [title, setTitle] = useState('查询结果')
  const [backStack, setBackStack] = useState<QueryViewSnapshot[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [needsReprepare, setNeedsReprepare] = useState(false)
  const requestSeq = useRef(0)
  const conversationRef = useRef(conversationId)
  conversationRef.current = conversationId
  const currentRef = useRef<{
    result: AgentQueryViewResult | null
    cursor: string | null
    conversationId: string | null
    querySpec: AgentQuerySpec | null
    drilldown: Record<string, unknown> | null
    title: string
    page: number
  }>({ result: null, cursor: null, conversationId, querySpec: null, drilldown: null, title, page })
  const releasedRef = useRef<Set<string>>(new Set())
  const activeSourceRef = useRef<{
    source: StepQuerySource | null
    conversationId: string | null
  } | null>(null)

  const result = pages[page]?.result ?? null
  const cursor = pages[page]?.cursor ?? null
  currentRef.current = {
    result,
    cursor,
    conversationId: pages[page]?.conversationId ?? conversationId,
    querySpec,
    drilldown,
    title,
    page,
  }

  const releaseCursor = useCallback((
    handle: string | null | undefined,
    owningConversationId?: string | null,
  ) => {
    const conversation = owningConversationId ?? conversationRef.current
    if (!conversation || !handle || releasedRef.current.has(handle)) return
    releasedRef.current.add(handle)
    void agentApi.releaseQueryView(oid, releaseBody({
      conversationId: conversation,
      cursor: handle,
    })).catch(() => {})
  }, [oid])

  const releaseCurrentHandle = useCallback(() => {
    const current = currentRef.current
    // 首查询时请求 cursor 为 null，但响应 cursor 就是 Web-owned 句柄。
    releaseCursor(current.cursor ?? current.result?.cursor, current.conversationId)
  }, [releaseCursor])

  const load = useCallback(async (options: {
    spec: AgentQuerySpec | null
    drilldown: Record<string, unknown> | null
    drilldownToken: string | null
    cursor: string | null
    pageIndex: number
    pageSize?: number | null
    title: string
  }) => {
    if (!conversationId) {
      setError('会话尚未建立，暂时无法在 Web 侧重查明细。')
      setLoading(false)
      return
    }
    const seq = ++requestSeq.current
    setLoading(true)
    setError('')
    setNeedsReprepare(false)
    try {
      const data = parseAgentQueryViewResult(await agentApi.queryView(oid, buildQueryViewBody({
        conversationId,
        querySpec: options.spec,
        drilldown: options.drilldownToken,
        cursor: options.cursor,
        pageSize: options.pageSize,
      })))
      if (seq !== requestSeq.current) {
        // 旧请求可能已生成新的 Web-owned cursor：用该请求的原会话释放，
        // 不能拿当前（可能已切走的）会话去 DELETE。
        if (data.cursor) releaseCursor(data.cursor, conversationId)
        return
      }
      const nextResult: AgentQueryViewResult = data
      setPages(previous => ({
        ...previous,
        [options.pageIndex]: {
          result: nextResult,
          cursor: options.cursor,
          conversationId,
        },
      }))
      setPage(options.pageIndex)
      setQuerySpec(nextResult.querySpec ?? options.spec)
      setDrilldown(options.drilldown)
      setTitle(options.title)
      // 终页释放 Web-owned 句柄；已展示页仍从本地 pages 缓存回看。
      if (!nextResult.hasNext) {
        releaseCursor(nextResult.cursor ?? options.cursor, conversationId)
      }
    } catch (cause: any) {
      if (seq !== requestSeq.current) return
      if (isReprepareError(cause)) {
        setNeedsReprepare(true)
        setError('查询句柄已失效（服务重启或结果已回收）。已展示页仍可阅读，请重新查询继续翻页/下钻。')
      } else {
        setError(cause?.detail || cause?.message || '明细查询失败，请稍后重试。')
      }
    } finally {
      if (seq === requestSeq.current) setLoading(false)
    }
  }, [conversationId, oid, releaseCursor, source?.columns, source?.consistency, source?.resolvedWindow])

  const firstQuery = useCallback((options: {
    spec: AgentQuerySpec | null
    drilldown: Record<string, unknown> | null
    fallbackDrilldown?: Record<string, unknown> | null
    title: string
  }) => {
    releaseCurrentHandle()
    requestSeq.current += 1
    setPages({})
    setPage(0)
    setBackStack([])
    setSort(null)
    setNeedsReprepare(false)
    setQuerySpec(options.spec)
    setDrilldown(options.drilldown)
    setFallbackDrilldown(options.fallbackDrilldown ?? null)
    setTitle(options.title)
    if (!options.spec && !options.drilldown) {
      if (source) {
        const local = localResult(source)
        setPages({ 0: { result: local, cursor: null, conversationId } })
        setError(local.rows.length === 0 && local.columns.length === 0
          ? '该结果未提供 querySpec，也没有随工具返回行数据，无法查看明细。'
          : '')
      }
      setLoading(false)
      return
    }
    void load({
      spec: options.spec,
      drilldown: options.drilldown,
      drilldownToken: null,
      cursor: null,
      pageIndex: 0,
      pageSize: queryPageSize(options.spec),
      title: options.title,
    })
  }, [conversationId, load, releaseCurrentHandle, source])

  // 首开/换结果：释放上一结果句柄，按 querySpec 现算第一页。
  useEffect(() => {
    if (!open) return
    // React StrictMode 会重复执行 effect：同一 source + conversation 只初始化一次。
    if (activeSourceRef.current?.source === source
      && activeSourceRef.current.conversationId === conversationId) {
      return
    }
    activeSourceRef.current = { source, conversationId }
    requestSeq.current += 1
    if (!source) {
      releaseCurrentHandle()
      setPages({})
      setQuerySpec(null)
      setDrilldown(null)
      setTitle('查询结果')
      setError('该工具步骤没有可展示的明细结果。')
      setLoading(false)
      return
    }
    firstQuery({
      spec: source.querySpec,
      drilldown: null,
      fallbackDrilldown: source.drilldown,
      title: source.description,
    })
  }, [open, source, oid, conversationId])

  useEffect(() => {
    if (open) return
    activeSourceRef.current = null
    requestSeq.current += 1
    releaseCurrentHandle()
  }, [open, releaseCurrentHandle])

  useEffect(() => () => {
    requestSeq.current += 1
    releaseCurrentHandle()
  }, [releaseCurrentHandle])

  const handleSort = (column: { key: string; label: string }, direction: 'asc' | 'desc') => {
    const nextSpec = applyQuerySort(querySpec, { key: column.key, label: column.label }, direction)
    if (!nextSpec || nextSpec === querySpec) return
    setSort({ key: column.key, direction })
    firstQuery({ spec: nextSpec, drilldown, fallbackDrilldown, title })
  }

  const canUseHandle = (handle: string | null) => !!handle && !releasedRef.current.has(handle)

  const handleNext = () => {
    const cached = pages[page + 1]
    if (cached) {
      setPage(page + 1)
      return
    }
    if (!result?.cursor) return
    if (!canUseHandle(result.cursor)) {
      setNeedsReprepare(true)
      setError('查询句柄已释放。请重新查询后继续翻页。')
      return
    }
    if (!querySpec) {
      setNeedsReprepare(true)
      setError('缺少可续页的 querySpec。请重新查询后继续翻页。')
      return
    }
    void load({
      spec: querySpec,
      drilldown: null,
      drilldownToken: null,
      cursor: result.cursor,
      pageIndex: page + 1,
      title,
    })
  }

  const handlePrev = () => {
    if (page <= 0) return
    // 已展示页保留本地缓存，不依赖已释放句柄。
    if (pages[page - 1]) setPage(page - 1)
  }

  const handleDrilldown = (row: Record<string, unknown>) => {
    const descriptor = drilldownForResultRow(
      { rows: result?.rows ?? [], drilldowns: result?.drilldowns, columns: result?.columns },
      row,
      fallbackDrilldown,
    )
    if (!descriptor) return
    const token = typeof descriptor.token === 'string' ? descriptor.token : null
    if (!token || !querySpec) return
    setBackStack(previous => [...previous, {
      pages,
      page,
      querySpec,
      drilldown,
      fallbackDrilldown,
      sort,
      title,
    }])
    const nextSpec = querySpec
    const nextTitle = drilldownTitle(descriptor, row)
    releaseCurrentHandle()
    requestSeq.current += 1
    setPages({})
    setPage(0)
    setSort(null)
    setNeedsReprepare(false)
    setQuerySpec(nextSpec)
    setDrilldown(descriptor)
    setFallbackDrilldown(null)
    setTitle(nextTitle)
    void load({
      spec: nextSpec,
      drilldown: descriptor,
      drilldownToken: token,
      cursor: null,
      pageIndex: 0,
      pageSize: queryPageSize(nextSpec),
      title: nextTitle,
    })
  }

  const handleBack = () => {
    const previous = backStack[backStack.length - 1]
    if (!previous) return
    releaseCurrentHandle()
    requestSeq.current += 1
    setBackStack(stack => stack.slice(0, -1))
    setPages(previous.pages)
    setPage(previous.page)
    setQuerySpec(previous.querySpec)
    setDrilldown(previous.drilldown)
    setFallbackDrilldown(previous.fallbackDrilldown)
    setSort(previous.sort)
    setTitle(previous.title)
    setError('')
    setNeedsReprepare(false)
    setLoading(false)
  }

  const handleRefresh = () => {
    if (!querySpec && !drilldown) {
      if (source) {
        const local = localResult(source)
        setPages({ 0: { result: local, cursor: null, conversationId } })
      }
      return
    }
    // drilldown token 过期（410）时不能复用旧 token：先回到父级
    // querySpec 刷新统计，再由新的行描述重新下钻。
    if (needsReprepare && drilldown) {
      const parent = backStack[backStack.length - 1]
      if (parent) {
        setBackStack(stack => stack.slice(0, -1))
        setDrilldown(parent.drilldown)
        setFallbackDrilldown(parent.fallbackDrilldown)
        firstQuery({
          spec: parent.querySpec,
          drilldown: parent.drilldown,
          fallbackDrilldown: parent.fallbackDrilldown,
          title: parent.title,
        })
      } else {
        const refreshedSpec = querySpec
        setDrilldown(null)
        firstQuery({
          spec: refreshedSpec,
          drilldown: null,
          fallbackDrilldown,
          title: source?.description ?? title,
        })
      }
      return
    }
    firstQuery({ spec: querySpec, drilldown, fallbackDrilldown, title })
  }

  const canSort = useMemo(() => canSortQuerySpec(querySpec), [querySpec])
  const consistencyNote = useMemo(
    () => consistencyNotice(result?.consistency),
    [result?.consistency],
  )
  const hasBack = backStack.length > 0
  const handleReleased = useMemo(() => {
    const handle = cursor ?? result?.cursor ?? null
    return !!handle && releasedRef.current.has(handle)
  }, [cursor, result?.cursor, pages, page])

  return (
    <Dialog open={open} onOpenChange={value => { if (!value) onClose() }}>
      <DialogContent
        className="flex h-[min(88vh,880px)] w-[min(94vw,1180px)] max-w-none flex-col p-4"
        data-testid="agent-result-dialog"
      >
        <DialogHeader icon={<Database size={18} />} className="mb-3 shrink-0">
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>
            打开时按原查询条件获取最新结果；可翻页查看，或从统计结果下钻明细。
          </DialogDescription>
        </DialogHeader>
        <div className="mb-2 flex shrink-0 flex-wrap items-center gap-2">
          {hasBack && (
            <Button type="button" variant="outline" size="sm" onClick={handleBack}
              data-testid="agent-result-back">
              返回统计
            </Button>
          )}
          <Button type="button" variant="ghost" size="sm" onClick={handleRefresh}
            disabled={loading} data-testid="agent-result-refresh">
            <RotateCcw size={13} />重新查询
          </Button>
          {result?.consistency != null && (
            <span className="max-w-[52rem] truncate text-[11px] text-[var(--color-text-tertiary)]">
              一致性 {formatConsistency(result.consistency)}
            </span>
          )}
          {result?.querySpecHash && (
            <span className="font-mono text-[10px] text-[var(--color-text-tertiary)]">
              spec {result.querySpecHash.slice(0, 10)}
            </span>
          )}
        </div>
        {!conversationId && (
          <Alert variant="warning" className="mb-2">
            会话尚未建立，明细表会在会话创建后可查询。
          </Alert>
        )}
        {needsReprepare && (
          <Alert variant="warning" className="mb-2" data-testid="agent-result-reprepare">
            <div className="flex items-center gap-2">
              <span className="min-w-0 flex-1">
                查询句柄已失效（服务重启或结果已回收），需要重新查询。
              </span>
              <Button type="button" variant="outline" size="sm" onClick={handleRefresh}>
                <RefreshCw size={13} />重新查询
              </Button>
            </div>
          </Alert>
        )}
        {handleReleased && !needsReprepare && (
          <Alert variant="info" className="mb-2">
            当前页为已展示的本地缓存；继续翻页/下钻前请重新查询。
          </Alert>
        )}
        {consistencyNote && (
          <Alert variant="info" className="mb-2" data-testid="agent-result-consistency-note" role="status">
            {consistencyNote}
          </Alert>
        )}
        <ResultTable
          result={result}
          loading={loading}
          error={error}
          sort={sort}
          canSort={canSort}
          page={page}
          canPrev={page > 0}
          canNext={!!pages[page + 1] || (!!result?.cursor && result.hasNext && !handleReleased)}
          onSort={handleSort}
          onPrev={handlePrev}
          onNext={handleNext}
          onDrilldown={handleDrilldown}
          drilldownFallback={fallbackDrilldown}
        />
      </DialogContent>
    </Dialog>
  )
}
