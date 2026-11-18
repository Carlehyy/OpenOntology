import type { ReactNode } from 'react'
import { ArrowDown, ArrowUp, Plus, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Alert } from '@/components/ui/Alert'
import type { OperationsDatasetColumn } from '@/api/v2/operations-reports'
import {
  PLAN_AGG_LABELS,
  PLAN_AGGS,
  PLAN_BLOCKS_MAX,
  PLAN_BUCKET_LABELS,
  PLAN_BUCKETS,
  PLAN_CHART_LABELS,
  PLAN_CHARTS,
  PLAN_CHART_SERIES_MAX,
  PLAN_FILTERS,
  PLAN_FILTER_TYPE_LABELS,
  PLAN_FILTERS_MAX,
  PLAN_QUERY_BLOCKS_MAX,
  PLAN_QUERY_COLUMNS_MAX,
  PLAN_QUERY_LIMIT_DEFAULT,
  PLAN_QUERY_LIMIT_MAX,
  PLAN_SORT_KEYS_MAX,
  PLAN_TEXT_MAX_CHARS,
  asciiAlias,
  defaultAlias,
  isTemporalColumn,
  isValidIdentifier,
  makeChartBlock,
  makeQueryBlock,
  queryBlocks,
  queryNames,
  queryOutputColumns,
  type PlanBucket,
  type PlanBlock,
  type PlanChartBlock,
  type PlanChartKind,
  type PlanQueryBlock,
  type PlanTimeBucket,
  type PlanQueryColumn,
  type PlanFilterType,
  type ReportPlan,
  validateReportPlan,
} from '../reportPlanModel'

const inputClass = 'h-8 w-full rounded-lg border border-border bg-card px-2.5 text-xs text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50'
const selectClass = 'h-8 w-full rounded-lg bg-card text-xs'
const sectionLabelClass = 'font-mono text-[10px] uppercase tracking-[0.12em] text-brand-ink'

function IconButton({ label, onClick, disabled, danger = false, children }: {
  label: string
  onClick: () => void
  disabled?: boolean
  danger?: boolean
  children: ReactNode
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onClick}
      className={`rounded p-1 text-[var(--color-text-tertiary)] hover:bg-muted disabled:pointer-events-none disabled:opacity-40 ${danger ? 'hover:text-[var(--color-danger)]' : 'hover:text-foreground'}`}
    >
      {children}
    </button>
  )
}

function MoveButtons({ index, total, prefix, readOnly, onMoveUp, onMoveDown }: {
  index: number
  total: number
  prefix: string
  readOnly: boolean
  onMoveUp: () => void
  onMoveDown: () => void
}) {
  return (
    <>
      <IconButton label={`上移${prefix}`} onClick={onMoveUp} disabled={readOnly || index <= 0}><ArrowUp size={14} /></IconButton>
      <IconButton label={`下移${prefix}`} onClick={onMoveDown} disabled={readOnly || index >= total - 1}><ArrowDown size={14} /></IconButton>
    </>
  )
}

function FieldSelect({ value, columns, onChange, label, disabled }: {
  value: string
  columns: OperationsDatasetColumn[]
  onChange: (value: string) => void
  label: string
  disabled?: boolean
}) {
  return (
    <Select value={value || '__none__'} onValueChange={next => onChange(next === '__none__' ? '' : next)} disabled={disabled}>
      <SelectTrigger className={selectClass} aria-label={label}><SelectValue placeholder="选择字段" /></SelectTrigger>
      <SelectContent>
        <SelectItem value="__none__">不指定</SelectItem>
        {columns.map(column => <SelectItem key={column.name} value={column.name}>{column.name} · {column.type}</SelectItem>)}
      </SelectContent>
    </Select>
  )
}

/** 时间桶默认名 `字段__粒度` 对中文字段不合法时，自动派生 ASCII 别名。 */
function buildTimeBucket(field: string, unit: PlanBucket): PlanTimeBucket {
  const defaultName = `${field}__${unit}`
  return { field, unit, ...(isValidIdentifier(defaultName) ? {} : { alias: `${asciiAlias(field, 'time')}__${unit}` }) }
}

/** 为新选择的字段建议一个查询内不冲突的合法 ASCII 别名。 */
function suggestColumnAlias(block: PlanQueryBlock, field: string, agg?: PlanQueryColumn['agg']): string {
  const taken = new Set(block.columns
    .map(column => column.alias || (column.agg ? defaultAlias(column.agg, column.field) : column.field))
    .filter((alias): alias is string => Boolean(alias)))
  const base = agg
    ? defaultAlias(agg, field)
    : (isValidIdentifier(field) ? field.slice(0, 63) : asciiAlias(field, 'col'))
  let candidate = base
  let suffix = 2
  while (taken.has(candidate)) candidate = `${base.slice(0, 63 - String(suffix).length)}_${suffix++}`
  return candidate
}

function QueryBlockEditor({ block, columns, readOnly, index, total, onChange, onRemove, onMoveUp, onMoveDown, onRename }: {
  block: PlanQueryBlock
  columns: OperationsDatasetColumn[]
  readOnly: boolean
  index: number
  total: number
  onChange: (next: PlanQueryBlock) => void
  onRemove: () => void
  onMoveUp: () => void
  onMoveDown: () => void
  onRename: (name: string) => void
}) {
  const patch = (next: Partial<PlanQueryBlock>) => onChange({ ...block, ...next })
  const updateColumn = (columnIndex: number, next: PlanQueryColumn) => {
    patch({ columns: block.columns.map((column, current) => current === columnIndex ? next : column) })
  }
  const outputs = queryOutputColumns(block)
  const sort = block.sort ?? []
  const temporalColumns = columns.filter(isTemporalColumn)
  const updateSort = (sortIndex: number, next: { field?: string; dir?: 'asc' | 'desc' }) => {
    patch({ sort: sort.map((key, current) => current === sortIndex ? { ...key, ...next } : key) })
  }

  return (
    <section className="rounded-xl border border-border bg-card p-3" data-testid={`report-plan-query-${block.name}`}>
      <div className="mb-2 flex items-center gap-1.5">
        <span className={sectionLabelClass}>查询</span>
        <input
          className={inputClass + ' flex-1'}
          value={block.name}
          aria-label="查询名称"
          placeholder="英文查询名，如 daily_total"
          disabled={readOnly}
          onChange={event => onRename(event.target.value)}
        />
        <MoveButtons index={index} total={total} prefix={`查询「${block.name || `第 ${index + 1} 块`}」`} readOnly={readOnly} onMoveUp={onMoveUp} onMoveDown={onMoveDown} />
        <IconButton label={`删除查询「${block.name || `第 ${index + 1} 块`}」`} onClick={onRemove} disabled={readOnly} danger><Trash2 size={14} /></IconButton>
      </div>
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <span className="text-[11px] text-muted-foreground">输出列（{block.columns.length}/{PLAN_QUERY_COLUMNS_MAX}）：字段、聚合与别名；别名须为 ASCII 标识符</span>
        </div>
        {block.columns.map((column, columnIndex) => (
          <div key={columnIndex} className="grid grid-cols-[minmax(0,1fr)_8rem_minmax(0,1fr)_auto] items-center gap-2">
            <FieldSelect
              value={column.field || ''}
              columns={columns}
              label={`查询「${block.name}」第 ${columnIndex + 1} 列字段`}
              disabled={readOnly}
              onChange={field => updateColumn(columnIndex, {
                ...column,
                field: field || undefined,
                ...(field && !column.alias ? { alias: suggestColumnAlias(block, field, column.agg) } : {}),
              })}
            />
            <Select
              value={column.agg || '__field__'}
              onValueChange={next => updateColumn(columnIndex, { ...column, agg: next === '__field__' ? undefined : next as PlanQueryColumn['agg'] })}
              disabled={readOnly}
            >
              <SelectTrigger className={selectClass} aria-label={`查询「${block.name}」第 ${columnIndex + 1} 列聚合`}><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="__field__">原始字段</SelectItem>
                {PLAN_AGGS.map(agg => <SelectItem key={agg} value={agg}>{PLAN_AGG_LABELS[agg]}</SelectItem>)}
              </SelectContent>
            </Select>
            <input
              className={inputClass}
              value={column.alias || ''}
              aria-label={`查询「${block.name}」第 ${columnIndex + 1} 列别名`}
              placeholder="输出别名（ASCII）"
              disabled={readOnly}
              onChange={event => updateColumn(columnIndex, { ...column, alias: event.target.value.trim() || undefined })}
            />
            <IconButton
              label={`删除查询「${block.name}」第 ${columnIndex + 1} 个输出列`}
              onClick={() => patch({ columns: block.columns.filter((_item, current) => current !== columnIndex) })}
              disabled={readOnly || block.columns.length <= 1}
              danger
            ><Trash2 size={13} /></IconButton>
          </div>
        ))}
        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            variant="ghost"
            disabled={readOnly || block.columns.length >= PLAN_QUERY_COLUMNS_MAX}
            onClick={() => patch({ columns: [...block.columns, columns.length
              ? { field: columns[0].name, alias: suggestColumnAlias(block, columns[0].name) }
              : { agg: 'count', alias: 'count' }] })}
          ><Plus size={13} /> 添加输出列</Button>
          <label className="flex items-center gap-2 text-xs text-muted-foreground">
            返回上限
            <input
              className={inputClass + ' w-20'}
              type="number"
              min={1}
              max={PLAN_QUERY_LIMIT_MAX}
              value={block.limit ?? PLAN_QUERY_LIMIT_DEFAULT}
              aria-label={`查询「${block.name}」返回上限（1 到 ${PLAN_QUERY_LIMIT_MAX}）`}
              disabled={readOnly}
              onChange={event => {
                const parsed = Number(event.target.value)
                patch({ limit: Number.isFinite(parsed) ? Math.max(1, Math.min(PLAN_QUERY_LIMIT_MAX, Math.floor(parsed))) : PLAN_QUERY_LIMIT_DEFAULT })
              }}
            />
          </label>
          <span className="text-[11px] text-muted-foreground">1–{PLAN_QUERY_LIMIT_MAX}，默认 {PLAN_QUERY_LIMIT_DEFAULT}</span>
        </div>
        <div className="space-y-1.5">
          <span className="text-[11px] text-muted-foreground">时间分桶（仅日期/时间列，输出列名为「字段__粒度」）</span>
          <div className="grid grid-cols-2 gap-2">
            <div className="space-y-1">
              <FieldSelect
                value={block.time_bucket?.field || ''}
                columns={temporalColumns}
                label={`查询「${block.name}」时间分桶字段`}
                disabled={readOnly}
                onChange={field => patch({ time_bucket: field ? buildTimeBucket(field, block.time_bucket?.unit || 'day') : undefined })}
              />
              {temporalColumns.length === 0 && <p className="text-[11px] text-muted-foreground">数据集没有日期/时间字段，无法分桶。</p>}
            </div>
            <Select
              value={block.time_bucket?.unit || '__none__'}
              disabled={readOnly || !block.time_bucket}
              onValueChange={unit => {
                if (unit === '__none__') patch({ time_bucket: undefined })
                else if (block.time_bucket) patch({ time_bucket: buildTimeBucket(block.time_bucket.field, unit as PlanBucket) })
              }}
            >
              <SelectTrigger className={selectClass} aria-label={`查询「${block.name}」时间粒度`}><SelectValue placeholder="时间粒度" /></SelectTrigger>
              <SelectContent>
                <SelectItem value="__none__">不分桶</SelectItem>
                {PLAN_BUCKETS.map(unit => <SelectItem key={unit} value={unit}>{PLAN_BUCKET_LABELS[unit]}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>
        </div>
        <div className="space-y-1.5">
          <span className="text-[11px] text-muted-foreground">排序（最多 {PLAN_SORT_KEYS_MAX} 个，按查询输出列）</span>
          {sort.map((key, sortIndex) => (
            <div key={sortIndex} className="grid grid-cols-[minmax(0,1fr)_7rem_auto] items-center gap-2">
              <Select value={key.field} onValueChange={field => updateSort(sortIndex, { field })} disabled={readOnly}>
                <SelectTrigger className={selectClass} aria-label={`查询「${block.name}」第 ${sortIndex + 1} 个排序字段`}><SelectValue placeholder="选择输出列" /></SelectTrigger>
                <SelectContent>
                  {outputs.map(output => <SelectItem key={output} value={output}>{output}</SelectItem>)}
                </SelectContent>
              </Select>
              <Select value={key.dir} onValueChange={dir => updateSort(sortIndex, { dir: dir as 'asc' | 'desc' })} disabled={readOnly}>
                <SelectTrigger className={selectClass} aria-label={`查询「${block.name}」第 ${sortIndex + 1} 个排序方向`}><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="asc">升序</SelectItem>
                  <SelectItem value="desc">降序</SelectItem>
                </SelectContent>
              </Select>
              <IconButton
                label={`删除查询「${block.name}」第 ${sortIndex + 1} 个排序键`}
                onClick={() => patch({ sort: sort.filter((_item, current) => current !== sortIndex) })}
                disabled={readOnly}
                danger
              ><Trash2 size={13} /></IconButton>
            </div>
          ))}
          <Button
            size="sm"
            variant="ghost"
            disabled={readOnly || sort.length >= PLAN_SORT_KEYS_MAX || outputs.length === 0}
            onClick={() => patch({ sort: [...sort, { field: outputs[0] ?? '', dir: 'desc' }] })}
          ><Plus size={13} /> 添加排序</Button>
        </div>
      </div>
    </section>
  )
}

function ChartBlockEditor({ block, plan, readOnly, index, total, onChange, onRemove, onMoveUp, onMoveDown }: {
  block: PlanChartBlock
  plan: ReportPlan
  readOnly: boolean
  index: number
  total: number
  onChange: (next: PlanChartBlock) => void
  onRemove: () => void
  onMoveUp: () => void
  onMoveDown: () => void
}) {
  const patch = (next: Partial<PlanChartBlock>) => onChange({ ...block, ...next })
  const referenced = queryBlocks(plan).find(query => query.name === block.query)
  const outputs = referenced ? queryOutputColumns(referenced) : []
  const y = block.y ?? []
  const usesX = block.chart === 'bar' || block.chart === 'line' || block.chart === 'pie'
  const yMode = block.chart === 'kpi' || block.chart === 'pie' ? 'single' : (block.chart === 'bar' || block.chart === 'line') ? 'multi' : 'none'
  const changeChart = (chart: PlanChartKind) => {
    if (chart === 'table') patch({ chart, x: undefined, y: [] })
    else if (chart === 'kpi' || chart === 'pie') patch({ chart, x: undefined, y: y.slice(0, 1) })
    else patch({ chart })
  }
  return (
    <section className="rounded-xl border border-border bg-card p-3" data-testid={`report-plan-chart-${index + 1}`}>
      <div className="mb-2 flex items-center gap-1.5">
        <span className={sectionLabelClass}>图表</span>
        <span className="flex-1" />
        <MoveButtons index={index} total={total} prefix={`图表（第 ${index + 1} 块）`} readOnly={readOnly} onMoveUp={onMoveUp} onMoveDown={onMoveDown} />
        <IconButton label={`删除图表（第 ${index + 1} 块）`} onClick={onRemove} disabled={readOnly} danger><Trash2 size={14} /></IconButton>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <Select value={block.chart} onValueChange={next => changeChart(next as PlanChartKind)} disabled={readOnly}>
          <SelectTrigger className={selectClass} aria-label={`图表（第 ${index + 1} 块）类型`}><SelectValue /></SelectTrigger>
          <SelectContent>{PLAN_CHARTS.map(kind => <SelectItem key={kind} value={kind}>{PLAN_CHART_LABELS[kind]}</SelectItem>)}</SelectContent>
        </Select>
        <Select value={block.query || '__none__'} onValueChange={query => patch({ query: query === '__none__' ? '' : query })} disabled={readOnly}>
          <SelectTrigger className={selectClass} aria-label={`图表（第 ${index + 1} 块）引用查询`}><SelectValue placeholder="选择查询" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="__none__">选择查询</SelectItem>
            {queryNames(plan).map(name => <SelectItem key={name} value={name}>{name}</SelectItem>)}
          </SelectContent>
        </Select>
      </div>
      {usesX && (
        <div className="mt-2 space-y-1">
          <span className="text-[11px] text-muted-foreground">x 轴（来自查询输出列）</span>
          <Select value={block.x || '__none__'} onValueChange={next => patch({ x: next === '__none__' ? undefined : next })} disabled={readOnly}>
            <SelectTrigger className={selectClass} aria-label={`图表（第 ${index + 1} 块）x 轴列`}><SelectValue placeholder="选择 x 列" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="__none__">不指定</SelectItem>
              {outputs.map(output => <SelectItem key={output} value={output}>{output}</SelectItem>)}
            </SelectContent>
          </Select>
        </div>
      )}
      {yMode === 'single' && (
        <div className="mt-2 space-y-1">
          <span className="text-[11px] text-muted-foreground">y 值列（恰好一个，来自查询输出列）</span>
          <Select value={y[0] || '__none__'} onValueChange={next => patch({ y: next === '__none__' ? [] : [next] })} disabled={readOnly}>
            <SelectTrigger className={selectClass} aria-label={`图表（第 ${index + 1} 块）y 值列`}><SelectValue placeholder="选择 y 列" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="__none__">不指定</SelectItem>
              {outputs.map(output => <SelectItem key={output} value={output}>{output}</SelectItem>)}
            </SelectContent>
          </Select>
        </div>
      )}
      {yMode === 'multi' && (
        <div className="mt-2 space-y-1">
          <span className="text-[11px] text-muted-foreground">y 值系列（最多 {PLAN_CHART_SERIES_MAX} 个，来自查询输出列）</span>
          {y.map((series, seriesIndex) => (
            <div key={seriesIndex} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-2">
              <Select value={series || '__none__'} onValueChange={next => patch({ y: y.map((item, current) => current === seriesIndex ? next : item) })} disabled={readOnly}>
                <SelectTrigger className={selectClass} aria-label={`图表（第 ${index + 1} 块）第 ${seriesIndex + 1} 个 y 系列`}><SelectValue placeholder="选择 y 列" /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="__none__">不指定</SelectItem>
                  {outputs.map(output => <SelectItem key={output} value={output}>{output}</SelectItem>)}
                </SelectContent>
              </Select>
              <IconButton
                label={`删除图表（第 ${index + 1} 块）第 ${seriesIndex + 1} 个 y 系列`}
                onClick={() => patch({ y: y.filter((_item, current) => current !== seriesIndex) })}
                disabled={readOnly}
                danger
              ><Trash2 size={13} /></IconButton>
            </div>
          ))}
          <Button
            size="sm"
            variant="ghost"
            disabled={readOnly || y.length >= PLAN_CHART_SERIES_MAX || outputs.length === 0}
            onClick={() => patch({ y: [...y, outputs[0] ?? ''] })}
          ><Plus size={13} /> 添加系列</Button>
        </div>
      )}
      {yMode === 'none' && <p className="mt-2 text-[11px] text-muted-foreground">表格按查询输出列全量展示，无需配置 x / y。</p>}
      {!referenced && <p className="mt-2 text-[11px] text-muted-foreground">请先选择要可视化的查询，再配置 x / y 列。</p>}
    </section>
  )
}

export default function ReportPlanEditor({ plan, columns, onChange, readOnly = false }: {
  plan: ReportPlan
  columns: OperationsDatasetColumn[]
  onChange: (next: ReportPlan) => void
  readOnly?: boolean
}) {
  const issues = validateReportPlan(plan, columns)
  const patch = (next: Partial<ReportPlan>) => onChange({ ...plan, ...next })
  const updateBlock = (index: number, next: PlanBlock) => patch({ blocks: plan.blocks.map((block, current) => current === index ? next : block) })
  const add = (block: PlanBlock) => patch({ blocks: [...plan.blocks, block] })
  const remove = (index: number) => patch({ blocks: plan.blocks.filter((_block, current) => current !== index) })
  const moveBlock = (index: number, delta: -1 | 1) => {
    const target = index + delta
    if (target < 0 || target >= plan.blocks.length) return
    const blocks = [...plan.blocks]
    const [moved] = blocks.splice(index, 1)
    blocks.splice(target, 0, moved)
    patch({ blocks })
  }
  // 查询重命名同步所有引用它的图表，避免编辑态断链。
  const renameQuery = (index: number, name: string) => {
    const previous = plan.blocks[index]
    if (!previous || previous.type !== 'query') return
    patch({ blocks: plan.blocks.map(block => {
      if (block === previous) return { ...block, name }
      if (block.type === 'chart' && previous.name && block.query === previous.name) return { ...block, query: name }
      return block
    }) })
  }
  const queryCount = plan.blocks.filter(block => block.type === 'query').length
  const blocksFull = plan.blocks.length >= PLAN_BLOCKS_MAX
  const uniqueName = (base: string, taken: Set<string>) => {
    let candidate = base
    let suffix = 2
    while (taken.has(candidate)) candidate = `${base}_${suffix++}`
    return candidate
  }
  const nextQueryName = uniqueName(`query_${queryCount + 1}`, new Set(queryNames(plan)))
  const nextFilterName = uniqueName(`filter_${plan.filters.length + 1}`, new Set(plan.filters.map(filter => filter.name)))
  const addFilter = () => patch({ filters: [...plan.filters, { name: nextFilterName, type: 'text', ...(columns[0] ? { field: columns[0].name } : {}) }] })

  return (
    <div className="space-y-3" data-testid="report-plan-editor">
      {issues.length > 0 && (
        <Alert variant="warning" role="alert">
          <ul className="list-disc space-y-0.5 pl-4">{issues.map(issue => <li key={issue}>{issue}</li>)}</ul>
        </Alert>
      )}
      <div className="flex flex-wrap items-center gap-2 rounded-xl border border-border bg-muted/40 p-2">
        <span className="mr-1 text-[11px] font-medium text-muted-foreground">添加内容（{plan.blocks.length}/{PLAN_BLOCKS_MAX} 块）</span>
        <Button size="sm" variant="outline" disabled={readOnly || blocksFull} onClick={() => add({ type: 'text', text: '在这里写下背景、结论或行动建议。' })}>说明文字</Button>
        <Button size="sm" variant="outline" disabled={readOnly || blocksFull || queryCount >= PLAN_QUERY_BLOCKS_MAX} onClick={() => add(makeQueryBlock(nextQueryName, columns))}>查询（{queryCount}/{PLAN_QUERY_BLOCKS_MAX}）</Button>
        <Button size="sm" variant="outline" disabled={readOnly || blocksFull} onClick={() => add(makeChartBlock(queryNames(plan)[0] ?? nextQueryName))}>图表</Button>
        <Button size="sm" variant="outline" disabled={readOnly || plan.filters.length >= PLAN_FILTERS_MAX} onClick={addFilter}>筛选器（{plan.filters.length}/{PLAN_FILTERS_MAX}）</Button>
      </div>
      <div className="space-y-2">
        {plan.blocks.map((block, index) => {
          const shared = {
            index,
            total: plan.blocks.length,
            readOnly,
            onMoveUp: () => moveBlock(index, -1),
            onMoveDown: () => moveBlock(index, 1),
          }
          if (block.type === 'text') {
            return (
              <section key={index} className="rounded-xl border border-border bg-card p-3">
                <div className="mb-2 flex items-center gap-1.5">
                  <span className={sectionLabelClass}>说明文字</span>
                  <span className="flex-1" />
                  <MoveButtons {...shared} prefix={`说明文字（第 ${index + 1} 块）`} />
                  <IconButton label={`删除说明文字（第 ${index + 1} 块）`} onClick={() => remove(index)} disabled={readOnly} danger><Trash2 size={14} /></IconButton>
                </div>
                <textarea
                  className="min-h-24 w-full resize-y rounded-lg border border-border bg-card px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
                  value={block.text}
                  maxLength={PLAN_TEXT_MAX_CHARS}
                  aria-label={`说明文字（第 ${index + 1} 块）内容`}
                  disabled={readOnly}
                  onChange={event => updateBlock(index, { ...block, text: event.target.value })}
                />
              </section>
            )
          }
          if (block.type === 'query') {
            return (
              <QueryBlockEditor
                key={index}
                block={block}
                columns={columns}
                onChange={next => updateBlock(index, next)}
                onRemove={() => remove(index)}
                onRename={name => renameQuery(index, name)}
                {...shared}
              />
            )
          }
          return (
            <ChartBlockEditor
              key={index}
              block={block}
              plan={plan}
              onChange={next => updateBlock(index, next)}
              onRemove={() => remove(index)}
              {...shared}
            />
          )
        })}
      </div>
      {plan.filters.length > 0 && (
        <section className="rounded-xl border border-border bg-card p-3" data-testid="report-plan-filters">
          <div className="mb-2 flex items-center justify-between">
            <span className={sectionLabelClass}>筛选器</span>
            <Button size="sm" variant="ghost" disabled={readOnly || plan.filters.length >= PLAN_FILTERS_MAX} onClick={addFilter} aria-label="添加筛选器"><Plus size={13} /> 添加筛选器（{plan.filters.length}/{PLAN_FILTERS_MAX}）</Button>
          </div>
          <div className="space-y-2">
            {plan.filters.map((filter, index) => (
              <div key={index} className="grid grid-cols-[minmax(0,1fr)_7rem_minmax(0,1fr)_auto] items-center gap-2">
                <input
                  className={inputClass}
                  value={filter.name}
                  aria-label={`筛选器 ${index + 1} 名称`}
                  placeholder="参数名（ASCII）"
                  disabled={readOnly}
                  onChange={event => patch({ filters: plan.filters.map((item, current) => current === index ? { ...item, name: event.target.value } : item) })}
                />
                <Select
                  value={filter.type}
                  onValueChange={type => patch({ filters: plan.filters.map((item, current) => current === index ? { ...item, type: type as PlanFilterType } : item) })}
                  disabled={readOnly}
                >
                  <SelectTrigger className={selectClass} aria-label={`筛选器 ${index + 1} 类型`}><SelectValue /></SelectTrigger>
                  <SelectContent>{PLAN_FILTERS.map(type => <SelectItem key={type} value={type}>{PLAN_FILTER_TYPE_LABELS[type]}</SelectItem>)}</SelectContent>
                </Select>
                <FieldSelect
                  value={filter.field || ''}
                  columns={columns}
                  label={`筛选器 ${index + 1} 字段`}
                  disabled={readOnly}
                  onChange={field => patch({ filters: plan.filters.map((item, current) => current === index ? { ...item, field: field || undefined } : item) })}
                />
                <IconButton
                  label={`删除筛选器 ${index + 1}`}
                  onClick={() => patch({ filters: plan.filters.filter((_item, current) => current !== index) })}
                  disabled={readOnly}
                  danger
                ><Trash2 size={14} /></IconButton>
              </div>
            ))}
          </div>
          <p className="mt-2 text-[11px] text-muted-foreground">筛选类型需与字段类型匹配：文本↔字符串、数字↔数值、日期↔日期/时间。</p>
        </section>
      )}
    </div>
  )
}
