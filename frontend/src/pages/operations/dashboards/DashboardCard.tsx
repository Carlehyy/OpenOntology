/**
 * 监控看板卡片（对齐运营报告卡与本体卡的版式）。
 *
 * - 头部：图标头像 + 名称 + 创建/更新时间；仅 missing 显示「已失效」徽标，
 *   unknown 不显示徽标（Grafana 探测失败不能误判为已失效）。
 * - 底部：信息 / 编排 / 预览（左）、删除（右）。编排与预览在 missing 或
 *   Grafana 不可用时置灰，并用外层 span 的 title 解释原因（disabled 按钮
 *   自身不触发 title 悬浮）。
 * - 拖拽排序用原生 HTML5 drag（与本体卡一致），筛选激活或只读角色时禁用。
 */
import type { DragEvent } from 'react'
import { formatDateTime } from '@/utils/datetime'
import { Badge } from '@/components/ui/Badge'
import { Eye, Pencil, SlidersHorizontal, Trash2 } from 'lucide-react'
import type { OperationsDashboardCard } from '@/api/v2/operations-dashboards'
import {
  dashboardTimeLabel,
  shouldShowMissingBadge,
  type DropPlace,
} from './dashboardModel'
import { dashboardIconOption } from './dashboardIcons'

interface DashboardCardProps {
  dashboard: OperationsDashboardCard
  canWrite: boolean
  /** dashboardActionBlockReason 的结果；非 null 时禁用编排/预览。 */
  actionBlockReason: string | null
  draggable: boolean
  dragging: boolean
  dropPlace: DropPlace | null
  onEdit: () => void
  onOrchestrate: () => void
  onPreview: () => void
  onDelete: () => void
  onDragStart: (id: string) => void
  onDragOver: (id: string, before: boolean) => void
  onDragLeave: (id: string) => void
  onDrop: (id: string) => void
  onDragEnd: () => void
}

const FOOTER_BUTTON_CLASS =
  'inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-lg bg-muted px-2 py-1.5 text-[11px] font-medium text-foreground transition-colors hover:bg-brand-soft hover:text-brand-ink disabled:pointer-events-none disabled:cursor-not-allowed disabled:text-[var(--color-text-tertiary)] disabled:opacity-60'

export function DashboardCardView({
  dashboard,
  canWrite,
  actionBlockReason,
  draggable,
  dragging,
  dropPlace,
  onEdit,
  onOrchestrate,
  onPreview,
  onDelete,
  onDragStart,
  onDragOver,
  onDragLeave,
  onDrop,
  onDragEnd,
}: DashboardCardProps) {
  const iconOption = dashboardIconOption(dashboard.icon)
  const Icon = iconOption.icon
  const time = dashboardTimeLabel(dashboard.created_at, dashboard.updated_at)
  const timeText = `${time.label} ${formatDateTime(time.value)}`
  const missing = shouldShowMissingBadge(dashboard)
  const actionsDisabled = actionBlockReason !== null

  const handleDragStart = (event: DragEvent<HTMLElement>) => {
    if (!draggable) {
      event.preventDefault()
      return
    }
    event.dataTransfer.effectAllowed = 'move'
    event.dataTransfer.setData('text/plain', dashboard.id)
    onDragStart(dashboard.id)
  }

  return (
    <article
      draggable={draggable}
      onDragStart={handleDragStart}
      onDragOver={event => {
        if (!draggable) return
        event.preventDefault()
        event.dataTransfer.dropEffect = 'move'
        const rect = event.currentTarget.getBoundingClientRect()
        onDragOver(dashboard.id, event.clientX < rect.left + rect.width / 2)
      }}
      onDragLeave={() => onDragLeave(dashboard.id)}
      onDrop={event => {
        if (!draggable) return
        event.preventDefault()
        onDrop(dashboard.id)
      }}
      onDragEnd={onDragEnd}
      data-testid="dashboard-card"
      data-dashboard-id={dashboard.id}
      data-grafana-status={dashboard.grafana_status}
      className={`group flex min-h-[190px] flex-col overflow-hidden rounded-2xl border border-border bg-card transition-all duration-200 hover:-translate-y-0.5 hover:border-brand-line hover:shadow-[var(--shadow-md)] ${
        draggable ? 'cursor-grab active:cursor-grabbing' : ''
      } ${dragging ? 'opacity-40' : ''} ${dropPlace ? 'outline-2 -outline-offset-2 outline-brand' : ''}`}
    >
      <div className="flex flex-col p-4 pb-2.5">
        <div className="flex min-h-10 items-start gap-3">
          <span
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-brand-soft text-brand-ink"
            title={iconOption.label}
          >
            <Icon size={18} />
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex min-w-0 items-center gap-2">
              <h3
                className="min-w-0 flex-1 truncate text-[15px] font-semibold leading-5 text-foreground"
                title={dashboard.name}
              >
                {dashboard.name}
              </h3>
              {missing && (
                <Badge variant="danger" data-testid="dashboard-missing-badge">已失效</Badge>
              )}
            </div>
            <p className="mt-1 text-[11px] tabular-nums text-muted-foreground" title={`${time.label}：${formatDateTime(time.value)}`}>
              {timeText}
            </p>
          </div>
        </div>
        <p
          className="mt-3 min-h-[40px] text-sm leading-5 text-muted-foreground"
          style={{ display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}
          title={dashboard.description || '暂无描述'}
        >
          {dashboard.description || '暂无描述'}
        </p>
      </div>
      <footer className="mt-auto flex min-h-10 items-center gap-1.5 border-t border-border px-4 py-1.5">
        {canWrite && (
          <button type="button" onClick={onEdit} className={FOOTER_BUTTON_CLASS} aria-label={`信息 ${dashboard.name}`}>
            <Pencil size={12} /> 信息
          </button>
        )}
        {canWrite && (
          <span title={actionBlockReason ?? undefined} className="inline-flex shrink-0">
            <button
              type="button"
              onClick={onOrchestrate}
              disabled={actionsDisabled}
              className={FOOTER_BUTTON_CLASS}
              aria-label={`编排 ${dashboard.name}`}
            >
              <SlidersHorizontal size={12} /> 编排
            </button>
          </span>
        )}
        <span title={actionBlockReason ?? undefined} className="inline-flex shrink-0">
          <button
            type="button"
            onClick={onPreview}
            disabled={actionsDisabled}
            className={FOOTER_BUTTON_CLASS}
            aria-label={`预览 ${dashboard.name}`}
          >
            <Eye size={12} /> 预览
          </button>
        </span>
        {canWrite && (
          <button
            type="button"
            onClick={onDelete}
            className="ml-auto shrink-0 rounded-lg p-1.5 text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            title="删除看板"
            aria-label={`删除看板 ${dashboard.name}`}
          >
            <Trash2 size={14} />
          </button>
        )}
      </footer>
    </article>
  )
}
