/**
 * 查询结果附带的数据集版本信息（阅读态展示实际版本/时间/物理来源）。
 * 同一次批量执行的各查询共享同一版本（后端同一事务内解析版本）。
 */
import type { OperationsQueryResult } from '@/api/v2/operations-reports'

const QUERY_SOURCE_LABELS: Record<string, string> = {
  sealed_relation: '封存版本',
  lake_table: '湖表',
  materialized_temp: '物化快照',
}

function formatQueryTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', { hour12: false })
}

export default function QueryVersionLabel({ result }: { result: OperationsQueryResult }) {
  const meta = result.dataset
  if (!meta) return null
  const parts = [`v${meta.version_no}`]
  if (meta.as_of) parts.push(formatQueryTime(meta.as_of))
  if (result.source) parts.push(QUERY_SOURCE_LABELS[result.source] ?? result.source)
  return (
    <span className="font-mono text-[11px] text-muted-foreground" data-testid="report-query-version">
      {parts.join(' · ')}
    </span>
  )
}
