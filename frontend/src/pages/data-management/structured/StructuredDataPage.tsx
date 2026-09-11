import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { toast } from 'sonner'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { PageSizeSelect } from '@/components/PageSizeSelect'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  CheckCircle, AlertTriangle, Clock,
  X, Loader2, Trash2, Table2, RefreshCw,
  Eye, Workflow, ListChecks, Database,
  Boxes, Network, ArrowRight, ArrowRightLeft, BarChart3, ChevronLeft, ChevronRight,
} from 'lucide-react'
import pipelinesApi, { type Pipeline } from '@/api/v2/pipelines'
import curatedApi from '@/api/v2/curated'
import type { CuratedDataset } from '@/api/v2/curated'
import { pipelineTasksApi, type PipelineTask } from '@/api/v2/pipeline-tasks'
import datasetsApi from '@/api/v2/datasets'
import CuratedDetailPanel from './CuratedDetailPanel'
import RawDatasetsView from './RawDatasetsView'
import MigrationTasksModal from './MigrationTasksModal'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'

interface Row {
  pipelineId: string
  pipelineName: string
  domain: string
  curatedId: string
  curatedName: string
  curatedStatus: string
  rowCount: number | null
  quality: number | null
  updatedAt: string | null
}

const STATUS_ICON = (status: string) => {
  if (status === 'approved') return <CheckCircle size={13} className="text-[var(--color-success)]" />
  if (status === 'rejected') return <AlertTriangle size={13} className="text-viz-rose" />
  return <Clock size={13} className="text-[var(--color-warning)]" />
}

const STATUS_LABEL: Record<string, string> = {
  pending_review: '待审核',
  pending:        '待审核',
  in_review:      '待审核',
  approved:       '已审核',
  rejected:       '已拒绝',
}

const STATUS_STYLE: Record<string, string> = {
  pending_review: 'bg-[var(--color-warning-bg)] text-[var(--color-warning)] border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)]',
  pending:        'bg-[var(--color-warning-bg)] text-[var(--color-warning)] border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)]',
  in_review:      'bg-[var(--color-warning-bg)] text-[var(--color-warning)] border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)]',
  approved:       'bg-[var(--color-success-bg)] text-[var(--color-success)] border-[color-mix(in_srgb,var(--color-success)_35%,transparent)]',
  rejected:       'bg-viz-rose-soft text-viz-rose border-viz-rose-soft',
}

type LakeTab = 'curated' | 'raw'

const LAKE_TABS: [LakeTab, string][] = [
  ['curated', '成品数据集'],
  ['raw', '人工数据集'],
]

const isPendingReview = (status: string) => status === 'pending_review' || status === 'pending' || status === 'in_review'
const ASSET_CHANGED_EVENT = 'ontoprompt:data-assets-changed'

const notifyAssetChanged = () => window.dispatchEvent(new Event(ASSET_CHANGED_EVENT))

function formatUpdatedAt(iso: string | null): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  })
}

function errorText(error: unknown, fallback: string): string {
  if (!error || typeof error !== 'object') return fallback
  const e = error as { detail?: unknown; data?: { detail?: unknown }; message?: unknown }
  const detail = e.detail ?? e.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (detail && typeof detail === 'object' && 'message' in detail) {
    const message = (detail as { message?: unknown }).message
    if (typeof message === 'string' && message.trim()) return message
  }
  return typeof e.message === 'string' && e.message.trim() ? e.message : fallback
}

function FlowArrow() {
  return (
    <div className="flex w-[clamp(0.625rem,1.1vw,1rem)] shrink-0 items-center" aria-hidden="true">
      <span className="h-px w-full border-t border-dashed border-border" />
      <ArrowRight className="-ml-1 h-[clamp(0.625rem,1vw,0.875rem)] w-[clamp(0.625rem,1vw,0.875rem)] shrink-0 text-[var(--color-text-tertiary)]" />
    </div>
  )
}

function FlowNode({
  label, icon, active = false, onClick, unavailableReason,
}: {
  label: string
  icon: ReactNode
  active?: boolean
  onClick?: () => void
  unavailableReason?: string
}) {
  const unavailable = Boolean(unavailableReason)
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={!onClick}
      aria-label={unavailableReason ? `${label}，${unavailableReason}` : label}
      title={unavailableReason || label}
      className={`group inline-flex h-[clamp(2rem,2.7vw,2.25rem)] flex-none items-center justify-center gap-[clamp(0.25rem,0.4vw,0.375rem)] rounded-lg border px-[clamp(0.375rem,0.65vw,0.625rem)] text-[clamp(10px,0.8vw,11px)] font-semibold transition-colors ${
        active
          ? 'border-[var(--color-success)] bg-[var(--color-success-bg)] text-[var(--color-success)] shadow-[inset_0_0_0_1px_rgba(16,185,129,0.12)]'
          : unavailable
            ? 'cursor-not-allowed border-border bg-muted text-[var(--color-text-tertiary)] opacity-75'
          : 'border-brand-line bg-brand-soft text-brand-ink hover:border-brand hover:bg-brand-soft'
      }`}
    >
      <span className={`grid h-[clamp(1.125rem,1.5vw,1.25rem)] w-[clamp(1.125rem,1.5vw,1.25rem)] shrink-0 place-items-center rounded-md [&_svg]:h-[clamp(0.6875rem,1vw,0.875rem)] [&_svg]:w-[clamp(0.6875rem,1vw,0.875rem)] ${active ? 'bg-[var(--color-success)] text-[var(--color-text-inverse)]' : unavailable ? 'bg-[var(--color-bg-active)] text-[var(--color-text-tertiary)]' : 'bg-brand-soft text-brand-ink'}`}>
        {icon}
      </span>
      <span className="whitespace-nowrap leading-none" title={label}>{label}</span>
      {active && <span className="ml-0.5 shrink-0 rounded bg-[var(--color-success)] px-[clamp(0.25rem,0.4vw,0.375rem)] py-0.5 text-[clamp(8px,0.65vw,9px)] font-medium leading-none text-[var(--color-text-inverse)]">当前</span>}
      {unavailable && <span className="ml-0.5 shrink-0 rounded bg-[var(--color-bg-active)] px-1 py-0.5 text-[9px] font-medium leading-none text-muted-foreground">即将开放</span>}
    </button>
  )
}

/** 洞察只使用接口返回的真实数据；任何接口失败时都明确提示，不补造指标。 */
function AssetInsightStrip() {
  const [retryToken, setRetryToken] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [metrics, setMetrics] = useState<Array<{ label: string; value: string; note: string }> | null>(null)

  useEffect(() => {
    const refresh = () => {
      setLoading(true)
      setError('')
      setRetryToken(token => token + 1)
    }
    window.addEventListener(ASSET_CHANGED_EVENT, refresh)
    return () => window.removeEventListener(ASSET_CHANGED_EVENT, refresh)
  }, [])

  useEffect(() => {
    let alive = true
    Promise.all([curatedApi.list(), datasetsApi.overview()])
      .then(([curatedResult, rawResult]) => {
        if (!alive) return
        const curatedItems = Array.isArray(curatedResult) ? curatedResult : []
        const rawItems = Array.isArray(rawResult?.items) ? rawResult.items : []
        const manualItems = rawItems.filter(item => item.source === 'upload' || item.source === 'manual')
        const legacySyncCount = rawItems.filter(item => item.source === 'sync').length
        const scored = curatedItems
          .map(item => item.quality_score)
          .filter((score): score is number => typeof score === 'number' && Number.isFinite(score))
        const avgQuality = scored.length
          ? `${Math.round((scored.reduce((sum, score) => sum + score, 0) / scored.length) * 100)}%`
          : '—'
        setMetrics([
          { label: '数据集总数', value: String(curatedItems.length + rawItems.length), note: `成品 ${curatedItems.length} · 人工 ${manualItems.length}${legacySyncCount ? ` · 历史同步 ${legacySyncCount}` : ''}` },
          { label: '人工数据集', value: String(manualItems.length), note: '文件上传或在线维护' },
          { label: '已声明主键', value: String(manualItems.filter(item => Boolean(item.primary_key)).length), note: '具备主键契约的人工数据集' },
          { label: '平均质量分', value: avgQuality, note: scored.length ? `基于 ${scored.length} 个已评分成品` : '暂无已评分成品' },
          { label: '待审核', value: String(curatedItems.filter(item => isPendingReview(item.status)).length), note: '需要人工确认的成品数据集' },
        ])
      })
      .catch(error => {
        if (!alive) return
        setMetrics(null)
        setError(errorText(error, '总览数据加载失败'))
      })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [retryToken])

  if (loading) {
    return (
      <div className="grid shrink-0 grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-5" aria-label="总览加载中">
        {Array.from({ length: 5 }).map((_, index) => <div key={index} className="h-[74px] animate-pulse rounded-xl border border-border bg-muted" />)}
      </div>
    )
  }

  if (error || !metrics) {
    return (
      <div className="flex shrink-0 items-center gap-2 rounded-xl border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-4 py-3 text-sm text-[var(--color-danger)]">
        <AlertTriangle size={15} className="shrink-0" />
        <span className="flex-1">{error || '总览数据不可用'}</span>
        <button
          type="button"
          onClick={() => { setLoading(true); setError(''); setRetryToken(token => token + 1) }}
          className="rounded-md border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-card px-2.5 py-1 text-xs hover:bg-[var(--color-danger-bg)]"
        >重试</button>
      </div>
    )
  }

  return (
    <div className="grid shrink-0 grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-5">
      {metrics.map(metric => (
        <div key={metric.label} className="rounded-xl border border-border bg-card px-4 py-3 shadow-sm/50">
          <p className="text-[11px] font-medium text-muted-foreground">{metric.label}</p>
          <p className="mt-0.5 text-xl font-semibold tabular-nums text-foreground">{metric.value}</p>
          <p className="mt-0.5 truncate text-[10px] text-[var(--color-text-tertiary)]" title={metric.note}>{metric.note}</p>
        </div>
      ))}
    </div>
  )
}

export default function StructuredDataPage() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const requestedTab = searchParams.get('tab')
  const activeTab: LakeTab = requestedTab === 'raw' ? 'raw' : 'curated'
  const [insightSelected, setInsightSelected] = useState(true)
  const tabsRef = useRef<HTMLDivElement>(null)
  const [indicatorPos, setIndicatorPos] = useState({ left: 0, width: 0 })
  const focusDatasetId = searchParams.get('dataset')

  useEffect(() => {
    if (!requestedTab || requestedTab === 'curated' || requestedTab === 'raw') return
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      next.set('tab', 'curated')
      next.delete('dataset')
      return next
    }, { replace: true })
  }, [requestedTab, setSearchParams])

  useEffect(() => {
    const container = tabsRef.current
    if (!container) return
    const activeButton = container.querySelector(`[data-tab-value="${activeTab}"]`) as HTMLElement | null
    if (!activeButton) return
    const containerRect = container.getBoundingClientRect()
    const buttonRect = activeButton.getBoundingClientRect()
    setIndicatorPos({
      left: buttonRect.left - containerRect.left,
      width: buttonRect.width,
    })
  }, [activeTab])

  const switchTab = (tab: LakeTab) => {
    setSearchParams(prev => {
      const n = new URLSearchParams(prev)
      n.set('tab', tab)
      n.delete('dataset')
      return n
    }, { replace: true })
  }

  return (
    <div className="flex h-full flex-col gap-3">
      {/* 不重复页面标题，首屏直接呈现用户真正需要理解和操作的数据流。 */}
      <div className="shrink-0 rounded-xl border border-border bg-card px-4 py-3 shadow-sm/50">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="max-w-full flex-none overflow-x-auto">
            <div className="flex w-max items-center">
              <FlowNode label="数据流水线" icon={<Workflow size={14} />} onClick={() => navigate('/data/pipelines')} />
              <FlowArrow />
              <FlowNode label="数据任务池" icon={<ListChecks size={14} />} onClick={() => navigate('/data/pipelines/sync-tasks')} />
              <FlowArrow />
              <FlowNode label="数据资产湖" icon={<Database size={14} />} active />
              <FlowArrow />
              <FlowNode label="本体数据映射" icon={<Boxes size={14} />} unavailableReason="本体数据映射暂未开放" />
              <FlowArrow />
              <FlowNode label="本体网络图谱" icon={<Network size={14} />} onClick={() => navigate('/ontologies')} />
            </div>
          </div>

          <div className="ml-auto flex shrink-0 items-center gap-1 rounded-lg border border-border bg-muted p-1">
            <div ref={tabsRef} className="relative flex items-center gap-1 rounded-md">
              <div
                data-testid="asset-lake-tab-indicator"
                className="absolute top-0 h-full rounded-md bg-[var(--color-success)] shadow-sm transition-all duration-300 ease-out"
                style={{ left: `${indicatorPos.left}px`, width: `${indicatorPos.width}px` }}
              />
              {LAKE_TABS.map(([key, label]) => (
                <button
                  key={key}
                  type="button"
                  onClick={() => switchTab(key)}
                  data-tab-value={key}
                  aria-pressed={activeTab === key}
                  className={`relative z-10 rounded-md px-4 py-2 text-sm font-medium transition-colors duration-200 ${
                    activeTab === key
                      ? 'text-[var(--color-text-inverse)]'
                      : 'text-muted-foreground hover:text-[var(--color-success)]'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
            <span className="mx-0.5 h-5 w-px bg-[var(--color-bg-active)]" aria-hidden="true" />
            <button
              type="button"
              aria-pressed={insightSelected}
              onClick={() => setInsightSelected(selected => !selected)}
              className={`flex items-center gap-1.5 rounded-md px-4 py-2 text-sm font-medium transition-colors ${
                insightSelected
                  ? 'bg-[var(--color-success)] text-[var(--color-text-inverse)] shadow-sm'
                  : 'bg-[var(--color-bg-active)] text-muted-foreground hover:bg-accent'
              }`}
              title={insightSelected ? '隐藏数据总览' : '显示数据总览'}
            >
              <BarChart3 size={14} /> 总览
            </button>
          </div>
        </div>
      </div>

      {insightSelected && <AssetInsightStrip />}

      {/* 下方内容区域 —— 单页展示，内容区可滚动 */}
      <div className="flex-1 min-h-0">
        <div key={activeTab} className="h-full animate-lake-tab-in">
          {activeTab === 'curated'
            ? <CuratedView focusDatasetId={focusDatasetId} />
            : <RawDatasetsView focusDatasetId={focusDatasetId} source="manual" />}
        </div>
      </div>
    </div>
  )
}

/** 成品数据集（Curated）视图：流水线 × 产物 关联表 */
function CuratedView({ focusDatasetId }: { focusDatasetId?: string | null }) {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()

  const [pipelines, setPipelines] = useState<Pipeline[]>([])
  const [pipelineTasks, setPipelineTasks] = useState<PipelineTask[]>([])
  const [curated, setCurated] = useState<CuratedDataset[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [curatedLoadFailed, setCuratedLoadFailed] = useState(false)
  const [actionError, setActionError] = useState('')
  const [taskFilter, setTaskFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [total, setTotal] = useState(0)
  const pipelineFilter = searchParams.get('pipeline') || ''

  const [panelRow, setPanelRow] = useState<Row | null>(null)
  const [deleteRow, setDeleteRow] = useState<Row | null>(null)
  const [deleting, setDeleting] = useState(false)
  // 成品 → 人工数据集异步迁移
  const [migrateRow, setMigrateRow] = useState<Row | null>(null)
  const [migrating, setMigrating] = useState(false)
  const [migrationNotice, setMigrationNotice] = useState('')
  const [tasksOpen, setTasksOpen] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setLoadError('')
    const results = await Promise.allSettled([
      pipelinesApi.list(),
      pipelineTasksApi.list(),
      curatedApi.listPage({
        pipeline: pipelineFilter || undefined,
        task_id: taskFilter || undefined,
        status: statusFilter || undefined,
        page,
        page_size: pageSize,
      }),
    ])
    const [pipelineResult, taskResult, curatedResult] = results
    if (pipelineResult.status === 'fulfilled') {
      setPipelines(Array.isArray(pipelineResult.value) ? pipelineResult.value : [])
    }
    if (taskResult.status === 'fulfilled') {
      setPipelineTasks(Array.isArray(taskResult.value?.items) ? taskResult.value.items : [])
    }
    if (curatedResult.status === 'fulfilled') {
      const items = Array.isArray(curatedResult.value?.items) ? curatedResult.value.items : []
      setCurated(items)
      setTotal(curatedResult.value?.total || 0)
      if (page > 1 && items.length === 0 && curatedResult.value.total > 0) setPage(page - 1)
      setCuratedLoadFailed(false)
    } else {
      setTotal(0)
      setCuratedLoadFailed(true)
    }
    const failures = [
      pipelineResult.status === 'rejected' ? `流水线：${errorText(pipelineResult.reason, '加载失败')}` : '',
      taskResult.status === 'rejected' ? `数据任务：${errorText(taskResult.reason, '加载失败')}` : '',
      curatedResult.status === 'rejected' ? `成品数据集：${errorText(curatedResult.reason, '加载失败')}` : '',
    ].filter(Boolean)
    if (failures.length) setLoadError(failures.join('；'))
    setLoading(false)
  }, [page, pageSize, pipelineFilter, statusFilter, taskFilter])

  useEffect(() => { void load() }, [load])

  // 以成品数据集为主视角，关联其来源流水线
  const allRows = useMemo<Row[]>(() => {
    const rows: Row[] = []
    const claimed = new Set<string>()

    const pushRow = (c: CuratedDataset, pl?: Pipeline) => rows.push({
      pipelineId: pl?.id ?? '', pipelineName: pl?.name ?? '—', domain: pl?.domain || '通用',
      curatedId: c.id, curatedName: c.name, curatedStatus: c.status || 'pending_review',
      rowCount: c.row_count ?? null, quality: c.quality_score ?? null,
      updatedAt: c.updated_at ?? null,
    })

    const curatedById = new Map(curated.map(c => [c.id, c]))
    pipelines.forEach(pl => {
      curated.filter(c => !claimed.has(c.id) && c.producer_pipeline_id === pl.id).forEach(c => {
        claimed.add(c.id); pushRow(c, pl)
      })
    })
    // legacy 资产只接受流水线明确保存的 target id 绑定，名称不再作为身份。
    pipelines.forEach(pl => {
      const ids: string[] = pl.target_curated_ids ?? []
      ids.forEach(cid => {
        const c = curatedById.get(cid)
        if (c && !claimed.has(c.id)) { claimed.add(c.id); pushRow(c, pl) }
      })
    })
    // 无来源流水线的孤儿产物也要可见可管理
    curated.filter(c => !claimed.has(c.id)).forEach(c => pushRow(c))
    return rows
  }, [pipelines, curated])

  // 已发布状态的流水线（用于筛选下拉）
  const publishedPipelines = useMemo(() =>
    pipelines.filter(p => p.status === 'published'),
  [pipelines])

  // 兼容历史深链传名称和新深链传 ID；筛选内部只使用解析后的稳定 ID。
  const normalizedPipelineFilter = useMemo(() => {
    if (!pipelineFilter) return ''
    return pipelines.find(pipeline => pipeline.id === pipelineFilter || pipeline.name === pipelineFilter)?.id ?? pipelineFilter
  }, [pipelineFilter, pipelines])

  const changePipelineFilter = (value: string) => {
    setPage(1)
    setSearchParams(previous => {
      const next = new URLSearchParams(previous)
      if (value) next.set('pipeline', value)
      else next.delete('pipeline')
      return next
    }, { replace: true })
  }

  // 选中任务关联的流水线 ID
  const taskPipelineId = useMemo(() => {
    if (!taskFilter) return null
    return pipelineTasks.find(task => task.id === taskFilter)?.pipeline_id || null
  }, [taskFilter, pipelineTasks])

  const filtered = useMemo(() => {
    return allRows.filter(r => {
      if (normalizedPipelineFilter && r.pipelineId !== normalizedPipelineFilter) return false
      if (taskPipelineId && r.pipelineId !== taskPipelineId) return false
      if (statusFilter === 'pending_review' && !isPendingReview(r.curatedStatus)) return false
      if (statusFilter === 'reviewed' && isPendingReview(r.curatedStatus)) return false
      return true
    })
  }, [allRows, normalizedPipelineFilter, taskPipelineId, statusFilter])

  useEffect(() => {
    if (!focusDatasetId || loading) return
    const target = allRows.find(row => row.curatedId === focusDatasetId)
    if (target) {
      setPanelRow(current => current?.curatedId === target.curatedId ? current : target)
      return
    }

    let active = true
    curatedApi.get(focusDatasetId)
      .then(dataset => {
        if (!active) return
        const pipeline = pipelines.find(item => item.id === dataset.producer_pipeline_id)
        setPanelRow({
          pipelineId: pipeline?.id ?? dataset.producer_pipeline_id ?? '',
          pipelineName: pipeline?.name ?? '—',
          domain: pipeline?.domain || '通用',
          curatedId: dataset.id,
          curatedName: dataset.name,
          curatedStatus: dataset.status || 'pending_review',
          rowCount: dataset.row_count ?? null,
          quality: dataset.quality_score ?? null,
          updatedAt: dataset.updated_at ?? null,
        })
      })
      .catch(error => {
        if (active) setActionError(errorText(error, '无法打开指定的成品数据集'))
      })
    return () => { active = false }
  }, [allRows, focusDatasetId, loading, pipelines])

  const clearFilters = () => {
    changePipelineFilter('')
    setTaskFilter('')
    setStatusFilter('')
    setPage(1)
  }

  const totalPages = Math.max(1, Math.ceil(total / pageSize))

  const handleStatusChange = (id: string, newStatus: string) => {
    setCurated(prev => prev.map(c => c.id === id ? { ...c, status: newStatus } : c))
    if (panelRow?.curatedId === id) setPanelRow(r => r ? { ...r, curatedStatus: newStatus } : r)
    notifyAssetChanged()
    void load()
  }

  const handleDeleted = (id: string) => {
    setCurated(prev => prev.filter(c => c.id !== id))
    setPanelRow(null)
    setDeleteRow(null)
    notifyAssetChanged()
    void load()
  }

  const handleQuickDelete = async () => {
    if (!deleteRow?.curatedId) return
    setDeleting(true)
    try {
      await curatedApi.delete(deleteRow.curatedId)
      handleDeleted(deleteRow.curatedId)
      setDeleteRow(null)
    } catch (error: unknown) {
      const raw = errorText(error, '删除失败')
      toast.error(raw === 'Admin required' ? '删除数据集需要管理员权限' : String(raw))
      setDeleteRow(null)
    } finally {
      setDeleting(false)
    }
  }

  const handleQuickMigrate = async () => {
    if (!migrateRow?.curatedId) return
    setMigrating(true)
    try {
      await datasetsApi.migrateCurated(migrateRow.curatedId)
      setMigrationNotice(
        `已提交「${migrateRow.curatedName}」的迁移任务，完成后副本将出现在人工数据集页签，可在“迁移任务”中查看进度`)
      setMigrateRow(null)
    } catch (error: unknown) {
      const raw = errorText(error, '迁移任务提交失败')
      toast.error(String(raw))
      setMigrateRow(null)
    } finally {
      setMigrating(false)
    }
  }

  return (
    <div className="bg-card rounded-xl border border-border shadow-sm/50 h-full flex flex-col">
      {/* 筛选 */}
      <div className="shrink-0 flex gap-3 flex-wrap items-center px-5 pt-4 pb-3 border-b border-border">
        {/* 按流水线筛选（仅已发布） */}
        <Select
          value={publishedPipelines.some(pipeline => pipeline.id === normalizedPipelineFilter) ? normalizedPipelineFilter : '__all__'}
          onValueChange={value => changePipelineFilter(value === '__all__' ? '' : value)}
        >
          <SelectTrigger className="w-48 max-w-full rounded-lg bg-card px-3 py-1.5 text-sm" aria-label="筛选已发布流水线">
            <SelectValue placeholder="全部已发布流水线" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">全部已发布流水线</SelectItem>
            {publishedPipelines.map(pl => (
              <SelectItem key={pl.id} value={pl.id}>{pl.name}</SelectItem>
            ))}
          </SelectContent>
        </Select>

        {/* 按数据任务筛选 */}
        <Select
          value={taskFilter || '__all__'}
          onValueChange={value => { setTaskFilter(value === '__all__' ? '' : value); setPage(1) }}
        >
          <SelectTrigger className="w-44 rounded-lg bg-card px-3 py-1.5 text-sm" aria-label="筛选数据任务">
            <SelectValue placeholder="全部数据任务" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">全部数据任务</SelectItem>
            {pipelineTasks.map(task => (
              <SelectItem key={task.id} value={task.id}>{task.name}</SelectItem>
            ))}
          </SelectContent>
        </Select>

        {/* 审核状态 */}
        <Select
          value={statusFilter || '__all__'}
          onValueChange={value => { setStatusFilter(value === '__all__' ? '' : value); setPage(1) }}
        >
          <SelectTrigger className="w-36 rounded-lg bg-card px-3 py-1.5 text-sm" aria-label="筛选审核状态">
            <SelectValue placeholder="全部审核状态" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">全部审核状态</SelectItem>
            <SelectItem value="pending_review">待审核</SelectItem>
            <SelectItem value="reviewed">已处理</SelectItem>
          </SelectContent>
        </Select>

        {/* 清除筛选 */}
        {(pipelineFilter || taskFilter || statusFilter) && (
          <button
            onClick={clearFilters}
            className="flex items-center gap-1 text-xs text-[var(--color-danger)] hover:text-[var(--color-danger)] px-2 py-1 rounded hover:bg-[var(--color-danger-bg)] transition-colors"
          >
            <X size={11} /> 清除筛选
          </button>
        )}

        <button onClick={load} className="ml-auto flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground px-2 py-1.5">
          <RefreshCw size={12} /> 刷新
        </button>
        <button
          type="button"
          onClick={() => setTasksOpen(true)}
          className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground px-2 py-1.5"
          title="查看成品数据集迁移到人工数据集的异步任务进度"
        >
          <ListChecks size={12} /> 迁移任务
        </button>
      </div>

      {(loadError || actionError) && (
        <div className="mx-5 mt-3 flex shrink-0 items-start gap-2 rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-3 py-2 text-xs text-[var(--color-danger)]">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span className="flex-1">{actionError || `部分数据加载失败：${loadError}`}</span>
          {loadError && (
            <button type="button" onClick={load} className="rounded border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-card px-2 py-0.5 hover:bg-[var(--color-danger-bg)]">重试</button>
          )}
          {actionError && <button type="button" onClick={() => setActionError('')} className="text-[var(--color-danger)] hover:text-[var(--color-danger)]" aria-label="关闭错误提示">×</button>}
        </div>
      )}

      {migrationNotice && (
        <div className="mx-5 mt-3 flex shrink-0 items-start gap-2 rounded-lg border border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-[var(--color-success-bg)] px-3 py-2 text-xs text-[var(--color-success)]" data-testid="migration-submitted-banner">
          <CheckCircle size={14} className="mt-0.5 shrink-0" />
          <span className="flex-1">{migrationNotice}</span>
          <button type="button" onClick={() => setTasksOpen(true)} className="shrink-0 font-medium underline decoration-[var(--color-success)] underline-offset-2 hover:text-[var(--color-success)]">
            查看迁移任务
          </button>
          <button type="button" onClick={() => setMigrationNotice('')} className="text-[var(--color-success)] hover:text-[var(--color-success)]" aria-label="关闭成功提示">×</button>
        </div>
      )}

      {/* 表格 — 可滚动 */}
      <div className="flex-1 overflow-auto">
      {loading ? (
        <div className="flex items-center justify-center gap-2 p-12 text-sm text-[var(--color-text-tertiary)]">
          <Loader2 size={16} className="animate-spin" /> 加载数据集...
        </div>
      ) : curatedLoadFailed && curated.length === 0 ? (
        <div className="m-5 rounded-xl border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] p-10 text-center text-[var(--color-danger)]">
          <AlertTriangle size={28} className="mx-auto mb-2 opacity-70" />
          <p className="text-sm font-medium">成品数据集加载失败</p>
          <p className="mt-1 text-xs text-[var(--color-danger)]">{loadError}</p>
          <button type="button" onClick={load} className="mt-3 rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-card px-3 py-1.5 text-xs hover:bg-[var(--color-danger-bg)]">重新加载</button>
        </div>
      ) : allRows.length === 0 ? (
        <div className="m-5 border-2 border-dashed rounded-xl p-12 text-center text-[var(--color-text-tertiary)] space-y-2">
          <Table2 size={32} className="mx-auto opacity-30" />
          <p className="text-sm font-medium">暂无成品数据集</p>
          <p className="text-xs">运行数据流水线后，加工产物会自动出现在这里</p>
          <button
            onClick={() => navigate('/data/pipelines')}
            className="text-xs px-3 py-1.5 mt-1 bg-[var(--color-nav-bg)] text-[var(--color-text-inverse)] rounded-lg hover:opacity-90"
          >
            去流水线运行
          </button>
        </div>
      ) : filtered.length === 0 ? (
        <div className="m-5 border rounded-xl p-8 text-center text-[var(--color-text-tertiary)] text-sm">没有匹配的数据集</div>
      ) : (
        <table className="w-full min-w-[1040px] text-sm">
            <thead className="bg-card border-b">
              <tr>
                <th className="px-4 py-2.5 text-center font-medium text-muted-foreground text-xs">数据集</th>
                <th className="px-4 py-2.5 text-center font-medium text-muted-foreground text-xs">来源流水线</th>
                <th className="px-4 py-2.5 text-center font-medium text-muted-foreground text-xs">领域</th>
                <th className="px-4 py-2.5 text-center font-medium text-muted-foreground text-xs">行数</th>
                <th className="px-4 py-2.5 text-center font-medium text-muted-foreground text-xs">质量分</th>
                <th className="px-4 py-2.5 text-center font-medium text-muted-foreground text-xs">审核状态</th>
                <th className="px-4 py-2.5 text-center font-medium text-muted-foreground text-xs">最近更新时间</th>
                <th className="px-4 py-2.5 text-center font-medium text-muted-foreground text-xs">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {filtered.map((row, idx) => (
                <tr
                  key={`${row.pipelineId}-${row.curatedId}-${idx}`}
                  className={`transition-colors hover:bg-muted ${row.curatedId ? '' : 'opacity-60'}`}
                >
                  <td className="max-w-[240px] px-4 py-3 text-center font-medium text-foreground">
                    <span className="block truncate" title={row.curatedName}>{row.curatedName}</span>
                  </td>
                  <td className="max-w-[180px] px-4 py-3 text-center text-xs">
                    {row.pipelineId ? (
                      <button
                        type="button"
                        onClick={() => navigate(`/data/pipelines?search=${encodeURIComponent(row.pipelineId)}`)}
                        className="inline-flex max-w-full items-center gap-1 rounded-md px-1.5 py-1 font-medium text-brand-ink underline decoration-brand underline-offset-2 transition-colors hover:bg-brand-soft hover:text-brand-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        title={`打开数据流水线并筛选「${row.pipelineName}」`}
                      >
                        <Workflow size={12} className="shrink-0" />
                        <span className="truncate">{row.pipelineName}</span>
                      </button>
                    ) : <span className="text-[var(--color-text-tertiary)]">—</span>}
                  </td>
                  <td className="px-4 py-3 text-center text-xs text-muted-foreground">{row.domain}</td>
                  <td className="px-4 py-3 text-center text-xs text-muted-foreground">
                    {row.rowCount != null ? (
                      <button
                        type="button"
                        onClick={() => setPanelRow(row)}
                        className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 font-medium text-foreground underline decoration-dotted decoration-[var(--color-text-tertiary)] underline-offset-2 transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        title="点击查看分页数据"
                      >
                        {row.rowCount.toLocaleString()} 行 <Eye size={11} />
                      </button>
                    ) : '—'}
                  </td>
                  <td className="px-4 py-3 text-center text-xs">
                    {row.quality != null ? (
                      <span className={row.quality >= 0.9 ? 'text-[var(--color-success)]' : row.quality >= 0.7 ? 'text-[var(--color-warning)]' : 'text-[var(--color-danger)]'}>
                        {(row.quality * 100).toFixed(0)}%
                      </span>
                    ) : <span className="text-[var(--color-text-tertiary)]">—</span>}
                  </td>
                  <td className="px-4 py-3 text-center">
                    {row.curatedStatus ? (
                      <span className={`inline-flex items-center gap-1 text-xs px-1.5 py-0.5 rounded border ${STATUS_STYLE[row.curatedStatus] || 'bg-muted text-muted-foreground border-border'}`}>
                        {STATUS_ICON(row.curatedStatus)}
                        {STATUS_LABEL[row.curatedStatus] || row.curatedStatus}
                      </span>
                    ) : (
                      <span className="text-xs text-[var(--color-text-tertiary)]">—</span>
                    )}
                  </td>
                  <td className="whitespace-nowrap px-4 py-3 text-center text-xs tabular-nums text-muted-foreground" title={row.updatedAt || ''}>
                    {formatUpdatedAt(row.updatedAt)}
                  </td>
                  <td className="px-4 py-3 text-center">
                    <div className="flex items-center justify-center gap-2">
                      {row.curatedId && (
                        <button
                          onClick={() => setPanelRow(row)}
                          className="inline-flex items-center gap-1 rounded-lg border border-border bg-card px-2.5 py-1.5 text-xs font-medium text-muted-foreground transition hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          title="查看详情"
                        >
                          <Eye size={12} /> 查看
                        </button>
                      )}
                      {row.curatedId && (
                        <button
                          type="button"
                          onClick={() => { setMigrationNotice(''); setMigrateRow(row) }}
                          disabled={migrating}
                          className="inline-flex items-center gap-1 rounded-lg border border-border bg-card px-2.5 py-1.5 text-xs font-medium text-muted-foreground transition hover:border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] hover:bg-[var(--color-success-bg)] hover:text-[var(--color-success)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-wait disabled:opacity-50"
                          title="异步拷贝为人工数据集（结构与当前数据一致）"
                        >
                          <ArrowRightLeft size={12} /> 迁移
                        </button>
                      )}
                      {row.curatedId && (
                        <button
                          type="button"
                          onClick={() => setDeleteRow(row)}
                          className="inline-flex items-center gap-1 rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-card px-2.5 py-1.5 text-xs font-medium text-[var(--color-danger)] transition hover:bg-[var(--color-danger-bg)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring active:scale-[0.98]"
                          title="完整删除数据集"
                        >
                          <Trash2 size={12} /> 删除
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
      )}
      </div>

      {!loading && !curatedLoadFailed && total > 0 && filtered.length > 0 && (
        <div className="flex shrink-0 items-center justify-end gap-3 border-t border-border bg-card px-5 py-2.5">
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            每页
            <PageSizeSelect
              value={pageSize}
              onChange={size => { setPageSize(size); setPage(1) }}
              sizes={[10, 20, 50]}
              ariaLabel="成品数据集每页显示条数"
            />
            条
          </label>
          <span className="min-w-20 text-center text-xs tabular-nums text-muted-foreground">第 {page} / {totalPages} 页</span>
          <div className="flex items-center gap-1">
            <button type="button" onClick={() => setPage(current => Math.max(1, current - 1))} disabled={page <= 1}
              className="flex h-8 w-8 items-center justify-center rounded-lg border border-border bg-card text-muted-foreground transition hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink disabled:cursor-not-allowed disabled:opacity-35"
              aria-label="成品数据集上一页"><ChevronLeft size={14} /></button>
            <button type="button" onClick={() => setPage(current => Math.min(totalPages, current + 1))} disabled={page >= totalPages}
              className="flex h-8 w-8 items-center justify-center rounded-lg border border-border bg-card text-muted-foreground transition hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink disabled:cursor-not-allowed disabled:opacity-35"
              aria-label="成品数据集下一页"><ChevronRight size={14} /></button>
          </div>
        </div>
      )}

      {/* 详情面板 */}
      {panelRow && (
        <CuratedDetailPanel
          key={panelRow.curatedId}
          datasetId={panelRow.curatedId}
          datasetName={panelRow.curatedName}
          datasetStatus={panelRow.curatedStatus}
          pipelineName={panelRow.pipelineName}
          onClose={() => setPanelRow(null)}
          onStatusChange={handleStatusChange}
        />
      )}

      {/* 删除确认 */}
      <ConfirmDialog
        open={!!deleteRow}
        title="删除数据集"
        description={`确认完整删除「${deleteRow?.curatedName}」？该数据集、全部历史版本、审核记录和行级审核修改都将被永久删除，且不可恢复。若已被流水线或本体映射引用，删除会被拦截。`}
        confirmText={deleting ? '删除中...' : '确认删除'}
        variant="danger"
        onConfirm={handleQuickDelete}
        onClose={() => setDeleteRow(null)}
      />

      {/* 迁移确认：异步拷贝，不改动源成品数据集 */}
      <ConfirmDialog
        open={!!migrateRow}
        title="迁移到人工数据集"
        description={`确认把成品数据集「${migrateRow?.curatedName}」迁移到人工数据集？平台将在后台拷贝当前最新数据的结构与全部行，生成「${migrateRow?.curatedName}（人工副本）」；源成品数据集与审核状态保持不变，完成后可在人工数据集页签在线维护。`}
        confirmText={migrating ? '提交中...' : '确认迁移'}
        onConfirm={handleQuickMigrate}
        onClose={() => { if (!migrating) setMigrateRow(null) }}
      />

      {/* 迁移任务进度弹窗 */}
      {tasksOpen && (
        <MigrationTasksModal
          onClose={() => setTasksOpen(false)}
          onSwitchToManual={() => navigate('/data/structured?tab=raw')}
        />
      )}

    </div>
  )
}
