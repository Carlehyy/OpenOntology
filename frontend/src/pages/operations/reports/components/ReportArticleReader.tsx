/**
 * 书式文章阅读态：
 * - 进入文章自动执行全部命名 SQL（最新版本打开即查），Refresh 显式重查，无轮询；
 * - 全部命名 SQL 走一次 execute 批量请求（{queries}）：同一数据集版本、
 *   同一事务，各结果卡片展示实际执行的版本号/时间/物理来源；
 * - filter（text/number/date 标量）修改不自动执行，点「查询」以当前值刷新；
 * - 图表/表格只消费服务端受限结果；query 断链、重复名、执行失败就地报错。
 *
 * 两种呈现：
 * - 默认（文档流）：自带标题头，嵌在编辑态预览、计划预览等容器内，内边距由容器提供；
 * - fill（贴边全高阅读态）：标题头由页面右卡头部承担，本组件铺满右卡正文区——
 *   筛选条吸顶、正文自带内边距。
 */
import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import operationsReportsApi, {
  operationsApiErrorText,
  type OperationsExecuteOutcome,
  type OperationsExecutePayload,
  type OperationsQueryResult,
} from '@/api/v2/operations-reports'
import {
  buildExecuteParams,
  duplicateQueryNames,
  parseReportMarkdown,
  defaultFilterValues,
  REPORT_EXECUTE_MAX_QUERIES,
  type ReportBlock,
  type ReportFilterDefinition,
} from '../reportModel'
import ReportProseView from './ReportProseView'
import ReportChartView from './ReportChartView'
import ReportResultTable from './ReportResultTable'
import ReportFilterControls from './ReportFilterControls'
import QueryVersionLabel from './QueryVersionLabel'

interface QueryRunState {
  status: 'running' | 'success' | 'error'
  result?: OperationsQueryResult
  error?: string
}

/** 结果/图表块统一外观：页面右卡内的描边内容块（间距由外层 space-y 提供）。 */
const blockClass = 'rounded-lg border border-[var(--color-border)] bg-card p-4'

function RunIssue({ message }: { message: string }) {
  return (
    <div
      role="alert"
      className="rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-3 py-2 text-xs text-[var(--color-danger)]"
    >
      {message}
    </div>
  )
}

function SqlResultCard({ name, sql, run, duplicated, hasActiveFilters, onClearFilters }: {
  name: string
  sql: string
  run: QueryRunState | undefined
  duplicated: boolean
  /** 是否存在已生效的筛选值：零行时区分「被筛掉」与「本来就没有」。 */
  hasActiveFilters: boolean
  onClearFilters?: () => void
}) {
  return (
    <section
      className={blockClass}
      data-testid="report-sql-block"
      aria-label={'查询 ' + (name || '（未命名）')}
    >
      <header className="mb-2 flex flex-wrap items-center gap-2">
        <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
          {name || '（缺少查询名）'}
        </code>
        {run?.status === 'success' && run.result && <QueryVersionLabel result={run.result} />}
        {duplicated && <span className="text-[11px] text-[var(--color-danger)]">查询名重复，后一次执行结果覆盖前一次</span>}
        {run?.status === 'running' && <span className="text-[11px] text-muted-foreground">执行中…</span>}
      </header>
      {run?.status === 'error' && <RunIssue message={'查询执行失败：' + run.error} />}
      {run?.status === 'success' && run.result && <ReportResultTable result={run.result} />}
      {run?.status === 'success' && run.result && !run.result.rows.length && (
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span>{hasActiveFilters ? '当前筛选条件下没有数据。' : '本次查询没有返回数据。'}</span>
          {hasActiveFilters && onClearFilters && (
            <Button size="sm" variant="ghost" className="h-6 px-2 text-xs" onClick={onClearFilters} data-testid="report-clear-filters">
              清除筛选
            </Button>
          )}
        </div>
      )}
      <details className="mt-2">
        <summary className="cursor-pointer text-[11px] text-muted-foreground select-none">SQL</summary>
        <pre className="mt-1.5 overflow-x-auto rounded-lg bg-[var(--color-bg-elevated)] p-3 font-mono text-[12px] leading-6 text-[var(--color-text-primary)]">{sql}</pre>
      </details>
    </section>
  )
}

function CodeBlock({ code }: { code: string }) {
  return (
    <pre className="overflow-x-auto rounded-lg bg-[var(--color-bg-elevated)] p-3 font-mono text-[12px] leading-6 text-[var(--color-text-primary)]">
      {code}
    </pre>
  )
}

export interface ReportArticleReaderHandle {
  /** 以当前筛选值重跑整批查询，只发一次批量请求（供页面右卡头部的「刷新数据」调用）。 */
  refresh: () => void
}

interface ReportArticleReaderProps {
  reportId: string
  articleId: string
  title: string
  datasetName: string
  markdown: string
  /** 编辑态预览：标题带预览标记，其余行为一致（查询仍真实执行）。 */
  preview?: boolean
  /** 未保存的结构化计划复用同一阅读器，通过服务端计划预览执行。 */
  execute?: (payload: OperationsExecutePayload) => Promise<OperationsExecuteOutcome>
  autoRun?: boolean
  refreshTestId?: string
  onSuccess?: () => void
  /** 铺满页面右卡正文区：不渲染标题头（由右卡头部承担）。 */
  fill?: boolean
}

const ReportArticleReader = forwardRef<ReportArticleReaderHandle, ReportArticleReaderProps>(function ReportArticleReader({
  reportId, articleId, title, datasetName, markdown, preview, execute, autoRun = true, refreshTestId = 'report-refresh', onSuccess, fill = false,
}, ref) {
    const parsed = useMemo(() => parseReportMarkdown(markdown), [markdown])
    const filters = useMemo<ReportFilterDefinition[]>(
      () => parsed.blocks.flatMap(block => (block.kind === 'filter' && block.filter ? [block.filter] : [])),
      [parsed.blocks],
    )
    const sqlBlocks = useMemo(
      () => parsed.blocks.filter((block): block is Extract<ReportBlock, { kind: 'sql' }> => block.kind === 'sql'),
      [parsed.blocks],
    )
    const duplicates = useMemo(() => duplicateQueryNames(parsed.blocks), [parsed.blocks])
    const [values, setValues] = useState<Record<string, string>>(() => defaultFilterValues(filters))
    const [runs, setRuns] = useState<Record<string, QueryRunState>>({})
    const [filterError, setFilterError] = useState('')
    const [batchError, setBatchError] = useState('')
    const [refreshing, setRefreshing] = useState(false)
    const runToken = useRef(0)
    // 筛选值镜像：自动执行 effect（markdown 变化触发）与手动「查询」
    // 都读 ref，避免 effect 闭包拿到上一轮渲染的 values。
    const valuesRef = useRef(defaultFilterValues(filters))

    useEffect(() => {
      const next = defaultFilterValues(filters)
      valuesRef.current = next
      setValues(next)
      setRuns({})
      setFilterError('')
    }, [filters])

    const setFilterValue = (name: string, value: string) => {
      setValues(prev => {
        const next = { ...prev, [name]: value }
        valuesRef.current = next
        return next
      })
    }

    const runAll = useCallback(async () => {
      const { params, errors } = buildExecuteParams(filters, valuesRef.current)
      if (errors.length) {
        setFilterError(errors.join('；'))
        return
      }
      setFilterError('')
      const named = sqlBlocks.filter(block => block.name)
      if (!named.length && !execute) return
      const uniqueNames = [...new Set(named.map(block => block.name))]
      if (uniqueNames.length > REPORT_EXECUTE_MAX_QUERIES) {
        setBatchError(`命名查询 ${uniqueNames.length} 条超过单次批量执行上限 ${REPORT_EXECUTE_MAX_QUERIES} 条，已全部阻止执行`)
        return
      }
      setBatchError('')
      // 批量请求：全部查询经一次 execute 在同一数据集版本、同一事务内执行。
      const queries: Record<string, string> = {}
      for (const block of named) queries[block.name] = block.sql
      const token = ++runToken.current
      setRefreshing(true)
      setRuns(Object.fromEntries(named.map(block => [block.name, { status: 'running' }])))
      try {
        const payload = {
          queries,
          params,
          // 预览态正文是未保存草稿：带上 markdown 让服务端按草稿重新校验
          // filter 声明，未保存的 filter 围栏即可试跑。
          ...(preview ? { markdown } : {}),
        }
        const outcome = execute
          ? await execute(payload)
          : await operationsReportsApi.execute(reportId, articleId, payload)

        if (runToken.current !== token) return
        if (!('results' in outcome) || !outcome.results) {
          throw new Error('BAD_BATCH_RESPONSE')
        }
        const results = outcome.results
        if (named.some(block => !results[block.name])) {
          throw new Error('BAD_BATCH_RESPONSE')
        }
        onSuccess?.()
        setRuns(Object.fromEntries(named.map(block => {
          const result = results[block.name]
          return [
            block.name,
            result
              ? { status: 'success', result }
              : { status: 'error', error: '服务未返回该查询的结果' },
          ] as [string, QueryRunState]
        })))
      } catch (error) {
        if (runToken.current !== token) return
        // 同一事务执行：任一查询失败即整体失败。全部查询显式置错，
        // 不残留上一次的旧结果（防假成功）。
        const text = error instanceof Error && error.message === 'BAD_BATCH_RESPONSE'
          ? '服务返回的批量结果缺少 results'
          : operationsApiErrorText(error, '服务执行失败')
        setBatchError(named.length ? '' : text)
        setRuns(Object.fromEntries(named.map(block => [block.name, { status: 'error', error: text } as QueryRunState])))
      } finally {
        if (runToken.current === token) setRefreshing(false)
      }
    }, [reportId, articleId, sqlBlocks, filters, preview, markdown, execute, onSuccess])

    useImperativeHandle(ref, () => ({ refresh: () => { void runAll() } }), [runAll])

    // 筛选值变化只走手动查询；切换内容时令在途结果失效，防止旧草稿覆盖新预览。
    useEffect(() => {
      setRuns({})
      setBatchError('')
      setRefreshing(false)
      if (autoRun && (sqlBlocks.length || execute)) void runAll()
      return () => { runToken.current += 1 }
    }, [reportId, articleId, markdown, autoRun, execute])

    // 已生效的筛选值（非空即参与参数绑定）：零行结果据此区分解释，
    // 并提供「清除筛选」一键回到未过滤查询。
    const hasActiveFilters = filters.some(filter => (values[filter.name] ?? '').trim() !== '')
    const clearFilters = () => {
      const next = Object.fromEntries(filters.map(filter => [filter.name, '']))
      valuesRef.current = next
      setValues(next)
      void runAll()
    }

    const refreshButton = (variant: 'default' | 'ghost', label: string, className?: string) => (
      <Button
        size="sm"
        variant={variant}
        onClick={() => void runAll()}
        loading={refreshing}
        className={className}
        data-testid={refreshTestId}
      >
        <RefreshCw size={13} /> {label}
      </Button>
    )

    // 标题锚点按块顺序切片（全局 id 与目录一致）
    let headingCursor = 0
    const headingIdsForProse = (segment: string) => {
      const count = segment.split(/\r?\n/).filter(line => /^(#{1,3})\s+/.test(line)).length
      const ids = parsed.headings.slice(headingCursor, headingCursor + count).map(heading => heading.id)
      headingCursor += count
      return ids
    }

    const blocks = (
      <div className="space-y-5">
        {parsed.blocks.map((block, index) => {
          if (block.kind === 'prose') {
            return <ReportProseView key={index} markdown={block.markdown} headingIds={headingIdsForProse(block.markdown)} />
          }
          if (block.kind === 'sql') {
            return (
              <SqlResultCard
                key={index}
                name={block.name}
                sql={block.sql}
                run={block.name ? runs[block.name] : undefined}
                duplicated={!!block.name && duplicates.includes(block.name)}
                hasActiveFilters={hasActiveFilters}
                onClearFilters={clearFilters}
              />
            )
          }
          if (block.kind === 'chart') {
            const issues = block.issues.map(issue => issue.message)
            if (!block.config) return <RunIssue key={index} message={issues.join('；') || '图表配置不完整'} />
            const defined = block.config.query
              ? sqlBlocks.some(sqlBlock => sqlBlock.name === block.config?.query)
              : false
            if (!defined) {
              return (
                <RunIssue
                  key={index}
                  message={issues.length ? issues.join('；') : `图表引用的查询「${block.config.query || '（未指定）'}」不存在（可用：${sqlBlocks.map(block => block.name).filter(Boolean).join('、') || '无'}）`}
                />
              )
            }
            const run = runs[block.config.query]
            if (issues.length) return <RunIssue key={index} message={issues.join('；')} />
            if (!run || run.status === 'running') {
              return (
                <div key={index} className={blockClass + ' text-xs text-muted-foreground'} data-testid="report-chart-pending">
                  图表等待查询「{block.config.query}」执行{run ? '中…' : '（未执行）'}
                </div>
              )
            }
            if (run.status === 'error') {
              return <RunIssue key={index} message={`查询「${block.config.query}」执行失败：${run.error}`} />
            }
            return <ReportChartView key={index} config={block.config} result={run.result!} />
          }
          if (block.kind === 'filter') {
            if (!block.issues.length) return null
            return <RunIssue key={index} message={block.issues.map(issue => issue.message).join('；')} />
          }
          return <CodeBlock key={index} code={block.code} />
        })}
      </div>
    )

    const runIssues = (
      <>
        {filterError && <RunIssue message={filterError} />}
        {batchError && <RunIssue message={batchError} />}
      </>
    )

    if (fill) {
      return (
        <article className="min-w-0" data-testid="report-article-reader" aria-label={title}>
          {filters.length > 0 && (
            // 筛选条吸顶：长报告滚动时筛选与「查询」始终可用（窄屏筛选项会折成
            // 多行，吸顶会占掉过多高度，改为随正文滚动）。
            // 筛选项与「查询」合为一条命令条（同接口管理的地址栏 + 调用按钮）。
            <div className="shrink-0 border-b border-[var(--color-border)] bg-card px-4 py-3 md:sticky md:top-0 md:z-10">
              <div className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-card p-1 pl-3 focus-within:border-[var(--color-nav-bg)]">
                <ReportFilterControls
                  layout="bar"
                  filters={filters}
                  values={values}
                  onChange={setFilterValue}
                />
                {refreshButton('default', '查询', 'ml-auto shrink-0')}
              </div>
            </div>
          )}
          {(filterError || batchError) && <div className="space-y-2 px-4 pt-3 lg:px-6">{runIssues}</div>}
          <div className="px-4 py-4 lg:px-6 lg:py-5">{blocks}</div>
        </article>
      )
    }

    return (
      <article className="min-w-0" data-testid="report-article-reader" aria-label={title}>
        <header className="mb-5 border-b border-border pb-4">
          <h2 className="text-lg font-semibold leading-6 text-foreground">
            {title}
            {preview && <span className="ml-2 rounded border border-border bg-muted px-1.5 py-0.5 align-middle text-[11px] font-normal text-muted-foreground">预览（未保存草稿）</span>}
          </h2>
          <p className="mt-1 text-xs text-muted-foreground">
            数据来源：<span className="font-mono">{datasetName || '—'}</span>
          </p>
        </header>

        {(filters.length > 0 || sqlBlocks.length > 0 || execute) && (
          <div className="mb-4 space-y-3">
            {filters.length > 0 && (
              <ReportFilterControls
                filters={filters}
                values={values}
                onChange={setFilterValue}
                trailing={refreshButton('default', '查询', 'ml-auto')}
              />
            )}
            {filters.length === 0 && refreshButton('default', '查询')}
            {runIssues}
          </div>
        )}

        {blocks}
      </article>
    )
})

export default ReportArticleReader
