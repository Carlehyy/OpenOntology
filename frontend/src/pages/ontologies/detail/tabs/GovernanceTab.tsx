import { formatTime } from '@/utils/datetime'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { apiClientV2 } from '@/api/client'
import { sentinelApi, type Sentinel, type SentinelFiring } from '@/api/sentinelApi'
import pipelinesApi from '@/api/v2/pipelines'
import { useAuthStore } from '@/stores/authStore'
import { buildGovernanceKpis, readableTargetSummary } from './governanceFormat'
import {
  CheckCircle2, LayoutGrid, ScrollText,
  Loader2, RefreshCw,
} from 'lucide-react'
import { KpiOverviewGrid } from '../governance/ExecutionHero'
import ChainPanorama from '../governance/ChainPanorama'
import OperationsBoard from '../governance/OperationsBoard'
import PendingStoryDialog from '../governance/PendingStoryDialog'
import FactStream, { type FactRow } from '../governance/FactStream'
import { ApproveDialog, RejectDialog } from '../governance/DecisionDialogs'
import {
  buildAutonomyTimeline, buildDailySpark, buildOperationsRows, resolvePendingContext,
  type OperationsAutonomyLike, type PendingLog, type TimelineDot,
} from '../governance/storyModel'
import {
  buildGovernanceChain,
  type ChainDatasetLike,
  type ChainLinkMappingLike,
  type ChainMappingLike,
  type ChainPipelineLike,
} from '../governance/chainModel'
import { buildKpiSparkSeries } from '../governance/charts'
import '../governance/governanceNarrative.css'

/* 治理与推演驾驶舱 ——「链路全景 + 工作台」版:
   ⓪ 顶部总览  —— 左侧 KPI 四小卡(待审批·批准率/哨兵·自治),右侧执行心电图
   ① 链路全景  —— 七段链路真实节点与流动连线,待审批即瓶颈,点开看前因后果
   ② 治理工作台 —— 待审批/自治等级/哨兵以动作为行一表汇总,信息集中
   ③ 事实流    —— 每一个变化的出处与因果,全量留痕(原样不动) */

interface WorkspaceActionDef {
  id: string; name?: string; displayName?: string; description?: string | null
  requiresApproval?: boolean
  rules?: Array<{ type: string; name?: string; enabled?: boolean; config?: Record<string, unknown> | null; description?: string }> | null
}

interface ActionLogRow {
  id: string; actionId: string; status?: string | null
  executedAt?: string | null; durationMs?: number | null
  decisionReason?: string | null; errorMessage?: string | null
  dryRun?: boolean | null; ontologyReleaseId?: string | null
}

interface OverviewLite {
  data?: {
    instances?: number
    linkInstances?: number
    instancesBySource?: Record<string, number>
    mappings?: { bound?: number; total?: number }
  }
  runtime?: {
    daily7d?: Array<{
      date: string
      firings?: { fired?: number; error?: number } | null
      actionRuns?: { success?: number; failed?: number } | null
    }>
  }
  facts?: { total?: number }
}

interface InstanceCatalogLite {
  objectTypes: Array<{ id: string; name: string; displayName?: string }>
}

/** 发布快照(workspace)里链路全景需要的部分:动作定义 + 映射 + 关系映射。 */
interface WorkspaceLite {
  actions?: WorkspaceActionDef[]
  mappings?: ChainMappingLike[]
  linkMappings?: ChainLinkMappingLike[]
}

interface CuratedDatasetLite extends ChainDatasetLike {
  status?: string | null
}

interface DatasetOverviewLite {
  items?: Array<{
    id: string
    name?: string | null
    source?: string | null
    rowcount?: number | null
  }>
}

interface GovernanceTabProps {
  ontologyId: string
  currentReleaseId?: string | null
  currentReleaseVersion: string
  /** 打开历史版本弹窗(由详情页壳层提供,保持当前治理现场不跳走)。 */
  onOpenVersions: () => void
}

const BACKGROUND_REFRESH_INTERVAL_MS = 12_000
const BACKGROUND_REFRESH_MAX_CYCLES = 10
const SUCCESS_MESSAGE_DISMISS_MS = 5_000

function SectionHead({ icon: Icon, iconCls, title, sub, badge, extra }: {
  icon: any; iconCls: string; title: string; sub: string
  badge?: React.ReactNode; extra?: React.ReactNode
}) {
  return (
    <div className="mb-3 flex flex-wrap items-center gap-2">
      <Icon size={16} className={iconCls} />
      <p className="whitespace-nowrap text-[15px] font-semibold text-foreground">{title}</p>
      {badge}
      <span className="text-xs text-[var(--color-text-tertiary)]">{sub}</span>
      {extra && <div className="ml-auto">{extra}</div>}
    </div>
  )
}

function errorMessage(error: unknown, fallback = '治理数据加载失败'): string {
  if (!error || typeof error !== 'object') return fallback
  const candidate = error as { detail?: unknown; message?: unknown }
  if (typeof candidate.detail === 'string') return candidate.detail
  if (candidate.detail && typeof candidate.detail === 'object' && 'message' in candidate.detail) {
    const message = (candidate.detail as { message?: unknown }).message
    if (typeof message === 'string') return message
  }
  return typeof candidate.message === 'string' ? candidate.message : fallback
}

export default function GovernanceTab({
  ontologyId, currentReleaseId, currentReleaseVersion, onOpenVersions,
}: GovernanceTabProps) {
  const qc = useQueryClient()
  const role = useAuthStore(state => state.user?.role)
  const canDecide = role === 'admin'
  const [busy, setBusy] = useState<string | null>(null)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [rejectTarget, setRejectTarget] = useState<PendingLog | null>(null)
  const [rejectError, setRejectError] = useState<string | null>(null)
  const [approveTarget, setApproveTarget] = useState<PendingLog | null>(null)
  const [approveError, setApproveError] = useState<string | null>(null)
  const [storyTarget, setStoryTarget] = useState<PendingLog | null>(null)
  const [kindFilter, setKindFilter] = useState('')
  const [isPageVisible, setIsPageVisible] = useState(() =>
    typeof document === 'undefined' || document.visibilityState === 'visible')
  const [remainingRefreshCycles, setRemainingRefreshCycles] = useState(
    BACKGROUND_REFRESH_MAX_CYCLES)
  const [manualRefreshPending, setManualRefreshPending] = useState(false)
  const boardRef = useRef<HTMLDivElement>(null)

  const releaseParam = currentReleaseId
    ? `release_id=${encodeURIComponent(currentReleaseId)}`
    : ''
  const queryEnabled = Boolean(currentReleaseId)
  const pendingQuery = useQuery<PendingLog[]>({
    queryKey: ['gov-pending', ontologyId, currentReleaseId],
    queryFn: () => apiClientV2.get(
      `/formal/ontologies/${ontologyId}/pending-actions?${releaseParam}`) as any,
    enabled: queryEnabled,
  })
  const autonomyQuery = useQuery<OperationsAutonomyLike[]>({
    queryKey: ['gov-autonomy', ontologyId, currentReleaseId],
    queryFn: () => apiClientV2.get(
      `/formal/ontologies/${ontologyId}/autonomy?${releaseParam}`) as any,
    enabled: queryEnabled,
  })
  const sentinelsQuery = useQuery<Sentinel[]>({
    queryKey: ['gov-sentinels', ontologyId, currentReleaseId],
    queryFn: () => sentinelApi.list(ontologyId, currentReleaseId) as any,
    enabled: queryEnabled,
  })
  const firingsQuery = useQuery<SentinelFiring[]>({
    queryKey: ['gov-firings', ontologyId, currentReleaseId],
    queryFn: () => sentinelApi.firings(ontologyId, currentReleaseId) as any,
    enabled: queryEnabled,
  })
  const factsQuery = useQuery<FactRow[]>({
    queryKey: ['gov-facts', ontologyId, currentReleaseId, kindFilter],
    queryFn: () => apiClientV2.get(
      `/formal/ontologies/${ontologyId}/facts/recent?limit=50&${releaseParam}${kindFilter ? `&kind=${kindFilter}` : ''}`) as any,
    enabled: queryEnabled,
  })
  // 叙事新增:动作定义(rules/description)、执行履历(自治时间线)、
  // 发布快照类型名(绑定句)、总览(心电图与执行链计数)。全部只读。
  // 链路全景复用同一快照的 mappings/linkMappings(上游三段)与 actions(动作段)。
  const workspaceQuery = useQuery<WorkspaceLite>({
    queryKey: ['gov-workspace', ontologyId],
    queryFn: () => apiClientV2.get(`/ontologies/${ontologyId}/current-release/workspace`) as any,
    enabled: queryEnabled,
    staleTime: 60_000,
  })
  const logsQuery = useQuery<ActionLogRow[]>({
    queryKey: ['gov-logs', ontologyId, currentReleaseId],
    queryFn: () => apiClientV2.get(`/formal/ontologies/${ontologyId}/logs`) as any,
    enabled: queryEnabled,
    staleTime: 30_000,
  })
  const catalogQuery = useQuery<InstanceCatalogLite>({
    queryKey: ['instance-browser-catalog', ontologyId],
    queryFn: () => apiClientV2.get(`/formal/ontologies/${ontologyId}/instance-browser/catalog`) as any,
    enabled: queryEnabled,
    staleTime: 60_000,
    retry: 1,
  })
  const overviewQuery = useQuery<OverviewLite>({
    queryKey: ['formal-overview', ontologyId],
    queryFn: () => apiClientV2.get(`/formal/ontologies/${ontologyId}/overview`) as any,
    enabled: queryEnabled,
    staleTime: 30_000,
    retry: 1,
  })
  // 链路全景上游三段:被映射引用的成品数据集/人工数据集及其产出管道。
  // queryKey 与数据映射页共享,命中同一缓存;全部只读。
  const curatedQuery = useQuery<CuratedDatasetLite[]>({
    queryKey: ['curated-all'],
    queryFn: async () => {
      const data = await apiClientV2.get('/curated') as any
      return Array.isArray(data) ? data : data?.items || []
    },
    enabled: queryEnabled,
    staleTime: 60_000,
    retry: 1,
  })
  const manualDatasetsQuery = useQuery<DatasetOverviewLite>({
    queryKey: ['manual-datasets-overview'],
    queryFn: () => apiClientV2.get('/datasets/overview') as any,
    enabled: queryEnabled,
    staleTime: 60_000,
    retry: 1,
  })
  const pipelinesQuery = useQuery<ChainPipelineLike[]>({
    queryKey: ['gov-pipelines'],
    queryFn: () => pipelinesApi.list() as any,
    enabled: queryEnabled,
    staleTime: 60_000,
    retry: 1,
  })

  const pending = pendingQuery.data ?? []
  const autonomy = autonomyQuery.data ?? []
  const sentinels = sentinelsQuery.data ?? []
  const firings = firingsQuery.data ?? []
  const facts = factsQuery.data ?? []
  const workspaceActions = workspaceQuery.data?.actions ?? []
  const workspaceMappings = workspaceQuery.data?.mappings ?? []
  const workspaceLinkMappings = workspaceQuery.data?.linkMappings ?? []
  const releaseLogs = (logsQuery.data ?? []).filter(
    log => !currentReleaseId || !log.ontologyReleaseId || log.ontologyReleaseId === currentReleaseId,
  )
  const governanceQueries = [
    pendingQuery, autonomyQuery, sentinelsQuery, firingsQuery, factsQuery,
  ]
  const failedQuery = governanceQueries.find(query => query.isError)
  const isInitialLoading = governanceQueries.some(query => query.isLoading)
  const isRefreshing = governanceQueries.some(query => query.isFetching)
  const lastUpdatedAt = Math.max(
    0, ...governanceQueries.map(query => query.dataUpdatedAt))
  const backgroundRefreshActive = queryEnabled && remainingRefreshCycles > 0
  const kpis = buildGovernanceKpis({ pending, autonomy, sentinels })

  const objectTypeName = useCallback((objectTypeId: string) => {
    const hit = catalogQuery.data?.objectTypes?.find(item => item.id === objectTypeId)
    return hit ? hit.displayName || hit.name : objectTypeId || '未知类型'
  }, [catalogQuery.data])

  const overview = overviewQuery.data
  const dailySpark = buildDailySpark(overview?.runtime?.daily7d)
  // 与总览页"运行汇总"同一张趋势图:直接吃 daily7d 按日桶(空值归零),
  // 命中/错误/成功/失败四序列口径与总览完全一致。
  const runtimeDays = useMemo(
    () => (overview?.runtime?.daily7d ?? []).map(day => ({
      date: day.date,
      firings: { fired: day.firings?.fired ?? 0, error: day.firings?.error ?? 0 },
      actionRuns: { success: day.actionRuns?.success ?? 0, failed: day.actionRuns?.failed ?? 0 },
    })),
    [overview?.runtime?.daily7d],
  )
  // 四个 KPI 卡的近 7 日迷你图序列(决策/批准率从执行日志按日归桶)。
  const kpiSparks = useMemo(
    () => buildKpiSparkSeries({ daily7d: dailySpark, logs: releaseLogs }),
    [dailySpark, releaseLogs],
  )

  // 七段链路全景:上游(管道→数据集→映射)+ 治理环路(实例→哨兵→待审批→动作)。
  const chain = useMemo(() => {
    const referencedIds = new Set<string>()
    for (const mapping of workspaceMappings) {
      const id = mapping.curated_dataset_id || mapping.curatedDatasetId
      if (id) referencedIds.add(id)
    }
    for (const linkMapping of workspaceLinkMappings) {
      for (const id of [linkMapping.src_dataset_id, linkMapping.tgt_dataset_id, linkMapping.edge_dataset_id]) {
        if (id) referencedIds.add(id)
      }
    }
    const curated = (curatedQuery.data ?? [])
      .filter(item => referencedIds.has(item.id))
    const manual = (manualDatasetsQuery.data?.items ?? [])
      .filter(item => referencedIds.has(item.id))
      .map(item => ({
        id: item.id,
        name: item.name ?? null,
        row_count: item.rowcount ?? null,
        quality_score: null,
        producer_pipeline_id: null,
      }))
    const datasets: ChainDatasetLike[] = [...curated, ...manual]
    const producerIds = new Set(
      datasets.map(item => item.producer_pipeline_id).filter(Boolean) as string[],
    )
    const pipelines = (pipelinesQuery.data ?? []).filter(item => producerIds.has(item.id))
    return buildGovernanceChain({
      pending,
      firings,
      sentinels,
      actions: workspaceActions,
      autonomy,
      mappings: workspaceMappings,
      linkMappings: workspaceLinkMappings,
      datasets,
      pipelines,
      instanceTotal: (overview?.data?.instances ?? 0) + (overview?.data?.linkInstances ?? 0),
      targetLabel: log => readableTargetSummary(log),
      objectTypeName,
    })
  }, [
    workspaceMappings, workspaceLinkMappings, curatedQuery.data, manualDatasetsQuery.data,
    pipelinesQuery.data, pending, firings, sentinels, workspaceActions, autonomy,
    overview, objectTypeName,
  ])

  const storyContext = storyTarget
    ? resolvePendingContext(storyTarget, firings, sentinels, workspaceActions)
    : { firing: null, sentinel: null, actionDef: null }

  // 治理工作台:以动作为行,并联待审批条目与绑定哨兵状态(有待审批的排前)。
  const boardRows = useMemo(
    () => buildOperationsRows({ autonomy, pending, firings }),
    [autonomy, pending, firings],
  )
  const boardTimelines = useMemo<Record<string, TimelineDot[]>>(
    () => Object.fromEntries(
      autonomy.map(stat => [stat.actionId, buildAutonomyTimeline(releaseLogs, stat.actionId)]),
    ),
    [autonomy, releaseLogs],
  )

  const scrollToBoard = () => {
    boardRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  // 待审批卡的直达路径:有待审批直接打开第一条的前因后果弹窗,免一跳;
  // 没有待审批则照旧滚动到工作台。
  const openFirstPendingStory = () => {
    const log = pending[0]
    if (log) setStoryTarget(log)
    else scrollToBoard()
  }

  const refreshAll = useCallback(() => Promise.all([
    qc.invalidateQueries({ queryKey: ['gov-pending', ontologyId] }),
    qc.invalidateQueries({ queryKey: ['gov-autonomy', ontologyId] }),
    qc.invalidateQueries({ queryKey: ['gov-sentinels', ontologyId] }),
    qc.invalidateQueries({ queryKey: ['gov-firings', ontologyId] }),
    qc.invalidateQueries({ queryKey: ['gov-facts', ontologyId] }),
    qc.invalidateQueries({ queryKey: ['gov-logs', ontologyId] }),
    qc.invalidateQueries({ queryKey: ['formal-overview', ontologyId] }),
  ]), [ontologyId, qc])

  useEffect(() => {
    setRemainingRefreshCycles(BACKGROUND_REFRESH_MAX_CYCLES)
  }, [currentReleaseId, ontologyId])

  useEffect(() => {
    const syncVisibility = () => {
      setIsPageVisible(document.visibilityState === 'visible')
    }
    document.addEventListener('visibilitychange', syncVisibility)
    return () => document.removeEventListener('visibilitychange', syncVisibility)
  }, [])

  useEffect(() => {
    if (
      !backgroundRefreshActive
      || !isPageVisible
      || isInitialLoading
      || isRefreshing
      || failedQuery
    ) return
    const timer = window.setTimeout(() => {
      setRemainingRefreshCycles(current => Math.max(0, current - 1))
      void refreshAll()
    }, BACKGROUND_REFRESH_INTERVAL_MS)
    return () => window.clearTimeout(timer)
  }, [
    backgroundRefreshActive, failedQuery, isInitialLoading, isPageVisible,
    isRefreshing, refreshAll, remainingRefreshCycles,
  ])

  // 成功反馈短暂展示后自动消退;错误反馈常驻,需用户注意。
  useEffect(() => {
    if (!msg?.ok) return
    const timer = window.setTimeout(() => setMsg(null), SUCCESS_MESSAGE_DISMISS_MS)
    return () => window.clearTimeout(timer)
  }, [msg])

  const reloadReleaseContext = () => {
    qc.invalidateQueries({ queryKey: ['ontology', ontologyId] })
    setRemainingRefreshCycles(BACKGROUND_REFRESH_MAX_CYCLES)
    void refreshAll()
  }

  const refreshNow = async () => {
    setRemainingRefreshCycles(BACKGROUND_REFRESH_MAX_CYCLES)
    setManualRefreshPending(true)
    try {
      await refreshAll()
    } finally {
      setManualRefreshPending(false)
    }
  }

  const decide = async (
    log: PendingLog,
    decision: 'approved' | 'rejected',
    reason?: string,
  ) => {
    setBusy(log.id)
    setMsg(null)
    if (decision === 'rejected') setRejectError(null)
    else setApproveError(null)
    try {
      await apiClientV2.post(`/formal/ontologies/${ontologyId}/action-logs/${log.id}/decide`,
        { decision, reason, releaseId: currentReleaseId })
      setMsg({ ok: true, text: decision === 'approved' ? '已批准并提交执行，决策已写入事实流。' : '已拒绝，决策已写入事实流。' })
      if (decision === 'approved') {
        setApproveTarget(null)
      }
      if (decision === 'rejected') {
        setRejectTarget(null)
      }
      setRemainingRefreshCycles(BACKGROUND_REFRESH_MAX_CYCLES)
      void refreshAll()
    } catch (e: any) {
      const text = errorMessage(e, '决策失败，请重试')
      if (decision === 'rejected') setRejectError(text)
      else setApproveError(text)
    } finally {
      setBusy(null)
    }
  }

  const lastRefreshText = lastUpdatedAt
    ? formatTime(new Date(lastUpdatedAt), { seconds: true })
    : '尚未完成'

  if (!currentReleaseId) {
    return (
      <div className="rounded-xl border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] p-4 text-sm text-[var(--color-danger)]">
        当前本体没有有效的发布指针。为避免混入草稿数据，治理推演已停止加载。
      </div>
    )
  }

  if (isInitialLoading) {
    return (
      <div className="flex items-center justify-center gap-2 py-16 text-sm text-muted-foreground">
        <Loader2 size={16} className="animate-spin" /> 正在加载当前发布版治理数据…
      </div>
    )
  }

  if (failedQuery) {
    return (
      <div className="rounded-xl border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] p-4 text-sm text-[var(--color-danger)]">
        <p className="font-medium">当前发布版治理数据加载失败</p>
        <p className="mt-1 text-xs">{errorMessage(failedQuery.error)}。页面已停止展示，避免把失败误判为“暂无数据”。</p>
        <button type="button" onClick={reloadReleaseContext}
          className="mt-3 rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-card px-3 py-1.5 text-xs hover:bg-[var(--color-danger-bg)]">
          重新加载
        </button>
      </div>
    )
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-brand-line bg-brand-soft px-3 py-2 text-xs text-brand-ink">
        <span className="inline-flex items-center gap-2">
          <CheckCircle2 size={13} />
          数据范围已锁定到最新发布版 <span className="font-mono font-semibold">{currentReleaseVersion}</span>
        </span>
        <span className="hidden h-3 w-px bg-brand-mist sm:block" aria-hidden="true" />
        <span
          className="inline-flex items-center gap-1.5 font-medium"
          data-testid="governance-background-refresh-status"
          role="status"
          aria-live="polite"
          aria-busy={isRefreshing}
        >
          {(backgroundRefreshActive || isRefreshing)
            ? <Loader2 size={12} className={isPageVisible ? 'animate-spin' : ''} />
            : <CheckCircle2 size={12} />}
          {backgroundRefreshActive
            ? isPageVisible
              ? '自动同步中 · 正在获取最新的审批与哨兵结果'
              : '自动同步已暂停 · 页面隐藏中'
            : isRefreshing ? '正在刷新治理结果' : '自动同步已结束 · 可手动刷新'}
        </span>
        {backgroundRefreshActive && isPageVisible && (
          <span className="text-brand-ink">每 12 秒刷新，最多 2 分钟</span>
        )}
        <span className="text-brand-ink" data-testid="governance-last-refreshed">
          最近刷新：{lastRefreshText}
        </span>
        <button
          type="button"
          onClick={() => void refreshNow()}
          disabled={manualRefreshPending}
          className="ml-auto inline-flex items-center gap-1 rounded-md border border-brand-line bg-card px-2 py-1 font-medium text-brand-ink transition hover:bg-card disabled:cursor-wait disabled:opacity-60"
          aria-label="立即刷新治理结果"
          title="立即刷新，并重新开始最多 2 分钟的自动同步"
        >
          <RefreshCw size={11} className={manualRefreshPending ? 'animate-spin' : ''} />
          立即刷新
        </button>
      </div>
      {msg && (
        <div role={msg.ok ? 'status' : 'alert'} aria-live={msg.ok ? 'polite' : 'assertive'}
          className={`rounded-lg border px-3 py-2 text-xs ${
          msg.ok ? 'border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-[var(--color-success-bg)] text-[var(--color-success)]' : 'border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] text-[var(--color-danger)]'
        }`}>{msg.text}</div>
      )}

      <KpiOverviewGrid
        kpis={kpis}
        runtimeDays={runtimeDays}
        sparks={kpiSparks}
        isRefreshing={isRefreshing}
        onNavigate={scrollToBoard}
        onOpenFirstPending={openFirstPendingStory}
      />

      {/* ① 链路全景:七段链路真实节点,待审批即当前瓶颈,点开看前因后果(点击不跳转) */}
      <ChainPanorama
        nodes={chain.nodes}
        edges={chain.edges}
        guides={chain.guides}
        isRefreshing={isRefreshing}
        isChainLoading={
          workspaceQuery.isPending || curatedQuery.isPending
          || manualDatasetsQuery.isPending || pipelinesQuery.isPending
        }
        onOpenPending={logId => {
          const log = pending.find(item => item.id === logId)
          if (log) setStoryTarget(log)
        }}
      />

      {/* ② 治理工作台:待审批 / 自治等级 / 哨兵以动作为行一表汇总 */}
      <div ref={boardRef} className="rounded-xl border bg-card p-5">
        <SectionHead icon={LayoutGrid} iconCls="text-brand-ink" title="治理工作台"
          badge={pending.length > 0 && (
            <span className="rounded-full bg-[var(--color-warning-bg)] px-2 py-0.5 text-[11px] font-medium text-[var(--color-warning)]">
              {pending.length} 项待裁决
            </span>
          )}
          sub="待审批 · 自治等级 · 哨兵以动作为中心一表汇总 · 点击待审批条目看前因后果"
          extra={<button onClick={onOpenVersions}
            className="inline-flex items-center gap-1 text-xs text-brand-ink hover:underline">
            在版本草稿中调整</button>} />
        <OperationsBoard
          rows={boardRows}
          timelines={boardTimelines}
          onOpenPending={log => setStoryTarget(log)}
          onGoVersions={onOpenVersions}
        />
      </div>

      {/* ③ 事实流:全宽审计底(原样不动) */}
      <div className="rounded-xl border bg-card p-5">
        <SectionHead icon={ScrollText} iconCls="text-viz-indigo" title="事实流"
          sub="追加不修改 · 每个变化都有出处与因果 · 最近 50 条" />
        <FactStream facts={facts} kindFilter={kindFilter} onKindFilterChange={setKindFilter} />
      </div>

      <PendingStoryDialog
        ontologyId={ontologyId}
        target={storyTarget}
        firing={storyContext.firing}
        sentinel={storyContext.sentinel}
        actionDef={storyContext.actionDef}
        objectTypeName={objectTypeName}
        canDecide={canDecide}
        busy={Boolean(storyTarget && busy === storyTarget.id)}
        onClose={() => setStoryTarget(null)}
        onApprove={log => {
          setStoryTarget(null)
          setApproveError(null)
          setApproveTarget(log)
        }}
        onReject={log => {
          setStoryTarget(null)
          setRejectError(null)
          setRejectTarget(log)
        }}
      />
      <RejectDialog
        target={rejectTarget}
        busy={Boolean(rejectTarget && busy === rejectTarget.id)}
        error={rejectError}
        onClose={() => {
          if (rejectTarget && busy === rejectTarget.id) return
          setRejectTarget(null)
          setRejectError(null)
        }}
        onConfirm={reason => {
          if (!rejectTarget || busy === rejectTarget.id) return
          void decide(rejectTarget, 'rejected', reason)
        }}
      />
      <ApproveDialog
        target={approveTarget}
        busy={Boolean(approveTarget && busy === approveTarget.id)}
        error={approveError}
        onClose={() => {
          if (approveTarget && busy === approveTarget.id) return
          setApproveTarget(null)
          setApproveError(null)
        }}
        onConfirm={() => {
          if (!approveTarget || busy === approveTarget.id) return
          void decide(approveTarget, 'approved')
        }}
      />
    </div>
  )
}
