/**
 * 任务实例弹窗 — 超级助手工作台的统一入口。
 *
 * 布局（ScheduledTasksDialog 同款两栏模式）：
 * 左栏：视图导航（实例看板 | 流程模板），导航归属见 dialogNavOf
 * 右栏：当前视图内容（看板/实例轻量详情/模板列表/激活表单/画布编辑器；
 * 模板列表头部承载「新建流程」入口，激活表单内嵌只读流程结构预览）
 * 全程不离开超级助手 HashRouter 上下文。
 * 跨视图共享文案统一取自 components/task-instances/copy.ts。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft, CheckCircle2, ClipboardList, ChevronDown, ChevronRight, Eye,
  ExternalLink, LayoutGrid, ListChecks, Loader2, Pencil, Play, Plus,
  RefreshCw, Search, Send, ThumbsDown, Trash2, XCircle,
} from 'lucide-react'
import { toast } from 'sonner'
import {
  apiError, taskInstancesApi,
  type TemplateDetail, type TemplateSummary, type InstanceDetail,
  type InstanceListItem, type WorkflowSpecSnapshot,
} from '@/api/taskInstances'
import { DialogShell } from './AssistantConfiguration'
import { Input } from '@/components/ui/Input'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import {
  ATTENTION_REASON_LABELS, BOARD_GROUPS, INSTANCE_STATUS_LABELS, INSTANCE_STATUS_TONE,
  attentionReasonOf, dialogNavOf, instanceCardTimeLabel, instanceGroup, isTerminalStatus,
  waitingNodesOf, type TaskDialogView,
} from '@/components/task-instances/instanceView'
import {
  filterTemplates, isPresetTemplate, templateEmptyKind,
  type TemplateEmptyKind, type TemplateSource,
} from '@/components/task-instances/templateFilters'
import * as copy from '@/components/task-instances/copy'
import ProposalCard from '@/components/task-instances/ProposalCard'
import FailureNotice from '@/components/task-instances/FailureNotice'
import WorkflowPreviewGraph from '@/components/task-instances/WorkflowPreviewGraph'
import TemplateEditorPanel from '@/components/task-instances/TemplateEditorPanel'
import { formatRelativeTime } from '@/utils/datetime'
import WorkflowCanvas from './WorkflowCanvas'

// ─── 视图类型 ───
type View = TaskDialogView

interface TaskInstancesDialogProps {
  open: boolean
  onClose: () => void
  /** 深链选中某实例（定时任务「查看实例」跳转入口）；打开时生效一次 */
  initialInstanceId?: string | null
}

// ─── 状态图标 ───
function StatusIcon({ status }: { status: string }) {
  if (status === 'completed') return <CheckCircle2 size={14} className="text-[var(--color-success)]" />
  if (['failed', 'cancelled'].includes(status)) return <XCircle size={14} className="text-[var(--color-danger)]" />
  if (status === 'active') return <Loader2 size={14} className="animate-spin text-brand-ink" />
  return <ClipboardList size={14} className="text-[var(--color-text-tertiary)]" />
}

export default function TaskInstancesDialog({ open, onClose, initialInstanceId }: TaskInstancesDialogProps) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [view, setView] = useState<View>('board')
  const [selectedTemplate, setSelectedTemplate] = useState<TemplateSummary | null>(null)
  const [selectedInstanceId, setSelectedInstanceId] = useState<string | null>(null)
  const [instanceName, setInstanceName] = useState('')
  const [goal, setGoal] = useState('')
  // 模板列表筛选（仅弹窗内派生，不改服务端搜索契约）；各视图往返保留，
  // 关闭弹窗恢复默认（预置 + 空关键词）
  const [templateKeyword, setTemplateKeyword] = useState('')
  const [templateSource, setTemplateSource] = useState<TemplateSource>('preset')
  // 编辑器脏稿（TemplateEditorPanel 上报）：所有离开 template-edit 的弹窗级出口
  // （右上关闭/Esc/左栏切视图）统一走确认守卫；面板自身返回按钮由面板内确认负责
  const [editorDirty, setEditorDirty] = useState(false)
  const [pendingExit, setPendingExit] = useState<(() => void) | null>(null)
  const [discardOpen, setDiscardOpen] = useState(false)

  // ─── 数据 ───
  const templates = useQuery({
    queryKey: ['task-instances', 'templates', ''],
    queryFn: () => taskInstancesApi.listTemplates(''),
    enabled: open,
  })
  const instances = useQuery({
    queryKey: ['task-instances', 'instances'],
    queryFn: () => taskInstancesApi.listInstances({ size: 50 }),
    enabled: open,
    refetchInterval: 8000,
  })
  // 激活/查看/编辑视图共用的模板详情（只读 DAG 预览与 YAML 原文数据源）
  const templateDetail = useQuery({
    queryKey: ['task-instances', 'template', selectedTemplate?.id],
    queryFn: () => taskInstancesApi.getTemplate(selectedTemplate?.id as string),
    enabled: (view === 'activate' || view === 'template-view' || view === 'template-edit')
      && Boolean(selectedTemplate),
  })

  // 关闭时重置（筛选恢复默认）
  useEffect(() => {
    if (!open) {
      setView('board'); setSelectedTemplate(null); setSelectedInstanceId(null)
      setInstanceName(''); setGoal('')
      setTemplateKeyword(''); setTemplateSource('preset')
      setEditorDirty(false); setPendingExit(null); setDiscardOpen(false)
    }
  }, [open])

  // 离开编辑视图（含面板自身确认后的返回）即复位脏稿标记，避免残留拦截下一次出口
  useEffect(() => {
    if (view !== 'template-edit') setEditorDirty(false)
  }, [view])

  // 打开时按深链直接选中实例（定时任务「查看实例」入口），与关闭重置互不干扰
  useEffect(() => {
    if (open && initialInstanceId) {
      setSelectedInstanceId(initialInstanceId)
      setView('instance')
    }
  }, [open, initialInstanceId])

  // ─── 激活实例 ───
  const activate = useMutation({
    mutationFn: () => taskInstancesApi.activateInstance(
      selectedTemplate?.id as string,
      { name: instanceName, goal },
      `dialog-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`),
    onSuccess: () => {
      toast.success(copy.ACTIVATED_TOAST)
      setView('board'); setInstanceName(''); setGoal(''); setSelectedTemplate(null)
      void queryClient.invalidateQueries({ queryKey: ['task-instances', 'instances'] })
    },
    onError: error => toast.error(apiError(error)),
  })

  const handleSelectTemplate = useCallback((template: TemplateSummary) => {
    setSelectedTemplate(template); setView('activate')
    setInstanceName(''); setGoal('')
  }, [])

  const handleSelectInstance = useCallback((id: string) => {
    setSelectedInstanceId(id); setView('instance')
  }, [])

  const backToBoard = useCallback(() => {
    setView('board'); setSelectedInstanceId(null)
  }, [])

  /** 离开编辑态的统一守卫（R2）：编辑器有脏稿时先弹「放弃未保存修改？」；
   *  「继续编辑」保留草稿且不执行原去向，「放弃」才执行原动作 */
  const exitEditGuard = useCallback((after: () => void) => {
    if (view === 'template-edit' && editorDirty) {
      setPendingExit(() => after)
      setDiscardOpen(true)
      return
    }
    after()
  }, [view, editorDirty])

  const requestClose = useCallback(() => exitEditGuard(onClose), [exitEditGuard, onClose])

  const discardDraft = () => {
    const exit = pendingExit
    setDiscardOpen(false); setPendingExit(null)
    if (exit) exit()
    else onClose()
  }

  const openFullDetail = useCallback((id: string) => {
    onClose()
    navigate(`/task-instances/instances/${id}`)
  }, [navigate, onClose])

  // 失败实例「用相同目标重新激活」：按快照 revision 定位模板。
  // 三态返回：模板列表未就绪（loading）/ 模板已演进或删除（missing）/
  // 已进入激活表单（ok）；调用方按态提示，避免把「未就绪」误报成「已删除」
  const handleReactivate = useCallback((detail: InstanceDetail): 'ok' | 'loading' | 'missing' => {
    if (templates.isLoading) return 'loading'
    const template = (templates.data ?? []).find(
      item => item.latest_revision_id === detail.template_revision_id)
    if (!template) return 'missing'
    setSelectedTemplate(template)
    setInstanceName(`${detail.name}·重试`.slice(0, 200))
    setGoal(detail.goal)
    setView('activate')
    return 'ok'
  }, [templates.data, templates.isLoading])

  const grouped = useMemo(() => {
    const map = new Map<string, InstanceListItem[]>(
      BOARD_GROUPS.map(g => [g.key, []]))
    for (const item of instances.data?.items ?? []) {
      map.get(instanceGroup(item))?.push(item)
    }
    return map
  }, [instances.data])

  // 弹窗内派生筛选：完整列表 ×（名称关键词 ∩ 来源单选）
  const filteredTemplates = useMemo(
    () => filterTemplates(templates.data ?? [], {
      keyword: templateKeyword, source: templateSource }),
    [templates.data, templateKeyword, templateSource])
  const templateEmpty = templateEmptyKind(templates.data ?? [], {
    keyword: templateKeyword, source: templateSource })

  if (!open) return null

  const navKey = dialogNavOf(view)

  return (
    <>
    <DialogShell
      title="任务实例"
      description={copy.DOMAIN_SUBTITLE}
      size="wide"
      onClose={requestClose}
      onInteractOutside={event => event.preventDefault()}
      icon={<ListChecks size={18} />}
      contentClassName="h-[min(82dvh,44rem)]"
    >
      <div className="flex min-h-0 flex-1" data-testid="task-instances-dialog">
        {/* ─── 左栏：视图导航 ─── */}
        <nav aria-label="任务实例视图" className="flex w-48 shrink-0 flex-col border-r border-[var(--color-border)]">
          <div className="p-2">
            <button
              type="button"
              onClick={() => exitEditGuard(backToBoard)}
              aria-pressed={navKey === 'instances'}
              className={`mb-1 flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs transition-colors focus-visible:ring-2 ring-ring ${
                navKey === 'instances'
                  ? 'bg-brand-soft font-medium text-brand-ink'
                  : 'text-[var(--color-text-primary)] hover:bg-[var(--color-bg-hover)]'
              }`}
            >
              <LayoutGrid size={14} className="shrink-0" /> 实例看板
            </button>
            <button
              type="button"
              onClick={() => exitEditGuard(() => setView('templates'))}
              aria-pressed={navKey === 'templates'}
              className={`mb-1 flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs transition-colors focus-visible:ring-2 ring-ring ${
                navKey === 'templates'
                  ? 'bg-brand-soft font-medium text-brand-ink'
                  : 'text-[var(--color-text-primary)] hover:bg-[var(--color-bg-hover)]'
              }`}
            >
              <ListChecks size={14} className="shrink-0" /> 流程模板
            </button>
          </div>
        </nav>

        {/* ─── 右栏：视图内容 ─── */}
        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          {view === 'board' && (
            <BoardView
              grouped={grouped}
              total={instances.data?.total ?? 0}
              loading={instances.isLoading}
              error={instances.isError}
              onRefresh={() => void instances.refetch()}
              onSelectInstance={handleSelectInstance}
              onGoTemplates={() => setView('templates')}
            />
          )}
          {view === 'instance' && selectedInstanceId && (
            <InstanceLiteView
              instanceId={selectedInstanceId}
              onBack={backToBoard}
              onOpenFull={openFullDetail}
              onReactivate={handleReactivate}
            />
          )}
          {view === 'activate' && selectedTemplate && (
            <ActivateForm
              template={selectedTemplate}
              previewSpec={templateDetail.data?.spec_compiled}
              previewLoading={templateDetail.isLoading}
              previewError={templateDetail.isError}
              instanceName={instanceName} setInstanceName={setInstanceName}
              goal={goal} setGoal={setGoal}
              onBack={() => { setView('templates'); setSelectedTemplate(null) }}
              onActivate={() => activate.mutate()}
              loading={activate.isPending}
            />
          )}
          {view === 'templates' && (
            <TemplatesPanel
              templates={filteredTemplates}
              emptyKind={templateEmpty}
              source={templateSource}
              keyword={templateKeyword}
              loading={templates.isLoading}
              error={templates.isError}
              onRetry={() => void templates.refetch()}
              onKeywordChange={setTemplateKeyword}
              onSourceChange={setTemplateSource}
              onNewFlow={() => setView('canvas')}
              onSelect={handleSelectTemplate}
              onView={template => {
                setSelectedTemplate(template); setView('template-view')
              }}
              onEdit={template => {
                setSelectedTemplate(template); setView('template-edit')
              }}
            />
          )}
          {view === 'template-view' && selectedTemplate && (
            <TemplateDetailView
              template={selectedTemplate}
              detail={templateDetail.data}
              loading={templateDetail.isLoading}
              error={templateDetail.isError}
              onBack={() => { setView('templates'); setSelectedTemplate(null) }}
            />
          )}
          {view === 'template-edit' && selectedTemplate && (
            <div className="min-h-0 flex-1 overflow-y-auto p-4" data-testid="template-edit-view">
              <TemplateEditorPanel
                templateId={selectedTemplate.id}
                exitLabel={copy.RETURN_TO_TEMPLATES}
                onDirtyChange={setEditorDirty}
                onExit={() => { setView('templates'); setSelectedTemplate(null) }}
              />
            </div>
          )}
          {view === 'canvas' && (
            <WorkflowCanvas
              onCreated={() => {
                // 新建成功归位「个人」并清空关键词，让刚创建的模板可见
                setTemplateSource('personal')
                setTemplateKeyword('')
              }}
              onClose={() => setView('templates')} />
          )}
        </div>
      </div>
    </DialogShell>

    {/* 离开编辑态统一守卫的确认框（ScheduledTasksDialog 同款模式） */}
    <ConfirmDialog
      open={discardOpen}
      onClose={() => { setDiscardOpen(false); setPendingExit(null) }}
      onConfirm={discardDraft}
      title="放弃未保存修改？"
      description="编辑器中未保存的草稿将丢失。"
      confirmText="放弃"
      cancelText="继续编辑"
      variant="warning"
    />
    </>
  )
}

// ═══════════════════════════════════════════════════════════
// 子组件：实例看板
// ═══════════════════════════════════════════════════════════
function BoardView({ grouped, total, loading, error, onRefresh, onSelectInstance, onGoTemplates }: {
  grouped: Map<string, InstanceListItem[]>
  total: number
  loading: boolean
  error: boolean
  onRefresh: () => void
  onSelectInstance: (id: string) => void
  onGoTemplates: () => void
}) {
  if (loading) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center text-xs text-[var(--color-text-tertiary)]" data-testid="task-instances-board">
        加载中…
      </div>
    )
  }
  if (error) {
    return (
      <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3 px-8 text-center" data-testid="task-instances-board">
        <XCircle size={24} className="text-[var(--color-danger)]" />
        <p className="text-sm text-[var(--color-text-secondary)]">实例列表加载失败</p>
        <Button size="sm" variant="outline" className="h-8 gap-1 text-xs" onClick={onRefresh}>
          <RefreshCw size={12} /> 重试
        </Button>
      </div>
    )
  }
  if (!total) {
    return (
      <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3 px-8 text-center" data-testid="task-instances-board">
        <ListChecks size={24} className="text-[var(--color-text-tertiary)]" />
        <p className="text-sm text-[var(--color-text-secondary)]">{copy.BOARD_EMPTY_TITLE}</p>
        <p className="text-xs text-[var(--color-text-tertiary)]">{copy.BOARD_EMPTY_HINT}</p>
        <Button size="sm" className="h-8 gap-1.5 text-xs" onClick={onGoTemplates}>
          <Play size={14} /> {copy.BOARD_EMPTY_ACTION}
        </Button>
      </div>
    )
  }
  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-4" data-testid="task-instances-board">
      <div className="mb-3 flex items-center justify-between">
        <span className="text-xs text-[var(--color-text-tertiary)]">
          {copy.BOARD_COUNT_LABEL(total)}
        </span>
        <Button
          variant="outline" size="sm"
          className="h-7 gap-1 px-2 text-[11px]"
          title="看板自动刷新中（每 8 秒），也可手动刷新"
          onClick={onRefresh}
        >
          <RefreshCw size={12} /> 刷新
        </Button>
      </div>
      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        {BOARD_GROUPS.map(group => {
          const items = grouped.get(group.key) ?? []
          return (
            <div key={group.key} className="space-y-1.5">
              <div className="flex items-center gap-1.5 px-1 text-xs font-medium text-[var(--color-text-secondary)]">
                {group.label}
                <span className="rounded-full bg-[var(--color-bg-hover)] px-1.5 text-[10px] font-medium tabular-nums text-[var(--color-text-secondary)]">
                  {items.length}
                </span>
              </div>
              {items.map(item => {
                const reason = attentionReasonOf(item.nodes)
                return (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => onSelectInstance(item.id)}
                    data-testid="task-instance-card"
                    className="w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-sidebar)] p-2.5 text-left transition-all hover:border-[var(--color-primary)]/40 hover:shadow-sm focus-visible:ring-2 ring-ring"
                  >
                    <div className="flex items-start justify-between gap-1.5">
                      <span className="min-w-0 truncate text-xs font-medium text-[var(--color-text-primary)]">
                        {item.name}
                      </span>
                      <span className="flex shrink-0 items-center gap-1">
                        {reason ? (
                          <span className={`rounded px-1.5 py-0.5 text-[11px] ${
                            reason === 'approval'
                              ? 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]'
                              : 'bg-[var(--color-info-bg)] text-[var(--color-text-primary)]'
                          }`}>
                            {ATTENTION_REASON_LABELS[reason]}
                          </span>
                        ) : null}
                        <span className={`rounded px-1.5 py-0.5 text-[11px] ${INSTANCE_STATUS_TONE[item.status] ?? ''}`}>
                          {INSTANCE_STATUS_LABELS[item.status] ?? item.status}
                        </span>
                      </span>
                    </div>
                    <div className="mt-1 flex items-center gap-1.5">
                      <StatusIcon status={item.status} />
                      <span className="truncate text-[11px] text-[var(--color-text-tertiary)]">
                        {item.goal || copy.TEMPLATE_UNTITLED_GOAL}
                      </span>
                    </div>
                    <div className="mt-1 text-[11px] tabular-nums text-[var(--color-text-tertiary)]">
                      {instanceCardTimeLabel(item)}
                    </div>
                  </button>
                )
              })}
              {!items.length ? (
                <p className="px-1 py-2 text-[11px] text-[var(--color-text-tertiary)]">
                  {copy.BOARD_GROUP_EMPTY}
                </p>
              ) : null}
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════════════════════
// 子组件：模板列表面板（搜索 + 来源单选 + 卡片四操作，YIYA-44/45）
// ═══════════════════════════════════════════════════════════
function TemplatesPanel({ templates, emptyKind, source, keyword, loading, error,
  onRetry, onKeywordChange, onSourceChange, onNewFlow, onSelect, onView, onEdit }: {
  templates: TemplateSummary[]
  emptyKind: TemplateEmptyKind | null
  source: TemplateSource
  keyword: string
  loading: boolean
  error: boolean
  onRetry: () => void
  onKeywordChange: (keyword: string) => void
  onSourceChange: (source: TemplateSource) => void
  onNewFlow: () => void
  onSelect: (template: TemplateSummary) => void
  onView: (template: TemplateSummary) => void
  onEdit: (template: TemplateSummary) => void
}) {
  const queryClient = useQueryClient()
  const [pendingDelete, setPendingDelete] = useState<TemplateSummary | null>(null)

  // 个人模板删除：确认后请求；取消不发请求；失败仅提示，不虚假移除卡片
  const remove = useMutation({
    mutationFn: (id: string) => taskInstancesApi.deleteTemplate(id),
    onSuccess: () => {
      toast.success(copy.TEMPLATE_DELETED_TOAST)
      void queryClient.invalidateQueries({ queryKey: ['task-instances', 'templates'] })
    },
    onError: mutationError => toast.error(apiError(mutationError)),
  })

  const sourceLabel = source === 'preset'
    ? copy.TEMPLATE_SOURCE_PRESET : copy.TEMPLATE_SOURCE_PERSONAL

  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="task-instances-templates">
      {/* 头部：搜索（仅名称）+ 来源两项单选 + 新建；窄屏允许换行，动作不溢出 */}
      <div className="flex flex-wrap items-center gap-2 px-4 py-2.5">
        <div className="relative w-56 max-w-full">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--color-text-tertiary)]" />
          <Input
            aria-label={copy.TEMPLATE_SEARCH_LABEL}
            placeholder={copy.TEMPLATE_SEARCH_LABEL}
            className="h-8 pl-8 text-xs"
            value={keyword}
            onChange={event => onKeywordChange(event.target.value)}
            data-testid="template-search-input"
          />
        </div>
        <div
          role="radiogroup"
          aria-label={copy.TEMPLATE_SOURCE_LABEL}
          className="flex shrink-0 rounded-lg border border-[var(--color-border)] p-0.5"
          data-testid="template-source-group"
        >
          {(['preset', 'personal'] as const).map(option => {
            const active = source === option
            return (
              <button
                key={option}
                type="button"
                role="radio"
                aria-checked={active}
                onClick={() => onSourceChange(option)}
                data-testid={`template-source-${option}`}
                className={`rounded-md px-2.5 py-1 text-xs transition-colors focus-visible:ring-2 ring-ring ${
                  active
                    ? 'bg-brand-soft font-medium text-brand-ink'
                    : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)]'
                }`}
              >
                {option === 'preset'
                  ? copy.TEMPLATE_SOURCE_PRESET : copy.TEMPLATE_SOURCE_PERSONAL}
              </button>
            )
          })}
        </div>
        <Button size="sm" className="ml-auto h-8 gap-1.5 text-xs" onClick={onNewFlow} data-testid="new-flow-button">
          <Plus size={14} /> {copy.TEMPLATE_NEW_BUTTON}
        </Button>
      </div>

      {loading ? (
        <div className="px-4 py-12 text-center text-xs text-[var(--color-text-tertiary)]">加载中…</div>
      ) : error ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 px-8 text-center">
          <XCircle size={24} className="text-[var(--color-danger)]" />
          <p className="text-sm text-[var(--color-text-secondary)]">模板列表加载失败</p>
          <Button size="sm" variant="outline" className="h-8 gap-1 text-xs" onClick={onRetry}>
            <RefreshCw size={12} /> 重试
          </Button>
        </div>
      ) : emptyKind !== null ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 px-8 text-center" data-testid="template-empty-state">
          <ListChecks size={24} className="text-[var(--color-text-tertiary)]" />
          {emptyKind === 'none' ? (
            <>
              <p className="text-sm text-[var(--color-text-secondary)]">{copy.TEMPLATE_EMPTY_TITLE}</p>
              <p className="text-xs text-[var(--color-text-tertiary)]">{copy.TEMPLATE_EMPTY_HINT}</p>
            </>
          ) : emptyKind === 'category' ? (
            <>
              <p className="text-sm text-[var(--color-text-secondary)]">
                {source === 'personal'
                  ? copy.TEMPLATE_PERSONAL_EMPTY_TITLE : copy.TEMPLATE_PRESET_EMPTY_TITLE}
              </p>
              <p className="text-xs text-[var(--color-text-tertiary)]">
                {source === 'personal'
                  ? copy.TEMPLATE_PERSONAL_EMPTY_HINT : copy.TEMPLATE_PRESET_EMPTY_HINT}
              </p>
            </>
          ) : (
            <>
              <p className="text-sm text-[var(--color-text-secondary)]">{copy.TEMPLATE_NOMATCH_TITLE}</p>
              <p className="text-xs text-[var(--color-text-tertiary)]">
                {copy.TEMPLATE_NOMATCH_HINT(keyword, sourceLabel)}
              </p>
            </>
          )}
        </div>
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            {templates.map(template => (
              <TemplateCard
                key={template.id}
                template={template}
                onSelect={onSelect}
                onView={onView}
                onEdit={onEdit}
                onDelete={setPendingDelete}
              />
            ))}
          </div>
        </div>
      )}

      <ConfirmDialog
        open={pendingDelete !== null}
        title={copy.TEMPLATE_DELETE_TITLE}
        description={copy.TEMPLATE_DELETE_DESC}
        confirmText="删除"
        variant="danger"
        loading={remove.isPending}
        onConfirm={() => {
          if (pendingDelete) remove.mutate(pendingDelete.id)
          setPendingDelete(null)
        }}
        onClose={() => setPendingDelete(null)}
      />
    </div>
  )
}

// ─── 模板卡：主体点击 → 既有激活表单；操作区四项互不冒泡 ───
function TemplateCard({ template, onSelect, onView, onEdit, onDelete }: {
  template: TemplateSummary
  onSelect: (template: TemplateSummary) => void
  onView: (template: TemplateSummary) => void
  onEdit: (template: TemplateSummary) => void
  onDelete: (template: TemplateSummary) => void
}) {
  const preset = isPresetTemplate(template)
  // 扫描顺序：名称 → 预置/个人及版本徽章 → 描述（两行）→ 元信息 → 操作区
  return (
    <div
      data-testid="template-card"
      className="flex flex-col overflow-hidden rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-sidebar)] text-left transition-all hover:border-[var(--color-primary)]/40 hover:shadow-sm"
    >
      {/* 主体快捷入口 = 激活（键盘等价路径：操作区「激活」按钮） */}
      <div
        className="flex flex-1 cursor-pointer flex-col gap-1.5 p-3.5"
        onClick={() => onSelect(template)}
      >
        <span className="min-w-0 truncate text-sm font-medium text-[var(--color-text-primary)]" title={template.name}>
          {template.name}
        </span>
        <div className="flex min-w-0 items-center gap-1.5">
          <span className={`shrink-0 rounded px-1.5 py-0.5 text-[11px] font-medium ${
            preset ? 'bg-brand-soft text-brand-ink' : 'bg-[var(--color-bg-hover)] text-[var(--color-text-secondary)]'
          }`}>
            {preset ? copy.TEMPLATE_SOURCE_PRESET : copy.TEMPLATE_SOURCE_PERSONAL}
          </span>
          <span className="shrink-0 rounded bg-[var(--color-bg-hover)] px-1.5 py-0.5 text-[10px] tabular-nums text-[var(--color-text-tertiary)]">
            v{template.latest_revision_no ?? '-'}
          </span>
        </div>
        <p className="line-clamp-2 text-xs leading-4 text-[var(--color-text-tertiary)]">
          {template.description || copy.TEMPLATE_UNTITLED_DESC}
        </p>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-[var(--color-text-tertiary)]">
          {template.node_count ? (
            <span className="tabular-nums">{template.node_count} 节点</span>
          ) : null}
          <span className="flex items-center gap-1 tabular-nums">
            <Play size={12} /> {template.instance_count} 次激活
          </span>
          {template.updated_at ? (
            <span>更新于 {formatRelativeTime(template.updated_at)}</span>
          ) : null}
        </div>
      </div>
      {/* 操作区：与主体分栏，点击/禁用不可能冒泡触发激活 */}
      <div className="flex flex-wrap items-center gap-0.5 border-t border-[var(--color-border)] px-2.5 py-1.5">
        <button
          type="button"
          onClick={() => onView(template)}
          className="inline-flex shrink-0 items-center gap-0.5 whitespace-nowrap rounded-lg px-1.5 py-1.5 text-[11px] font-medium text-[var(--color-text-primary)] transition-colors hover:bg-brand-soft hover:text-brand-ink focus-visible:ring-2 ring-ring"
        >
          <Eye size={12} /> {copy.TEMPLATE_VIEW_BUTTON}
        </button>
        <span title={preset ? copy.TEMPLATE_PRESET_EDIT_DISABLED : undefined}>
          <button
            type="button"
            disabled={preset}
            onClick={() => onEdit(template)}
            aria-label={preset
              ? `编辑模板 ${template.name}（${copy.TEMPLATE_PRESET_EDIT_DISABLED}）`
              : `编辑模板 ${template.name}`}
            className="inline-flex shrink-0 items-center gap-0.5 whitespace-nowrap rounded-lg px-1.5 py-1.5 text-[11px] font-medium text-[var(--color-text-primary)] transition-colors hover:bg-brand-soft hover:text-brand-ink focus-visible:ring-2 ring-ring disabled:pointer-events-none disabled:cursor-not-allowed disabled:text-[var(--color-text-tertiary)]"
          >
            <Pencil size={12} /> {copy.TEMPLATE_EDIT_BUTTON}
          </button>
        </span>
        <button
          type="button"
          onClick={() => onSelect(template)}
          aria-label={`激活模板 ${template.name}`}
          className="inline-flex shrink-0 items-center gap-0.5 whitespace-nowrap rounded-lg px-1.5 py-1.5 text-[11px] font-medium text-[var(--color-text-primary)] transition-colors hover:bg-brand-soft hover:text-brand-ink focus-visible:ring-2 ring-ring"
        >
          <Play size={12} /> {copy.TEMPLATE_ACTIVATE_BUTTON}
        </button>
        <span
          className="ml-auto shrink-0"
          title={preset
            ? copy.TEMPLATE_PRESET_DELETE_DISABLED
            : copy.TEMPLATE_DELETE_NAME(template.name)}
        >
          <button
            type="button"
            disabled={preset}
            onClick={() => onDelete(template)}
            aria-label={preset
              ? `${copy.TEMPLATE_DELETE_NAME(template.name)}（${copy.TEMPLATE_PRESET_DELETE_DISABLED}）`
              : copy.TEMPLATE_DELETE_NAME(template.name)}
            className="rounded-lg p-1.5 text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)] focus-visible:ring-2 ring-ring disabled:pointer-events-none disabled:cursor-not-allowed disabled:text-[var(--color-text-tertiary)] disabled:hover:bg-transparent"
          >
            <Trash2 size={14} />
          </button>
        </span>
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════════════════════
// 子组件：模板只读详情（查看；无任何保存/提交副作用）
// ═══════════════════════════════════════════════════════════
function TemplateDetailView({ template, detail, loading, error, onBack }: {
  template: TemplateSummary
  detail: TemplateDetail | undefined
  loading: boolean
  error: boolean
  onBack: () => void
}) {
  const [showYaml, setShowYaml] = useState(false)
  const preset = isPresetTemplate(template)
  return (
    <>
      <div className="flex shrink-0 items-center gap-2 border-b border-[var(--color-border)] px-4 py-3">
        <button
          type="button"
          onClick={onBack}
          className="inline-flex h-8 items-center gap-1 rounded-md px-2 text-xs hover:bg-[var(--color-bg-hover)] focus-visible:ring-2 ring-ring"
        >
          <ArrowLeft size={14} /> {copy.RETURN_TO_TEMPLATES}
        </button>
        <span className="truncate text-sm font-semibold text-[var(--color-text-primary)]">
          {copy.TEMPLATE_VIEW_TITLE}「{template.name}」
        </span>
      </div>
      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-5" data-testid="template-detail-view">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-base font-semibold text-[var(--color-text-primary)]">{template.name}</span>
          <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${
            preset ? 'bg-brand-soft text-brand-ink' : 'bg-[var(--color-bg-hover)] text-[var(--color-text-secondary)]'
          }`}>
            {preset ? copy.TEMPLATE_SOURCE_PRESET : copy.TEMPLATE_SOURCE_PERSONAL}
          </span>
          <span className="rounded bg-[var(--color-bg-hover)] px-1.5 py-0.5 text-[11px] tabular-nums text-[var(--color-text-tertiary)]">
            v{template.latest_revision_no ?? '-'}
          </span>
        </div>
        <p className="text-sm leading-6 text-[var(--color-text-secondary)]">
          {template.description || copy.TEMPLATE_UNTITLED_DESC}
        </p>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-[var(--color-text-tertiary)]">
          {detail?.node_count ? <span className="tabular-nums">{detail.node_count} 节点</span> : null}
          <span className="tabular-nums">{template.instance_count} 次激活</span>
          {template.updated_at ? <span>更新于 {formatRelativeTime(template.updated_at)}</span> : null}
          {template.created_at ? <span>创建于 {formatRelativeTime(template.created_at)}</span> : null}
        </div>

        <div>
          <p className="mb-2 text-xs font-medium text-[var(--color-text-primary)]">流程结构（只读）</p>
          {loading ? (
            <div className="flex h-72 items-center justify-center rounded-lg border border-dashed border-[var(--color-border)] text-xs text-[var(--color-text-tertiary)]">
              流程结构加载中…
            </div>
          ) : error ? (
            <div className="flex h-72 items-center justify-center rounded-lg border border-dashed border-[var(--color-border)] text-xs text-[var(--color-text-tertiary)]">
              流程结构加载失败，可返回后重试
            </div>
          ) : detail?.spec_compiled?.nodes && Object.keys(detail.spec_compiled.nodes).length ? (
            <div data-testid="template-detail-graph">
              <WorkflowPreviewGraph spec={detail.spec_compiled} />
            </div>
          ) : (
            <div className="flex h-72 items-center justify-center rounded-lg border border-dashed border-[var(--color-border)] text-xs text-[var(--color-text-tertiary)]">
              该模板暂无节点结构数据
            </div>
          )}
        </div>

        <div>
          <button
            type="button"
            onClick={() => setShowYaml(previous => !previous)}
            aria-expanded={showYaml}
            className="flex items-center gap-1.5 text-xs font-medium text-[var(--color-text-primary)] focus-visible:ring-2 ring-ring"
          >
            {showYaml ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            {copy.TEMPLATE_YAML_SOURCE}
          </button>
          {showYaml ? (
            <pre className="mt-2 max-h-72 overflow-auto rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-hover)] p-3 font-mono text-xs leading-5 text-[var(--color-text-secondary)]">
              {detail?.spec_yaml ?? '暂无 YAML 原文'}
            </pre>
          ) : null}
        </div>
      </div>
    </>
  )
}

// ═══════════════════════════════════════════════════════════
// 子组件：激活表单（字段前置；流程结构作为确认区可折叠）
// ═══════════════════════════════════════════════════════════
function ActivateForm({ template, previewSpec, previewLoading, previewError, instanceName, setInstanceName, goal, setGoal, onBack, onActivate, loading }: {
  template: TemplateSummary
  previewSpec: WorkflowSpecSnapshot | null | undefined
  previewLoading: boolean
  previewError: boolean
  instanceName: string
  setInstanceName: (value: string) => void
  goal: string
  setGoal: (value: string) => void
  onBack: () => void
  onActivate: () => void
  loading: boolean
}) {
  const [showGraph, setShowGraph] = useState(true)
  return (
    <>
      <div className="flex shrink-0 items-center gap-2 border-b border-[var(--color-border)] px-4 py-3">
        <button
          type="button"
          onClick={onBack}
          className="inline-flex h-8 items-center gap-1 rounded-md px-2 text-xs hover:bg-[var(--color-bg-hover)] focus-visible:ring-2 ring-ring"
        >
          <ArrowLeft size={14} /> {copy.RETURN_TO_TEMPLATES}
        </button>
        <span className="text-sm font-semibold text-[var(--color-text-primary)]">
          激活「{template.name}」
        </span>
      </div>
      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-5">
        <div>
          <label htmlFor="ti-instance-name" className="mb-1.5 block text-xs font-medium text-[var(--color-text-primary)]">
            实例名称 <span className="text-[var(--color-danger)]">*</span>
          </label>
          <Input
            id="ti-instance-name"
            required maxLength={200}
            value={instanceName}
            onChange={event => setInstanceName(event.target.value)}
            placeholder={copy.TEMPLATE_NAME_PLACEHOLDER}
          />
        </div>
        <div>
          <label htmlFor="ti-instance-goal" className="mb-1.5 block text-xs font-medium text-[var(--color-text-primary)]">
            任务目标 <span className="text-[var(--color-danger)]">*</span>
          </label>
          <textarea
            id="ti-instance-goal"
            className="min-h-28 w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-3 py-2 text-sm leading-6 focus-visible:ring-2 ring-ring"
            value={goal}
            onChange={event => setGoal(event.target.value)}
            placeholder={copy.TEMPLATE_GOAL_PLACEHOLDER}
          />
          <p className="mt-1.5 text-[11px] leading-5 text-[var(--color-text-tertiary)]">
            目标描述越具体，Agent 产出质量越高。可包含背景、约束、期望产出格式等。
          </p>
        </div>
        <div>
          <button
            type="button"
            onClick={() => setShowGraph(previous => !previous)}
            className="flex w-full items-center gap-1.5 text-xs font-medium text-[var(--color-text-primary)] focus-visible:ring-2 ring-ring"
            aria-expanded={showGraph}
          >
            {showGraph ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            流程结构
            <span className="font-normal text-[var(--color-text-tertiary)]">
              （只读 · 激活前确认节点编排）
            </span>
          </button>
          {showGraph ? (
            <div className="mt-2">
              {previewLoading ? (
                <div className="flex h-72 items-center justify-center rounded-lg border border-dashed border-[var(--color-border)] text-xs text-[var(--color-text-tertiary)]">
                  流程结构加载中…
                </div>
              ) : previewError ? (
                <div className="flex h-72 items-center justify-center rounded-lg border border-dashed border-[var(--color-border)] text-xs text-[var(--color-text-tertiary)]">
                  流程结构加载失败，可返回后重试
                </div>
              ) : previewSpec?.nodes && Object.keys(previewSpec.nodes).length ? (
                <div data-testid="template-graph-preview">
                  <WorkflowPreviewGraph spec={previewSpec} />
                </div>
              ) : (
                <div className="flex h-72 items-center justify-center rounded-lg border border-dashed border-[var(--color-border)] text-xs text-[var(--color-text-tertiary)]">
                  该模板暂无节点结构数据
                </div>
              )}
            </div>
          ) : null}
        </div>
      </div>
      <div className="flex shrink-0 justify-end border-t border-[var(--color-border)] px-5 py-3.5">
        <Button
          onClick={onActivate}
          disabled={loading || !instanceName.trim() || !goal.trim()}
          className="h-9 min-w-28 rounded-lg px-4 text-xs font-medium"
        >
          {loading ? copy.ACTIVATE_RUNNING : copy.ACTIVATE_BUTTON}
        </Button>
      </div>
    </>
  )
}

// ═══════════════════════════════════════════════════════════
// 子组件：实例轻量详情（看板卡片就地处置；重操作与时间线走独立详情页）
// ═══════════════════════════════════════════════════════════
function InstanceLiteView({ instanceId, onBack, onOpenFull, onReactivate }: {
  instanceId: string
  onBack: () => void
  onOpenFull: (id: string) => void
  onReactivate: (detail: InstanceDetail) => 'ok' | 'loading' | 'missing'
}) {
  const queryClient = useQueryClient()
  const [humanOutput, setHumanOutput] = useState('{}')
  // 审批与人工是两个独立表单，理由分别持有（共用会在双等待时串值）
  const [approvalReason, setApprovalReason] = useState('')
  const [humanRejectReason, setHumanRejectReason] = useState('')

  const instance = useQuery({
    queryKey: ['task-instances', 'instance', instanceId],
    queryFn: () => taskInstancesApi.getInstance(instanceId),
    // 运行中轻量轮询收口（重实时性走独立详情页 SSE）
    refetchInterval: query => {
      const status = query.state.data?.status
      return status === 'active' || status === 'cancelling' ? 5000 : false
    },
  })

  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['task-instances', 'instance', instanceId] })
    void queryClient.invalidateQueries({ queryKey: ['task-instances', 'instances'] })
  }, [queryClient, instanceId])

  const decide = useMutation({
    mutationFn: (input: { approvalId: string; decision: 'approved' | 'rejected'; reason?: string }) =>
      taskInstancesApi.decideApproval(
        instanceId, waitingNodesOf(instance.data).approval[0]?.node_id ?? '',
        input.approvalId, input.decision, input.reason),
    onSuccess: () => {
      toast.success(copy.DECIDED_TOAST)
      setApprovalReason('')
      refresh()
    },
    onError: error => toast.error(apiError(error)),
  })

  const submit = useMutation({
    mutationFn: () => {
      let output: Record<string, unknown>
      try {
        output = JSON.parse(humanOutput) as Record<string, unknown>
      } catch {
        throw new Error(copy.HUMAN_OUTPUT_INVALID)
      }
      return taskInstancesApi.humanSubmit(
        instanceId, waitingNodesOf(instance.data).human[0]?.node_id ?? '', output)
    },
    onSuccess: () => {
      toast.success(copy.HUMAN_SUBMITTED_TOAST)
      setHumanOutput('{}')
      refresh()
    },
    onError: error => toast.error(apiError(error)),
  })

  const reject = useMutation({
    mutationFn: () => taskInstancesApi.humanReject(
      instanceId, waitingNodesOf(instance.data).human[0]?.node_id ?? '',
      humanRejectReason),
    onSuccess: () => {
      toast.success(copy.HUMAN_REJECTED_TOAST)
      setHumanRejectReason('')
      refresh()
    },
    onError: error => toast.error(apiError(error)),
  })

  if (instance.isLoading) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center text-xs text-[var(--color-text-tertiary)]">
        加载中…
      </div>
    )
  }
  const detail = instance.data
  if (!detail) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center text-xs text-[var(--color-text-tertiary)]">
        实例不存在或已被删除
      </div>
    )
  }

  const waiting = waitingNodesOf(detail)
  const approvalNode = waiting.approval[0]
  const approval = approvalNode
    ? detail.approvals.find(item => item.node_run_id === approvalNode.node_run_id
      && item.status === 'pending')
    : undefined
  const humanNode = waiting.human[0]
  const isFailed = detail.status === 'failed'
  const reactivateHint = isFailed && !approvalNode && !humanNode

  return (
    <div className="min-h-0 flex-1 overflow-y-auto" data-testid="task-instance-lite">
      <div className="sticky top-0 z-10 flex shrink-0 items-center gap-2 border-b border-[var(--color-border)] bg-[var(--color-bg-sidebar)] px-4 py-3">
        <button
          type="button"
          onClick={onBack}
          className="inline-flex h-8 shrink-0 items-center gap-1 rounded-md px-2 text-xs hover:bg-[var(--color-bg-hover)] focus-visible:ring-2 ring-ring"
        >
          <ArrowLeft size={14} /> {copy.RETURN_TO_BOARD}
        </button>
        <span className="min-w-0 truncate text-sm font-semibold text-[var(--color-text-primary)]">
          {detail.name}
        </span>
        <span className={`shrink-0 rounded px-1.5 py-0.5 text-[11px] ${INSTANCE_STATUS_TONE[detail.status] ?? ''}`}>
          {INSTANCE_STATUS_LABELS[detail.status] ?? detail.status}
        </span>
        <button
          type="button"
          onClick={() => onOpenFull(detail.id)}
          className="ml-auto inline-flex h-8 shrink-0 items-center gap-1 rounded-md px-2 text-xs text-[var(--color-primary)] hover:bg-[var(--color-bg-hover)] focus-visible:ring-2 ring-ring"
        >
          {copy.OPEN_FULL_DETAIL} <ExternalLink size={12} />
        </button>
      </div>

      <div className="space-y-4 p-5">
        <div>
          <div className="text-[11px] tabular-nums text-[var(--color-text-tertiary)]">
            {instanceCardTimeLabel(detail)}
          </div>
          <p className="mt-1 whitespace-pre-wrap text-sm text-[var(--color-text-secondary)]">
            {detail.goal || copy.TEMPLATE_UNTITLED_GOAL}
          </p>
          {detail.fail_reason ? <FailureNotice failReason={detail.fail_reason} /> : null}
          {detail.cancel_reason ? (
            <p className="mt-1.5 rounded bg-[var(--color-bg-hover)] px-2 py-1 text-xs text-[var(--color-text-tertiary)]">
              取消原因：{detail.cancel_reason}
            </p>
          ) : null}
        </div>

        <div className="space-y-1.5">
          <div className="text-xs font-medium text-[var(--color-text-primary)]">流程快照</div>
          <WorkflowPreviewGraph spec={detail.spec_snapshot} nodes={detail.nodes} />
        </div>

        {approvalNode && approval ? (
          <div className="space-y-2 rounded-lg border border-[var(--color-border)] p-2.5">
            <div className="flex items-center gap-2 text-xs">
              <CheckCircle2 size={14} className="text-[var(--color-warning)]" />
              <span className="font-medium text-[var(--color-text-primary)]">
                {copy.APPROVAL_SECTION_TITLE}
              </span>
              <span className="font-mono text-[11px] text-[var(--color-text-tertiary)]">
                节点 {approvalNode.node_id}
              </span>
            </div>
            <ProposalCard proposal={approval.proposal} />
            <Input
              aria-label={copy.REJECT_INPUT_ARIA}
              placeholder={copy.REJECT_INPUT_PLACEHOLDER}
              value={approvalReason}
              onChange={event => setApprovalReason(event.target.value)}
            />
            <div className="flex gap-2">
              <Button size="sm" className="flex-1 gap-1.5"
                onClick={() => decide.mutate({ approvalId: approval.id, decision: 'approved' })}>
                <CheckCircle2 size={14} />{copy.APPROVE_BUTTON}
              </Button>
              <Button size="sm" variant="outline" className="flex-1 gap-1.5 text-[var(--color-danger)]"
                onClick={() => decide.mutate({ approvalId: approval.id, decision: 'rejected', reason: approvalReason })}>
                <ThumbsDown size={14} />{copy.REJECT_BUTTON}
              </Button>
            </div>
          </div>
        ) : null}

        {humanNode ? (
          <div className="space-y-2 rounded-lg border border-[var(--color-border)] p-2.5">
            <div className="flex items-center gap-2 text-xs">
              <span className="font-medium text-[var(--color-text-primary)]">
                {copy.HUMAN_SECTION_TITLE}
              </span>
              <span className="font-mono text-[11px] text-[var(--color-text-tertiary)]">
                节点 {humanNode.node_id} · 尝试 #{humanNode.attempt_no}
              </span>
            </div>
            <textarea
              aria-label={copy.HUMAN_OUTPUT_ARIA}
              className="min-h-20 w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-3 py-2 font-mono text-xs focus-visible:ring-2 ring-ring"
              value={humanOutput}
              onChange={event => setHumanOutput(event.target.value)}
              placeholder={copy.HUMAN_OUTPUT_PLACEHOLDER}
            />
            <Input
              aria-label={copy.HUMAN_REJECT_INPUT_ARIA}
              placeholder={copy.HUMAN_REJECT_INPUT_PLACEHOLDER}
              value={humanRejectReason}
              onChange={event => setHumanRejectReason(event.target.value)}
            />
            <div className="flex gap-2">
              <Button size="sm" className="flex-1 gap-1.5" onClick={() => submit.mutate()}>
                <Send size={14} />{copy.HUMAN_SUBMIT_BUTTON}
              </Button>
              <Button size="sm" variant="outline" className="flex-1 gap-1.5 text-[var(--color-danger)]" onClick={() => reject.mutate()}>
                <ThumbsDown size={14} />{copy.REJECT_BUTTON}
              </Button>
            </div>
          </div>
        ) : null}

        {isTerminalStatus(detail.status) && !approvalNode && !humanNode ? (
          <div className="space-y-2 rounded-lg border border-dashed border-[var(--color-border)] p-3 text-xs text-[var(--color-text-tertiary)]">
            <p>{copy.TERMINAL_NO_ACTION}</p>
            {reactivateHint ? (
              <Button
                size="sm" className="h-8 gap-1.5 text-xs"
                onClick={() => {
                  const result = onReactivate(detail)
                  if (result === 'loading') {
                    toast.info('模板列表加载中，请稍后重试')
                  } else if (result === 'missing') {
                    toast.info('原模板已更新或删除，请在流程模板中选择模板重新激活')
                  }
                }}
              >
                <RefreshCw size={12} /> {copy.REACTIVATE_BUTTON}
              </Button>
            ) : null}
            <p>{copy.FULL_PAGE_HINT}</p>
          </div>
        ) : null}
      </div>
    </div>
  )
}
