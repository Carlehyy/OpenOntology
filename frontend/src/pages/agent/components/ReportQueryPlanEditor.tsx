/**
 * 报告模板查询计划编辑器。
 *
 * 只暴露 reporting.py 允许的三种只读工具与白名单字段；不提供自由 JSON/SQL
 * 输入。query_objects 的相对时间窗口、时间桶、聚合、排序和关联参数会原样
 * 回写，保存前由 reportQueryPlan.validateEditorSections 统一校验。
 */
import { useEffect, useId, useState } from 'react'
import { Plus, Trash2 } from 'lucide-react'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { cn } from '@/lib/utils'
import {
  AGGREGATE_METRICS,
  DEFAULT_QUERY_PAGE_SIZE,
  FILTER_OPS,
  MAX_QUERIES_PER_SECTION,
  MAX_QUERY_PAGE_SIZE,
  QUERY_AGGREGATE_FNS,
  QUERY_BUCKETS,
  QUERY_FILTER_OPS,
  REPORT_QUERY_TOOLS,
  WINDOW_UNITS,
  aggregateRows,
  argumentNumber,
  argumentString,
  defaultEditorQuery,
  filterRows,
  filterValueFromText,
  filterValueToText,
  groupRows,
  joinRows,
  orderRows,
  querySelectText,
  switchQueryTool,
  toolLabel,
  unknownArgumentKeys,
  windowRow,
  withAggregateRows,
  withArgument,
  withFilterRows,
  withGroupRows,
  withJoinRows,
  withOrderRows,
  withQuerySelectText,
  withWindowRow,
  type EditorQuery,
} from './reportQueryPlan'

export interface ReportObjectTypeOption {
  name: string
  displayName: string
  properties: string[]
}

export interface ReportLinkTypeOption {
  name: string
  displayName: string
}

const fieldClass = 'w-full rounded-lg border border-border bg-card px-2 py-1.5 text-[11px] text-foreground outline-none transition focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60'
const labelClass = 'text-[10px] font-semibold text-muted-foreground'
const miniButtonClass = 'inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-[10px] font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40'
const removeButtonClass = 'inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)] disabled:cursor-not-allowed disabled:opacity-40'

// Radix Select 不允许空字符串作为选项值；空值选项经哨兵映射回 ''。
const EMPTY_SELECT_VALUE = '__empty__'

interface SelectOption {
  value: string
  label: string
}

function CompactSelect({
  value,
  options,
  onChange,
  disabled,
  ariaLabel,
  placeholder,
  className,
}: {
  value: string
  options: SelectOption[]
  onChange: (value: string) => void
  disabled?: boolean
  ariaLabel: string
  placeholder?: string
  className?: string
}) {
  return (
    <Select
      value={value || EMPTY_SELECT_VALUE}
      onValueChange={next => onChange(next === EMPTY_SELECT_VALUE ? '' : next)}
      disabled={disabled}
    >
      <SelectTrigger aria-label={ariaLabel} className={cn('h-8 rounded-lg px-2 py-1 text-[11px]', className)}>
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent>
        {options.map(option => (
          <SelectItem key={option.value || EMPTY_SELECT_VALUE} value={option.value || EMPTY_SELECT_VALUE}>
            {option.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}


function PropertyField({
  value,
  onChange,
  properties,
  disabled,
  placeholder = '属性名',
  ariaLabel,
}: {
  value: string
  onChange: (value: string) => void
  properties: string[]
  disabled?: boolean
  placeholder?: string
  ariaLabel: string
}) {
  const listId = useId()
  return (
    <>
      <input
        aria-label={ariaLabel}
        value={value}
        list={listId}
        disabled={disabled}
        placeholder={placeholder}
        onChange={event => onChange(event.target.value)}
        className={fieldClass}
        spellCheck={false}
      />
      <datalist id={listId}>
        {properties.map(property => <option key={property} value={property} />)}
      </datalist>
    </>
  )
}

function ObjectTypeField({
  value,
  options,
  onChange,
  disabled,
  emptyLabel = '选择对象类型',
}: {
  value: string
  options: ReportObjectTypeOption[]
  onChange: (value: string) => void
  disabled?: boolean
  emptyLabel?: string
}) {
  const known = options.some(option => option.name === value)
  const selectOptions: SelectOption[] = [
    { value: '', label: emptyLabel },
    ...options.map(option => ({ value: option.name, label: `${option.displayName} · ${option.name}` })),
    ...(value && !known ? [{ value, label: `${value}（不在授权清单）` }] : []),
  ]
  return (
    <CompactSelect
      value={value}
      options={selectOptions}
      onChange={onChange}
      disabled={disabled}
      ariaLabel="对象类型"
      placeholder={emptyLabel}
    />
  )
}

function LinkTypeField({
  value,
  options,
  onChange,
  disabled,
}: {
  value: string
  options: ReportLinkTypeOption[]
  onChange: (value: string) => void
  disabled?: boolean
}) {
  const known = options.some(option => option.name === value)
  const selectOptions: SelectOption[] = [
    { value: '', label: '链接类型' },
    ...options.map(option => ({ value: option.name, label: `${option.displayName} · ${option.name}` })),
    ...(value && !known ? [{ value, label: `${value}（不在模型清单）` }] : []),
  ]
  return (
    <CompactSelect
      value={value}
      options={selectOptions}
      onChange={onChange}
      disabled={disabled}
      ariaLabel="链接类型"
      placeholder="链接类型"
    />
  )
}

/** 逗号分隔的 select 文本保留输入中的临时逗号，同时即时同步结构化参数。 */
function QuerySelectField({
  query,
  onChange,
  disabled,
}: {
  query: EditorQuery
  onChange: (query: EditorQuery) => void
  disabled?: boolean
}) {
  const external = querySelectText(query)
  const [text, setText] = useState(external)
  const [editing, setEditing] = useState(false)
  useEffect(() => {
    if (!editing) setText(external)
  }, [external, editing])
  return (
    <input
      aria-label="输出属性"
      value={text}
      disabled={disabled}
      placeholder="留空 = 全部授权属性；跨对象用 类型.属性"
      onChange={event => {
        setEditing(true)
        setText(event.target.value)
        onChange(withQuerySelectText(query, event.target.value))
      }}
      onBlur={() => {
        setEditing(false)
        setText(querySelectText(query))
      }}
      className={fieldClass}
      spellCheck={false}
    />
  )
}

export function ReportQueryPlanEditor({
  queries,
  objectTypes,
  linkTypes,
  readonly = false,
  onChange,
}: {
  queries: EditorQuery[]
  objectTypes: ReportObjectTypeOption[]
  linkTypes: ReportLinkTypeOption[]
  readonly?: boolean
  onChange: (queries: EditorQuery[]) => void
}) {
  const replace = (index: number, next: EditorQuery) => {
    onChange(queries.map((query, current) => current === index ? next : query))
  }
  const patch = (index: number, key: string, value: unknown) => {
    replace(index, withArgument(queries[index], key, value))
  }
  const addQuery = () => {
    if (readonly || queries.length >= MAX_QUERIES_PER_SECTION) return
    onChange([...queries, defaultEditorQuery(objectTypes[0]?.name || '')])
  }

  return (
    <div className="mt-2 space-y-2" data-testid="report-query-plan-editor">
      {queries.map((query, index) => {
        const objectType = argumentString(query, 'object_type')
        const properties = objectTypes.find(option => option.name === objectType)?.properties || []
        const filters = filterRows(query)
        const groups = groupRows(query)
        const aggregates = aggregateRows(query)
        const orders = orderRows(query)
        const joins = joinRows(query)
        const window = windowRow(query)
        const unknownKeys = unknownArgumentKeys(query)
        const filterOps = query.tool === 'query_objects' ? QUERY_FILTER_OPS : FILTER_OPS

        return (
          <div key={index} data-testid={`report-query-${index}`} className="rounded-xl border border-border bg-card p-2.5">
            <div className="flex items-center justify-between gap-2">
              <span className="text-[10px] font-semibold uppercase tracking-[0.12em] text-brand-ink">查询 {index + 1}</span>
              <div className="flex items-center gap-1">
                <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] text-muted-foreground">{toolLabel(query.tool)}</span>
                <button
                  type="button"
                  aria-label={`删除查询 ${index + 1}`}
                  disabled={readonly || queries.length <= 1}
                  onClick={() => onChange(queries.filter((_item, current) => current !== index))}
                  className={removeButtonClass}
                >
                  <Trash2 size={12} />
                </button>
              </div>
            </div>

            <div className="mt-2 space-y-2">
              <label className="block space-y-1">
                <span className={labelClass}>查询工具</span>
                <CompactSelect
                  ariaLabel={`查询 ${index + 1} 工具`}
                  value={query.tool}
                  disabled={readonly}
                  onChange={value => {
                    const tool = REPORT_QUERY_TOOLS.find(item => item.value === value)
                    if (tool) replace(index, switchQueryTool(query, tool.value))
                  }}
                  options={REPORT_QUERY_TOOLS.map(tool => ({
                    value: tool.value,
                    label: `${tool.label} · ${tool.hint}`,
                  }))}
                />
              </label>

              <label className="block space-y-1">
                <span className={labelClass}>对象类型</span>
                <ObjectTypeField
                  value={objectType}
                  options={objectTypes}
                  disabled={readonly}
                  onChange={value => patch(index, 'object_type', value || undefined)}
                />
              </label>

              {query.tool === 'aggregate_objects' && (
                <div className="grid grid-cols-2 gap-2">
                  <label className="block space-y-1">
                    <span className={labelClass}>聚合指标</span>
                    <CompactSelect
                      ariaLabel="聚合指标"
                      value={argumentString(query, 'metric') || 'count'}
                      disabled={readonly}
                      onChange={value => patch(index, 'metric', value)}
                      options={AGGREGATE_METRICS.map(metric => ({ value: metric, label: metric }))}
                    />
                  </label>
                  <label className="block space-y-1">
                    <span className={labelClass}>指标属性</span>
                    <PropertyField
                      value={argumentString(query, 'metric_property')}
                      properties={properties}
                      disabled={readonly || (argumentString(query, 'metric') || 'count') === 'count'}
                      onChange={value => patch(index, 'metric_property', value || undefined)}
                      ariaLabel="聚合指标属性"
                      placeholder={(argumentString(query, 'metric') || 'count') === 'count' ? 'count 无需属性' : '数值属性'}
                    />
                  </label>
                  <label className="col-span-2 block space-y-1">
                    <span className={labelClass}>分组属性（单个）</span>
                    <PropertyField
                      value={argumentString(query, 'group_by')}
                      properties={properties}
                      disabled={readonly}
                      onChange={value => patch(index, 'group_by', value || undefined)}
                      ariaLabel="聚合分组属性"
                    />
                  </label>
                </div>
              )}

              {query.tool === 'search_objects' && (
                <div className="grid grid-cols-[minmax(0,1fr)_88px] gap-2">
                  <label className="block space-y-1">
                    <span className={labelClass}>关键词</span>
                    <input
                      aria-label="检索关键词"
                      value={argumentString(query, 'keyword')}
                      disabled={readonly}
                      onChange={event => patch(index, 'keyword', event.target.value || undefined)}
                      className={fieldClass}
                    />
                  </label>
                  <label className="block space-y-1">
                    <span className={labelClass}>条数</span>
                    <input
                      type="number"
                      aria-label="检索条数"
                      min={1}
                      max={MAX_QUERY_PAGE_SIZE}
                      value={argumentNumber(query, 'limit', DEFAULT_QUERY_PAGE_SIZE)}
                      disabled={readonly}
                      onChange={event => patch(index, 'limit', Math.max(1, Math.min(MAX_QUERY_PAGE_SIZE, Math.floor(Number(event.target.value) || DEFAULT_QUERY_PAGE_SIZE))))}
                      className={fieldClass}
                    />
                  </label>
                </div>
              )}

              {query.tool === 'query_objects' && (
                <>
                  <label className="block space-y-1">
                    <span className={labelClass}>输出属性</span>
                    <QuerySelectField
                      query={query}
                      disabled={readonly}
                      onChange={next => replace(index, next)}
                    />
                    <span className="block text-[10px] leading-4 text-[var(--color-text-tertiary)]">
                      明细模式留空即全部授权属性；跨对象属性用「类型.属性」。
                    </span>
                  </label>

                  <div className="space-y-1">
                    <div className="flex items-center justify-between">
                      <span className={labelClass}>分组维度</span>
                      <button type="button" disabled={readonly} className={miniButtonClass}
                              onClick={() => replace(index, withGroupRows(query, [...groups, { property: '', bucket: '', objectType: '' }]))}>
                        <Plus size={11} />添加分组
                      </button>
                    </div>
                    {groups.map((group, groupIndex) => (
                      <div key={groupIndex} className="flex items-center gap-1.5">
                        <PropertyField
                          value={group.property}
                          properties={properties}
                          disabled={readonly}
                          onChange={value => replace(index, withGroupRows(query, groups.map((item, current) => current === groupIndex ? { ...item, property: value } : item)))}
                          ariaLabel={`分组 ${groupIndex + 1} 属性`}
                        />
                        <CompactSelect
                          ariaLabel={`分组 ${groupIndex + 1} 时间桶`}
                          value={group.bucket}
                          disabled={readonly}
                          onChange={value => replace(index, withGroupRows(query, groups.map((item, current) => current === groupIndex ? { ...item, bucket: value } : item)))}
                          options={[
                            { value: '', label: '不分桶' },
                            ...QUERY_BUCKETS.map(bucket => ({ value: bucket, label: bucket })),
                          ]}
                          className="w-24 shrink-0"
                        />
                        <button type="button" aria-label={`删除分组 ${groupIndex + 1}`} disabled={readonly} className={removeButtonClass}
                                onClick={() => replace(index, withGroupRows(query, groups.filter((_item, current) => current !== groupIndex)))}>
                          <Trash2 size={12} />
                        </button>
                      </div>
                    ))}
                  </div>

                  <div className="space-y-1">
                    <div className="flex items-center justify-between">
                      <span className={labelClass}>聚合指标</span>
                      <button type="button" disabled={readonly} className={miniButtonClass}
                              onClick={() => replace(index, withAggregateRows(query, [...aggregates, { fn: 'count', property: '', as: '', objectType: '' }]))}>
                        <Plus size={11} />添加聚合
                      </button>
                    </div>
                    {aggregates.map((aggregate, aggregateIndex) => (
                      <div key={aggregateIndex} className="flex items-center gap-1.5">
                        <CompactSelect
                          ariaLabel={`聚合 ${aggregateIndex + 1} 函数`}
                          value={aggregate.fn}
                          disabled={readonly}
                          onChange={value => replace(index, withAggregateRows(query, aggregates.map((item, current) => current === aggregateIndex ? { ...item, fn: value } : item)))}
                          options={QUERY_AGGREGATE_FNS.map(fn => ({ value: fn, label: fn }))}
                          className="w-28 shrink-0"
                        />
                        <PropertyField
                          value={aggregate.property}
                          properties={properties}
                          disabled={readonly || aggregate.fn === 'count'}
                          onChange={value => replace(index, withAggregateRows(query, aggregates.map((item, current) => current === aggregateIndex ? { ...item, property: value } : item)))}
                          ariaLabel={`聚合 ${aggregateIndex + 1} 属性`}
                          placeholder={aggregate.fn === 'count' ? 'count 无需属性' : '属性'}
                        />
                        <input
                          aria-label={`聚合 ${aggregateIndex + 1} 别名`}
                          value={aggregate.as}
                          disabled={readonly}
                          placeholder="别名"
                          onChange={event => replace(index, withAggregateRows(query, aggregates.map((item, current) => current === aggregateIndex ? { ...item, as: event.target.value } : item)))}
                          className={`${fieldClass} w-20 shrink-0`}
                        />
                        <button type="button" aria-label={`删除聚合 ${aggregateIndex + 1}`} disabled={readonly} className={removeButtonClass}
                                onClick={() => replace(index, withAggregateRows(query, aggregates.filter((_item, current) => current !== aggregateIndex)))}>
                          <Trash2 size={12} />
                        </button>
                      </div>
                    ))}
                  </div>

                  <div className="space-y-1">
                    <div className="flex items-center justify-between">
                      <span className={labelClass}>排序</span>
                      <button type="button" disabled={readonly} className={miniButtonClass}
                              onClick={() => replace(index, withOrderRows(query, [...orders, { output: '', direction: 'asc' }]))}>
                        <Plus size={11} />添加排序
                      </button>
                    </div>
                    {orders.map((order, orderIndex) => (
                      <div key={orderIndex} className="flex items-center gap-1.5">
                        <input
                          aria-label={`排序 ${orderIndex + 1} 输出字段`}
                          value={order.output}
                          disabled={readonly}
                          placeholder="输出字段名"
                          onChange={event => replace(index, withOrderRows(query, orders.map((item, current) => current === orderIndex ? { ...item, output: event.target.value } : item)))}
                          className={fieldClass}
                        />
                        <CompactSelect
                          ariaLabel={`排序 ${orderIndex + 1} 方向`}
                          value={order.direction}
                          disabled={readonly}
                          onChange={value => replace(index, withOrderRows(query, orders.map((item, current) => current === orderIndex ? { ...item, direction: value } : item)))}
                          options={[{ value: 'asc', label: '升序' }, { value: 'desc', label: '降序' }]}
                          className="w-20 shrink-0"
                        />
                        <button type="button" aria-label={`删除排序 ${orderIndex + 1}`} disabled={readonly} className={removeButtonClass}
                                onClick={() => replace(index, withOrderRows(query, orders.filter((_item, current) => current !== orderIndex)))}>
                          <Trash2 size={12} />
                        </button>
                      </div>
                    ))}
                  </div>

                  <div className="grid grid-cols-2 gap-2">
                    <label className="block space-y-1">
                      <span className={labelClass}>每页条数</span>
                      <input
                        type="number"
                        aria-label="每页条数"
                        min={1}
                        max={MAX_QUERY_PAGE_SIZE}
                        value={argumentNumber(query, 'page_size', DEFAULT_QUERY_PAGE_SIZE)}
                        disabled={readonly}
                        onChange={event => patch(index, 'page_size', Math.max(1, Math.min(MAX_QUERY_PAGE_SIZE, Math.floor(Number(event.target.value) || DEFAULT_QUERY_PAGE_SIZE))))}
                        className={fieldClass}
                      />
                    </label>
                  </div>

                  <div className="space-y-1 rounded-lg bg-muted px-2 py-1.5">
                    <div className="flex items-center justify-between">
                      <span className={labelClass}>时间窗口（相对时间由服务端解析）</span>
                      {window ? (
                        <button type="button" disabled={readonly} className={miniButtonClass}
                                onClick={() => replace(index, withWindowRow(query, null))}>
                          移除
                        </button>
                      ) : (
                        <button type="button" disabled={readonly} className={miniButtonClass}
                                onClick={() => replace(index, withWindowRow(query, {
                                  property: '', timezone: 'Asia/Shanghai', mode: 'last',
                                  unit: 'day', count: 7, start: '', end: '',
                                }))}>
                          <Plus size={11} />添加窗口
                        </button>
                      )}
                    </div>
                    {window && (
                      <div className="mt-1 space-y-1">
                        <div className="grid grid-cols-2 gap-1.5">
                          <PropertyField
                            value={window.property}
                            properties={properties}
                            disabled={readonly}
                            onChange={value => replace(index, withWindowRow(query, { ...window, property: value }))}
                            ariaLabel="时间窗口属性"
                            placeholder="时间属性"
                          />
                          <input
                            aria-label="时间窗口时区"
                            value={window.timezone}
                            disabled={readonly}
                            onChange={event => replace(index, withWindowRow(query, { ...window, timezone: event.target.value }))}
                            className={fieldClass}
                            placeholder="Asia/Shanghai"
                          />
                        </div>
                        <div className="grid grid-cols-2 gap-1.5">
                          <CompactSelect
                            ariaLabel="时间窗口模式"
                            value={window.mode}
                            disabled={readonly}
                            onChange={value => replace(index, withWindowRow(query, { ...window, mode: value === 'range' ? 'range' : 'last' }))}
                            options={[
                              { value: 'last', label: '最近 N 个时间单位' },
                              { value: 'range', label: '固定起止时间' },
                            ]}
                          />
                          {window.mode === 'last' ? (
                            <div className="grid grid-cols-[72px_minmax(0,1fr)] gap-1.5">
                              <CompactSelect
                                ariaLabel="相对时间单位"
                                value={window.unit}
                                disabled={readonly}
                                onChange={value => replace(index, withWindowRow(query, { ...window, unit: value }))}
                                options={WINDOW_UNITS.map(unit => ({ value: unit, label: unit }))}
                              />
                              <input
                                type="number"
                                aria-label="相对时间数量"
                                min={1}
                                max={1000}
                                value={window.count}
                                disabled={readonly}
                                onChange={event => replace(index, withWindowRow(query, { ...window, count: Math.max(1, Math.min(1000, Math.floor(Number(event.target.value) || 1))) }))}
                                className={fieldClass}
                              />
                            </div>
                          ) : (
                            <div className="grid grid-cols-2 gap-1.5">
                              <input
                                aria-label="窗口开始时间"
                                value={window.start}
                                disabled={readonly}
                                onChange={event => replace(index, withWindowRow(query, { ...window, start: event.target.value }))}
                                className={fieldClass}
                                placeholder="2026-01-01T00:00:00"
                              />
                              <input
                                aria-label="窗口结束时间"
                                value={window.end}
                                disabled={readonly}
                                onChange={event => replace(index, withWindowRow(query, { ...window, end: event.target.value }))}
                                className={fieldClass}
                                placeholder="2026-02-01T00:00:00"
                              />
                            </div>
                          )}
                        </div>
                      </div>
                    )}
                  </div>

                  <details className="rounded-lg bg-muted px-2 py-1.5">
                    <summary className="cursor-pointer text-[10px] font-semibold text-muted-foreground">关联查询（最多 6 跳）</summary>
                    <div className="mt-1.5 space-y-1">
                      {joins.map((join, joinIndex) => (
                        <div key={joinIndex} className="space-y-1 rounded-md bg-card p-1.5">
                          <div className="flex items-center gap-1.5">
                            <ObjectTypeField
                              value={join.objectType}
                              options={objectTypes.filter(option => option.name !== objectType)}
                              disabled={readonly}
                              emptyLabel="关联对象"
                              onChange={value => replace(index, withJoinRows(query, joins.map((item, current) => current === joinIndex ? { ...item, objectType: value } : item)))}
                            />
                            <LinkTypeField
                              value={join.linkType}
                              options={linkTypes}
                              disabled={readonly}
                              onChange={value => replace(index, withJoinRows(query, joins.map((item, current) => current === joinIndex ? { ...item, linkType: value } : item)))}
                            />
                            <button type="button" aria-label={`删除关联 ${joinIndex + 1}`} disabled={readonly} className={removeButtonClass}
                                    onClick={() => replace(index, withJoinRows(query, joins.filter((_item, current) => current !== joinIndex)))}>
                              <Trash2 size={12} />
                            </button>
                          </div>
                          <CompactSelect
                            ariaLabel={`关联 ${joinIndex + 1} 方式`}
                            value={join.type}
                            disabled={readonly}
                            onChange={value => replace(index, withJoinRows(query, joins.map((item, current) => current === joinIndex ? { ...item, type: value } : item)))}
                            options={[{ value: 'inner', label: 'inner' }, { value: 'left', label: 'left' }]}
                          />
                        </div>
                      ))}
                      <button type="button" disabled={readonly || joins.length >= 6} className={miniButtonClass}
                              onClick={() => replace(index, withJoinRows(query, [...joins, { objectType: '', linkType: '', type: 'inner' }]))}>
                        <Plus size={11} />添加关联
                      </button>
                    </div>
                  </details>

                  <p className="text-[10px] leading-4 text-[var(--color-text-tertiary)]">
                    分组/聚合结果会在报告中自动生成图表与明细表；报告运行不发放交互下钻令牌。
                  </p>
                </>
              )}

              <div className="space-y-1">
                <div className="flex items-center justify-between">
                  <span className={labelClass}>过滤条件</span>
                  <button type="button" disabled={readonly} className={miniButtonClass}
                          onClick={() => replace(index, withFilterRows(query, [...filters, { property: '', op: 'eq', value: '', objectType: '' }]))}>
                    <Plus size={11} />添加过滤
                  </button>
                </div>
                {filters.map((filter, filterIndex) => {
                  const valueHidden = filter.op === 'is_null' || filter.op === 'is_not_null'
                  return (
                    <div key={filterIndex} className="space-y-1 rounded-md bg-muted p-1.5">
                      <div className="flex items-center gap-1.5">
                        <PropertyField
                          value={filter.property}
                          properties={properties}
                          disabled={readonly}
                          onChange={value => replace(index, withFilterRows(query, filters.map((item, current) => current === filterIndex ? { ...item, property: value } : item)))}
                          ariaLabel={`过滤 ${filterIndex + 1} 属性`}
                        />
                        <CompactSelect
                          ariaLabel={`过滤 ${filterIndex + 1} 操作符`}
                          value={filter.op}
                          disabled={readonly}
                          onChange={value => replace(index, withFilterRows(query, filters.map((item, current) => current === filterIndex ? { ...item, op: value } : item)))}
                          options={filterOps.map(op => ({ value: op, label: op }))}
                          className="w-24 shrink-0"
                        />
                        <button type="button" aria-label={`删除过滤 ${filterIndex + 1}`} disabled={readonly} className={removeButtonClass}
                                onClick={() => replace(index, withFilterRows(query, filters.filter((_item, current) => current !== filterIndex)))}>
                          <Trash2 size={12} />
                        </button>
                      </div>
                      {!valueHidden && (
                        <input
                          aria-label={`过滤 ${filterIndex + 1} 值`}
                          value={filterValueToText(filter.value)}
                          disabled={readonly}
                          placeholder="值（数组/对象用 JSON，数字/布尔自动识别）"
                          onChange={event => replace(index, withFilterRows(query, filters.map((item, current) => current === filterIndex ? { ...item, value: filterValueFromText(event.target.value) } : item)))}
                          className={fieldClass}
                          spellCheck={false}
                        />
                      )}
                      {query.tool === 'query_objects' && joins.length > 0 && (
                        <ObjectTypeField
                          value={filter.objectType}
                          options={objectTypes}
                          disabled={readonly}
                          emptyLabel="主对象"
                          onChange={value => replace(index, withFilterRows(query, filters.map((item, current) => current === filterIndex ? { ...item, objectType: value } : item)))}
                        />
                      )}
                    </div>
                  )
                })}
              </div>

              {unknownKeys.length > 0 && (
                <div className="rounded-lg bg-[var(--color-warning-bg)] px-2 py-1.5 text-[10px] leading-4 text-[var(--color-warning)]">
                  含界面不支持的参数：{unknownKeys.join('、')}。保存前需清理或改选工具。
                  {!readonly && (
                    <button
                      type="button"
                      className="ml-1 font-semibold underline"
                      onClick={() => {
                        let next = query
                        for (const key of unknownKeys) next = withArgument(next, key, undefined)
                        replace(index, next)
                      }}
                    >
                      清理未知参数
                    </button>
                  )}
                </div>
              )}
            </div>
          </div>
        )
      })}

      <button
        type="button"
        disabled={readonly || queries.length >= MAX_QUERIES_PER_SECTION}
        onClick={addQuery}
        className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-[10px] font-semibold text-brand-ink transition-colors hover:bg-brand-soft disabled:cursor-not-allowed disabled:opacity-40"
      >
        <Plus size={12} />添加查询（每章最多 {MAX_QUERIES_PER_SECTION} 个）
      </button>
    </div>
  )
}
