/**
 * 智能运营 → 监控看板工作台（/operations/dashboard）。
 *
 * 业务边界（冻结契约 §1/§7）：平台只维护看板卡片（名称、描述、图标、顺序）
 * 与 Grafana 入口；数据源与面板由使用者在「编排」打开的 Grafana 原生界面中
 * 自行配置，平台不预置数据源、采集指标或告警，也不读取/回写 dashboard JSON。
 *
 * 关键交互：
 * - 进入页面签发一次看板会话，停留期间每 30 分钟续签（hook 卸载即清理）；
 * - 编排：用户点击事件中**同步**预开空白标签页，续签成功后跳到后端给出的
 *   `edit_path`；续签失败关闭空白页并明确报错；弹窗被拦截时清楚提示可重试；
 * - 预览：弹窗内先续签再挂载同源 kiosk iframe；关闭即卸载（连带 WS/计时器）；
 * - 排序：仅在无筛选且 canWrite 时可拖拽，落位后提交全量 id，失败回滚；
 * - 三态：missing 显示「已失效」并禁用编排/预览；unknown 不渲染为已失效；
 *   Grafana 整体不可用时显示告警条，但仍可查看/修改本地卡片信息。
 */
import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Plus, Search, X } from 'lucide-react'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { LoadingState } from '@/components/ui/LoadingState'
import { useAuthStore } from '@/stores/authStore'
import operationsDashboardsApi, {
  operationsApiErrorText,
  type OperationsDashboardCard,
  type OperationsDashboardCreatePayload,
  type OperationsDashboardList,
  type OperationsDashboardUpdatePayload,
} from '@/api/v2/operations-dashboards'
import { DashboardCardView } from './dashboards/DashboardCard'
import { DashboardFormDialog } from './dashboards/DashboardFormDialog'
import { DashboardPreviewDialog } from './dashboards/DashboardPreviewDialog'
import { useGrafanaSession } from './dashboards/useGrafanaSession'
import {
  canWriteDashboards,
  dashboardActionBlockReason,
  filterDashboards,
  reorderDashboardIds,
  type DropPlace,
} from './dashboards/dashboardModel'

const DASHBOARDS_QUERY_KEY = ['operations-dashboards'] as const

function CreateDashboardCard({ onCreate }: { onCreate: () => void }) {
  return (
    <article className="group flex min-h-[190px] flex-col items-center justify-center rounded-2xl border border-dashed border-brand-line bg-brand-soft p-6 text-center transition-all hover:border-brand hover:shadow-[var(--shadow-md)]">
      <div className="flex h-12 w-12 items-center justify-center rounded-full bg-brand text-[var(--color-text-inverse)] shadow-sm transition-transform group-hover:scale-105">
        <Plus size={22} />
      </div>
      <h2 className="mt-3 text-base font-semibold text-foreground">新建看板</h2>
      <p className="mt-1 max-w-[210px] text-xs leading-5 text-muted-foreground">
        先在平台建卡片，再到 Grafana 中配置数据源与面板
      </p>
      <button
        type="button"
        onClick={onCreate}
        className="mt-4 rounded-lg border border-brand-line bg-card px-3 py-1.5 text-xs font-medium text-brand-ink shadow-sm transition-colors hover:bg-brand-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
      >
        立即创建
      </button>
    </article>
  )
}

export default function MonitoringDashboardPage() {
  const queryClient = useQueryClient()
  const role = useAuthStore(state => state.user?.role)
  const canWrite = canWriteDashboards(role)
  const { ensureSession } = useGrafanaSession()

  const [keyword, setKeyword] = useState('')
  const [createOpen, setCreateOpen] = useState(false)
  const [editId, setEditId] = useState<string | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<OperationsDashboardCard | null>(null)
  const [previewId, setPreviewId] = useState<string | null>(null)
  const [draggingId, setDraggingId] = useState<string | null>(null)
  const [dropTarget, setDropTarget] = useState<{ id: string; before: boolean } | null>(null)

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: DASHBOARDS_QUERY_KEY,
    queryFn: () => operationsDashboardsApi.list(),
  })
  const items = useMemo(() => data?.items ?? [], [data?.items])
  const grafanaAvailable = data?.grafana_available ?? true
  const filtered = useMemo(() => filterDashboards(items, keyword), [items, keyword])
  const filterActive = keyword.trim().length > 0

  const editTarget = editId ? items.find(item => item.id === editId) ?? null : null
  const previewTarget = previewId ? items.find(item => item.id === previewId) ?? null : null

  // 列表刷新/他端删除后，正在编辑或预览的目标已不存在则收起对应弹窗。
  useEffect(() => {
    if (editId && !isLoading && !items.some(item => item.id === editId)) setEditId(null)
  }, [editId, isLoading, items])
  useEffect(() => {
    if (previewId && !isLoading && !items.some(item => item.id === previewId)) setPreviewId(null)
  }, [previewId, isLoading, items])

  const refreshList = () => queryClient.invalidateQueries({ queryKey: DASHBOARDS_QUERY_KEY })

  const createMutation = useMutation({
    mutationFn: (payload: OperationsDashboardCreatePayload) => operationsDashboardsApi.create(payload),
    onSuccess: () => {
      void refreshList()
      setCreateOpen(false)
      toast.success('看板已创建', { description: '点击「编排」在 Grafana 中配置面板与数据源。' })
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: OperationsDashboardUpdatePayload }) =>
      operationsDashboardsApi.update(id, payload),
    onSuccess: () => {
      void refreshList()
      setEditId(null)
      toast.success('看板信息已更新', { description: '仅更新平台卡片，未改动 Grafana 看板。' })
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => operationsDashboardsApi.remove(id),
    onSuccess: () => {
      void refreshList()
      setDeleteTarget(null)
      toast.success('看板已删除', { description: 'Grafana 中对应的看板已一并移除。' })
    },
    onError: error => {
      // 失败保留卡片与确认弹窗，用户可重试或取消。
      toast.error('删除失败', {
        description: operationsApiErrorText(error, '卡片已保留，请稍后重试。'),
      })
    },
  })

  const reorderMutation = useMutation({
    mutationFn: (dashboardIds: string[]) => operationsDashboardsApi.reorder(dashboardIds),
    onMutate: async dashboardIds => {
      await queryClient.cancelQueries({ queryKey: DASHBOARDS_QUERY_KEY })
      const previous = queryClient.getQueryData<OperationsDashboardList>(DASHBOARDS_QUERY_KEY)
      if (previous) {
        const byId = new Map(previous.items.map(item => [item.id, item]))
        const nextItems = dashboardIds
          .map(id => byId.get(id))
          .filter((item): item is OperationsDashboardCard => Boolean(item))
        queryClient.setQueryData<OperationsDashboardList>(
          DASHBOARDS_QUERY_KEY,
          { ...previous, items: nextItems },
        )
      }
      return { previous }
    },
    onSuccess: result => {
      queryClient.setQueryData<OperationsDashboardList>(
        DASHBOARDS_QUERY_KEY,
        current => (current ? { ...current, items: result.items } : current),
      )
      toast.success('看板顺序已保存')
    },
    onError: (error, _dashboardIds, context) => {
      // 仅回滚顺序并以最新缓存的卡片内容为准，避免覆盖并发保存的元数据或卡片增删。
      if (context?.previous) {
        queryClient.setQueryData<OperationsDashboardList>(DASHBOARDS_QUERY_KEY, current => {
          const source = current ?? context.previous!
          const byId = new Map(source.items.map(item => [item.id, item]))
          const previousIds = new Set(context.previous!.items.map(item => item.id))
          const restoredItems = context.previous!.items
            .map(item => byId.get(item.id))
            .filter((item): item is OperationsDashboardCard => Boolean(item))
          const addedItems = source.items
            .filter(item => !previousIds.has(item.id))
            .sort((left, right) => left.sort_order - right.sort_order)
          return { ...source, items: [...restoredItems, ...addedItems] }
        })
      }
      void refreshList()
      toast.error('排序保存失败', {
        description: operationsApiErrorText(error, '已恢复拖拽前顺序，请稍后重试。'),
      })
    },
  })

  const dragEnabled = canWrite && !filterActive && !reorderMutation.isPending

  const handleCardDrop = (targetId: string) => {
    const draggedId = draggingId
    const place: DropPlace = dropTarget?.id === targetId && dropTarget.before ? 'before' : 'after'
    setDraggingId(null)
    setDropTarget(null)
    if (!draggedId || !dragEnabled || !data) return
    const currentIds = items.map(item => item.id)
    const nextIds = reorderDashboardIds(currentIds, draggedId, targetId, place)
    if (nextIds === currentIds) return
    reorderMutation.mutate(nextIds)
  }

  /**
   * 编排：必须在点击事件中同步预开空白标签页（避免 await 后被弹窗拦截），
   * 续签成功后才把空白页导航到后端给出的 edit_path；失败关闭空白页。
   */
  const handleOrchestrate = async (dashboard: OperationsDashboardCard) => {
    const blocked = dashboardActionBlockReason(dashboard, grafanaAvailable)
    if (blocked) {
      toast.warning('暂时无法编排', { description: blocked })
      return
    }
    const tab = window.open('', '_blank')
    if (!tab) {
      toast.error('浏览器拦截了新标签页', {
        description: '请允许本站打开弹窗后重新点击「编排」。',
      })
      return
    }
    try {
      await ensureSession()
      tab.location.href = dashboard.edit_path
    } catch (error) {
      try {
        tab.close()
      } catch {
        // 空白页可能已被浏览器回收，关闭失败不影响错误反馈。
      }
      toast.error('打开编排失败', {
        description: operationsApiErrorText(error, '看板会话签发失败，请稍后重试。'),
      })
    }
  }

  const openPreview = (dashboard: OperationsDashboardCard) => {
    setPreviewId(dashboard.id)
  }

  const stateSpan = canWrite
    ? 'sm:col-span-1 lg:col-span-2 xl:col-span-3'
    : 'sm:col-span-2 lg:col-span-3 xl:col-span-4'

  return (
    <div className="min-h-full" data-testid="operations-dashboard-workbench">
      <header className="mb-4">
        <h1 className="text-xl font-semibold tracking-tight text-foreground">监控看板</h1>
        <p className="mt-1 max-w-3xl text-sm leading-6 text-muted-foreground">
          以卡片管理看板入口：平台只保存名称、描述、图标与顺序，数据源和面板请在「编排」打开的
          Grafana 原生界面中自行配置。
        </p>
        <p className="mt-1 max-w-3xl text-xs leading-5 text-[var(--color-text-tertiary)]">
          Grafana 使用共享管理员身份；平台角色只约束卡片管理，不构成 Grafana 内的权限隔离。
        </p>
      </header>

      <section
        className="mb-4 flex flex-wrap items-center gap-3 rounded-xl border border-border bg-card px-4 py-3 shadow-[var(--shadow-sm)]"
        aria-label="看板筛选"
      >
        <div className="relative w-full sm:w-72">
          <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-[var(--color-text-tertiary)]" />
          <input
            value={keyword}
            onChange={event => setKeyword(event.target.value)}
            placeholder="搜索看板名称或描述"
            aria-label="按名称或描述筛选看板"
            className="h-9 w-full rounded-lg border border-border bg-card pl-8 pr-8 text-sm text-foreground placeholder:text-[var(--color-text-tertiary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          {keyword && (
            <button
              type="button"
              onClick={() => setKeyword('')}
              className="absolute right-2.5 top-1/2 -translate-y-1/2 text-[var(--color-text-tertiary)] hover:text-muted-foreground"
              aria-label="清除筛选"
            >
              <X size={13} />
            </button>
          )}
        </div>
        {filterActive && canWrite && (
          <span className="hidden text-[11px] text-[var(--color-text-tertiary)] sm:inline">
            筛选状态下不可拖拽排序
          </span>
        )}
        <span className="ml-auto hidden text-xs tabular-nums text-[var(--color-text-tertiary)] sm:inline" aria-live="polite">
          {filterActive ? `共 ${filtered.length} / ${items.length} 个看板` : `共 ${items.length} 个看板`}
        </span>
        {canWrite && (
          <Button size="sm" onClick={() => setCreateOpen(true)} data-testid="dashboard-create">
            <Plus size={14} /> 新建看板
          </Button>
        )}
      </section>

      {!isLoading && !isError && data && !grafanaAvailable && (
        <Alert
          variant="warning"
          role="status"
          data-testid="dashboard-grafana-unavailable"
          className="mb-4"
        >
          Grafana 服务暂不可用，无法预览或编排；卡片信息仍可查看与修改。
        </Alert>
      )}

      {isError && data && (
        <Alert
          variant="warning"
          role="alert"
          data-testid="dashboard-list-stale-error"
          className="mb-4"
        >
          <span>看板列表刷新失败，已保留当前顺序。请重新加载以确认最新状态。</span>
          <Button size="sm" variant="outline" onClick={() => void refetch()}>
            重新加载
          </Button>
        </Alert>
      )}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4" data-testid="dashboard-grid">
        {canWrite && <CreateDashboardCard onCreate={() => setCreateOpen(true)} />}

        {isLoading ? (
          <div className={`flex min-h-[260px] items-center justify-center rounded-2xl border border-border bg-card ${stateSpan}`}>
            <LoadingState message="加载看板列表..." />
          </div>
        ) : isError && !data ? (
          <div
            className={`flex min-h-[260px] flex-col items-center justify-center gap-3 rounded-2xl border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-6 text-center ${stateSpan}`}
            role="alert"
          >
            <p className="text-sm text-[var(--color-danger)]">看板列表加载失败，请检查网络连接后重试。</p>
            <Button size="sm" variant="outline" onClick={() => void refetch()}>重新加载</Button>
          </div>
        ) : filtered.length === 0 ? (
          <div
            data-testid="dashboard-list-empty"
            className={`flex min-h-[260px] flex-col items-center justify-center rounded-2xl border border-dashed border-border bg-card px-6 text-center ${stateSpan}`}
          >
            <p className="text-sm font-medium text-muted-foreground">
              {filterActive ? '没有符合条件的看板' : '还没有监控看板'}
            </p>
            <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">
              {filterActive
                ? '请调整筛选条件'
                : '创建第一个看板卡片，再在 Grafana 中配置运行态势面板'}
            </p>
            {!filterActive && canWrite && (
              <Button size="sm" className="mt-3" onClick={() => setCreateOpen(true)}>
                <Plus size={13} /> 新建看板
              </Button>
            )}
          </div>
        ) : (
          filtered.map(dashboard => (
            <DashboardCardView
              key={dashboard.id}
              dashboard={dashboard}
              canWrite={canWrite}
              actionBlockReason={dashboardActionBlockReason(dashboard, grafanaAvailable)}
              draggable={dragEnabled}
              dragging={draggingId === dashboard.id}
              dropPlace={dragEnabled && draggingId && draggingId !== dashboard.id && dropTarget?.id === dashboard.id
                ? (dropTarget.before ? 'before' : 'after')
                : null}
              onEdit={() => setEditId(dashboard.id)}
              onOrchestrate={() => void handleOrchestrate(dashboard)}
              onPreview={() => openPreview(dashboard)}
              onDelete={() => setDeleteTarget(dashboard)}
              onDragStart={setDraggingId}
              onDragOver={(id, before) => setDropTarget(draggingId && draggingId !== id ? { id, before } : null)}
              onDragLeave={id => setDropTarget(current => (current?.id === id ? null : current))}
              onDrop={handleCardDrop}
              onDragEnd={() => { setDraggingId(null); setDropTarget(null) }}
            />
          ))
        )}
      </div>

      {createOpen && (
        <DashboardFormDialog
          key="create"
          mode="create"
          open
          onClose={() => setCreateOpen(false)}
          onSubmit={payload => createMutation.mutateAsync(payload)}
        />
      )}

      {editTarget && (
        <DashboardFormDialog
          key={editTarget.id}
          mode="edit"
          open
          dashboard={editTarget}
          onClose={() => setEditId(null)}
          onSubmit={payload => updateMutation.mutateAsync({ id: editTarget.id, payload })}
          onRefresh={() => refetch().then(() => undefined)}
        />
      )}

      <ConfirmDialog
        open={!!deleteTarget}
        onClose={() => { if (!deleteMutation.isPending) setDeleteTarget(null) }}
        onConfirm={() => deleteTarget && deleteMutation.mutate(deleteTarget.id)}
        title={deleteTarget ? `删除「${deleteTarget.name}」？` : '删除看板？'}
        description="该看板在 Grafana 中的全部面板与配置将被永久删除，此操作无法撤销。"
        confirmText="删除看板"
        variant="danger"
        loading={deleteMutation.isPending}
      />

      {previewTarget && (
        <DashboardPreviewDialog
          key={previewTarget.id}
          dashboard={previewTarget}
          ensureSession={ensureSession}
          onClose={() => setPreviewId(null)}
        />
      )}
    </div>
  )
}
