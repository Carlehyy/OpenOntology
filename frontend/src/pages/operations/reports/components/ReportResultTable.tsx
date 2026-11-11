/**
 * 查询结果表（antd Table）：按服务端受限执行返回的 columns/rows 渲染。
 * 只消费服务端结果，不猜测聚合语义；truncated 时显式提示被截断。
 */
import { Table } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { formatQueryCell } from '../reportModel'
import type { OperationsQueryResult } from '@/api/v2/operations-reports'

export function resultColumns(columns: string[]): ColumnsType<Record<string, unknown>> {
  return columns.map(column => ({
    key: column,
    dataIndex: column,
    title: column,
    ellipsis: true,
    render: (value: unknown) => {
      const text = formatQueryCell(value)
      return (
        <span
          className={'block max-w-[24rem] truncate text-xs ' + (typeof value === 'number' ? 'tabular-nums' : '')}
          title={text}
        >
          {text}
        </span>
      )
    },
  }))
}

export default function ReportResultTable({ result, rowKey }: {
  result: OperationsQueryResult
  /** 表格行 key 列名（默认取第一列，回退行号）。 */
  rowKey?: string
}) {
  const keyColumn = rowKey && result.columns.includes(rowKey) ? rowKey : result.columns[0]
  return (
    <div data-testid="report-result-table">
      <Table
        size="small"
        columns={resultColumns(result.columns)}
        dataSource={result.rows}
        rowKey={(record, index) => String(keyColumn ? record[keyColumn] : index ?? '')}
        pagination={false}
        scroll={{ x: 'max-content', y: 320 }}
      />
      <p className="mt-1.5 flex items-center gap-2 text-[11px] text-muted-foreground">
        <span className="tabular-nums">{result.rowcount} 行 · {result.duration_ms} ms</span>
        {result.truncated && (
          <span className="rounded border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-1.5 py-0.5 text-[var(--color-warning)]">
            已达服务端行数上限，结果被截断
          </span>
        )}
      </p>
    </div>
  )
}
