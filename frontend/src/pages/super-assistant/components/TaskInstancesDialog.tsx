/**
 * 任务实例弹窗 — 超级助手工作台的统一入口。
 * 复用 components/task-instances 共享视图（模板列表 + 实例看板），
 * 实例详情与 YAML 编排跳转独立页面（弹窗内点卡片即路由）。
 */
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { LayoutGrid, ListChecks } from 'lucide-react'
import { taskInstancesApi } from '@/api/taskInstances'
import { DialogShell } from './AssistantConfiguration'
import InstancesBoardView from '@/components/task-instances/InstancesBoardView'
import TemplatesView from '@/components/task-instances/TemplatesView'

interface TaskInstancesDialogProps {
  open: boolean
  onClose: () => void
}

export default function TaskInstancesDialog({ open, onClose }: TaskInstancesDialogProps) {
  const navigate = useNavigate()
  const [view, setView] = useState<'instances' | 'templates'>('instances')

  const templates = useQuery({
    queryKey: ['task-instances', 'templates', ''],
    queryFn: () => taskInstancesApi.listTemplates(''),
    enabled: open,
  })

  if (!open) return null

  return (
    <DialogShell
      title="任务实例"
      description="声明式 DAG 流程模板 → 契约验收的自动化任务轨道"
      size="wide"
      onClose={onClose}
      icon={<ListChecks size={18} />}
    >
      <div className="flex h-[70vh] flex-col gap-3">
        <div className="flex items-center gap-2">
          <div className="flex rounded-lg border border-border p-0.5">
            <button
              type="button"
              aria-pressed={view === 'instances'}
              className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm transition-colors ${
                view === 'instances'
                  ? 'bg-[var(--color-bg-hover)] font-medium text-[var(--color-text-primary)]'
                  : 'text-[var(--color-text-secondary)]'
              }`}
              onClick={() => setView('instances')}
            >
              <LayoutGrid className="h-4 w-4" /> 实例看板
            </button>
            <button
              type="button"
              aria-pressed={view === 'templates'}
              className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm transition-colors ${
                view === 'templates'
                  ? 'bg-[var(--color-bg-hover)] font-medium text-[var(--color-text-primary)]'
                  : 'text-[var(--color-text-secondary)]'
              }`}
              onClick={() => setView('templates')}
            >
              <ListChecks className="h-4 w-4" /> 模板
            </button>
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {view === 'templates' ? (
            <TemplatesView onNew={() => { onClose(); navigate('/task-instances/templates/new') }} />
          ) : (
            <InstancesBoardView templates={templates.data} />
          )}
        </div>
      </div>
    </DialogShell>
  )
}
