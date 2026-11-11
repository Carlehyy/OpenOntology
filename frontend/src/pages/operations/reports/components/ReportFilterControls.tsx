/**
 * 筛选控件（首版 text/number/date 标量）：值由父组件持有，
 * 修改不自动执行——由「查询」按钮手动刷新（报告阅读态无轮询）。
 */
import type { ReportFilterDefinition } from '../reportModel'

const inputClass = 'h-8 w-full rounded-lg border border-border bg-card px-2 text-xs text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring'

export default function ReportFilterControls({ filters, values, onChange }: {
  filters: ReportFilterDefinition[]
  values: Record<string, string>
  onChange: (name: string, value: string) => void
}) {
  if (!filters.length) return null
  return (
    <div
      className="flex flex-wrap items-end gap-3 rounded-xl border border-border bg-card px-4 py-3"
      data-testid="report-filter-bar"
      aria-label="筛选条件"
    >
      {filters.map(filter => (
        <label key={filter.name} className="flex min-w-40 flex-col gap-1">
          <span className="text-[11px] font-medium text-muted-foreground">{filter.label}</span>
          <input
            type={filter.type === 'number' ? 'number' : filter.type === 'date' ? 'date' : 'text'}
            value={values[filter.name] ?? ''}
            onChange={event => onChange(filter.name, event.target.value)}
            aria-label={filter.label}
            data-testid={`report-filter-${filter.name}`}
            className={inputClass}
          />
        </label>
      ))}
    </div>
  )
}
