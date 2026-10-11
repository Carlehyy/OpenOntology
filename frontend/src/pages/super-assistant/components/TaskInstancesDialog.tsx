/**
 * 任务实例弹窗 — 超级助手工作台的统一入口。
 *
 * 布局（ScheduledTasksDialog 同款两栏模式）：
 * 左栏：视图导航（实例看板 | 模板）+ 操作按钮
 * 右栏：当前视图内容（看板/模板列表/激活表单/画布编辑器）
 * 全程不离开超级助手 HashRouter 上下文。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft, Bot, CheckCircle2, ClipboardList, LayoutGrid, ListChecks,
  Loader2, Play, Plus, Settings2, User, XCircle,
} from 'lucide-react'
import { toast } from 'sonner'
import {
  apiError, taskInstancesApi,
  type TemplateSummary, type InstanceListItem,
} from '@/api/taskInstances'
import { DialogShell } from './AssistantConfiguration'
import { Input } from '@/components/ui/Input'
import { Button } from '@/components/ui/Button'
import {
  BOARD_GROUPS, INSTANCE_STATUS_LABELS, INSTANCE_STATUS_TONE, instanceGroup,
} from '@/components/task-instances/instanceView'
import WorkflowCanvas from './WorkflowCanvas'

// ─── 视图类型 ───
type View = 'board' | 'templates' | 'activate' | 'canvas'

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
  const [view, setView] = useState<View>('board')
  const [selectedTemplate, setSelectedTemplate] = useState<TemplateSummary | null>(null)
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

  // 关闭时重置
  useEffect(() => {
    if (!open) {
      setView('board'); setSelectedTemplate(null)
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

  const grouped = useMemo(() => {
    const map = new Map<string, InstanceListItem[]>(
      BOARD_GROUPS.map(g => [g.key, []]))
    for (const item of instances.data?.items ?? []) {
      map.get(instanceGroup(item))?.push(item)
    }
    return map
  }, [instances.data])

  if (!open) return null

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
        {/* ─── 左栏：视图导航 + 操作 ─── */}
        <nav aria-label="任务实例视图" className="flex w-48 shrink-0 flex-col border-r border-[var(--color-border)]">
          <div className="p-2">
            <button
              type="button"
              onClick={() => setView('board')}
              aria-pressed={view === 'board' || view === 'activate'}
              className={`mb-1 flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs transition-colors focus-visible:ring-2 ring-ring ${
                view === 'board' || view === 'activate'
                  ? 'bg-brand-soft font-medium text-brand-ink'
                  : 'text-[var(--color-text-primary)] hover:bg-[var(--color-bg-hover)]'
              }`}
            >
              <LayoutGrid size={14} className="shrink-0" /> 实例看板
            </button>
            <button
              type="button"
              onClick={() => setView('templates')}
              aria-pressed={view === 'templates' || view === 'canvas'}
              className={`mb-1 flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs transition-colors focus-visible:ring-2 ring-ring ${
                view === 'templates' || view === 'canvas'
                  ? 'bg-brand-soft font-medium text-brand-ink'
                  : 'text-[var(--color-text-primary)] hover:bg-[var(--color-bg-hover)]'
              }`}
            >
              <ListChecks size={14} className="shrink-0" /> 流程模板
            </button>
          </div>
          <div className="mt-auto border-t border-[var(--color-border)] p-2">
            <button
              type="button"
              onClick={() => setView('canvas')}
              className="flex h-9 w-full items-center justify-center gap-1.5 rounded-lg bg-brand text-xs font-medium text-white hover:bg-brand-deep focus-visible:ring-2 ring-ring"
            >
              <Plus size={14} /> 新建流程
            </button>
          </div>
        </nav>

        {/* ─── 右栏：视图内容 ─── */}
        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          {view === 'board' && <BoardView grouped={grouped} total={instances.data?.total ?? 0} onRefresh={() => void instances.refetch()} />}
          {view === 'activate' && selectedTemplate && (
            <ActivateForm
              template={selectedTemplate}
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
function BoardView({ grouped, total, onRefresh }: {
  grouped: Map<string, InstanceListItem[]>
  total: number
  onRefresh: () => void
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
                  className="w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-sidebar)] p-2.5 text-left transition-shadow hover:shadow-sm focus-visible:ring-2 ring-ring"
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
function TemplatesPanel({ templates, loading, onSelect }: {
  templates: TemplateSummary[]
  loading: boolean
  onSelect: (template: TemplateSummary) => void
}) {
  if (loading) {
    return <div className="px-4 py-12 text-center text-xs text-[var(--color-text-tertiary)]">加载中…</div>
  }
  if (!templates.length) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 px-8 text-center">
        <ListChecks size={24} className="text-[var(--color-text-tertiary)]" />
        <p className="text-sm text-[var(--color-text-secondary)]">还没有流程模板</p>
        <p className="text-xs text-[var(--color-text-tertiary)]">
          点击左下角「新建流程」用画布创建第一个自动化流程
        </p>
      </div>
    )
  }
  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-4" data-testid="task-instances-templates">
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
  )
}

// ═══════════════════════════════════════════════════════════
// 子组件：激活表单
// ═══════════════════════════════════════════════════════════
function ActivateForm({ template, instanceName, setInstanceName, goal, setGoal, onBack, onActivate, loading }: {
  template: TemplateSummary
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
