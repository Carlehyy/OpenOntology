/**
 * 任务实例弹窗 — 超级助手工作台的统一入口。
 *
 * 布局（ScheduledTasksDialog 同款两栏模式）：
 * 左栏：视图导航（实例看板 | 流程模板），导航归属见 dialogNavOf
 * 右栏：当前视图内容（看板/实例轻量详情/模板列表/激活表单/画布编辑器；
 * 模板列表头部承载「新建流程」入口，激活表单内嵌只读流程结构预览）
 * 全程不离开超级助手 HashRouter 上下文。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft, CheckCircle2, ClipboardList, ExternalLink, LayoutGrid, ListChecks,
  Loader2, Play, Plus, Send, ThumbsDown, XCircle,
} from 'lucide-react'
import { toast } from 'sonner'
import {
  apiError, taskInstancesApi,
  type TemplateSummary, type InstanceListItem, type WorkflowSpecSnapshot,
} from '@/api/taskInstances'
import { DialogShell } from './AssistantConfiguration'
import { Input } from '@/components/ui/Input'
import { Button } from '@/components/ui/Button'
import {
  BOARD_GROUPS, INSTANCE_STATUS_LABELS, INSTANCE_STATUS_TONE, NODE_KIND_LABELS,
  dialogNavOf, instanceGroup, isTerminalStatus, waitingNodesOf,
  type TaskDialogView,
} from '@/components/task-instances/instanceView'
import WorkflowPreviewGraph from '@/components/task-instances/WorkflowPreviewGraph'
import { formatDateTime } from '@/utils/datetime'
import WorkflowCanvas from './WorkflowCanvas'

// ─── 视图类型 ───
type View = TaskDialogView

interface TaskInstancesDialogProps {
  open: boolean
  onClose: () => void
}

// ─── 状态图标 ───
function StatusIcon({ status }: { status: string }) {
  if (status === 'completed') return <CheckCircle2 size={14} className="text-[var(--color-success)]" />
  if (['failed', 'cancelled'].includes(status)) return <XCircle size={14} className="text-[var(--color-danger)]" />
  if (status === 'active') return <Loader2 size={14} className="animate-spin text-brand-ink" />
  return <ClipboardList size={14} className="text-[var(--color-text-tertiary)]" />
}

export default function TaskInstancesDialog({ open, onClose }: TaskInstancesDialogProps) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [view, setView] = useState<View>('board')
  const [selectedTemplate, setSelectedTemplate] = useState<TemplateSummary | null>(null)
  const [selectedInstanceId, setSelectedInstanceId] = useState<string | null>(null)
  const [instanceName, setInstanceName] = useState('')
  const [goal, setGoal] = useState('')

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
  // 激活视图的模板详情（只读 DAG 预览数据源）
  const templateDetail = useQuery({
    queryKey: ['task-instances', 'template', selectedTemplate?.id],
    queryFn: () => taskInstancesApi.getTemplate(selectedTemplate?.id as string),
    enabled: view === 'activate' && Boolean(selectedTemplate),
  })

  // 关闭时重置
  useEffect(() => {
    if (!open) {
      setView('board'); setSelectedTemplate(null); setSelectedInstanceId(null)
      setInstanceName(''); setGoal('')
    }
  }, [open])

  // ─── 激活实例 ───
  const activate = useMutation({
    mutationFn: () => taskInstancesApi.activateInstance(
      selectedTemplate?.id as string,
      { name: instanceName, goal },
      `dialog-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`),
    onSuccess: () => {
      toast.success('实例已激活')
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

  const openFullDetail = useCallback((id: string) => {
    onClose()
    navigate(`/task-instances/instances/${id}`)
  }, [navigate, onClose])

  const grouped = useMemo(() => {
    const map = new Map<string, InstanceListItem[]>(
      BOARD_GROUPS.map(g => [g.key, []]))
    for (const item of instances.data?.items ?? []) {
      map.get(instanceGroup(item))?.push(item)
    }
    return map
  }, [instances.data])

  if (!open) return null

  const navKey = dialogNavOf(view)

  return (
    <DialogShell
      title="任务实例"
      description="DAG 流程模板 → 契约验收的自动化任务轨道"
      size="wide"
      onClose={onClose}
      icon={<ListChecks size={18} />}
      contentClassName="h-[min(82dvh,44rem)]"
    >
      <div className="flex min-h-0 flex-1" data-testid="task-instances-dialog">
        {/* ─── 左栏：视图导航 ─── */}
        <nav aria-label="任务实例视图" className="flex w-48 shrink-0 flex-col border-r border-[var(--color-border)]">
          <div className="p-2">
            <button
              type="button"
              onClick={backToBoard}
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
              onClick={() => setView('templates')}
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
          {view === 'board' && <BoardView grouped={grouped} total={instances.data?.total ?? 0} onRefresh={() => void instances.refetch()} onSelectInstance={handleSelectInstance} />}
          {view === 'instance' && selectedInstanceId && (
            <InstanceLiteView
              instanceId={selectedInstanceId}
              onBack={backToBoard}
              onOpenFull={openFullDetail}
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
              templates={templates.data ?? []}
              loading={templates.isLoading}
              onNewFlow={() => setView('canvas')}
              onSelect={handleSelectTemplate}
            />
          )}
          {view === 'canvas' && (
            <WorkflowCanvas onClose={() => setView('templates')} />
          )}
        </div>
      </div>
    </DialogShell>
  )
}

// ═══════════════════════════════════════════════════════════
// 子组件：实例看板
// ═══════════════════════════════════════════════════════════
function BoardView({ grouped, total, onRefresh, onSelectInstance }: {
  grouped: Map<string, InstanceListItem[]>
  total: number
  onRefresh: () => void
  onSelectInstance: (id: string) => void
}) {
  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-4" data-testid="task-instances-board">
      <div className="mb-3 flex items-center justify-between">
        <span className="text-xs text-[var(--color-text-tertiary)]">
          共 {total} 个实例 · 8 秒自动刷新
        </span>
        <Button variant="outline" size="sm" className="h-7 gap-1 px-2 text-[11px]" onClick={onRefresh}>
          <Loader2 size={12} /> 刷新
        </Button>
      </div>
      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        {BOARD_GROUPS.map(group => {
          const items = grouped.get(group.key) ?? []
          return (
            <div key={group.key} className="space-y-1.5">
              <div className="flex items-center gap-1.5 px-1 text-xs font-medium text-[var(--color-text-secondary)]">
                {group.label}
                <span className="rounded-full bg-[var(--color-bg-hover)] px-1.5 text-[10px] tabular-nums text-[var(--color-text-tertiary)]">
                  {items.length}
                </span>
              </div>
              {items.map(item => (
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
                    <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] ${INSTANCE_STATUS_TONE[item.status] ?? ''}`}>
                      {INSTANCE_STATUS_LABELS[item.status] ?? item.status}
                    </span>
                  </div>
                  <div className="mt-1 flex items-center gap-1.5">
                    <StatusIcon status={item.status} />
                    <span className="truncate text-[10px] text-[var(--color-text-tertiary)]">
                      {item.goal || '（无目标描述）'}
                    </span>
                  </div>
                </button>
              ))}
              {!items.length && (
                <div className="rounded-lg border border-dashed border-[var(--color-border)] py-6 text-center text-[10px] text-[var(--color-text-tertiary)]">
                  暂无
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════════════════════
// 子组件：模板列表面板
// ═══════════════════════════════════════════════════════════
function TemplatesPanel({ templates, loading, onNewFlow, onSelect }: {
  templates: TemplateSummary[]
  loading: boolean
  onNewFlow: () => void
  onSelect: (template: TemplateSummary) => void
}) {
  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="task-instances-templates">
      <div className="flex shrink-0 items-center justify-between gap-2 border-b border-[var(--color-border)] px-4 py-2.5">
        <span className="text-xs text-[var(--color-text-tertiary)]">共 {templates.length} 个模板</span>
        <Button size="sm" className="h-8 gap-1.5 text-xs" onClick={onNewFlow} data-testid="new-flow-button">
          <Plus size={14} /> 新建流程
        </Button>
      </div>
      {loading ? (
        <div className="px-4 py-12 text-center text-xs text-[var(--color-text-tertiary)]">加载中…</div>
      ) : !templates.length ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 px-8 text-center">
          <ListChecks size={24} className="text-[var(--color-text-tertiary)]" />
          <p className="text-sm text-[var(--color-text-secondary)]">还没有流程模板</p>
          <p className="text-xs text-[var(--color-text-tertiary)]">
            点击右上角「新建流程」用画布创建第一个自动化流程
          </p>
        </div>
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            {templates.map(template => (
              <button
                key={template.id}
                type="button"
                onClick={() => onSelect(template)}
                className="w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-sidebar)] p-3.5 text-left transition-all hover:border-[var(--color-primary)]/40 hover:shadow-sm focus-visible:ring-2 ring-ring"
              >
                <div className="flex items-start justify-between gap-2">
                  <span className="min-w-0 truncate text-sm font-medium text-[var(--color-text-primary)]">
                    {template.name}
                  </span>
                  <span className="shrink-0 rounded bg-[var(--color-bg-hover)] px-1.5 py-0.5 text-[10px] tabular-nums text-[var(--color-text-tertiary)]">
                    v{template.latest_revision_no ?? '-'}
                  </span>
                </div>
                <p className="mt-1 line-clamp-2 text-xs leading-4 text-[var(--color-text-tertiary)]">
                  {template.description || '（无描述）'}
                </p>
                <div className="mt-2.5 flex items-center gap-3 text-[10px] text-[var(--color-text-tertiary)]">
                  <span className="flex items-center gap-1">
                    <Play size={11} /> {template.instance_count} 次激活
                  </span>
                </div>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// ═══════════════════════════════════════════════════════════
// 子组件：激活表单
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
  return (
    <>
      <div className="flex shrink-0 items-center gap-2 border-b border-[var(--color-border)] px-4 py-3">
        <button
          type="button"
          onClick={onBack}
          className="inline-flex h-8 items-center gap-1 rounded-md px-2 text-xs hover:bg-[var(--color-bg-hover)] focus-visible:ring-2 ring-ring"
        >
          <ArrowLeft size={13} /> 返回
        </button>
        <span className="text-sm font-semibold text-[var(--color-text-primary)]">
          激活「{template.name}」
        </span>
      </div>
      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-5">
        <div>
          <div className="mb-1.5 text-xs font-medium text-[var(--color-text-primary)]">
            流程结构
            <span className="ml-1.5 font-normal text-[var(--color-text-tertiary)]">
              （只读 · 激活前确认节点编排）
            </span>
          </div>
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
        <div>
          <label htmlFor="ti-instance-name" className="mb-1.5 block text-xs font-medium text-[var(--color-text-primary)]">
            实例名称 <span className="text-[var(--color-danger)]">*</span>
          </label>
          <Input
            id="ti-instance-name"
            required maxLength={200}
            value={instanceName}
            onChange={event => setInstanceName(event.target.value)}
            placeholder="例如：登录页改版评审-1023"
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
            placeholder="这次任务要完成什么？agent 节点的执行以此为骨干……"
          />
          <p className="mt-1.5 text-[11px] leading-5 text-[var(--color-text-tertiary)]">
            目标描述越具体，agent 产出质量越高。可包含背景、约束、期望产出格式等。
          </p>
        </div>
      </div>
      <div className="flex shrink-0 justify-end border-t border-[var(--color-border)] px-5 py-3.5">
        <Button
          onClick={onActivate}
          disabled={loading || !instanceName.trim() || !goal.trim()}
          className="h-9 min-w-28 rounded-lg bg-brand px-4 text-xs font-medium text-white hover:bg-brand-deep disabled:opacity-50"
        >
          {loading ? '激活中…' : '激活实例'}
        </Button>
      </div>
    </>
  )
}

// ═══════════════════════════════════════════════════════════
// 子组件：实例轻量详情（看板卡片就地处置；重操作与时间线走独立详情页）
// ═══════════════════════════════════════════════════════════
function InstanceLiteView({ instanceId, onBack, onOpenFull }: {
  instanceId: string
  onBack: () => void
  onOpenFull: (id: string) => void
}) {
  const queryClient = useQueryClient()
  const [humanOutput, setHumanOutput] = useState('{}')
  const [rejectReason, setRejectReason] = useState('')

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
      toast.success('审批决定已提交')
      setRejectReason('')
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
        throw new Error('交活产出必须是合法 JSON 对象')
      }
      return taskInstancesApi.humanSubmit(
        instanceId, waitingNodesOf(instance.data).human[0]?.node_id ?? '', output)
    },
    onSuccess: () => {
      toast.success('已交活')
      setHumanOutput('{}')
      refresh()
    },
    onError: error => toast.error(apiError(error)),
  })

  const reject = useMutation({
    mutationFn: () => taskInstancesApi.humanReject(
      instanceId, waitingNodesOf(instance.data).human[0]?.node_id ?? '',
      rejectReason),
    onSuccess: () => {
      toast.success('已打回上游，等待重做')
      setRejectReason('')
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

  return (
    <div className="min-h-0 flex-1 overflow-y-auto" data-testid="task-instance-lite">
      <div className="sticky top-0 z-10 flex shrink-0 items-center gap-2 border-b border-[var(--color-border)] bg-[var(--color-bg-sidebar)] px-4 py-3">
        <button
          type="button"
          onClick={onBack}
          className="inline-flex h-8 shrink-0 items-center gap-1 rounded-md px-2 text-xs hover:bg-[var(--color-bg-hover)] focus-visible:ring-2 ring-ring"
        >
          <ArrowLeft size={13} /> 返回看板
        </button>
        <span className="min-w-0 truncate text-sm font-semibold text-[var(--color-text-primary)]">
          {detail.name}
        </span>
        <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] ${INSTANCE_STATUS_TONE[detail.status] ?? ''}`}>
          {INSTANCE_STATUS_LABELS[detail.status] ?? detail.status}
        </span>
        <button
          type="button"
          onClick={() => onOpenFull(detail.id)}
          className="ml-auto inline-flex h-8 shrink-0 items-center gap-1 rounded-md px-2 text-xs text-[var(--color-primary)] hover:bg-[var(--color-bg-hover)] focus-visible:ring-2 ring-ring"
        >
          查看完整详情 <ExternalLink size={12} />
        </button>
      </div>

      <div className="space-y-4 p-5">
        <div>
          <div className="text-xs text-[var(--color-text-tertiary)]">
            目标 · 创建于 {formatDateTime(detail.created_at)}
          </div>
          <p className="mt-1 whitespace-pre-wrap text-sm text-[var(--color-text-secondary)]">
            {detail.goal || '（无目标描述）'}
          </p>
          {detail.fail_reason ? (
            <p className="mt-1.5 rounded bg-[var(--color-bg-danger)] px-2 py-1 text-xs text-[var(--color-danger)]">
              失败原因：{detail.fail_reason}
            </p>
          ) : null}
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
                审批关口 {approvalNode.node_id}
              </span>
            </div>
            <pre className="max-h-32 overflow-auto rounded bg-[var(--color-bg-hover)] p-2 font-mono text-[10px] text-[var(--color-text-secondary)]">
              {JSON.stringify(approval.proposal, null, 2)}
            </pre>
            <Input
              aria-label="驳回理由（驳回时必填）"
              placeholder="驳回理由（驳回时必填，将随打回送达上游）"
              value={rejectReason}
              onChange={event => setRejectReason(event.target.value)}
            />
            <div className="flex gap-2">
              <Button size="sm" className="flex-1 gap-1.5"
                onClick={() => decide.mutate({ approvalId: approval.id, decision: 'approved' })}>
                <CheckCircle2 size={14} />批准
              </Button>
              <Button size="sm" variant="outline" className="flex-1 gap-1.5 text-[var(--color-danger)]"
                onClick={() => decide.mutate({ approvalId: approval.id, decision: 'rejected', reason: rejectReason })}>
                <ThumbsDown size={14} />驳回并打回
              </Button>
            </div>
          </div>
        ) : null}

        {humanNode ? (
          <div className="space-y-2 rounded-lg border border-[var(--color-border)] p-2.5">
            <div className="text-xs font-medium text-[var(--color-text-primary)]">
              人工待办 {humanNode.node_id}
              <span className="ml-1.5 font-normal text-[var(--color-text-tertiary)]">
                {NODE_KIND_LABELS.human} · 尝试#{humanNode.attempt_no}
              </span>
            </div>
            <textarea
              aria-label="交活产出 JSON"
              className="min-h-20 w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-3 py-2 font-mono text-xs focus-visible:ring-2 ring-ring"
              value={humanOutput}
              onChange={event => setHumanOutput(event.target.value)}
              placeholder='交活产出（JSON 对象，须满足端口契约）'
            />
            <Input
              aria-label="打回理由"
              placeholder="打回直接上游的理由（可选操作）"
              value={rejectReason}
              onChange={event => setRejectReason(event.target.value)}
            />
            <div className="flex gap-2">
              <Button size="sm" className="flex-1 gap-1.5" onClick={() => submit.mutate()}>
                <Send size={14} />交活
              </Button>
              <Button size="sm" variant="outline" className="flex-1 gap-1.5 text-[var(--color-danger)]" onClick={() => reject.mutate()}>
                <ThumbsDown size={14} />打回上游
              </Button>
            </div>
          </div>
        ) : null}

        {isTerminalStatus(detail.status) && !approvalNode && !humanNode ? (
          <p className="text-xs text-[var(--color-text-tertiary)]">实例已结束，无待处理事项</p>
        ) : null}
      </div>
    </div>
  )
}
