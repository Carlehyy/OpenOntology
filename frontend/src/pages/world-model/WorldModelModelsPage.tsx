import { formatDateTime } from '@/utils/datetime'
import { useReducedMotion } from 'motion/react'
import { useEffect, useMemo, useState } from 'react'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import {
  AlertTriangle,
  Boxes,
  ChevronLeft,
  ChevronRight,
  Code2,
  Orbit,
  Pencil,
  Plus,
  Rocket,
  Search,
  Trash2,
  X,
} from 'lucide-react'
import {
  apiError,
  worldModelApi,
  type EngineType,
  type WorldModelProjectSummary,
} from '@/api/worldModel'
import { LoadingState } from '@/components/ui/LoadingState'
import { ConfirmModal, Modal } from '@/components/ui/Modal'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { toast } from 'sonner'
import { useDebouncedValue } from '@/utils/useDebouncedValue'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Tooltip } from '@/components/motion-ui/tooltip'
import { IconButton } from '@/components/motion-ui/icon-button'
import { TiltCard } from '@/components/motion-ui/tilt-card'
import {
  CenterMorphModal,
  CenterMorphModalContent,
} from '@/components/motion-ui/center-morph-modal'

/** 列表走服务端分页：每页卡片数（含新建卡占位的网格为 4 列） */
const PAGE_SIZE = 12

export const ENGINE_TYPE_OPTIONS: { value: EngineType; label: string; hint: string }[] = [
  { value: 'statistical', label: '统计预测', hint: '基于历史数据的统计/机器学习方法' },
  { value: 'mechanistic', label: '机理仿真', hint: '基于物理定律或业务机理的仿真' },
  { value: 'state_machine', label: '状态机推演', hint: '基于规则与离散状态转移' },
  { value: 'learned', label: '学习型动力学', hint: '从交互数据学习状态转移规律' },
]

export function engineTypeLabel(value: string): string {
  return ENGINE_TYPE_OPTIONS.find(item => item.value === value)?.label ?? value
}

function formatChangedAt(value: string | null) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return formatDateTime(date)
}

interface ProjectFormValue {
  name: string
  description: string
  engine_type: EngineType
}

function ProjectFormModal({
  open,
  title,
  submitText,
  initial,
  onClose,
  onSubmit,
}: {
  open: boolean
  title: string
  submitText: string
  initial?: WorldModelProjectSummary | null
  onClose: () => void
  onSubmit: (value: ProjectFormValue) => Promise<unknown>
}) {
  const [name, setName] = useState(initial?.name ?? '')
  const [description, setDescription] = useState(initial?.description ?? '')
  const [engineType, setEngineType] = useState<EngineType>(initial?.engine_type ?? 'statistical')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const submit = async () => {
    if (!name.trim()) {
      setError('请输入模型名称')
      return
    }
    setSaving(true)
    setError('')
    try {
      await onSubmit({ name: name.trim(), description: description.trim(), engine_type: engineType })
    } catch (submitError) {
      setError(apiError(submitError))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={() => !saving && onClose()}
      title={title}
      description={initial
        ? '更新模型的名称、引擎类型与说明，不会影响已保存的脚本和版本。'
        : '创建后即可进入开发页编写推演脚本，脚本需实现 simulate(context, actions, horizon) 入口函数。'}
      headerIcon={initial
        ? <Pencil size={18} className="text-brand-ink" />
        : <Plus size={19} className="text-brand-ink" />}
      size="lg"
      footer={(
        <>
          <Button variant="ghost" onClick={onClose} disabled={saving}>取消</Button>
          <Button
            onClick={submit}
            loading={saving}
            disabled={!name.trim()}
            className="bg-brand text-white hover:bg-brand-deep active:bg-brand-deep disabled:bg-muted disabled:text-muted-foreground disabled:shadow-none"
          >
            {submitText}
          </Button>
        </>
      )}
    >
      <div className="space-y-5">
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Input
            label="模型名称"
            required
            autoFocus
            maxLength={200}
            value={name}
            onChange={event => setName(event.target.value)}
            placeholder="例如：台区负荷短期推演"
            className=" focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-ring"
          />
          <div>
            <label className="mb-1.5 block text-sm font-medium text-[var(--color-text-primary)]">
              引擎类型<span className="ml-0.5 text-[var(--color-danger)]">*</span>
            </label>
            <Select
              value={engineType}
              onValueChange={value => setEngineType(value as EngineType)}
            >
              <SelectTrigger aria-label="引擎类型" className="h-9 rounded-lg">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {ENGINE_TYPE_OPTIONS.map(item => (
                  <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="mt-1 text-[11px] text-[var(--color-text-tertiary)]">
              {ENGINE_TYPE_OPTIONS.find(item => item.value === engineType)?.hint}
            </p>
          </div>
        </div>

        <div>
          <label className="mb-1.5 block text-sm font-medium text-[var(--color-text-primary)]">模型描述</label>
          <textarea
            value={description}
            onChange={event => setDescription(event.target.value)}
            maxLength={500}
            rows={3}
            placeholder="简要说明该模型推演的业务对象、时域与用途"
            className="w-full resize-none rounded-md border border-[var(--color-border)] bg-card px-3 py-2 text-sm text-[var(--color-text-primary)] placeholder:text-[var(--color-text-tertiary)]  focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          <p className="mt-1 text-right text-[11px] text-[var(--color-text-tertiary)]">{description.length}/500</p>
        </div>

        {error && <p role="alert" className="text-sm text-[var(--color-danger)]">{error}</p>}
      </div>
    </Modal>
  )
}

function CreateProjectCard({ onCreate }: { onCreate: () => void }) {
  return (
    <button
      type="button"
      onClick={onCreate}
      className="group flex min-h-[190px] flex-col items-center justify-center gap-3 rounded-2xl border-2 border-dashed border-border bg-card/60 px-6 text-center transition-all hover:border-brand hover:bg-brand-soft/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <span className="flex h-11 w-11 items-center justify-center rounded-xl bg-brand-soft text-brand-ink transition-colors group-hover:bg-brand-mist">
        <Plus size={22} />
      </span>
      <span className="text-sm font-medium text-muted-foreground group-hover:text-brand-ink">新建推演模型</span>
      <span className="text-xs text-muted-foreground">以代码承载演化规律，调试通过后可保存版本</span>
    </button>
  )
}

function ProjectCard({
  item,
  onDevelop,
  onEdit,
  onDelete,
  onOpenService,
}: {
  item: WorldModelProjectSummary
  onDevelop: () => void
  onEdit: () => void
  onDelete: () => void
  onOpenService: () => void
}) {
  const reduce = useReducedMotion() ?? false
  return (
    <TiltCard className="h-full" max={8} glare={false}>
      <article className="flex h-full min-h-[190px] flex-col rounded-2xl border border-border bg-card p-5 shadow-sm/50 transition-shadow hover:shadow-md">
      <header className="flex items-start gap-3">
        <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-brand-soft text-brand-ink">
          <Orbit size={20} />
        </span>
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-sm font-semibold text-[var(--color-text-primary)]" title={item.name}>
            {item.name}
          </h3>
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            <span className="inline-flex items-center rounded-md bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground">
              {engineTypeLabel(item.engine_type)}
            </span>
            <span className={`inline-flex items-center rounded-md px-1.5 py-0.5 text-[11px] ${
              item.service_status === 'online'
                ? 'bg-brand-soft text-brand-ink'
                : item.service_status === 'offline'
                  ? 'bg-muted text-muted-foreground'
                  : 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]'
            }`}>
              {item.service_status === 'online' ? '在线' : item.service_status === 'offline' ? '已下线' : '草稿'}
              {item.service_count != null && item.service_count > 1 ? ` ×${item.service_count}` : ''}
            </span>
            {item.service_name && (
              <Tooltip
                content={`推演服务${item.service_status === 'online' ? '（在线）' : '（已下线）'}${item.service_count != null && item.service_count > 1 ? ` · 共 ${item.service_count} 个服务（多本体发布），状态为最近发布者` : ''} · 点击进入「推演服务」页管理`}
              >
                <button
                  type="button"
                  onClick={onOpenService}
                  className="inline-flex max-w-[11rem] items-center gap-1 rounded-md border border-brand-mist bg-brand-soft px-1.5 py-0.5 text-[11px] text-brand-ink transition-colors hover:bg-brand-mist focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <Rocket size={11} className="shrink-0" />
                  <span className="truncate">
                    {item.service_name}{item.service_version_no != null ? ` · v${item.service_version_no}` : ''}
                  </span>
                </button>
              </Tooltip>
            )}
          </div>
        </div>
      </header>

      <p className="mt-3 line-clamp-2 min-h-[2.5rem] text-xs leading-5 text-[var(--color-text-tertiary)]" title={item.description}>
        {item.description || '暂无描述'}
      </p>

      <footer className="mt-auto flex items-center justify-between pt-3 text-[11px] text-muted-foreground">
        <span>{item.version_count} 个版本 · 更新于 {formatChangedAt(item.updated_at)}</span>
        <span className="flex items-center gap-1">
          <button
            type="button"
            onClick={onDevelop}
            className="inline-flex h-7 shrink-0 items-center gap-1 whitespace-nowrap rounded-md bg-brand px-2.5 text-xs font-medium text-white transition-colors hover:bg-brand-deep focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Code2 size={13} /> 开发
          </button>
          <Tooltip content="编辑模型信息">
            <IconButton label={`编辑 ${item.name}`} reduce={reduce} onClick={onEdit} className="h-7 w-7">
              <Pencil size={13} />
            </IconButton>
          </Tooltip>
          <Tooltip content="删除模型">
            <IconButton
              label={`删除 ${item.name}`}
              reduce={reduce}
              onClick={onDelete}
              className="h-7 w-7 hover:bg-[var(--color-danger-bg)] hover:text-destructive focus-visible:ring-ring"
            >
              <Trash2 size={13} />
            </IconButton>
          </Tooltip>
        </span>
      </footer>
    </article>
    </TiltCard>
  )
}

export default function WorldModelModelsPage() {

    const [nameFilter, setNameFilter] = useState('')
  const [engineFilter, setEngineFilter] = useState('')
  const [page, setPage] = useState(1)
  const [createOpen, setCreateOpen] = useState(false)
  const [editTarget, setEditTarget] = useState<WorldModelProjectSummary | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<WorldModelProjectSummary | null>(null)
  const queryClient = useQueryClient()
  const navigate = useNavigate()

  // 输入防抖后再发服务端筛选请求，避免每次击键都查询
  const keyword = useDebouncedValue(nameFilter.trim(), 300)

  // 筛选条件变化回到第一页
  useEffect(() => { setPage(1) }, [keyword, engineFilter])

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['world-model-projects', keyword, engineFilter, page],
    queryFn: () => worldModelApi.listProjects({
      keyword: keyword || undefined,
      engine_type: engineFilter || undefined,
      page,
      size: PAGE_SIZE,
    }),
    // 翻页/筛选时保留上一页数据，避免网格闪烁
    placeholderData: keepPreviousData,
  })

  const items = useMemo(() => data?.items ?? [], [data?.items])
  const total = data?.total ?? 0
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  // 删除末页最后一条等场景导致当前页变空时，回退一页
  useEffect(() => {
    if (page > 1 && data && data.items.length === 0) setPage(page - 1)
  }, [data, page])

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['world-model-projects'] })

  const createMutation = useMutation({
    mutationFn: (value: ProjectFormValue) => worldModelApi.createProject(value),
    onSuccess: project => {
      refresh()
      setCreateOpen(false)
      toast.success('推演模型已创建', { description: '即将进入开发页编写推演脚本。' })
      navigate(`/world-model/models/${project.id}/develop`)
    },
  })
  const updateMutation = useMutation({
    mutationFn: ({ id, value }: { id: string; value: ProjectFormValue }) => worldModelApi.updateProject(id, value),
    onSuccess: () => {
      refresh()
      setEditTarget(null)
      toast.success('模型信息已更新')
    },
  })
  const deleteMutation = useMutation({
    mutationFn: (id: string) => worldModelApi.deleteProject(id),
    onSuccess: () => {
      refresh()
      setDeleteTarget(null)
      toast.success('推演模型已删除', { description: '相关脚本与版本记录已一并移除。' })
    },
    onError: error => {
      toast.error('删除失败', { description: apiError(error) })
    },
  })

  return (
    <div>
      <section className="mb-4 flex flex-wrap items-center gap-3 rounded-xl border border-border bg-card px-4 py-3 shadow-sm/50" aria-label="推演模型筛选">
        <div className="relative w-full sm:w-72">
          <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <input
            value={nameFilter}
            onChange={event => setNameFilter(event.target.value)}
            placeholder="搜索模型名称或描述"
            aria-label="按模型名称或描述筛选"
            className="h-9 w-full rounded-lg border border-border bg-card pl-8 pr-8 text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          {nameFilter && (
            <button
              type="button"
              onClick={() => setNameFilter('')}
              className="absolute right-2.5 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
              aria-label="清除名称筛选"
            >
              <X size={13} />
            </button>
          )}
        </div>
        <Select
          value={engineFilter || '__all__'}
          onValueChange={value => setEngineFilter(value === '__all__' ? '' : value)}
        >
          <SelectTrigger aria-label="按引擎类型筛选" className="h-9 w-fit min-w-36 rounded-lg">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">全部引擎类型</SelectItem>
            {ENGINE_TYPE_OPTIONS.map(item => (
              <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        {(nameFilter || engineFilter) && (
          <button
            type="button"
            onClick={() => { setNameFilter(''); setEngineFilter('') }}
            className="inline-flex h-9 items-center gap-1 rounded-lg px-2.5 text-xs text-muted-foreground hover:bg-muted hover:text-muted-foreground"
          >
            <X size={13} /> 清除筛选
          </button>
        )}
        <span className="ml-auto hidden text-xs tabular-nums text-muted-foreground sm:inline" aria-live="polite">
          {keyword || engineFilter ? `符合条件 ${total} 个模型` : `共 ${total} 个模型`}
        </span>
        <button
          type="button"
          onClick={() => setCreateOpen(true)}
          className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-[var(--color-nav-bg)] px-4 text-sm font-medium text-white shadow-sm transition-all duration-200 hover:-translate-y-0.5 hover:opacity-90 active:translate-y-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        >
          <Plus size={15} /> 立即创建
        </button>
      </section>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        <CreateProjectCard onCreate={() => setCreateOpen(true)} />

        {isLoading ? (
          <div className="flex min-h-[300px] items-center justify-center rounded-2xl border border-border bg-card sm:col-span-1 lg:col-span-2 xl:col-span-3">
            <LoadingState message="加载推演模型列表..." />
          </div>
        ) : isError ? (
          <div className="flex min-h-[300px] flex-col items-center justify-center gap-3 rounded-2xl border border-[var(--color-danger-bg)] bg-[var(--color-danger-bg)] px-6 text-center sm:col-span-1 lg:col-span-2 xl:col-span-3" role="alert">
            <p className="text-sm text-destructive">推演模型列表加载失败，请检查网络连接后重试。</p>
            <button
              type="button"
              onClick={() => void refetch()}
              className="rounded-lg border border-[var(--color-danger-bg)] bg-card px-3 py-1.5 text-xs font-medium text-destructive transition-colors hover:bg-[var(--color-danger-bg)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              重新加载
            </button>
          </div>
        ) : items.length === 0 ? (
          keyword || engineFilter ? (
            <div className="flex min-h-[300px] flex-col items-center justify-center rounded-2xl border border-border bg-card px-6 text-center sm:col-span-1 lg:col-span-2 xl:col-span-3">
              <div>
                <Boxes size={28} className="text-muted-foreground" />
              </div>
              <p className="mt-3 text-sm font-medium text-muted-foreground">没有符合条件的推演模型</p>
              <p className="mt-1 text-xs text-muted-foreground">请调整名称或引擎类型筛选条件</p>
            </div>
          ) : (
            /* 无数据的空态：用三步引导替代一句话提示，消除大段空白 */
            <div className="flex min-h-[300px] flex-col items-center justify-center gap-7 rounded-2xl border border-border bg-card px-8 py-10 sm:col-span-1 lg:col-span-2 xl:col-span-3">
              <div className="text-center">
                <p className="text-sm font-semibold text-muted-foreground">从第一个推演模型开始</p>
                <p className="mt-1 text-xs text-muted-foreground">以代码承载演化规律，三步即可对外提供在线推演服务</p>
              </div>
              <ol className="grid w-full max-w-3xl grid-cols-1 gap-3 sm:grid-cols-3" aria-label="创建流程指引">
                {[
                  { icon: <Plus size={16} />, title: '创建模型', desc: '选择引擎类型，定义推演的业务对象与时域' },
                  { icon: <Code2 size={16} />, title: '开发调试', desc: '在开发页编写 simulate 脚本，用测试入参试跑验证' },
                  { icon: <Rocket size={16} />, title: '保存发布', desc: '保存即冻结版本，一键发布为在线推演服务' },
                ].map((step, index) => (
                  <li key={step.title} className="relative flex flex-col items-center gap-2 rounded-xl border border-border bg-muted px-4 pb-4 pt-5 text-center">
                    <span className="absolute left-3 top-3 flex h-5 w-5 items-center justify-center rounded-full bg-brand/10 text-[11px] font-semibold text-brand-ink">
                      {index + 1}
                    </span>
                    <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-brand-soft text-brand-ink">
                      {step.icon}
                    </span>
                    <p className="text-sm font-medium text-foreground">{step.title}</p>
                    <p className="text-[11px] leading-5 text-muted-foreground">{step.desc}</p>
                  </li>
                ))}
              </ol>
              <button
                type="button"
                onClick={() => setCreateOpen(true)}
                className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-brand px-5 text-sm font-medium text-white shadow-sm transition-all duration-200 hover:-translate-y-0.5 hover:bg-brand-deep active:translate-y-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
              >
                <Plus size={15} /> 立即创建第一个模型
              </button>
            </div>
          )
        ) : (
          items.map((item, _index) => (
            <div
              key={item.id}
            >
              <ProjectCard
                item={item}
                onDevelop={() => navigate(`/world-model/models/${item.id}/develop`)}
                onEdit={() => setEditTarget(item)}
                onDelete={() => setDeleteTarget(item)}
                onOpenService={() => navigate('/world-model/services')}
              />
            </div>
          ))
        )}
      </div>

      {!isLoading && !isError && totalPages > 1 && (
        <div className="mt-4 flex items-center justify-between rounded-xl border border-border bg-card px-4 py-2.5 text-xs text-muted-foreground shadow-sm/50">
          <span>共 {total} 条</span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setPage(current => Math.max(1, current - 1))}
              disabled={page <= 1}
              aria-label="上一页"
              className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-border disabled:opacity-40"
            >
              <ChevronLeft size={14} />
            </button>
            <span className="tabular-nums">{page} / {totalPages}</span>
            <button
              type="button"
              onClick={() => setPage(current => Math.min(totalPages, current + 1))}
              disabled={page >= totalPages}
              aria-label="下一页"
              className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-border disabled:opacity-40"
            >
              <ChevronRight size={14} />
            </button>
          </div>
        </div>
      )}

      {createOpen && (
        <ProjectFormModal
          open
          title="新建推演模型"
          submitText="创建模型"
          onClose={() => setCreateOpen(false)}
          onSubmit={value => createMutation.mutateAsync(value)}
        />
      )}

      {editTarget && (
        <ProjectFormModal
          open
          title="编辑推演模型"
          submitText="保存修改"
          initial={editTarget}
          onClose={() => setEditTarget(null)}
          onSubmit={value => updateMutation.mutateAsync({ id: editTarget.id, value })}
        />
      )}

      {/* 在线模型删除保护：服务未下线时不开放删除，引导先去推演服务页下线 */}
      <CenterMorphModal
        open={!!deleteTarget && deleteTarget.service_status === 'online'}
        onOpenChange={open => { if (!open) setDeleteTarget(null) }}
      >
        <CenterMorphModalContent
          ariaLabel={deleteTarget ? `删除「${deleteTarget.name}」？` : '删除推演模型？'}
          className="max-w-sm"
        >
          <div className="p-6">
            <div className="flex items-center gap-2.5">
              <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-[var(--color-warning-bg)] text-[var(--color-warning)]">
                <AlertTriangle size={18} />
              </span>
              <h2 className="text-sm font-semibold text-foreground">
                {deleteTarget ? `删除「${deleteTarget.name}」？` : '删除推演模型？'}
              </h2>
            </div>
            <div className="mt-3 rounded-xl border border-[var(--color-warning)] bg-[var(--color-warning-bg)] px-4 py-3 text-sm leading-6 text-[var(--color-warning)]">
              该模型的推演服务{deleteTarget?.service_name ? `「${deleteTarget.service_name}」` : ''}当前在线，
              调用端点仍可被访问。删除会立即移除在线端点、服务与全部历史版本，
              请先在「推演服务」页将服务下线，再删除模型。
            </div>
            <div className="mt-5 flex justify-end gap-2 pr-8">
              <Button variant="outline" onClick={() => setDeleteTarget(null)}>取消</Button>
              <Button
                onClick={() => { setDeleteTarget(null); navigate('/world-model/services') }}
                className="bg-brand text-white hover:bg-brand-deep"
              >
                前往推演服务
              </Button>
            </div>
          </div>
        </CenterMorphModalContent>
      </CenterMorphModal>

      {/* 草稿/已下线模型：常规永久删除确认 */}
      <ConfirmModal
        open={!!deleteTarget && deleteTarget.service_status !== 'online'}
        onClose={() => { if (!deleteMutation.isPending) setDeleteTarget(null) }}
        onConfirm={() => deleteTarget && deleteMutation.mutate(deleteTarget.id)}
        title={deleteTarget ? `删除「${deleteTarget.name}」？` : '删除推演模型？'}
        description={deleteTarget?.service_name
          ? `模型脚本、已下线的推演服务「${deleteTarget.service_name}」与全部历史版本将被永久移除。此操作无法撤销，请确认你不再需要这些内容。`
          : '模型脚本与全部历史版本将被永久移除。此操作无法撤销，请确认你不再需要这些内容。'}
        confirmText="删除模型"
        variant="danger"
        loading={deleteMutation.isPending}
      />
    </div>
  )
}
