import { formatDateTime } from '@/utils/datetime'
import { useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { domainApi, ontologyApi } from '@/api/ontologies'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { LoadingState } from '@/components/ui/LoadingState'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { toast } from 'sonner'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ICON_OPTIONS, OntologyAvatar } from '@/components/OntologyAvatar'
import type { OntologyListItem } from '@/types/ontology'
import {
  applyCardOrder, readSavedCardOrder, reorderCardIds, writeSavedCardOrder,
} from './cardOrder'
import {
  AlertCircle,
  Eye,
  MessageCircle,
  Network,
  Pencil,
  Plus,
  Search,
  Trash2,
  X,
} from 'lucide-react'

interface DomainItem {
  id: string
  name: string
  description: string
}

interface OntologyFormValue {
  name: string
  domain: string
  description: string
  icon: string
}

function errorMessage(error: unknown, fallback: string) {
  if (!error || typeof error !== 'object') return fallback
  const candidate = error as { detail?: unknown; message?: unknown }
  if (typeof candidate.detail === 'string') return candidate.detail
  if (Array.isArray(candidate.detail)) {
    const first = candidate.detail[0] as { msg?: unknown } | undefined
    if (typeof first?.msg === 'string') return `导入文件格式不正确：${first.msg}`
  }
  if (candidate.detail && typeof candidate.detail === 'object') {
    const detail = candidate.detail as { message?: unknown }
    if (typeof detail.message === 'string') return detail.message
  }
  if (typeof candidate.message === 'string') return candidate.message
  return fallback
}

function formatChangedAt(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return formatDateTime(date)
}

function OntologyFormModal({
  open,
  title,
  submitText,
  domains,
  initial,
  onClose,
  onSubmit,
  onManageDomains,
}: {
  open: boolean
  title: string
  submitText: string
  domains: DomainItem[]
  initial?: OntologyListItem | null
  onClose: () => void
  onSubmit: (value: OntologyFormValue) => Promise<unknown>
  onManageDomains: () => void
}) {
  const [name, setName] = useState(initial?.name ?? '')
  const [domain, setDomain] = useState(initial?.domain ?? domains[0]?.name ?? '')
  const [description, setDescription] = useState(initial?.description ?? '')
  const [icon, setIcon] = useState(initial?.icon ?? ICON_OPTIONS[0].key)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const initialDomain = initial?.domain
  const availableDomains = initialDomain && !domains.some(item => item.name === initialDomain)
    ? [{ id: `legacy-${initialDomain}`, name: initialDomain, description: '' }, ...domains]
    : domains
  const selectedDomain = domain || availableDomains[0]?.name || ''

  const submit = async () => {
    if (!name.trim()) {
      setError('请输入本体名称')
      return
    }
    if (!selectedDomain) {
      setError('请选择所属领域')
      return
    }
    setSaving(true)
    setError('')
    try {
      await onSubmit({
        name: name.trim(),
        domain: selectedDomain,
        description: description.trim(),
        icon,
      })
    } catch (submitError) {
      setError(errorMessage(submitError, `${submitText}失败，请稍后重试`))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={next => { if (!next && !saving) onClose() }}>
      <DialogContent className="w-[min(92vw,32rem)]">
        <DialogHeader icon={initial ? <Pencil size={18} /> : <Plus size={18} />}>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>
            {initial
              ? '更新本体的名称、领域与说明，不会影响已经维护的结构和版本。'
              : '填写基本信息后即可使用，后续可在详情页维护结构与版本。'}
          </DialogDescription>
        </DialogHeader>
      <div className="space-y-5">
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Input
            label="本体名称"
            required
            autoFocus
            maxLength={200}
            value={name}
            onChange={event => setName(event.target.value)}
            placeholder="例如：供应链知识本体"
            className="selection:bg-brand-mist selection:text-brand-ink focus-visible:border-ring focus-visible:ring-ring"
          />
          <div>
            <label className="mb-1.5 block text-sm font-medium text-[var(--color-text-primary)]">
              所属领域<span className="ml-0.5 text-[var(--color-danger)]">*</span>
            </label>
            {availableDomains.length > 0 ? (
              <Select value={selectedDomain} onValueChange={setDomain}>
                <SelectTrigger className="h-9 w-full rounded-md bg-card px-3 text-sm" aria-label="所属领域">
                  <SelectValue placeholder="请选择领域" />
                </SelectTrigger>
                <SelectContent>
                  {availableDomains.map(item => <SelectItem key={item.id} value={item.name}>{item.name}</SelectItem>)}
                </SelectContent>
              </Select>
            ) : (
              <button
                type="button"
                onClick={onManageDomains}
                className="flex h-9 w-full items-center justify-between rounded-md border border-dashed border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-3 text-sm text-[var(--color-warning)]"
              >
                暂无可用领域 <span className="font-medium">前往设置</span>
              </button>
            )}
          </div>
        </div>

        <div>
          <label className="mb-1.5 block text-sm font-medium text-[var(--color-text-primary)]">本体描述</label>
          <textarea
            value={description}
            onChange={event => setDescription(event.target.value)}
            maxLength={500}
            rows={3}
            placeholder="简要说明本体覆盖的业务范围和用途"
            className="w-full resize-none rounded-md border border-[var(--color-border)] bg-card px-3 py-2 text-sm text-[var(--color-text-primary)] placeholder:text-[var(--color-text-tertiary)] selection:bg-brand-mist selection:text-brand-ink focus-visible:border-ring focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          <p className="mt-1 text-right text-[11px] text-[var(--color-text-tertiary)]">{description.length}/500</p>
        </div>

        <fieldset>
          <legend className="mb-2 text-sm font-medium text-[var(--color-text-primary)]">本体图标</legend>
          <div className="grid grid-cols-7 gap-2 max-sm:grid-cols-5">
            {ICON_OPTIONS.map(option => {
              const Icon = option.icon
              const selected = icon === option.key
              return (
                <button
                  key={option.key}
                  type="button"
                  onClick={() => setIcon(option.key)}
                  title={option.label}
                  aria-label={option.label}
                  aria-pressed={selected}
                  className={`flex aspect-square items-center justify-center rounded-xl border transition-all ${
                    selected
                      ? 'border-brand bg-brand-soft text-brand-ink shadow-sm ring-2 ring-ring'
                      : 'border-border bg-card text-muted-foreground hover:border-brand-line hover:bg-muted'
                  }`}
                >
                  <Icon size={20} strokeWidth={1.8} />
                </button>
              )
            })}
          </div>
          <p className="mt-2 text-xs text-[var(--color-text-tertiary)]">所选图标将作为本体头像，可随时编辑。</p>
        </fieldset>

        {error && (
          <div role="alert" className="flex items-start gap-2.5 rounded-xl border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-3.5 py-3 text-sm text-[var(--color-danger)]">
            <AlertCircle size={16} className="mt-0.5 shrink-0" />
            <span className="leading-5">{error}</span>
          </div>
        )}
      </div>
      <DialogFooter>
        <Button variant="ghost" onClick={onClose} disabled={saving}>取消</Button>
        <Button onClick={submit} loading={saving} disabled={!name.trim() || !selectedDomain}>
          {submitText}
        </Button>
      </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function CreateOntologyCard({
  onCreate,
  onImport,
  importing,
}: {
  onCreate: () => void
  onImport: () => void
  importing: boolean
}) {
  return (
    <article className="group flex min-h-[256px] flex-col items-center justify-center rounded-2xl border border-dashed border-brand-line bg-gradient-to-br from-brand-soft via-white to-viz-cyan-soft p-6 text-center transition-all hover:border-brand hover:shadow-lg">
      <div className="mb-4 flex h-14 w-14 items-center justify-center rounded-full bg-brand text-[var(--color-text-inverse)] shadow-md transition-transform group-hover:scale-105">
        <Plus size={25} />
      </div>
      <h3 className="text-base font-semibold text-foreground">新建本体</h3>
      <p className="mt-2 max-w-[210px] text-xs leading-5 text-muted-foreground">快速创建本体模型</p>
      <div className="mt-5 flex items-center justify-center gap-2">
        <button
          type="button"
          onClick={onCreate}
          className="rounded-lg border border-brand-line bg-card px-3 py-1.5 text-xs font-medium text-brand-ink shadow-sm transition-colors hover:border-brand-line hover:bg-brand-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        >
          立即创建
        </button>
        <button
          type="button"
          disabled={importing}
          onClick={onImport}
          className="rounded-lg border border-brand-line bg-card px-3 py-1.5 text-xs font-medium text-brand-ink shadow-sm transition-colors hover:border-brand-line hover:bg-brand-soft disabled:cursor-wait disabled:opacity-60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          aria-busy={importing}
        >
          {importing ? '正在导入' : '本地导入'}
        </button>
      </div>
    </article>
  )
}

function OntologyCard({
  item,
  onEdit,
  onDetail,
  onChat,
  onDelete,
  draggable = false,
  dragging = false,
  dropPlace = null,
  onCardDragStart,
  onCardDragOver,
  onCardDragLeave,
  onCardDrop,
  onCardDragEnd,
}: {
  item: OntologyListItem
  onEdit: () => void
  onDetail: () => void
  onChat: () => void
  onDelete: () => void
  draggable?: boolean
  dragging?: boolean
  dropPlace?: 'before' | 'after' | null
  onCardDragStart?: (id: string) => void
  onCardDragOver?: (id: string, before: boolean) => void
  onCardDragLeave?: (id: string) => void
  onCardDrop?: (id: string) => void
  onCardDragEnd?: () => void
}) {
  const chatAvailable = Boolean(item.current_release_id)
  const releaseVersion = chatAvailable ? item.current_release_version || item.version : null
  // 时间语义显式化：有实质更新展示「更新于」，否则回退「创建于」，避免右下角时间歧义。
  const hasUpdate = Boolean(item.updated_at) && item.updated_at !== item.created_at
  const timeText = `${hasUpdate ? '更新于' : '创建于'} ${formatChangedAt(hasUpdate ? item.updated_at! : item.created_at)}`
  const timeTitle = hasUpdate
    ? `最近更新：${formatDateTime(item.updated_at!)}`
    : `创建时间：${formatDateTime(item.created_at)}`

  return (
    <article
      draggable={draggable}
      onDragStart={event => {
        if (!draggable) {
          event.preventDefault()
          return
        }
        event.dataTransfer.effectAllowed = 'move'
        event.dataTransfer.setData('text/plain', item.id)
        onCardDragStart?.(item.id)
      }}
      onDragOver={event => {
        if (!draggable) return
        event.preventDefault()
        event.dataTransfer.dropEffect = 'move'
        const rect = event.currentTarget.getBoundingClientRect()
        onCardDragOver?.(item.id, event.clientX < rect.left + rect.width / 2)
      }}
      onDragLeave={() => onCardDragLeave?.(item.id)}
      onDrop={event => {
        if (!draggable) return
        event.preventDefault()
        onCardDrop?.(item.id)
      }}
      onDragEnd={() => onCardDragEnd?.()}
      data-testid="ontology-card"
      data-ontology-id={item.id}
      className={`group flex min-h-[256px] flex-col overflow-hidden rounded-2xl border bg-card transition-all duration-200 hover:-translate-y-0.5 hover:border-brand-line hover:shadow-lg ${
        draggable ? 'cursor-grab active:cursor-grabbing' : ''
      } ${dragging ? 'opacity-40' : ''} ${
        dropPlace ? 'outline-2 -outline-offset-2 outline-brand' : ''
      } border-border`}
    >
      <div className="flex flex-col p-4 pb-2.5">
        <div className="flex min-h-11 items-start gap-3 overflow-hidden">
          <OntologyAvatar icon={item.icon} />
          <div className="flex min-h-11 min-w-0 flex-1 flex-col justify-center overflow-hidden">
            <div className="flex min-w-0 items-center overflow-hidden">
              <button
                type="button"
                onClick={onDetail}
                className="min-w-0 flex-1 truncate text-left text-[15px] font-semibold leading-5 text-foreground transition-colors hover:text-brand-ink"
                title={item.name}
              >
                {item.name}
              </button>
            </div>
            <div className="mt-1 flex min-w-0 items-center gap-1.5">
              <span className="inline-flex min-w-0 max-w-full truncate rounded-md border border-brand-line bg-brand-soft px-2 py-0.5 text-[11px] font-medium leading-4 text-brand-ink">
                {item.domain || '未设置领域'}
              </span>
              {releaseVersion ? (
                <span className="inline-flex shrink-0 rounded-md border border-viz-violet-soft bg-viz-violet-soft px-2 py-0.5 font-mono text-[11px] font-medium leading-4 text-viz-violet">
                  {releaseVersion}
                </span>
              ) : (
                <span className="inline-flex shrink-0 rounded-md border border-border bg-muted px-2 py-0.5 text-[11px] font-medium leading-4 text-muted-foreground">
                  未发布
                </span>
              )}
            </div>
          </div>
        </div>

        <button
          type="button"
          onClick={onDetail}
          className="mt-4 min-h-[44px] w-full cursor-pointer text-left text-sm leading-[22px] text-muted-foreground transition-colors hover:text-brand-ink"
          style={{ display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}
          title={item.description || '暂无描述'}
        >
          {item.description || '暂无描述'}
        </button>

        <div className="mt-3 grid grid-cols-4 gap-1.5">
          {[
            { label: '对象实体', value: item.entity_count ?? 0 },
            { label: '实体关系', value: item.relation_count ?? 0 },
            { label: '执行动作', value: item.action_count ?? 0 },
            { label: '哨兵引擎', value: item.sentinel_count ?? 0 },
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
            onClick={onDetail}
            aria-label={`查看本体 ${item.name} 详情`}
            className="inline-flex shrink-0 items-center gap-0.5 whitespace-nowrap rounded-lg bg-muted px-1.5 py-1.5 text-[11px] font-medium text-foreground transition-colors hover:bg-brand-soft hover:text-brand-ink"
          >
            <Eye size={12} /> 查看
          </button>
          <button
            type="button"
            onClick={onEdit}
            className="inline-flex shrink-0 items-center gap-0.5 whitespace-nowrap rounded-lg bg-muted px-1.5 py-1.5 text-[11px] font-medium text-foreground transition-colors hover:bg-brand-soft hover:text-brand-ink"
          >
            <Pencil size={12} /> 编辑
          </button>
          <span title={chatAvailable ? undefined : '本体发布后可进入助手对话'} className="inline-flex shrink-0">
            <button
              type="button"
              onClick={onChat}
              disabled={!chatAvailable}
              aria-label={chatAvailable ? `使用${item.name}进入本体助手对话` : `${item.name}尚未发布，暂不可对话`}
              className="inline-flex shrink-0 items-center gap-0.5 whitespace-nowrap rounded-lg bg-muted px-1.5 py-1.5 text-[11px] font-medium text-foreground transition-colors hover:bg-brand-soft hover:text-brand-ink disabled:pointer-events-none disabled:cursor-not-allowed disabled:text-[var(--color-text-tertiary)] hover:bg-muted"
            >
              <MessageCircle size={12} /> 对话
            </button>
          </span>
        </div>
        <div className="ml-auto flex shrink-0 items-center gap-0.5">
          <span
            data-testid="ontology-card-time"
            className="hidden shrink-0 whitespace-nowrap text-[11px] tabular-nums text-[var(--color-text-tertiary)] min-[1400px]:inline"
            title={timeTitle}
          >
            {timeText}
          </span>
          <button
            type="button"
            onClick={onDelete}
            className="shrink-0 rounded-lg p-1.5 text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)]"
            title="删除本体"
            aria-label={`删除本体 ${item.name}`}
          >
            <Trash2 size={14} />
          </button>
        </div>
      </footer>
    </article>
  )
}

export default function OntologyListPage({ defaultCreateOpen = false }: { defaultCreateOpen?: boolean }) {
  const [nameFilter, setNameFilter] = useState('')
  const [domainFilter, setDomainFilter] = useState('')
  const [createOpen, setCreateOpen] = useState(defaultCreateOpen)
  const [editTarget, setEditTarget] = useState<OntologyListItem | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<OntologyListItem | null>(null)
  // 手动排序快照：localStorage 持久化（本浏览器），拖拽落位时更新。
  const [cardOrder, setCardOrder] = useState<string[]>(() => readSavedCardOrder(window.localStorage))
  const [draggingId, setDraggingId] = useState<string | null>(null)
  const [dropTarget, setDropTarget] = useState<{ id: string; before: boolean } | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const queryClient = useQueryClient()
  const navigate = useNavigate()

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['ontologies'],
    queryFn: () => ontologyApi.list({ page_size: 1000 }),
  })
  const { data: configuredDomains = [] } = useQuery<DomainItem[]>({
    queryKey: ['domains'],
    queryFn: () => domainApi.list(),
  })

  const allItems = useMemo(() => data?.items ?? [], [data?.items])
  const domains = useMemo(() => {
    return [...configuredDomains].sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'))
  }, [configuredDomains])

  const filteredItems = useMemo(() => {
    const keyword = nameFilter.trim().toLocaleLowerCase('zh-CN')
    const matched = [...allItems]
      .filter(item => !keyword
        || `${item.name} ${item.description ?? ''}`.toLocaleLowerCase('zh-CN').includes(keyword))
      .filter(item => !domainFilter || item.domain === domainFilter)
    // 有手动排序快照时按快照相对位置排列（未入序的新本体插最前）；无快照保持创建时间倒序。
    return applyCardOrder(matched, cardOrder)
  }, [allItems, cardOrder, domainFilter, nameFilter])

  // 筛选/搜索激活时禁用拖拽：部分可见列表上落位会产生歧义顺序。
  const dragEnabled = !nameFilter && !domainFilter

  const orderedAllIds = useMemo(
    () => applyCardOrder(allItems, cardOrder).map(item => item.id),
    [allItems, cardOrder],
  )

  const handleCardDrop = (targetId: string) => {
    const draggedId = draggingId
    const place = dropTarget?.id === targetId && dropTarget.before ? 'before' : 'after'
    setDraggingId(null)
    setDropTarget(null)
    if (!draggedId || !dragEnabled || draggedId === targetId) return
    const next = reorderCardIds(orderedAllIds, draggedId, targetId, place)
    if (next === orderedAllIds) return
    setCardOrder(next)
    writeSavedCardOrder(window.localStorage, next)
  }

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['ontologies'] })
    queryClient.invalidateQueries({ queryKey: ['domains'] })
    queryClient.invalidateQueries({ queryKey: ['stats'] })
  }

  const createMutation = useMutation({
    mutationFn: (value: OntologyFormValue) => ontologyApi.create(value),
    onSuccess: () => {
      refresh()
      setCreateOpen(false)
      toast.success('本体已创建', { description: '现在可以继续维护对象实体、实体关系与版本。' })
      if (defaultCreateOpen) navigate('/ontologies', { replace: true })
    },
  })
  const updateMutation = useMutation({
    mutationFn: ({ id, value }: { id: string; value: OntologyFormValue }) => ontologyApi.update(id, value),
    onSuccess: () => {
      refresh()
      setEditTarget(null)
      toast.success('本体信息已更新')
    },
  })
  const deleteMutation = useMutation({
    mutationFn: (id: string) => ontologyApi.delete(id),
    onSuccess: () => {
      refresh()
      setDeleteTarget(null)
      toast.success('本体已删除', { description: '相关结构、映射与版本数据已一并移除。' })
    },
    onError: error => {
      toast.error('本体删除失败', { description: errorMessage(error, '请稍后重试。') })
    },
  })
  const importMutation = useMutation({
    mutationFn: (body: unknown) => ontologyApi.importStructure(body),
    onSuccess: result => {
      refresh()
      toast.success('本体导入完成', { description: `已导入「${result.ontology.name}」，即将打开详情。` })
      navigate(`/ontologies/${result.ontology.id}`)
    },
  })

  const handleImportFile = async (file?: File) => {
    if (!file) return
    if (!file.name.toLocaleLowerCase().endsWith('.json')) {
      toast.error('无法导入本体', { description: '请选择 JSON 格式的本体结构文件。' })
      return
    }
    if (file.size > 5 * 1024 * 1024) {
      toast.error('文件超过大小限制', { description: '本体结构文件不能超过 5 MB。' })
      return
    }
    try {
      const text = await file.text()
      let body: unknown
      try {
        body = JSON.parse(text)
      } catch {
        toast.error('文件内容无法识别', { description: '文件不是有效的 JSON，请重新选择本体结构导出文件。' })
        return
      }
      if (!body || typeof body !== 'object' || Array.isArray(body)) {
        throw new Error('文件根节点必须是 JSON 对象')
      }
      await importMutation.mutateAsync(body)
    } catch (error: unknown) {
      toast.error('本体导入失败', { description: errorMessage(error, '请确认文件来自本体结构 JSON 导出。') })
    } finally {
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  const openCreate = () => setCreateOpen(true)
  const closeCreate = () => {
    setCreateOpen(false)
    if (defaultCreateOpen) navigate('/ontologies', { replace: true })
  }

  return (
    <div className="min-h-full">
      <section className="mb-4 flex flex-wrap items-center gap-3 rounded-xl border border-border bg-card px-4 py-3 shadow-sm/50" aria-label="本体筛选">
        <div className="relative w-full sm:w-72">
          <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-[var(--color-text-tertiary)]" />
          <input
            value={nameFilter}
            onChange={event => setNameFilter(event.target.value)}
            placeholder="搜索本体名称或描述"
            aria-label="按本体名称或描述筛选"
            className="h-9 w-full rounded-lg border border-border bg-card pl-8 pr-8 text-sm text-foreground placeholder:text-[var(--color-text-tertiary)] focus-visible:border-ring focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
        {/* Radix Select 不允许空字符串 value，「全部领域」用哨兵值 __all__ 映射为空 */}
        <Select
          value={domainFilter || '__all__'}
          onValueChange={value => setDomainFilter(value === '__all__' ? '' : value)}
        >
          {/* 样式对齐旁边搜索输入框：白底/slate 描边/teal 聚焦（覆盖 vendored bg-background 画布灰） */}
          <SelectTrigger
            aria-label="按所属领域筛选"
            className="h-9 w-44 rounded-lg border-border bg-card text-foreground focus-visible:border-ring focus-visible:ring-ring"
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">全部领域</SelectItem>
            {domains.map(item => <SelectItem key={item.id} value={item.name}>{item.name}</SelectItem>)}
          </SelectContent>
        </Select>
        {(nameFilter || domainFilter) && (
          <button
            type="button"
            onClick={() => { setNameFilter(''); setDomainFilter('') }}
            className="inline-flex h-9 items-center gap-1 rounded-lg px-2.5 text-xs text-[var(--color-text-tertiary)] hover:bg-muted hover:text-muted-foreground"
          >
            <X size={13} /> 清除筛选
          </button>
        )}
        <span className="ml-auto hidden text-xs tabular-nums text-[var(--color-text-tertiary)] sm:inline" aria-live="polite">
          {nameFilter || domainFilter
            ? `共 ${filteredItems.length} / ${allItems.length} 个本体`
            : `共 ${allItems.length} 个本体`}
        </span>
        <button
          type="button"
          onClick={openCreate}
          className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-[var(--color-nav-bg)] px-4 text-sm font-medium text-[var(--color-text-inverse)] shadow-sm transition-all duration-200 hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        >
          <Plus size={15} /> 立即创建
        </button>
      </section>

      <input
        ref={fileInputRef}
        type="file"
        accept=".json,application/json"
        className="sr-only focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        aria-label="选择本体结构 JSON 文件"
        onChange={event => void handleImportFile(event.target.files?.[0])}
      />
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        <CreateOntologyCard
          onCreate={openCreate}
          onImport={() => {
            if (fileInputRef.current) fileInputRef.current.value = ''
            fileInputRef.current?.click()
          }}
          importing={importMutation.isPending}
        />

        {isLoading ? (
          <div className="flex min-h-[300px] items-center justify-center rounded-2xl border border-border bg-card sm:col-span-1 lg:col-span-2 xl:col-span-3">
            <LoadingState message="加载本体列表..." />
          </div>
        ) : isError ? (
          <div className="flex min-h-[300px] flex-col items-center justify-center gap-3 rounded-2xl border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-6 text-center sm:col-span-1 lg:col-span-2 xl:col-span-3" role="alert">
            <p className="text-sm text-[var(--color-danger)]">本体列表加载失败，请检查网络连接后重试。</p>
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
            <Network size={28} className="text-[var(--color-text-tertiary)]" />
            <p className="mt-3 text-sm font-medium text-muted-foreground">{nameFilter || domainFilter ? '没有符合条件的本体' : '还没有创建本体'}</p>
            <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">{nameFilter || domainFilter ? '请调整名称或领域筛选条件' : '点击左侧卡片创建第一个本体'}</p>
          </div>
        ) : (
          filteredItems.map(item => (
            <OntologyCard
              key={item.id}
              item={item}
              onEdit={() => setEditTarget(item)}
              onDetail={() => navigate(`/ontologies/${item.id}`)}
              onChat={() => navigate(`/agent?ontology_id=${encodeURIComponent(item.id)}`)}
              onDelete={() => setDeleteTarget(item)}
              draggable={dragEnabled}
              dragging={draggingId === item.id}
              dropPlace={dragEnabled && draggingId && draggingId !== item.id && dropTarget?.id === item.id
                ? (dropTarget.before ? 'before' : 'after')
                : null}
              onCardDragStart={setDraggingId}
              onCardDragOver={(id, before) => setDropTarget(draggingId && draggingId !== id ? { id, before } : null)}
              onCardDragLeave={id => setDropTarget(current => current?.id === id ? null : current)}
              onCardDrop={handleCardDrop}
              onCardDragEnd={() => { setDraggingId(null); setDropTarget(null) }}
            />
          ))
        )}
      </div>

      {createOpen && (
        <OntologyFormModal
          open
          title="新建本体"
          submitText="创建本体"
          domains={configuredDomains}
          onClose={closeCreate}
          onSubmit={value => createMutation.mutateAsync(value)}
          onManageDomains={() => navigate('/settings/domains')}
        />
      )}

      {editTarget && (
        <OntologyFormModal
          open
          title="编辑本体"
          submitText="保存修改"
          domains={configuredDomains}
          initial={editTarget}
          onClose={() => setEditTarget(null)}
          onSubmit={value => updateMutation.mutateAsync({ id: editTarget.id, value })}
          onManageDomains={() => navigate('/settings/domains')}
        />
      )}

      <ConfirmDialog
        open={!!deleteTarget}
        onClose={() => { if (!deleteMutation.isPending) setDeleteTarget(null) }}
        onConfirm={() => deleteTarget && deleteMutation.mutate(deleteTarget.id)}
        title={deleteTarget ? `删除「${deleteTarget.name}」？` : '删除本体？'}
        description="本体结构、数据映射与版本记录将被永久移除。此操作无法撤销，请确认你不再需要这些内容。"
        confirmText="删除本体"
        variant="danger"
        loading={deleteMutation.isPending}
      />
    </div>
  )
}
