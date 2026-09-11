import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { PageSizeSelect } from '@/components/PageSizeSelect'
import {
  Plus, Search, Play, GitBranch, Trash2, Pencil, ChevronLeft, ChevronRight,
  X, Loader2, CheckCircle2, XCircle, Clock, Sparkles, ExternalLink,
  AlertCircle, FileCode2, Copy,
} from 'lucide-react'
import pipelinesApi from '@/api/v2/pipelines'
import type { Pipeline, PipelineOverview } from '@/api/v2/pipelines'
import { getPipelineEngine } from '@/api/v2/pipelines'
import { stewardApi } from '@/api/steward'
import type { StewardStatus } from '@/api/steward'
import ConfirmDialog from '@/components/ConfirmDialog'
import { toast } from 'sonner'
import RunPreviewModal from './RunPreviewModal'
import PipelineEditWizard from './PipelineEditWizard'
import PipelineOverviewBar from './PipelineOverviewBar'
import PipelineRunHistoryDrawer from './PipelineRunHistoryDrawer'
import { ArtifactPreviewPopover, TaskPreviewPopover } from './PipelineLinkPopovers'

// 发布状态：draft/published 双态（运行态在「最近执行结果」列，不混入生命周期）；
// editing/running/failed 是 0008 迁移前的遗留值，展示上归为草稿
const STATUS_STYLE: Record<string, string> = {
  draft:     'bg-muted text-muted-foreground border-border',
  published: 'bg-brand-soft text-brand-ink border-brand-line',
}

const STATUS_LABEL: Record<string, string> = {
  draft: '未发布', published: '已发布',
}

const normStatus = (s?: string): 'draft' | 'published' => (s === 'published' ? 'published' : 'draft')

const RUN_STATUS_META: Record<string, { icon: React.ReactNode; label: string; color: string }> = {
  success: { icon: <CheckCircle2 size={12} />, label: '成功', color: 'text-[var(--color-success)]' },
  failed:  { icon: <XCircle size={12} />,      label: '失败', color: 'text-[var(--color-danger)]' },
  running: { icon: <Loader2 size={12} className="animate-spin" />, label: '运行中', color: 'text-[var(--color-info)]' },
  pending: { icon: <Clock size={12} />,        label: '排队中', color: 'text-muted-foreground' },
}

function isN8nPipeline(pl: Pipeline): boolean {
  return getPipelineEngine(pl) === 'n8n'
}

function isPythonPipeline(pl: Pipeline): boolean {
  return getPipelineEngine(pl) === 'python'
}

function mergePipeline(current: Pipeline, updated: Pipeline): Pipeline {
  const currentDefinition = current.definition as Record<string, unknown> | null
  const updatedDefinition = updated.definition as Record<string, unknown> | null
  const currentN8n = currentDefinition?.n8n as Record<string, unknown> | undefined
  const updatedN8n = updatedDefinition?.n8n as Record<string, unknown> | undefined

  return {
    ...current,
    ...updated,
    definition: updatedDefinition
      ? {
          ...currentDefinition,
          ...updatedDefinition,
          ...(currentN8n || updatedN8n
            ? { n8n: { ...currentN8n, ...updatedN8n } }
            : {}),
        } as Pipeline['definition']
      : current.definition,
  }
}

function formatTime(iso?: string | null): string {
  if (!iso) return '-'
  try {
    return new Date(iso).toLocaleString('zh-CN', {
      month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
    })
  } catch { return iso }
}

function EnabledSwitch({ on, busy, lockReason, onToggle, onLocked }: {
  on: boolean
  busy: boolean
  lockReason?: string
  onToggle: () => void
  onLocked?: () => void
}) {
  const locked = !!lockReason
  return (
    <button
      role="switch" aria-checked={on} aria-disabled={locked} disabled={busy}
      onClick={locked ? onLocked : onToggle}
      className={`relative inline-flex h-5 w-9 shrink-0 rounded-full transition-colors ${
        on ? 'bg-brand-deep' : 'bg-accent'} ${busy ? 'opacity-60' : locked ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}`}
      title={locked
        ? lockReason
        : on ? '已启用：任务池调度与联动触发会执行该流水线' : '未启用：任务池调度与联动触发将跳过（仍可手动执行试运行）'}
    >
      <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-card shadow transition-all ${on ? 'left-[18px]' : 'left-0.5'}`} />
    </button>
  )
}

/** 弹性列宽：table-fixed + 百分比，各列按比例自适应浏览器宽度 */

export default function PipelineListPage() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const [pipelines, setPipelines] = useState<Pipeline[]>([])
  const [overview, setOverview] = useState<PipelineOverview | null>(null)
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState(() => searchParams.get('search') || '')
  const [filterSource, setFilterSource] = useState('')
  const [filterStatus, setFilterStatus] = useState('')
  const [filterEnabled, setFilterEnabled] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [total, setTotal] = useState(0)

  const [showCreate, setShowCreate] = useState(false)
  const [previewTarget, setPreviewTarget] = useState<Pipeline | null>(null)
  const [editTarget, setEditTarget] = useState<Pipeline | null>(null)
  const [historyTarget, setHistoryTarget] = useState<Pipeline | null>(null)
  const [n8nApiUrl, setN8nApiUrl] = useState('')
  const [n8nStatus, setN8nStatus] = useState<StewardStatus['n8n'] | null>(null)
  const [togglingId, setTogglingId] = useState<string | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<Pipeline | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [cloneTarget, setCloneTarget] = useState<Pipeline | null>(null)
  const [cloning, setCloning] = useState(false)

  const load = useCallback(() => {
    setLoading(true)
    pipelinesApi.listPage({
      search: search || undefined,
      engine: filterSource || undefined,
      status: filterStatus || undefined,
      enabled: filterEnabled ? filterEnabled === 'enabled' : undefined,
      page,
      page_size: pageSize,
    })
      .then(res => {
        setPipelines(Array.isArray(res.items) ? res.items : [])
        setTotal(res.total || 0)
        setOverview(res.overview ?? null)
        if (page > 1 && res.items.length === 0 && res.total > 0) setPage(page - 1)
      })
      .catch(() => {
        setPipelines([])
        setTotal(0)
        toast.error('流水线列表加载失败', { description: '请检查服务连接后重试。' })
      })
      .finally(() => setLoading(false))
  }, [filterEnabled, filterSource, filterStatus, page, pageSize, search])

  useEffect(() => { load() }, [load])

  useEffect(() => {
    stewardApi.status()
      .then(s => {
        setN8nStatus(s.n8n)
        setN8nApiUrl(s.n8n.api_url)
      })
      .catch(() => setN8nStatus({
        configured: false,
        enabled: false,
        api_url: '',
        reachable: false,
        error: '无法读取 n8n 配置状态',
      }))
  }, [])

  const matchesActiveFilters = useCallback((pl: Pipeline) => {
    const keyword = search.trim().toLowerCase()
    if (keyword && !pl.name.toLowerCase().includes(keyword) && !pl.id.toLowerCase().includes(keyword)) {
      return false
    }
    if (filterSource && getPipelineEngine(pl) !== filterSource) return false
    if (filterStatus && normStatus(pl.status) !== filterStatus) return false
    if (filterEnabled) {
      const enabled = pl.enabled ?? true
      if ((filterEnabled === 'enabled') !== enabled) return false
    }
    return true
  }, [filterEnabled, filterSource, filterStatus, search])

  const insertPipelineLocally = (pl: Pipeline, toastTitle = '流水线已创建') => {
    if (matchesActiveFilters(pl)) {
      setTotal(current => current + 1)
      if (page === 1) {
        setPipelines(current => [pl, ...current.filter(item => item.id !== pl.id)].slice(0, pageSize))
      }
    }
    toast.success(toastTitle, { description: page === 1 && matchesActiveFilters(pl)
        ? `「${pl.name}」已加入当前列表。`
        : `「${pl.name}」已创建，可调整筛选或返回第一页查看。` })
  }

  const updatePipelineLocally = (updated: Pipeline) => {
    const current = pipelines.find(item => item.id === updated.id)
    if (!current) return
    const merged = mergePipeline(current, updated)
    const remainsVisible = matchesActiveFilters(merged)
    setPipelines(items => remainsVisible
      ? items.map(item => item.id === merged.id ? merged : item)
      : items.filter(item => item.id !== merged.id))
    if (!remainsVisible) setTotal(value => Math.max(0, value - 1))
    toast.success('流水线已更新', { description: remainsVisible
        ? `「${merged.name}」的信息已局部更新。`
        : `「${merged.name}」已更新，并因当前筛选条件从列表中移除。` })
  }

  const handleToggleEnabled = async (pl: Pipeline) => {
    const next = !(pl.enabled ?? true)
    setTogglingId(pl.id)
    setPipelines(ps => ps.map(p => p.id === pl.id ? { ...p, enabled: next } : p))
    try {
      await pipelinesApi.setEnabled(pl.id, next)
      const updated = { ...pl, enabled: next }
      if (!matchesActiveFilters(updated)) {
        setPipelines(items => items.filter(item => item.id !== pl.id))
        setTotal(value => Math.max(0, value - 1))
      }
      toast.success(next ? '流水线已启用' : '流水线已停用', { description: `「${pl.name}」的启用状态已更新。` })
    } catch (e: unknown) {
      const err = e as { detail?: string; message?: string }
      setPipelines(ps => ps.map(p => p.id === pl.id ? { ...p, enabled: !next } : p))
      toast.error('启用状态更新失败', { description: err?.detail || err?.message || '请稍后重试。' })
    } finally {
      setTogglingId(null)
    }
  }

  const handleDelete = async () => {
    if (!deleteTarget) return
    const target = deleteTarget
    setDeleting(true)
    try {
      await pipelinesApi.delete(target.id)
      setDeleteTarget(null)
      setPipelines(items => items.filter(item => item.id !== target.id))
      setTotal(value => Math.max(0, value - 1))
      toast.success('流水线已归档', { description: `「${target.name}」已从当前列表移除。` })
    } catch (e: unknown) {
      // 典型场景：被调度任务引用（后端引用保护 400）
      const err = e as { detail?: string; message?: string }
      setDeleteTarget(null)
      toast.error('流水线归档失败', { description: err?.detail || err?.message || '请稍后重试。' })
    } finally {
      setDeleting(false)
    }
  }

  const handleClone = async () => {
    if (!cloneTarget) return
    const target = cloneTarget
    setCloning(true)
    try {
      const cloned = await pipelinesApi.clone(target.id)
      setCloneTarget(null)
      insertPipelineLocally(cloned, '流水线已克隆')
    } catch (e: unknown) {
      const err = e as { detail?: string; message?: string }
      setCloneTarget(null)
      toast.error('流水线克隆失败', { description: err?.detail || err?.message || '请稍后重试。' })
    } finally {
      setCloning(false)
    }
  }

  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const hasActiveFilters = Boolean(search || filterSource || filterStatus || filterEnabled)
  const resetFilters = () => {
    setSearch('')
    setFilterSource('')
    setFilterStatus('')
    setFilterEnabled('')
    setPage(1)
  }
  return (
    <div className="space-y-4 pb-4">
      {/* 运行概况：全量口径统计卡（来自列表响应 overview 字段），布局对齐数据任务池 */}
      {overview && <PipelineOverviewBar overview={overview} />}

      {/* 搜索、筛选、操作按钮 */}
      <div className="flex flex-wrap items-center gap-3 rounded-2xl border border-border bg-card px-4 py-3 xl:flex-nowrap">
        <div className="relative w-full sm:w-64 xl:w-72 xl:flex-none">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-[var(--color-text-tertiary)]" />
          <input
            value={search}
            onChange={e => { setSearch(e.target.value); setPage(1) }}
            placeholder="搜索名称 / ID..."
            className="w-full rounded-xl border border-border py-2 pl-8 pr-3 text-sm outline-none transition focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring"
          />
          {search && (
            <button onClick={() => { setSearch(''); setPage(1) }} className="absolute right-2 top-1/2 -translate-y-1/2 text-[var(--color-text-tertiary)] hover:text-foreground">
              <X size={12} />
            </button>
          )}
        </div>
        <Select
          value={filterSource || '__all__'}
          onValueChange={value => { setFilterSource(value === '__all__' ? '' : value); setPage(1) }}
        >
          <SelectTrigger className="w-fit min-w-32 shrink-0 rounded-xl bg-card px-3 py-2 text-sm" aria-label="按类型筛选">
            <SelectValue placeholder="全部类型" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">全部类型</SelectItem>
            <SelectItem value="n8n">n8n 流水线</SelectItem>
            <SelectItem value="python">Python 脚本</SelectItem>
          </SelectContent>
        </Select>
        <Select
          value={filterStatus || '__all__'}
          onValueChange={value => { setFilterStatus(value === '__all__' ? '' : value); setPage(1) }}
        >
          <SelectTrigger className="w-fit min-w-36 shrink-0 rounded-xl bg-card px-3 py-2 text-sm" aria-label="按发布状态筛选">
            <SelectValue placeholder="全部发布状态" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">全部发布状态</SelectItem>
            <SelectItem value="published">已发布</SelectItem>
            <SelectItem value="draft">未发布</SelectItem>
          </SelectContent>
        </Select>
        <Select
          value={filterEnabled || '__all__'}
          onValueChange={value => { setFilterEnabled(value === '__all__' ? '' : value); setPage(1) }}
        >
          <SelectTrigger className="w-fit min-w-36 shrink-0 rounded-xl bg-card px-3 py-2 text-sm" aria-label="按启用状态筛选">
            <SelectValue placeholder="全部启用状态" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">全部启用状态</SelectItem>
            <SelectItem value="enabled">已启用</SelectItem>
            <SelectItem value="disabled">未启用</SelectItem>
          </SelectContent>
        </Select>
        {hasActiveFilters && (
          <button
            onClick={resetFilters}
            className="shrink-0 px-2 py-1 text-xs text-muted-foreground hover:text-foreground"
          >
            清除筛选
          </button>
        )}
        <div className="ml-auto flex shrink-0 items-center gap-2">
          <button
            onClick={() => navigate('/data/pipelines/steward')}
            className="flex items-center gap-1.5 rounded-xl border border-brand-line bg-brand-soft px-3.5 py-2 text-sm font-medium text-brand-ink transition hover:bg-brand-soft active:translate-y-px"
            title="用对话创建与编排 n8n 数据流水线"
          >
            <Sparkles size={15} /> 数据管家
          </button>
          <button
            onClick={() => setShowCreate(true)}
            className="flex items-center gap-1.5 rounded-xl bg-brand-deep px-3.5 py-2 text-sm font-medium text-[var(--color-text-inverse)] transition hover:bg-brand-deep active:translate-y-px"
          >
            <Plus size={15} /> 新建流水线
          </button>
        </div>
      </div>

      {/* 列表 */}
      {loading ? (
        <div className="text-[var(--color-text-tertiary)] text-sm p-8 text-center">加载中...</div>
      ) : pipelines.length === 0 ? (
        <div className="border-2 border-dashed rounded-xl p-12 text-center text-[var(--color-text-tertiary)] space-y-2">
          <GitBranch size={32} className="mx-auto opacity-30" />
          {hasActiveFilters ? (
            <>
              <p className="text-sm font-medium">没有匹配的流水线</p>
              <p className="text-xs">当前搜索与筛选条件下没有匹配结果，可调整条件或清除筛选后重试</p>
              <div className="flex items-center justify-center gap-2 pt-2">
                <button
                  type="button"
                  onClick={resetFilters}
                  className="rounded-xl bg-brand-deep px-4 py-2 text-sm font-medium text-[var(--color-text-inverse)] transition hover:bg-brand-deep"
                >
                  清除筛选
                </button>
                <button
                  type="button"
                  onClick={() => setShowCreate(true)}
                  className="flex items-center gap-1.5 rounded-xl border border-border bg-card px-4 py-2 text-sm font-medium text-muted-foreground transition hover:border-brand-line hover:text-brand-ink"
                >
                  <Plus size={15} /> 新建流水线
                </button>
              </div>
            </>
          ) : (
            <>
              <p className="text-sm font-medium">暂无流水线</p>
              <p className="text-xs">新建 n8n 流水线后，可到数据管家完善编排，再通过编辑向导验证并发布</p>
              <div className="pt-2">
                <button
                  type="button"
                  onClick={() => setShowCreate(true)}
                  className="inline-flex items-center gap-1.5 rounded-xl bg-brand-deep px-4 py-2 text-sm font-medium text-[var(--color-text-inverse)] transition hover:bg-brand-deep"
                >
                  <Plus size={15} /> 新建流水线
                </button>
              </div>
            </>
          )}
        </div>
      ) : (
        <div className="overflow-x-auto rounded-2xl border border-border bg-card shadow-[0_8px_30px_rgba(15,23,42,0.04)]">
          <table className="w-full min-w-[1080px] text-sm table-fixed">
            <thead className="sticky top-0 z-10 border-b border-border bg-card backdrop-blur">
              <tr>
                <th className="text-left px-4 py-2.5 font-medium text-muted-foreground text-xs rounded-tl-xl" style={{ width: '23%' }}>
                  流水线信息
                </th>
                <th className="text-center px-4 py-2.5 font-medium text-muted-foreground text-xs" style={{ width: '9%' }}>流水线类型</th>
                <th className="text-center px-4 py-2.5 font-medium text-muted-foreground text-xs" style={{ width: '9%' }}>发布状态</th>
                <th className="text-center px-4 py-2.5 font-medium text-muted-foreground text-xs" style={{ width: '10%' }}>启用状态</th>
                <th className="text-center px-4 py-2.5 font-medium text-muted-foreground text-xs" style={{ width: '14%' }}>最近执行结果</th>
                <th className="text-center px-4 py-2.5 font-medium text-muted-foreground text-xs" style={{ width: '11%' }}>产物</th>
                <th className="text-center px-4 py-2.5 font-medium text-muted-foreground text-xs" style={{ width: '11%' }}>关联任务</th>
                <th className="text-center px-2 py-2.5 font-medium text-muted-foreground text-xs rounded-tr-xl" style={{ width: '13%' }}>操作</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {pipelines.map(pl => {
                const runMeta = pl.last_run_status ? RUN_STATUS_META[pl.last_run_status] : null
                const runFailed = pl.last_run_status === 'failed' && !!pl.last_run_error
                const curatedCount = pl.target_curated_ids?.length ?? 0
                const taskCount = pl.task_count ?? 0
                const n8n = isN8nPipeline(pl)
                const python = isPythonPipeline(pl)
                const enabled = pl.enabled ?? true
                const enableLockReason = taskCount > 0
                  ? `流水线「${pl.name}」已被 ${taskCount} 个数据任务关联。为避免影响任务调度，请先在数据任务池删除或改绑关联任务，解除关联后再更改启用状态。`
                  : !enabled && normStatus(pl.status) !== 'published'
                    ? '只有已发布的流水线才能启用，请先在编辑向导中完成发布'
                    : undefined
                return (
                  <tr
                    key={pl.id}
                    className={`align-middle transition-colors hover:bg-muted ${enabled ? '' : 'bg-muted'}`}
                  >
                    <td className="px-4 py-3 align-middle">
                      <p className="font-medium text-foreground truncate" title={pl.name}>{pl.name}</p>
                      <p className="text-xs text-[var(--color-text-tertiary)] truncate" title={pl.description || undefined}>
                        {pl.description || '暂未设置描述信息'}
                      </p>
                    </td>
                    <td className="px-4 py-3 text-center align-middle whitespace-nowrap">
                      {n8n ? (
                        <span className="inline-flex w-[100px] justify-center whitespace-nowrap items-center gap-1 rounded-lg border border-brand-line bg-brand-soft px-2 py-1 text-[11px] font-medium text-brand-ink"
                          title="由数据管家托管的 n8n 流水线：编排在数据管家，发布在编辑向导，启用由本列表开关控制">
                          <Sparkles size={10} /> n8n 流水线
                        </span>
                      ) : python ? (
                        <span className="inline-flex w-[100px] justify-center whitespace-nowrap items-center gap-1 rounded-lg border border-viz-indigo-soft bg-viz-indigo-soft px-2 py-1 text-[11px] font-medium text-viz-indigo"
                          title="Python 脚本流水线：在脚本编辑页编写取数脚本，输出 list[dict] 行数据入湖">
                          <FileCode2 size={10} /> Python 脚本
                        </span>
                      ) : (
                        <span className="whitespace-nowrap inline-flex items-center gap-1 rounded border border-border bg-muted px-2 py-0.5 text-[11px] text-muted-foreground"
                          title="存量数据缺少可识别的引擎标记（engine 非 n8n/python），无编排入口">
                          <GitBranch size={10} /> 未知引擎
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-center align-middle whitespace-nowrap">
                      <span
                        className={`inline-flex items-center px-2 py-0.5 rounded-full border text-[11px] ${STATUS_STYLE[normStatus(pl.status)]}`}
                        title={normStatus(pl.status) === 'published'
                          ? '已发布：契约与编排封版，可被任务池挂接'
                          : '未发布：可自由修改；发布后才能被任务池使用'}
                      >
                        {STATUS_LABEL[normStatus(pl.status)]}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-center align-middle whitespace-nowrap" onClick={e => e.stopPropagation()}>
                      <div className="inline-flex items-center gap-2">
                        <EnabledSwitch
                          on={enabled}
                          busy={togglingId === pl.id}
                          lockReason={enableLockReason}
                          onToggle={() => handleToggleEnabled(pl)}
                          onLocked={() => enableLockReason && toast.warning('当前无法切换启用状态', { description: enableLockReason })}
                        />
                        <span className={`text-xs whitespace-nowrap ${enabled ? 'text-brand-ink' : 'text-[var(--color-text-tertiary)]'}`}>
                          {enabled ? '已启用' : '未启用'}
                        </span>
                      </div>
                    </td>
                    <td className="px-4 py-3 text-center align-middle whitespace-nowrap" onClick={e => e.stopPropagation()}>
                      {runMeta ? (
                        <button
                          type="button"
                          onClick={() => setHistoryTarget(pl)}
                          className="relative inline-flex items-center gap-1.5 rounded-md px-2 py-1 transition-colors hover:bg-brand-soft group/hist group/err"
                          title="点击查看历史执行记录"
                        >
                          <span className={`inline-flex items-center gap-1 text-xs ${runMeta.color}`}>
                            {runMeta.icon}{runMeta.label}
                          </span>
                          <span className="text-xs text-[var(--color-text-tertiary)] group-hover/hist:text-brand-ink">{formatTime(pl.last_run_at)}</span>
                          {runFailed && (
                            <div className="pointer-events-none absolute left-1/2 -translate-x-1/2 top-full mt-1.5 z-30 hidden group-hover/err:block w-80 text-left">
                              <div className="bg-accent text-[var(--color-text-inverse)] text-xs rounded-lg px-3 py-2.5 shadow-xl whitespace-normal break-all leading-relaxed">
                                {pl.last_run_error}
                              </div>
                            </div>
                          )}
                        </button>
                      ) : (
                        <span className="text-xs text-[var(--color-text-tertiary)]">从未运行</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-center align-middle whitespace-nowrap" onClick={e => e.stopPropagation()}>
                      {curatedCount > 0 ? (
                        <ArtifactPreviewPopover pipeline={pl} />
                      ) : (
                        <span className="text-xs text-[var(--color-text-tertiary)]">-</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-center align-middle whitespace-nowrap" onClick={e => e.stopPropagation()}>
                      {taskCount > 0 ? (
                        <TaskPreviewPopover pipeline={pl} />
                      ) : (
                        <span className="text-xs text-[var(--color-text-tertiary)]">-</span>
                      )}
                    </td>
                    <td className="px-2 py-3 text-center align-middle whitespace-nowrap" onClick={e => e.stopPropagation()}>
                      <div className="flex gap-0.5 justify-center">
                        {(n8n || python) && (
                          <button
                            type="button"
                            onClick={() => {
                              if (n8n) {
                                const wfId = (pl.definition as Record<string, unknown> | null)?.n8n as Record<string, unknown> | undefined
                                const workflowId = wfId?.n8n_workflow_id as string | undefined
                                if (workflowId && n8nApiUrl) {
                                  const webUrl = n8nApiUrl.replace(/\/api\/.*$/, '').replace(/\/+$/, '')
                                  window.open(`${webUrl}/workflow/${workflowId}`, '_blank')
                                }
                              } else {
                                navigate(`/data/pipelines/script/${pl.id}`)
                              }
                            }}
                            className="flex w-[34px] flex-col items-center gap-0.5 rounded py-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                            title={n8n ? '跳转 n8n 工作流' : '编辑 Python 脚本'}
                            aria-label={n8n ? '编排：跳转 n8n 工作流' : '脚本：编辑 Python 脚本'}
                          >
                            <ExternalLink size={14} />
                            <span className="text-[10px] leading-3">{n8n ? '编排' : '脚本'}</span>
                          </button>
                        )}
                        <button
                          type="button"
                          onClick={() => setEditTarget(pl)}
                          className="flex w-[34px] flex-col items-center gap-0.5 rounded py-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          title={normStatus(pl.status) === 'published' ? '查看发布契约 / 编辑名称与描述' : '配置流水线：信息 / 执行预览 / 主键组 / 发布'}
                          aria-label={normStatus(pl.status) === 'published' ? '编辑：查看发布契约 / 编辑名称与描述' : '编辑：配置流水线信息 / 执行预览 / 主键组 / 发布'}
                        >
                          <Pencil size={14} />
                          <span className="text-[10px] leading-3">编辑</span>
                        </button>
                        <button
                          type="button"
                          onClick={() => setPreviewTarget(pl)}
                          className="flex w-[34px] flex-col items-center gap-0.5 rounded py-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          title="试执行流水线并查看输出"
                          aria-label="试运行：试执行流水线并查看输出"
                        >
                          <Play size={14} />
                          <span className="text-[10px] leading-3">试运行</span>
                        </button>
                        {(n8n || python) && (
                          <button
                            type="button"
                            onClick={() => setCloneTarget(pl)}
                            className="flex w-[34px] flex-col items-center gap-0.5 rounded py-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                            title="克隆流水线结构为未发布草稿"
                            aria-label="克隆：复制流水线结构为未发布草稿"
                          >
                            <Copy size={14} />
                            <span className="text-[10px] leading-3">克隆</span>
                          </button>
                        )}
                        <button
                          type="button"
                          onClick={() => setDeleteTarget(pl)}
                          className="flex w-[34px] flex-col items-center gap-0.5 rounded py-1 text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          title="归档流水线"
                          aria-label="归档流水线"
                        >
                          <Trash2 size={14} />
                          <span className="text-[10px] leading-3">归档</span>
                        </button>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          <div className="flex items-center justify-end gap-3 border-t border-border bg-card px-4 py-2.5">
            <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
              每页
              <PageSizeSelect
                value={pageSize}
                onChange={size => { setPageSize(size); setPage(1) }}
                sizes={[10, 20, 50]}
                ariaLabel="每页显示条数"
              />
              条
            </label>
            <span className="min-w-20 text-center text-xs tabular-nums text-muted-foreground">第 {page} / {totalPages} 页</span>
            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={() => setPage(current => Math.max(1, current - 1))}
                disabled={page <= 1}
                className="flex h-8 w-8 items-center justify-center rounded-lg border border-border bg-card text-muted-foreground transition-colors hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink disabled:cursor-not-allowed disabled:opacity-35"
                aria-label="上一页"
              >
                <ChevronLeft size={14} />
              </button>
              <button
                type="button"
                onClick={() => setPage(current => Math.min(totalPages, current + 1))}
                disabled={page >= totalPages}
                className="flex h-8 w-8 items-center justify-center rounded-lg border border-border bg-card text-muted-foreground transition-colors hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink disabled:cursor-not-allowed disabled:opacity-35"
                aria-label="下一页"
              >
                <ChevronRight size={14} />
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 试执行：仅运行并查看输出，入湖统一由数据任务池负责 */}
      {previewTarget && (
        <RunPreviewModal
          pipeline={previewTarget}
          onClose={() => setPreviewTarget(null)}
        />
      )}

      {/* 编辑向导 */}
      {editTarget && (
        <PipelineEditWizard
          pipeline={editTarget}
          onClose={() => setEditTarget(null)}
          onSaved={(updated) => {
            setEditTarget(null)
            updatePipelineLocally(updated)
          }}
        />
      )}

      {/* 历史执行记录抽屉（最近执行结果列入口） */}
      {historyTarget && (
        <PipelineRunHistoryDrawer
          pipeline={historyTarget}
          onClose={() => setHistoryTarget(null)}
        />
      )}

      {/* 新建弹窗 */}
      {showCreate && (
        <PipelineCreateModal
          n8nStatus={n8nStatus}
          onClose={() => setShowCreate(false)}
          onCreated={(pl) => {
            setShowCreate(false)
            insertPipelineLocally(pl)
          }}
        />
      )}

      {/* 删除确认：后端对所有引擎统一归档语义（保留版本、运行记录与资产湖产物） */}
      <ConfirmDialog
        open={!!deleteTarget}
        title="归档流水线"
        message={`确认归档流水线「${deleteTarget?.name}」？系统会停用该流水线，并保留发布版本、运行记录和资产湖产物用于审计。`}
        confirmLabel={deleting ? '处理中...' : '确认归档'}
        onConfirm={handleDelete}
        onCancel={() => setDeleteTarget(null)}
      />

      {/* 克隆确认：复制编排结构（n8n workflow / Python 脚本）与字段契约，副本未发布未启用 */}
      <ConfirmDialog
        open={!!cloneTarget}
        title="克隆流水线"
        message={`确认克隆流水线「${cloneTarget?.name}」？系统将复制其${cloneTarget && isN8nPipeline(cloneTarget) ? ' n8n 工作流编排' : ' Python 脚本'}与字段契约，生成未发布、未启用的草稿副本，名称在原名称后追加「_复制」尾缀（重名自动递增）。`}
        confirmLabel={cloning ? '克隆中...' : '确认克隆'}
        tone="primary"
        onConfirm={handleClone}
        onCancel={() => setCloneTarget(null)}
      />
    </div>
  )
}

function PipelineCreateModal({
  pipeline, n8nStatus, onClose, onCreated, onSaved,
}: {
  pipeline?: Pipeline
  n8nStatus?: StewardStatus['n8n'] | null
  onClose: () => void
  onCreated?: (pl: Pipeline) => void
  onSaved?: () => void
}) {
  const isEdit = !!pipeline
  const n8nReady = Boolean(
    n8nStatus?.configured && n8nStatus.enabled && n8nStatus.reachable !== false,
  )
  const defaultMode = isEdit
    ? (isN8nPipeline(pipeline) ? 'n8n' : 'python')
    : (n8nReady ? 'n8n' : 'python')

  const [name, setName] = useState(pipeline?.name || '')
  const [description, setDescription] = useState(pipeline?.description || '')
  const [mode, setMode] = useState<'n8n' | 'python'>(defaultMode)
  const [saving, setSaving] = useState(false)

  const handleSubmit = async () => {
    if (!name.trim()) {
      toast.warning('请填写流水线名称')
      return
    }
    if (!isEdit && mode === 'n8n' && !n8nReady) {
      toast.warning('n8n 当前不可用', { description: '请联系管理员检查部署环境的 N8N_* 启动配置并重启平台。' })
      return
    }
    setSaving(true)
    try {
      if (isEdit) {
        await pipelinesApi.update(pipeline.id, { name: name.trim(), description })
        onSaved?.()
      } else if (mode === 'n8n') {
        const res = await stewardApi.bootstrap(name.trim(), description)
        if (!res.record.pipelineId) throw new Error('n8n 流水线已创建，但未生成平台流水线记录。')
        const pl = await pipelinesApi.get(res.record.pipelineId)
        const definition = pl.definition as Record<string, unknown> | null
        const n8n = definition?.n8n as Record<string, unknown> | undefined
        onCreated?.({
          ...pl,
          definition: {
            ...definition,
            n8n: {
              ...n8n,
              n8n_workflow_id: res.record.n8nWorkflowId,
            },
          } as unknown as Pipeline['definition'],
        })
      } else if (mode === 'python') {
        const pl = await pipelinesApi.create({
          name: name.trim(),
          description,
          // 脚本留空：首个脚本必须经脚本编辑页「保存」（服务端重跑复验）写入，
          // 保证落库脚本一定通过执行与输出格式校验；编辑页会预填模板。
          definition: { engine: 'python', nodes: [], edges: [], python: {} },
        })
        onCreated?.(pl)
      }
    } catch (e: unknown) {
      const err = e as { detail?: string; message?: string }
      toast.error(isEdit ? '流水线保存失败' : '流水线创建失败', { description: err?.detail || err?.message || '请稍后重试。' })
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-accent p-4 backdrop-blur-[2px]" onClick={onClose}>
      <div className="w-[500px] max-w-full rounded-2xl border border-border bg-card p-6 shadow-[0_28px_90px_rgba(15,23,42,0.24)]" onClick={e => e.stopPropagation()}>
        <div className="flex justify-between items-center mb-4">
          <h3 className="font-semibold">{isEdit ? '编辑数据流水线' : '新建数据流水线'}</h3>
          <button onClick={onClose} className="text-[var(--color-text-tertiary)] hover:text-foreground">
            <X size={16} />
          </button>
        </div>
        <div className="space-y-3">
          {!isEdit && (
            <div>
              <label className="block text-xs text-muted-foreground mb-1.5">创建方式 *</label>
              <div className="grid grid-cols-2 gap-2">
                <button
                  type="button"
                  onClick={() => setMode('n8n')}
                  disabled={!n8nReady}
                  className={`text-left p-3 rounded-lg border-2 transition-all ${
                    mode === 'n8n'
                      ? 'border-brand bg-brand-soft'
                      : !n8nReady
                        ? 'cursor-not-allowed border-border bg-muted opacity-55'
                        : 'border-border hover:border-border'}`}
                >
                  <div className={`text-sm font-medium flex items-center gap-1.5 ${mode === 'n8n' ? 'text-brand-ink' : 'text-foreground'}`}>
                    <Sparkles size={13} /> n8n 流水线
                  </div>
                  <div className="text-xs text-muted-foreground mt-0.5">后台自动在 n8n 创建骨架工作流并加入列表；点击流水线可到数据管家用 AI 完善编排</div>
                </button>
                <button
                  type="button"
                  onClick={() => setMode('python')}
                  className={`text-left p-3 rounded-lg border-2 transition-all ${
                    mode === 'python' ? 'border-viz-indigo bg-viz-indigo-soft' : 'border-border hover:border-border'}`}
                >
                  <div className={`text-sm font-medium flex items-center gap-1.5 ${mode === 'python' ? 'text-viz-indigo' : 'text-foreground'}`}>
                    <FileCode2 size={13} /> Python 脚本
                  </div>
                  <div className="text-xs text-muted-foreground mt-0.5">自行编写 Python 脚本取数（HTTP 请求等），输出行数据写入资产湖</div>
                </button>
              </div>
              {!n8nReady && (
                <div className="mt-2 flex items-center gap-2 rounded-lg border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-3 py-2 text-xs text-[var(--color-warning)]">
                  <AlertCircle size={13} className="shrink-0" />
                  <span className="flex-1">
                    {!n8nStatus
                      ? '正在检查 n8n 配置状态…'
                      : !n8nStatus.configured
                        ? '启动配置缺少 n8n 地址或 API Key，请联系管理员在部署环境补齐 N8N_* 并重启平台。'
                        : !n8nStatus.enabled
                          ? 'n8n 集成当前处于停用状态。'
                          : `n8n 当前不可达${n8nStatus.error ? `：${n8nStatus.error}` : '。'}`}
                  </span>
                </div>
              )}
            </div>
          )}
          <div>
            <label className="block text-xs text-muted-foreground mb-1">流水线名称 *</label>
            <input
              value={name}
              onChange={e => setName(e.target.value)}
              className="w-full border rounded-lg px-3 py-2 text-sm focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none transition-colors"
              placeholder="例：供应链数据清洗"
              autoFocus
            />
          </div>
          <div>
            <label className="block text-xs text-muted-foreground mb-1">流水线描述</label>
            <textarea
              value={description}
              onChange={e => setDescription(e.target.value)}
              className="w-full border rounded-lg px-3 py-2 text-sm focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none transition-colors"
              rows={3}
              placeholder="流水线用途说明"
            />
          </div>
        </div>
        <div className="flex items-center justify-between mt-4">
          <p className="text-xs text-[var(--color-text-tertiary)] max-w-[60%] leading-relaxed">
            {isEdit
              ? '名称和描述始终可修改；发布后仅编排与字段契约封版。'
              : n8nReady
                ? '推荐使用 n8n 流水线；需要自行编写取数逻辑时选择 Python 脚本。'
                : 'n8n 当前不可用；可创建 Python 脚本流水线，或检查启动配置与服务连通性后再创建 n8n 流水线。'}
          </p>
          <div className="flex gap-3 shrink-0">
            <button onClick={onClose} className="px-4 py-2 border rounded-lg text-sm hover:bg-muted">
              取消
            </button>
            <button
              onClick={handleSubmit}
              disabled={saving}
              className="flex items-center gap-1.5 px-4 py-2 bg-[var(--color-nav-bg)] text-[var(--color-text-inverse)] rounded-lg text-sm disabled:opacity-50 hover:opacity-90 transition-opacity"
            >
              {saving && <Loader2 size={13} className="animate-spin" />}
              {saving ? (isEdit ? '保存中...' : '创建中...') : (isEdit ? '保存' : '创建')}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
