/**
 * 三维场景管理页 — 逐段对照本体管理页（OntologyListPage）的卡片式管理体验：
 * 筛选条 / 四列卡片网格 / 新建卡 / 统计瓦片 / 底部操作行。
 * 草稿态负责场景生成，发布态对外生效；支持新建/编辑/快照克隆/删除。
 */
import { formatDateTime } from '@/utils/datetime'
import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useNavigate } from 'react-router-dom'
import { Boxes, Copy, Pencil, Plus, Search, Sparkles, Trash2, X } from 'lucide-react'
import { scenesApi } from '@/api/scenes'
import type { SceneSummary } from '@/types/scene'
import { LoadingState } from '@/components/ui/LoadingState'
import { ConfirmModal } from '@/components/ui/Modal'
import { toast } from 'sonner'
import { SceneFormModal } from '../components/SceneFormModal'

function formatChangedAt(value: string | null) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return formatDateTime(date)
}

function CreateSceneCard({ onCreate }: {
  onCreate: () => void
}) {
  return (
    <article className="group flex min-h-[256px] flex-col items-center justify-center overflow-hidden rounded-2xl border border-dashed border-brand-line bg-gradient-to-br from-brand-soft via-white to-viz-cyan-soft px-4 py-6 text-center transition-all hover:-translate-y-0.5 hover:border-brand hover:shadow-lg">
      <div className="mb-4 flex h-14 w-14 items-center justify-center rounded-full bg-brand text-[var(--color-text-inverse)] shadow-md transition-transform group-hover:scale-105">
        <Plus size={25} />
      </div>
      <h3 className="text-base font-semibold text-foreground">新建场景</h3>
      {/* 提示语保持单行：窄栏下负外边距吃掉左右内边距，字号降至 11px 保证不折行 */}
      <p className="-mx-4 mt-2 whitespace-nowrap text-[11px] leading-5 text-muted-foreground min-[1500px]:text-xs">快速创建草稿态场景，或通过场景助手对话生成</p>
      <div className="mt-5 flex items-center justify-center">
        <button
          type="button"
          onClick={onCreate}
          className="rounded-lg border border-brand-line bg-card px-3 py-1.5 text-xs font-medium text-brand-ink shadow-sm transition-colors hover:border-brand-line hover:bg-brand-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        >
          立即创建
        </button>
      </div>
    </article>
  )
}

function SceneCard({ scene, onEdit, onDetail, onClone, onDelete }: {
  scene: SceneSummary
  onEdit: () => void
  onDetail: () => void
  onClone: () => void
  onDelete: () => void
}) {
  const published = scene.status === 'published'
  return (
    <article className="group flex min-h-[256px] flex-col overflow-hidden rounded-2xl border border-border bg-card transition-all duration-200 hover:-translate-y-0.5 hover:border-brand-line hover:shadow-lg">
      <div className="flex flex-col p-4 pb-2.5">
        <div className="flex min-h-11 items-start gap-3 overflow-hidden">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-brand to-viz-cyan text-[var(--color-text-inverse)] shadow-sm">
            <Boxes size={20} />
          </span>
          <div className="flex min-h-11 min-w-0 flex-1 flex-col justify-center overflow-hidden">
            <div className="flex min-w-0 items-center overflow-hidden">
              <button
                type="button"
                onClick={onDetail}
                className="min-w-0 flex-1 truncate text-left text-[15px] font-semibold leading-5 text-foreground transition-colors hover:text-brand-ink"
                title={scene.name}
              >
                {scene.name}
              </button>
            </div>
            <div className="mt-1 flex min-w-0 items-center gap-1.5">
              <span className={
                'inline-flex min-w-0 max-w-full truncate rounded-md border px-2 py-0.5 text-[11px] font-medium leading-4 ' +
                (published
                  ? 'border-brand-line bg-brand-soft text-brand-ink'
                  : 'border-border bg-muted text-muted-foreground')
              }>
                {published ? '已发布' : '草稿'}
              </span>
              {scene.published_version_no != null ? (
                <span className="inline-flex shrink-0 rounded-md border border-viz-violet-soft bg-viz-violet-soft px-2 py-0.5 font-mono text-[11px] font-medium leading-4 text-viz-violet">
                  v{scene.published_version_no}
                </span>
              ) : (
                <span className="inline-flex shrink-0 rounded-md border border-border bg-muted px-2 py-0.5 text-[11px] font-medium leading-4 text-muted-foreground">
                  未发布
                </span>
              )}
            </div>
          </div>
        </div>

        <p
          className="mt-4 min-h-[44px] text-sm leading-[22px] text-muted-foreground"
          style={{ display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}
          title={scene.description || '暂无描述'}
        >
          {scene.description || '暂无描述'}
        </p>

        <div className="mt-3 grid grid-cols-4 gap-1.5">
          {[
            { label: '版本总数', value: scene.version_count ?? 0 },
            { label: '最新草稿', value: scene.current_version_no > 0 ? 'v' + scene.current_version_no : '—' },
            // 发布态只有唯一生效版本（重复发布覆盖指针），展示"哪个版本在生效"而非发布次数
            { label: '生效版本', value: scene.published_version_no != null ? 'v' + scene.published_version_no : '—' },
            { label: '运行日志', value: scene.runtime_log_count ?? 0 },
          ].map(metric => (
            <div key={metric.label} className="min-w-0 rounded-xl bg-muted px-0.5 py-2.5 text-center">
              <p className="whitespace-nowrap text-[11px] font-medium text-[var(--color-text-tertiary)]">{metric.label}</p>
              <p className="mt-0.5 text-lg font-semibold tabular-nums text-foreground">{metric.value}</p>
            </div>
          ))}
        </div>
      </div>

      <footer className="mt-auto flex min-h-11 items-center gap-0.5 border-t border-border px-4 py-1.5">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onEdit}
            className="inline-flex shrink-0 items-center gap-0.5 whitespace-nowrap rounded-lg bg-muted px-1.5 py-1.5 text-[11px] font-medium text-foreground transition-colors hover:bg-brand-soft hover:text-brand-ink"
          >
            <Pencil size={12} /> 编辑
          </button>
          <button
            type="button"
            onClick={onClone}
            className="inline-flex shrink-0 items-center gap-0.5 whitespace-nowrap rounded-lg bg-muted px-1.5 py-1.5 text-[11px] font-medium text-foreground transition-colors hover:bg-brand-soft hover:text-brand-ink"
          >
            <Copy size={12} /> 克隆
          </button>
        </div>
        <div className="ml-auto flex shrink-0 items-center gap-0.5">
          <span className="hidden shrink-0 whitespace-nowrap text-[11px] tabular-nums text-[var(--color-text-tertiary)] min-[1400px]:inline" title={'最近更新：' + formatDateTime(scene.updated_at)}>
            {formatChangedAt(scene.updated_at)}
          </span>
          <button
            type="button"
            onClick={onDelete}
            className="shrink-0 rounded-lg p-1.5 text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)]"
            title="删除场景"
            aria-label={'删除场景 ' + scene.name}
          >
            <Trash2 size={14} />
          </button>
        </div>
      </footer>
    </article>
  )
}

export default function SceneListPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [nameFilter, setNameFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [createOpen, setCreateOpen] = useState(false)
  const [editTarget, setEditTarget] = useState<SceneSummary | null>(null)
  const [cloneTarget, setCloneTarget] = useState<SceneSummary | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<SceneSummary | null>(null)

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['scenes'],
    queryFn: () => scenesApi.list({ page_size: 200 }),
  })

  const allItems = useMemo(() => data?.items ?? [], [data?.items])
  const filteredItems = useMemo(() => {
    const keyword = nameFilter.trim().toLocaleLowerCase('zh-CN')
    return [...allItems]
      .filter(item => !keyword || (item.name + ' ' + (item.description ?? '')).toLocaleLowerCase('zh-CN').includes(keyword))
      .filter(item => !statusFilter || item.status === statusFilter)
      .sort((a, b) => new Date(b.updated_at ?? 0).getTime() - new Date(a.updated_at ?? 0).getTime())
  }, [allItems, nameFilter, statusFilter])

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['scenes'] })

  const createMutation = useMutation({
    mutationFn: (value: { name: string; description: string }) => scenesApi.create(value),
    onSuccess: () => {
      refresh()
      setCreateOpen(false)
      toast.success('场景已创建', { description: '当前为草稿态，可在详情页或场景助手中继续完善。' })
    },
    onError: error => toast.error('创建失败', { description: apiErrorText(error) }),
  })
  const updateMutation = useMutation({
    mutationFn: ({ id, value }: { id: string; value: { name: string; description: string } }) =>
      scenesApi.updateBasicInfo(id, value),
    onSuccess: () => {
      refresh()
      setEditTarget(null)
      toast.success('场景信息已更新')
    },
    onError: error => toast.error('更新失败', { description: apiErrorText(error) }),
  })
  const cloneMutation = useMutation({
    mutationFn: (id: string) => scenesApi.clone(id),
    onSuccess: created => {
      refresh()
      setCloneTarget(null)
      toast.success('已克隆为新草稿场景', { description: '版本历史从 v1 开始。' })
      navigate('/scenes/' + created.id)
    },
    onError: error => { setCloneTarget(null); toast.error('克隆失败', { description: apiErrorText(error) }) },
  })
  const deleteMutation = useMutation({
    mutationFn: (id: string) => scenesApi.remove(id),
    onSuccess: () => {
      refresh()
      setDeleteTarget(null)
      toast.success('场景已删除', { description: '相关版本定义与运行日志已一并移除。' })
    },
    onError: error => toast.error('删除失败', { description: apiErrorText(error) }),
  })

  return (
    <div className="min-h-full">
      <section className="mb-4 flex flex-wrap items-center gap-3 rounded-xl border border-border bg-card px-4 py-3 shadow-sm/50" aria-label="场景筛选">
        <div className="relative w-full sm:w-72">
          <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-[var(--color-text-tertiary)]" />
          <input
            value={nameFilter}
            onChange={event => setNameFilter(event.target.value)}
            placeholder="搜索场景名称或描述"
            aria-label="按场景名称或描述筛选"
            className="h-9 w-full rounded-lg border border-border bg-card pl-8 pr-8 text-sm text-foreground placeholder:text-[var(--color-text-tertiary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          {nameFilter && (
            <button
              type="button"
              onClick={() => setNameFilter('')}
              className="absolute right-2.5 top-1/2 -translate-y-1/2 text-[var(--color-text-tertiary)] hover:text-muted-foreground"
              aria-label="清除名称筛选"
            >
              <X size={13} />
            </button>
          )}
        </div>
        <Select
          value={statusFilter || '__all__'}
          onValueChange={value => setStatusFilter(value === '__all__' ? '' : value)}
        >
          <SelectTrigger className="h-9 min-w-36 w-fit rounded-lg bg-card px-3 text-sm" aria-label="按状态筛选">
            <SelectValue placeholder="全部状态" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">全部状态</SelectItem>
            <SelectItem value="draft">草稿</SelectItem>
            <SelectItem value="published">已发布</SelectItem>
          </SelectContent>
        </Select>
        {(nameFilter || statusFilter) && (
          <button
            type="button"
            onClick={() => { setNameFilter(''); setStatusFilter('') }}
            className="inline-flex h-9 items-center gap-1 rounded-lg px-2.5 text-xs text-[var(--color-text-tertiary)] hover:bg-muted hover:text-muted-foreground"
          >
            <X size={13} /> 清除筛选
          </button>
        )}
        <span className="ml-auto hidden text-xs tabular-nums text-[var(--color-text-tertiary)] sm:inline" aria-live="polite">
          {nameFilter || statusFilter
            ? '共 ' + filteredItems.length + ' / ' + allItems.length + ' 个场景'
            : '共 ' + allItems.length + ' 个场景'}
        </span>
        <button
          type="button"
          onClick={() => navigate('/scenes/modeling')}
          className="inline-flex h-9 items-center gap-1.5 rounded-lg border border-brand-line bg-card px-4 text-sm font-medium text-brand-ink shadow-sm transition-all duration-200 hover:-translate-y-0.5 hover:border-brand-line hover:bg-brand-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        >
          <Sparkles size={15} /> 场景助手
        </button>
        <button
          type="button"
          onClick={() => setCreateOpen(true)}
          className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-[var(--color-nav-bg)] px-4 text-sm font-medium text-[var(--color-text-inverse)] shadow-sm transition-all duration-200 hover:-translate-y-0.5 hover:opacity-90 active:translate-y-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        >
          <Plus size={15} /> 新建场景
        </button>
      </section>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        <CreateSceneCard onCreate={() => setCreateOpen(true)} />

        {isLoading ? (
          <div className="flex min-h-[300px] items-center justify-center rounded-2xl border border-border bg-card sm:col-span-1 lg:col-span-2 xl:col-span-3">
            <LoadingState message="加载场景列表..." />
          </div>
        ) : isError ? (
          <div className="flex min-h-[300px] flex-col items-center justify-center gap-3 rounded-2xl border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-6 text-center sm:col-span-1 lg:col-span-2 xl:col-span-3" role="alert">
            <p className="text-sm text-[var(--color-danger)]">场景列表加载失败，请检查网络连接后重试。</p>
            <button
              type="button"
              onClick={() => void refetch()}
              className="rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-card px-3 py-1.5 text-xs font-medium text-[var(--color-danger)] transition-colors hover:bg-[var(--color-danger-bg)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              重新加载
            </button>
          </div>
        ) : filteredItems.length === 0 ? (
          <div className="flex min-h-[300px] flex-col items-center justify-center rounded-2xl border border-border bg-card px-6 text-center sm:col-span-1 lg:col-span-2 xl:col-span-3">
            <Boxes size={28} className="text-[var(--color-text-tertiary)]" />
            <p className="mt-3 text-sm font-medium text-muted-foreground">{nameFilter || statusFilter ? '没有符合条件的场景' : '还没有创建场景'}</p>
            <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">{nameFilter || statusFilter ? '请调整名称或状态筛选条件' : '点击左侧卡片创建第一个场景，或通过场景助手对话生成'}</p>
          </div>
        ) : (
          filteredItems.map(scene => (
            <SceneCard
              key={scene.id}
              scene={scene}
              onEdit={() => setEditTarget(scene)}
              onDetail={() => navigate('/scenes/' + scene.id)}
              onClone={() => setCloneTarget(scene)}
              onDelete={() => setDeleteTarget(scene)}
            />
          ))
        )}
      </div>

      {createOpen && (
        <SceneFormModal
          open
          title="新建场景"
          onClose={() => setCreateOpen(false)}
          onSubmit={value => createMutation.mutateAsync(value)}
        />
      )}

      {editTarget && (
        <SceneFormModal
          open
          title="编辑场景信息"
          initial={{ name: editTarget.name, description: editTarget.description }}
          onClose={() => setEditTarget(null)}
          onSubmit={value => updateMutation.mutateAsync({ id: editTarget.id, value })}
        />
      )}

      <ConfirmModal
        open={!!cloneTarget}
        onClose={() => { if (!cloneMutation.isPending) setCloneTarget(null) }}
        onConfirm={() => cloneTarget && cloneMutation.mutate(cloneTarget.id)}
        title={cloneTarget ? '克隆「' + cloneTarget.name + '」？' : '克隆场景？'}
        description="将克隆当前生效定义生成一个全新的草稿态场景（名称自动加“-副本”），版本历史从 v1 开始。"
        confirmText="确认克隆"
        loading={cloneMutation.isPending}
      />

      <ConfirmModal
        open={!!deleteTarget}
        onClose={() => { if (!deleteMutation.isPending) setDeleteTarget(null) }}
        onConfirm={() => deleteTarget && deleteMutation.mutate(deleteTarget.id)}
        title={deleteTarget ? '删除「' + deleteTarget.name + '」？' : '删除场景？'}
        description="场景的全部版本定义与运行日志将被永久移除。此操作无法撤销，请确认你不再需要这些内容。"
        confirmText="删除场景"
        variant="danger"
        loading={deleteMutation.isPending}
      />
    </div>
  )
}

function apiErrorText(error: unknown): string {
  if (!error || typeof error !== 'object') return '请稍后重试'
  const detail = (error as { detail?: unknown }).detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object') {
    const message = (detail as { message?: unknown }).message
    if (typeof message === 'string') return message
  }
  return '请稍后重试'
}
