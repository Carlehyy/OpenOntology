/**
 * 筛选控件（首版 text/number/date 标量）：值由父组件持有，
 * 修改不自动执行——由「查询」按钮手动刷新（报告阅读态无轮询）。
 *
 * layout：
 * - card（默认）：独立圆角卡片，标签在输入框上方，嵌在编辑态预览等文档流中；
 * - bar：一体化命令条（与接口管理的地址栏同构）——标签在输入框左侧，
 *   右端承接「查询」按钮，外层容器由调用方提供。
 */
import type { ReactNode } from 'react'
import type { ReportFilterDefinition } from '../reportModel'

const inputClass = 'h-8 rounded-md border border-border bg-card px-2 text-xs text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring'

export default function ReportFilterControls({ filters, values, onChange, layout = 'card', trailing }: {
  filters: ReportFilterDefinition[]
  values: Record<string, string>
  onChange: (name: string, value: string) => void
  layout?: 'card' | 'bar'
  /** 右端的执行按钮等尾部内容。 */
  trailing?: ReactNode
}) {
  if (!filters.length) return null
  const bar = layout === 'bar'
  return (
    <div
      className={bar ? 'flex min-w-0 flex-wrap items-center gap-x-5 gap-y-2' : 'flex flex-wrap items-end gap-3 rounded-xl border border-border bg-card px-4 py-3'}
      data-testid="report-filter-bar"
      aria-label="筛选条件"
    >
      {filters.map(filter => (
        <label key={filter.name} className={bar ? 'flex items-center gap-2' : 'flex min-w-40 flex-col gap-1'}>
          <span className="shrink-0 text-[11px] font-medium text-muted-foreground">{filter.label}</span>
          <input
            type={filter.type === 'number' ? 'number' : filter.type === 'date' ? 'date' : 'text'}
            value={values[filter.name] ?? ''}
            onChange={event => onChange(filter.name, event.target.value)}
            aria-label={filter.label}
            data-testid={`report-filter-${filter.name}`}
            className={bar ? inputClass + ' w-40' : inputClass + ' w-full'}
          />
        </label>
      ))}
      {trailing}
    </div>
  )
}
