/**
 * SQL 试跑面板（编辑态）：命名 SQL 单块在线编辑 + 受限执行 + antd Table 结果。
 *
 * - 块选择用按钮列表（禁用原生 select）；
 * - 补全来自 PostgreSQL 关键字 + 数据集表名 + schema 列（ReportSqlEditor）；
 * - 试跑把当前草稿 markdown 与筛选值一并传给 execute：服务端按草稿重新
 *   校验 filter 声明，未保存的 filter 围栏即可参与试跑（无静默丢弃）；
 * - 「写回正文」把编辑后的 SQL 替换进 Markdown 同名围栏（replaceSqlBlock）；
 * - 执行失败显式报错，不静默造成功；切换查询块时清空上一块的旧结果。
 */
import { useEffect, useMemo, useState } from 'react'
import { Play, CornerDownLeft } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { toast } from 'sonner'
import operationsReportsApi, {
  operationsApiErrorText,
  type OperationsQueryResult,
  type OperationsReportArticle,
  type OperationsReportSchema,
} from '@/api/v2/operations-reports'
import {
  buildExecuteParams,
  defaultFilterValues,
  parseReportMarkdown,
  replaceSqlBlock,
  type ReportFilterDefinition,
} from '../reportModel'
import ReportSqlEditor from './ReportSqlEditor'
import ReportResultTable from './ReportResultTable'
import ReportFilterControls from './ReportFilterControls'

export default function ReportSqlTrialPanel({ reportId, article, markdown, schema, onApplyToMarkdown }: {
  reportId: string
  article: OperationsReportArticle
  /** 编辑器当前草稿正文（试跑与写回都基于它）。 */
  markdown: string
  schema: OperationsReportSchema | undefined
  onApplyToMarkdown: (next: string) => void
}) {
  const parsed = useMemo(() => parseReportMarkdown(markdown), [markdown])
  const sqlBlocks = useMemo(
    () => parsed.blocks.flatMap(block => (block.kind === 'sql' && block.name ? [block] : [])),
    [parsed.blocks],
  )
  const filters = useMemo<ReportFilterDefinition[]>(
    () => parsed.blocks.flatMap(block => (block.kind === 'filter' && block.filter ? [block.filter] : [])),
    [parsed.blocks],
  )
  const [selected, setSelected] = useState('')
  const [draftSql, setDraftSql] = useState<Record<string, string>>({})
  const [filterValues, setFilterValues] = useState<Record<string, string>>(() => defaultFilterValues(filters))
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<OperationsQueryResult | null>(null)
  const [error, setError] = useState('')
  const [filterError, setFilterError] = useState('')

  const activeName = selected && sqlBlocks.some(block => block.name === selected)
    ? selected
    : (sqlBlocks[0]?.name ?? '')
  const activeBlock = sqlBlocks.find(block => block.name === activeName)
  const activeSql = activeBlock ? (draftSql[activeName] ?? activeBlock.sql) : ''

  // 正文变化（在 Markdown 编辑器里改了 SQL / 写回）后重置本地试跑态：
  // 草稿回到正文最新值、筛选值回到草稿 defaults，结果与错误清空；
  // 试跑面板内编辑不触碰 markdown，不会触发本 effect。
  // （sqlBlocks/filters 是 markdown 的派生 memo，依赖只挂 markdown 即可。）
  useEffect(() => {
    setDraftSql(Object.fromEntries(sqlBlocks.map(block => [block.name, block.sql])))
    setFilterValues(defaultFilterValues(filters))
    setResult(null)
    setError('')
    setFilterError('')
  }, [markdown])

  // 切换查询块：上一块的试跑结果/错误不属于新块，必须清空（旧结果残留）。
  useEffect(() => {
    setResult(null)
    setError('')
  }, [activeName])

  const run = async () => {
    if (!activeBlock) return
    const { params, errors } = buildExecuteParams(filters, filterValues)
    if (errors.length) {
      setFilterError(errors.join('；'))
      return
    }
    setFilterError('')
    setRunning(true)
    setError('')
    setResult(null)
    try {
      const outcome = await operationsReportsApi.execute(reportId, article.id, {
        sql: activeSql,
        params,
        // 草稿正文一并上送：服务端按草稿重新校验 filter 声明，
        // 未保存的 filter 围栏在试跑期即可生效。
        markdown,
      })
      if ('results' in outcome) {
        setError('服务对单条试跑返回了批量结果，已忽略')
        return
      }
      setResult(outcome)
    } catch (err) {
      setError(operationsApiErrorText(err, '试跑失败'))
    } finally {
      setRunning(false)
    }
  }

  const writeBack = () => {
    if (!activeBlock) return
    const next = replaceSqlBlock(markdown, activeName, activeSql)
    if (next === markdown) {
      toast.error('写回失败：正文中没有找到同名 SQL 围栏')
      return
    }
    onApplyToMarkdown(next)
    toast.success(`已把「${activeName}」写回正文`)
  }

  if (!sqlBlocks.length) {
    return (
      <div className="rounded-xl border border-border bg-card p-4 text-xs text-muted-foreground">
        当前正文没有命名 SQL 围栏。先在正文里写 <code className="rounded bg-muted px-1 py-0.5 font-mono">{'```sql 查询名'}</code> 围栏，再在这里试跑。
      </div>
    )
  }

  return (
    <section className="flex min-h-0 flex-col gap-3 rounded-xl border border-border bg-card p-4" data-testid="report-sql-trial" aria-label="SQL 试跑">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="mr-1 text-[11px] font-medium text-muted-foreground">查询</span>
        {sqlBlocks.map(block => (
          <button
            key={block.name}
            type="button"
            onClick={() => setSelected(block.name)}
            aria-pressed={block.name === activeName}
            className={
              'rounded-lg px-2 py-1 font-mono text-[11px] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ' +
              (block.name === activeName
                ? 'bg-brand-soft text-brand-ink'
                : 'bg-muted text-muted-foreground hover:bg-[var(--color-bg-hover)] hover:text-foreground')
            }
          >
            {block.name}
          </button>
        ))}
      </div>

      <div className="h-56 overflow-hidden rounded-lg border border-border" data-testid="report-sql-trial-editor">
        <ReportSqlEditor
          value={activeSql}
          onChange={next => setDraftSql(prev => ({ ...prev, [activeName]: next }))}
          schema={schema}
          ariaLabel={'查询 ' + activeName + ' 的 SQL 编辑器'}
        />
      </div>

      {filters.length > 0 && (
        <div className="flex flex-wrap items-center gap-2" data-testid="report-sql-trial-filters">
          <span className="text-[11px] font-medium text-muted-foreground">筛选（来自当前草稿，试跑按草稿校验声明）</span>
          <ReportFilterControls
            filters={filters}
            values={filterValues}
            onChange={(name, value) => setFilterValues(prev => ({ ...prev, [name]: value }))}
          />
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" onClick={() => void run()} loading={running} data-testid="report-sql-trial-run">
          <Play size={12} /> 试跑
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={writeBack}
          disabled={!activeBlock || activeSql === activeBlock.sql}
          title={activeSql === activeBlock?.sql ? 'SQL 与正文一致，无需写回' : '把编辑后的 SQL 替换进正文同名围栏'}
        >
          <CornerDownLeft size={12} /> 写回正文
        </Button>
        {schema && (
          <span className="text-[11px] text-muted-foreground">
            补全：PostgreSQL 关键字 · <span className="font-mono">dataset</span> 逻辑表
            {schema.columns.length ? `（${schema.columns.length} 列）` : '（无列信息）'}
          </span>
        )}
      </div>

      {filterError && (
        <div role="alert" className="rounded-lg border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-3 py-2 text-xs text-[var(--color-warning)]" data-testid="report-sql-trial-filter-error">
          {filterError}
        </div>
      )}
      {error && (
        <div role="alert" className="rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-3 py-2 text-xs text-[var(--color-danger)]" data-testid="report-sql-trial-error">
          {error}
        </div>
      )}
      {result && <ReportResultTable result={result} />}
    </section>
  )
}
