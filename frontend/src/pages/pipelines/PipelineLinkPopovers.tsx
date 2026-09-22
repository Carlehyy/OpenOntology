/**
 * 产物 / 关联任务列内预览气泡。
 *
 * 把原来"点击即整页跳转"的两个按钮改为：点击先在列内弹出预览（懒加载，
 * 首次打开才请求数据），确认需要深入处理时再点气泡底部的链接跳转到
 * 数据资产湖 / 数据任务池——浏览上下文不丢失，原有跳转能力完整保留。
 *
 * 数据源均为现有只读接口：产物按 target_curated_ids 调 curatedApi.get 取
 * 数据集名称；关联任务复用 pipeline-tasks 列表接口按 pipeline_id 过滤。
 */
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowRight, Loader2, Table2, ListChecks } from 'lucide-react'
import type { Pipeline } from '@/api/v2/pipelines'
import curatedApi from '@/api/v2/curated'
import { pipelineTasksApi, type PipelineTask } from '@/api/v2/pipeline-tasks'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'

const TASK_STATUS_LABEL: Record<string, { label: string; dot: string }> = {
  running: { label: '运行中', dot: 'bg-[var(--color-info)]' },
  success: { label: '上次成功', dot: 'bg-[var(--color-success)]' },
  failed:  { label: '上次失败', dot: 'bg-[var(--color-danger)]' },
  idle:    { label: '空闲', dot: 'bg-accent' },
}

/** 产物列：预览数据集名称，底部保留「前往数据资产湖」跳转 */
export function ArtifactPreviewPopover({ pipeline }: { pipeline: Pipeline }) {
  const navigate = useNavigate()
  const ids = pipeline.target_curated_ids ?? []
  const [names, setNames] = useState<string[] | null>(null)
  const [failed, setFailed] = useState(false)

  const load = () => {
    if (names !== null || failed) return
    Promise.all(
      ids.map(id =>
        curatedApi.get(id)
          .then(dataset => dataset.name)
          .catch(() => null),
      ),
    )
      .then(result => setNames(result.filter((name): name is string => !!name)))
      .catch(() => setFailed(true))
  }

  return (
    <Popover onOpenChange={open => { if (open) load() }}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-[var(--color-nav-bg)] transition-colors hover:bg-brand-soft hover:text-brand-ink"
          title="预览数据集，确认后再跳转"
        >
          <Table2 size={12} /> {ids.length} 个数据集
        </button>
      </PopoverTrigger>
      <PopoverContent align="center" className="w-64 p-0">
        <div className="border-b border-border px-3.5 py-2.5 text-xs font-semibold text-foreground">
          产物数据集
        </div>
        <div className="max-h-44 overflow-y-auto px-3.5 py-2.5">
          {failed ? (
            <p className="text-xs text-viz-rose">数据集信息加载失败，请稍后重试。</p>
          ) : names === null ? (
            <span className="inline-flex items-center gap-1.5 text-xs text-[var(--color-text-tertiary)]">
              <Loader2 size={12} className="animate-spin" /> 加载数据集...
            </span>
          ) : names.length === 0 ? (
            <p className="text-xs text-[var(--color-text-tertiary)]">数据集可能已被删除，可前往数据资产湖确认。</p>
          ) : (
            <ul className="space-y-1.5">
              {names.map(name => (
                <li key={name} className="flex items-center gap-1.5 text-xs text-muted-foreground">
                  <Table2 size={11} className="shrink-0 text-[var(--color-text-tertiary)]" />
                  <span className="truncate" title={name}>{name}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="border-t border-border px-3.5 py-2">
          <button
            type="button"
            onClick={() => navigate(`/data/structured?pipeline=${encodeURIComponent(pipeline.name)}`)}
            className="inline-flex items-center gap-1 text-xs font-medium text-brand-ink transition hover:text-brand-ink"
          >
            前往数据资产湖 <ArrowRight size={12} />
          </button>
        </div>
      </PopoverContent>
    </Popover>
  )
}

/** 关联任务列：预览任务名称与运行状态，底部保留「前往数据任务池」跳转 */
export function TaskPreviewPopover({ pipeline }: { pipeline: Pipeline }) {
  const navigate = useNavigate()
  const taskCount = pipeline.task_count ?? 0
  const [tasks, setTasks] = useState<PipelineTask[] | null>(null)
  const [failed, setFailed] = useState(false)

  const load = () => {
    if (tasks !== null || failed) return
    pipelineTasksApi.list({ pipeline_id: pipeline.id, page: 1, page_size: 5 })
      .then(res => setTasks(Array.isArray(res.items) ? res.items : []))
      .catch(() => setFailed(true))
  }

  return (
    <Popover onOpenChange={open => { if (open) load() }}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-[var(--color-nav-bg)] transition-colors hover:bg-brand-soft hover:text-brand-ink"
          title="预览关联任务，确认后再跳转"
        >
          <ListChecks size={12} /> {taskCount} 个任务
        </button>
      </PopoverTrigger>
      <PopoverContent align="center" className="w-64 p-0">
        <div className="border-b border-border px-3.5 py-2.5 text-xs font-semibold text-foreground">
          关联数据任务
        </div>
        <div className="max-h-44 overflow-y-auto px-3.5 py-2.5">
          {failed ? (
            <p className="text-xs text-viz-rose">任务信息加载失败，请稍后重试。</p>
          ) : tasks === null ? (
            <span className="inline-flex items-center gap-1.5 text-xs text-[var(--color-text-tertiary)]">
              <Loader2 size={12} className="animate-spin" /> 加载任务...
            </span>
          ) : tasks.length === 0 ? (
            <p className="text-xs text-[var(--color-text-tertiary)]">暂无关联任务。</p>
          ) : (
            <ul className="space-y-1.5">
              {tasks.map(task => {
                const meta = TASK_STATUS_LABEL[task.status] ?? TASK_STATUS_LABEL.idle
                return (
                  <li key={task.id} className="flex items-center gap-1.5 text-xs text-muted-foreground">
                    <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${meta.dot}`} title={meta.label} />
                    <span className="min-w-0 flex-1 truncate" title={task.name}>{task.name}</span>
                    <span className="shrink-0 text-[10px] text-[var(--color-text-tertiary)]">{meta.label}</span>
                  </li>
                )
              })}
              {taskCount > tasks.length && (
                <li className="text-[10px] text-[var(--color-text-tertiary)]">还有 {taskCount - tasks.length} 个</li>
              )}
            </ul>
          )}
        </div>
        <div className="border-t border-border px-3.5 py-2">
          <button
            type="button"
            onClick={() => navigate(`/data/pipelines/sync-tasks?pipeline_id=${encodeURIComponent(pipeline.id)}`)}
            className="inline-flex items-center gap-1 text-xs font-medium text-brand-ink transition hover:text-brand-ink"
          >
            前往数据任务池 <ArrowRight size={12} />
          </button>
        </div>
      </PopoverContent>
    </Popover>
  )
}
