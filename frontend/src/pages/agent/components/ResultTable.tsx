/**
 * 通用结果明细表（antd Table）：按 query-view 返回的 columns/rows 渲染，
 * 排序/分页都是服务端行为（cursor），行级下钻由 query-view 的 drilldown
 * 描述驱动。表格不猜测聚合语义，也不把未返回的列补成假数据。
 */
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { ChevronLeft, ChevronRight, Rows3 } from 'lucide-react'
import { Table } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type { AgentQueryColumn, AgentQueryViewResult } from '@/api/agent'
import {
  describeRowCount,
  drilldownForResult,
  drilldownForResultRow,
  formatQueryValue,
  formatResolvedWindow,
} from './queryResult'

export interface ResultTableProps {
  result: AgentQueryViewResult | null
  loading: boolean
  error: string
  sort: { key: string; direction: 'asc' | 'desc' } | null
  canSort: boolean
  page: number
  canPrev: boolean
  canNext: boolean
  onSort: (column: AgentQueryColumn, direction: 'asc' | 'desc') => void
  onPrev: () => void
  onNext: () => void
  onDrilldown: (row: Record<string, unknown>) => void
  /** 行内无 drilldown 时，step 级描述可作为兜底下钻入口。 */
  drilldownFallback?: Record<string, unknown> | null
}

function resultColumns(
  columns: AgentQueryColumn[],
  props: ResultTableProps,
  hasDrilldown: boolean,
): ColumnsType<Record<string, unknown>> {
  const mapped: ColumnsType<Record<string, unknown>> = columns.map(column => ({
    key: column.key,
    dataIndex: column.key,
    title: column.label,
    width: column.width,
    align: column.align,
    ellipsis: true,
    sorter: props.canSort,
    sortOrder: props.sort?.key === column.key
      ? (props.sort.direction === 'asc' ? 'ascend' : 'descend')
      : null,
    render: (value: unknown) => {
      const text = formatQueryValue(value)
      return (
        <span className="block max-w-[36rem] truncate text-xs text-[var(--color-text-primary)]" title={text}>
          {text}
        </span>
      )
    },
  }))
  if (hasDrilldown) {
    mapped.push({
      key: '__drilldown__',
      title: '',
      width: 88,
      fixed: 'right',
      render: (_value, row) => {
        // 只有当前结果自身（行描述或 step 级 fallback）有有效 descriptor
        // 的行才提供下钻；父级 drilldown 上下文不得泄漏到 child 明细行。
        const descriptor = props.result
          ? drilldownForResultRow(props.result, row, props.drilldownFallback ?? null)
          : null
        if (!descriptor) return null
        return (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            data-testid="agent-result-drilldown"
            className="h-7 px-2 text-[11px] text-brand-ink"
            onClick={() => props.onDrilldown(row)}
          >
            下钻
          </Button>
        )
      },
    })
  }
  return mapped
}

export function ResultTable(props: ResultTableProps) {
  const { result, loading, error, page, canPrev, canNext, onPrev, onNext } = props
  const rows = result?.rows ?? []
  const columns = result?.columns ?? []
  const drilldown = result ? drilldownForResult(rows, props.drilldownFallback ?? null) : null
  const antdColumns = resultColumns(columns, props, !!drilldown)

  if (error) {
    return <Alert variant="danger" role="alert">{error}</Alert>
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="agent-result-table">
      <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-[var(--color-text-tertiary)]">
        <span className="inline-flex items-center gap-1 text-[var(--color-text-secondary)]">
          <Rows3 size={12} />
          {result ? describeRowCount(result) : '等待查询结果'}
        </span>
        {result?.observedAt && <span>观测于 {result.observedAt}</span>}
        {result?.resolvedWindow != null && <span>时间窗 {formatResolvedWindow(result.resolvedWindow)}</span>}
        {result?.dataMode && <span>数据模式 {result.dataMode}</span>}
      </div>
      <div className="min-h-0 flex-1 overflow-auto rounded-lg border border-[var(--color-border)]">
        <Table<Record<string, unknown>>
          size="small"
          bordered={false}
          rowKey={(_row, index) => String(index)}
          loading={loading}
          columns={antdColumns}
          dataSource={rows}
          pagination={false}
          scroll={{ x: 'max-content' }}
          locale={{ emptyText: loading ? '正在查询…' : '没有查询结果' }}
          onChange={(_pagination, _filters, sorter) => {
            if (!props.canSort) return
            const current = Array.isArray(sorter) ? sorter[0] : sorter
            if (!current?.order) return
            const column = columns.find(item => item.key === String(current.columnKey ?? current.field))
            if (!column) return
            props.onSort(column, current.order === 'ascend' ? 'asc' : 'desc')
          }}
        />
      </div>
      <div className="mt-2 flex items-center justify-between gap-2">
        <span className="text-[11px] text-[var(--color-text-tertiary)]">
          第 {page + 1} 页{result?.pageSize ? ` · 每页 ${result.pageSize}` : ''}
        </span>
        <span className="flex items-center gap-1.5">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={!canPrev || loading}
            onClick={onPrev}
            data-testid="agent-result-prev"
          >
            <ChevronLeft size={13} />上一页
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={!canNext || loading}
            onClick={onNext}
            data-testid="agent-result-next"
          >
            下一页<ChevronRight size={13} />
          </Button>
        </span>
      </div>
    </div>
  )
}
