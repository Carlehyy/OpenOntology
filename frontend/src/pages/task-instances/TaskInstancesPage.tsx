/**
 * 任务实例页（/task-instances）— 独立运行时功能域入口。
 * 顶部双视图切换（模板 / 实例看板）；编排页与实例详情为独立路由。
 */
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ListChecks, LayoutGrid } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { taskInstancesApi } from '@/api/taskInstances'
import InstancesBoardView from './InstancesBoardView'
import TemplatesView from './TemplatesView'

type ViewKey = 'templates' | 'instances'

export default function TaskInstancesPage({ initialView = 'templates' }: {
  initialView?: ViewKey
}) {
  const [view, setView] = useState<ViewKey>(initialView)
  const navigate = useNavigate()
  const templates = useQuery({
    queryKey: ['task-instances', 'templates', ''],
    queryFn: () => taskInstancesApi.listTemplates(''),
  })

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <ListChecks className="h-5 w-5 text-[var(--color-text-secondary)]" />
          <div>
            <h1 className="text-lg font-semibold text-[var(--color-text-primary)]">任务实例</h1>
            <p className="text-xs text-[var(--color-text-tertiary)]">
              声明式 DAG 流程模板 → 契约验收的自动化任务轨道
            </p>
          </div>
        </div>
        <div className="ml-auto flex rounded-lg border border-border p-0.5">
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
            <ListChecks className="h-4 w-4" />模板
          </button>
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
            <LayoutGrid className="h-4 w-4" />实例看板
          </button>
        </div>
      </div>

      {view === 'templates' ? (
        <TemplatesView onNew={() => navigate('/task-instances/templates/new')} />
      ) : (
        <InstancesBoardView templates={templates.data} />
      )}
    </div>
  )
}
