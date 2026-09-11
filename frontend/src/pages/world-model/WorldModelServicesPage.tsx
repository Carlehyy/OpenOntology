import { formatDateTime } from '@/utils/datetime'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { motion, useReducedMotion } from 'motion/react'
import { SPRING_LAYOUT } from '@/components/motion-ui/ease'
import {
  Activity,
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Copy,
  ExternalLink,
  Eye,
  Gauge,
  Play,
  Power,
  RefreshCw,
  Rocket,
  Route,
  Search,
  Terminal,
  X,
  XCircle,
} from 'lucide-react'

import { ontologyApi } from '@/api/ontologies'
import {
  apiError,
  worldModelApi,
  type CallRecordItem,
  type ServiceInvokeResult,
  type WorldModelServiceOverview,
  type WorldModelServiceSummary,
} from '@/api/worldModel'
import { Button } from '@/components/ui/Button'
import { ConfirmModal, Modal } from '@/components/ui/Modal'
import { toast } from 'sonner'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Tooltip } from '@/components/motion-ui/tooltip'
import { writeTextToClipboard } from '@/utils/clipboard'
import { validateJsonObject, type JsonParseIssue } from '@/utils/jsonInput'
import StatCard from './StatCard'
import { formatDurationMs, formatSuccessRate } from './statsFormat'

const PAGE_SIZE = 20
const DEFAULT_INVOKE_INPUT = JSON.stringify({ context: {}, actions: [], horizon: 3 }, null, 2)

/** 端点路径（/api/v2/...）拼成当前部署下可直接调用的完整地址 */
function fullEndpointUrl(endpointPath: string): string {
  if (/^https?:\/\//i.test(endpointPath)) return endpointPath
  return window.location.origin + endpointPath
}

/** 外部集成用 curl 示例：完整 URL + 鉴权头 + 当前入参压缩为单行 */
function buildCurlExample(url: string, inputText: string): string {
  const compact = inputText.replace(/\s+/g, ' ').trim() || '{}'
  return [
    `curl -X POST "${url}" \\`,
    `  -H "Authorization: Bearer <访问令牌>" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -d '${compact}'`,
  ].join('\n')
}

type StatusFilter = '' | 'online' | 'offline'

function statusBadge(status: string) {
  return status === 'online'
    ? <span className="inline-flex items-center rounded-md bg-brand-soft px-1.5 py-0.5 text-[11px] text-brand-ink">在线</span>
    : <span className="inline-flex items-center rounded-md bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground">已下线</span>
}

export default function WorldModelServicesPage() {
  const reduce = useReducedMotion() ?? false
  const navigate = useNavigate()
  const [items, setItems] = useState<WorldModelServiceSummary[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [draftKeyword, setDraftKeyword] = useState('')
  const [draftStatus, setDraftStatus] = useState<StatusFilter>('')
  const [keyword, setKeyword] = useState('')
  const [status, setStatus] = useState<StatusFilter>('')

  const [invokeTarget, setInvokeTarget] = useState<WorldModelServiceSummary | null>(null)
  const [invokeInput, setInvokeInput] = useState(DEFAULT_INVOKE_INPUT)
  const [invoking, setInvoking] = useState(false)
  const [invokeResult, setInvokeResult] = useState<ServiceInvokeResult | null>(null)
  // 入参 JSON 即时校验：输入时定位语法错误，提交前再拦一次
  const [jsonIssue, setJsonIssue] = useState<JsonParseIssue | null>(null)
  // 发布版本自带的调试入参作为试调用示例，避免首次试调用必然空转
  const [exampleInput, setExampleInput] = useState<string | null>(null)
  const [copiedCurl, setCopiedCurl] = useState(false)

  const [detailTarget, setDetailTarget] = useState<WorldModelServiceSummary | null>(null)
  const [ontologyName, setOntologyName] = useState('')
  const [objectTypeLabels, setObjectTypeLabels] = useState<Record<string, string>>({})
  const [serviceCalls, setServiceCalls] = useState<CallRecordItem[]>([])
  const [serviceCallsTotal, setServiceCallsTotal] = useState(0)
  const [serviceCallsLoading, setServiceCallsLoading] = useState(false)

  const [copiedId, setCopiedId] = useState<string | null>(null)
  const [togglingId, setTogglingId] = useState<string | null>(null)
  const [offlineTarget, setOfflineTarget] = useState<WorldModelServiceSummary | null>(null)
  const [overview, setOverview] = useState<WorldModelServiceOverview | null>(null)
  // 概览接口失败与「真的是 0」必须区分：失败时显示占位与重试，而不是误导性的 0
  const [overviewError, setOverviewError] = useState(false)
  const exampleRequestSeq = useRef(0)

  const loadOverview = useCallback(async () => {
    try {
      setOverview(await worldModelApi.servicesOverview())
      setOverviewError(false)
    } catch {
      setOverview(null)
      setOverviewError(true)
    }
  }, [])

  useEffect(() => { void loadOverview() }, [loadOverview])

  const loadServices = useCallback(async (silent = false) => {
    if (!silent) setLoading(true)
    setError('')
    try {
      const result = await worldModelApi.listServices({
        page, size: PAGE_SIZE, keyword, status: status || undefined,
      })
      // 防御：异常/降级响应可能缺少 items 字段（曾由兜底 mock 触发整页白屏）
      setItems(result.items ?? [])
      setTotal(result.total ?? 0)
    } catch (err) {
      setError(apiError(err))
    } finally {
      if (!silent) setLoading(false)
    }
  }, [page, keyword, status])

  useEffect(() => { void loadServices() }, [loadServices])

  const applyFilters = () => {
    setPage(1)
    setKeyword(draftKeyword.trim())
    setStatus(draftStatus)
  }

  const resetFilters = () => {
    setDraftKeyword('')
    setDraftStatus('')
    setPage(1)
    setKeyword('')
    setStatus('')
  }

  const refreshAll = async () => {
    setRefreshing(true)
    try {
      await Promise.all([loadServices(true), loadOverview()])
    } finally {
      setRefreshing(false)
    }
  }

  const toggleStatus = async (item: WorldModelServiceSummary) => {
    if (togglingId) return
    setTogglingId(item.id)
    const next = item.status === 'online' ? 'offline' : 'online'
    try {
      const updated = await worldModelApi.setServiceStatusById(item.id, next)
      setItems(current => current.map(row => row.id === updated.id ? { ...row, ...updated } : row))
      toast.success(next === 'online' ? '服务已上线' : '服务已下线')
      setOfflineTarget(null)
      void loadOverview()
    } catch (err) {
      toast.error('状态切换失败', { description: apiError(err) })
    } finally {
      setTogglingId(null)
    }
  }

  // 下线会影响正在集成的调用方，先经确认弹窗；上线保持轻量直接执行
  const requestToggleStatus = (item: WorldModelServiceSummary) => {
    if (item.status === 'online') {
      setOfflineTarget(item)
    } else {
      void toggleStatus(item)
    }
  }

  const copyEndpoint = (item: WorldModelServiceSummary) => {
    if (!item.endpoint_path) return
    // 复制完整可调用地址（协议+主机+路径），拿到后无需再自行拼接部署地址
    writeTextToClipboard(fullEndpointUrl(item.endpoint_path)).then(() => {
      setCopiedId(item.id)
      window.setTimeout(() => setCopiedId(null), 1400)
    }).catch(() => {
      // 剪贴板写入可能被浏览器拒绝（HTTP 部署/未聚焦）：如实提示手动路径
      toast.error('未能写入剪贴板', { description: '请手动选中端点文本后复制。' })
    })
  }

  const copyCurlExample = (curl: string) => {
    writeTextToClipboard(curl).then(() => {
      setCopiedCurl(true)
      window.setTimeout(() => setCopiedCurl(false), 1400)
    }).catch(() => {
      toast.error('未能写入剪贴板', { description: '请手动选中示例文本后复制。' })
    })
  }

  const openInvoke = (item: WorldModelServiceSummary) => {
    setInvokeTarget(item)
    setInvokeInput(DEFAULT_INVOKE_INPUT)
    setInvokeResult(null)
    setJsonIssue(null)
    setExampleInput(null)
    setCopiedCurl(false)
    // 带入该服务发布版本的调试入参作为示例；用户已改输入时不覆盖。
    // seq 守卫：连续打开不同服务时只采纳最后一次请求的结果
    const seq = ++exampleRequestSeq.current
    if (!item.version_id) return
    worldModelApi.getVersion(item.project_id, item.version_id)
      .then(version => {
        if (seq !== exampleRequestSeq.current) return
        const testInput = version?.test_input
        if (!testInput || Object.keys(testInput).length === 0) return
        const text = JSON.stringify(testInput, null, 2)
        setExampleInput(text)
        setInvokeInput(current => (current === DEFAULT_INVOKE_INPUT ? text : current))
      })
      .catch(() => undefined)
  }

  const handleInvokeInputChange = (text: string) => {
    setInvokeInput(text)
    setJsonIssue(text.trim() ? validateJsonObject(text).issue : null)
  }

  const submitInvoke = async () => {
    if (!invokeTarget) return
    const validated = validateJsonObject(invokeInput)
    if (validated.issue) {
      setJsonIssue(validated.issue)
      toast.error('测试入参不是有效 JSON', { description: '错误位置见输入框下方提示。' })
      return
    }
    setJsonIssue(null)
    const parsed = validated.value
    const body = {
      context: (parsed.context ?? {}) as Record<string, unknown>,
      actions: (parsed.actions ?? []) as unknown[],
      horizon: typeof parsed.horizon === 'number' ? parsed.horizon : Number(parsed.horizon ?? 1) || 1,
    }
    setInvoking(true)
    setInvokeResult(null)
    try {
      const result = await worldModelApi.invokeService(invokeTarget.id, body)
      setInvokeResult(result)
      if (result.ok) {
        // 脚本正常执行但返回空轨迹（如被边界拒绝）时如实提示，避免误读为有效预测
        const payload = result.payload as { trajectory?: unknown; boundary?: unknown } | null
        const emptyTrajectory = !!payload && Array.isArray(payload.trajectory) && payload.trajectory.length === 0
        const boundaryNote = typeof payload?.boundary === 'string' ? payload.boundary : ''
        toast.success('调用成功', { description: emptyTrajectory
            ? `耗时 ${result.duration_ms} ms · 注意：未产生预测输出${boundaryNote ? `（${boundaryNote}）` : ''}`
            : '耗时 ' + result.duration_ms + ' ms' })
        setItems(current => current.map(row => row.id === invokeTarget.id
          ? { ...row, call_count: row.call_count + 1 }
          : row))
        void loadOverview()
      }
    } catch (err) {
      setInvokeResult({ ok: false, payload: null, error: apiError(err), duration_ms: 0, call_id: null })
    } finally {
      setInvoking(false)
    }
  }

  const openDetail = (item: WorldModelServiceSummary) => {
    setDetailTarget(item)
    setOntologyName('')
    setObjectTypeLabels({})
    setServiceCalls([])
    setServiceCallsTotal(0)
    const ontologyId = item.applicable_object_types?.ontology_id
    if (ontologyId) {
      ontologyApi.list({ page_size: 200 })
        .then(result => {
          const found = result.items.find(entry => entry.id === ontologyId)
          if (found) setOntologyName(found.name)
        })
        .catch(() => undefined)
      ontologyApi.listEntities(ontologyId)
        .then(entities => {
          const labels: Record<string, string> = {}
          entities.forEach(entity => {
            labels[entity.id] = entity.name_cn || entity.name_en || entity.id
          })
          setObjectTypeLabels(labels)
        })
        .catch(() => undefined)
    }
    setServiceCallsLoading(true)
    worldModelApi.listCalls({ service_id: item.id, page: 1, size: 10 })
      .then(result => {
        setServiceCalls(result.items)
        setServiceCallsTotal(result.total)
      })
      .catch(() => { setServiceCalls([]); setServiceCallsTotal(0) })
      .finally(() => setServiceCallsLoading(false))
  }

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <div className="space-y-4">
      {/* 概览统计条：全局聚合，直读服务规模与调用健康度；接口失败时占位+重试，绝不显示误导性的 0 */}
      {overviewError ? (
        <section
          className="flex flex-wrap items-center gap-3 rounded-xl border border-[var(--color-warning)] bg-[var(--color-warning-bg)] px-4 py-3 text-xs text-[var(--color-warning)]"
          aria-label="推演服务概览"
          role="alert"
        >
          <AlertTriangle size={15} className="shrink-0" />
          <span>概览统计暂时不可用（加载失败），不代表服务规模或调用健康度。</span>
          <button
            type="button"
            onClick={() => void loadOverview()}
            className="ml-auto inline-flex h-7 items-center gap-1 rounded-lg border border-[var(--color-warning)] bg-card px-2.5 text-[11px] font-medium text-[var(--color-warning)] hover:bg-[var(--color-warning-bg)]"
          >
            <RefreshCw size={12} /> 重试
          </button>
        </section>
      ) : (
        <section className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5" aria-label="推演服务概览">
          <StatCard
            icon={<Rocket size={17} />}
            label="服务总数"
            value={overview?.total ?? 0}
            sub={`已下线 ${overview?.offline ?? 0}`}
          />
          <StatCard
            icon={<Power size={17} />}
            label="在线服务"
            value={overview?.online ?? 0}
            sub="端点可对外调用"
          />
          <StatCard
            icon={<Route size={17} />}
            label="总调用次数"
            value={overview?.call_total ?? 0}
            sub={`失败 ${overview?.call_failed ?? 0}`}
          />
          <StatCard
            icon={<CheckCircle2 size={17} />}
            label="全局成功率"
            value={
              (overview?.call_total ?? 0) > 0
                ? (((overview?.call_total ?? 0) - (overview?.call_failed ?? 0)) / (overview?.call_total ?? 1)) * 100
                : 0
            }
            format={n => ((overview?.call_total ?? 0) > 0 ? `${n.toFixed(1).replace(/\.0$/, '')}%` : '—')}
            tone={(overview?.call_failed ?? 0) > 0 ? 'danger' : 'default'}
          />
          <StatCard
            icon={<Gauge size={17} />}
            label="平均耗时"
            value={overview?.avg_duration_ms ?? 0}
            format={formatDurationMs}
            sub="按全部调用记录计算"
          />
        </section>
      )}

      {/* 筛选栏 */}
      <section className="flex flex-wrap items-center gap-2 rounded-xl border border-border bg-card px-4 py-3 shadow-sm/50" aria-label="推演服务筛选">
        <div className="relative w-full sm:w-72">
          <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <input
            value={draftKeyword}
            onChange={event => setDraftKeyword(event.target.value)}
            onKeyDown={event => { if (event.key === 'Enter') applyFilters() }}
            placeholder="搜索服务名称或描述"
            aria-label="按服务名称或描述筛选"
            className="h-9 w-full rounded-lg border border-border bg-card pl-8 pr-3 text-sm text-foreground placeholder:text-muted-foreground focus-visible:border-ring focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </div>
        <Select
          value={draftStatus || '__all__'}
          onValueChange={value => setDraftStatus((value === '__all__' ? '' : value) as StatusFilter)}
        >
          <SelectTrigger aria-label="按服务状态筛选" className="h-9 w-fit min-w-32 rounded-lg">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">全部状态</SelectItem>
            <SelectItem value="online">在线</SelectItem>
            <SelectItem value="offline">已下线</SelectItem>
          </SelectContent>
        </Select>
        <Button onClick={applyFilters} className="h-9 bg-brand text-white hover:bg-brand-deep">查询</Button>
        <button
          type="button"
          onClick={resetFilters}
          className="inline-flex h-9 items-center gap-1 rounded-lg px-2.5 text-xs text-muted-foreground hover:bg-muted hover:text-muted-foreground"
        >
          <X size={13} /> 重置
        </button>
        <span className="ml-auto hidden text-xs tabular-nums text-muted-foreground sm:inline" aria-live="polite">
          共 {total} 个推演服务
        </span>
        <button
          type="button"
          onClick={() => void refreshAll()}
          className="inline-flex h-9 items-center gap-1.5 rounded-lg border border-border px-3 text-xs text-muted-foreground hover:bg-muted"
        >
          <RefreshCw size={13} className={refreshing ? 'animate-spin' : ''} /> 刷新
        </button>
      </section>

      {/* 服务注册表 */}
      <section className="overflow-hidden rounded-xl border border-border bg-card shadow-sm/50">
        {loading ? (
          <p className="py-16 text-center text-sm text-muted-foreground">加载推演服务…</p>
        ) : error ? (
          <div className="flex flex-col items-center gap-3 py-16" role="alert">
            <p className="text-sm text-destructive">{error}</p>
            <button
              type="button"
              onClick={() => void loadServices()}
              className="rounded-lg border border-[var(--color-danger-bg)] bg-card px-3 py-1.5 text-xs font-medium text-destructive hover:bg-[var(--color-danger-bg)]"
            >
              重新加载
            </button>
          </div>
        ) : items.length === 0 ? (
          <div className="flex flex-col items-center justify-center px-6 py-16 text-center">
            <motion.div
              animate={reduce ? undefined : { y: [0, -6, 0] }}
              transition={{ repeat: Infinity, duration: 3, ease: 'easeInOut' }}
            >
              <Rocket size={28} className="text-muted-foreground" />
            </motion.div>
            <p className="mt-3 text-sm font-medium text-muted-foreground">{keyword || status ? '没有符合条件的推演服务' : '暂无推演服务'}</p>
            <p className="mt-1 max-w-md text-xs leading-5 text-muted-foreground">
              {keyword || status
                ? '请调整名称或状态筛选条件'
                : '在「推演模型」开发页执行通过、保存版本并发布后，服务会作为一等实体出现在这里，对外提供统一调用端点。'}
            </p>
          </div>
        ) : (
          <>
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-border text-xs text-muted-foreground">
                  <th className="px-4 py-2.5 font-medium">推演服务</th>
                  <th className="px-4 py-2.5 font-medium">所属模型</th>
                  <th className="px-4 py-2.5 font-medium">版本</th>
                  <th className="px-4 py-2.5 font-medium">状态</th>
                  <th className="px-4 py-2.5 font-medium">调用</th>
                  <th className="px-4 py-2.5 font-medium">更新时间</th>
                  <th className="px-4 py-2.5 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item, index) => (
                  <motion.tr
                    key={item.id}
                    initial={reduce ? false : { opacity: 0, y: 6 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ ...SPRING_LAYOUT, delay: Math.min(index * 0.035, 0.35) }}
                    className="border-b border-border transition-colors hover:bg-muted">
                    <td className="max-w-[240px] px-4 py-2.5">
                      <p className="truncate font-medium text-foreground" title={item.name}>{item.name}</p>
                      {item.description && (
                        <p className="truncate text-[11px] text-muted-foreground" title={item.description}>{item.description}</p>
                      )}
                    </td>
                    <td className="px-4 py-2.5">
                      <button
                        type="button"
                        onClick={() => { window.location.hash = '#/world-model/models/' + item.project_id + '/develop' }}
                        className="text-brand-ink underline-offset-2 hover:underline"
                        title={'进入「' + item.project_name + '」开发页'}
                      >
                        {item.project_name || '—'}
                      </button>
                    </td>
                    <td className="px-4 py-2.5 tabular-nums text-muted-foreground">{item.version_no !== null ? 'v' + item.version_no : '—'}</td>
                    <td className="px-4 py-2.5">{statusBadge(item.status)}</td>
                    <td className="px-4 py-2.5 tabular-nums text-muted-foreground">
                      <span className={item.failed_count > 0 ? 'text-destructive' : 'text-brand-ink'}>
                        {item.call_count - item.failed_count}
                      </span>
                      <span className="text-muted-foreground"> / </span>
                      {item.call_count}
                      <span
                        className={`ml-1.5 text-[11px] ${item.failed_count > 0 ? 'text-destructive' : 'text-muted-foreground'}`}
                        title={`成功率 ${formatSuccessRate(item.call_count - item.failed_count, item.call_count)}`}
                      >
                        {formatSuccessRate(item.call_count - item.failed_count, item.call_count)}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 tabular-nums text-muted-foreground">{formatDateTime(item.updated_at, { fallback: '-' })}</td>
                    <td className="px-4 py-2.5">
                      <span className="flex items-center justify-end gap-1">
                        <Tooltip content={item.status === 'online' ? '试调用该推演服务' : '服务已下线，无法调用'}>
                          <button
                            type="button"
                            onClick={() => openInvoke(item)}
                            disabled={item.status !== 'online'}
                            aria-label={'试调用 ' + item.name}
                            className="inline-flex h-7 items-center gap-1 rounded-md border border-border px-2 text-[11px] text-muted-foreground transition-colors hover:bg-muted disabled:cursor-not-allowed disabled:opacity-40"
                          >
                            <Play size={12} /> 试调用
                          </button>
                        </Tooltip>
                        <Tooltip content="服务详情与语义注册">
                          <button
                            type="button"
                            onClick={() => openDetail(item)}
                            aria-label={'查看 ' + item.name + ' 详情'}
                            className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-border text-muted-foreground transition-colors hover:bg-muted"
                          >
                            <Eye size={13} />
                          </button>
                        </Tooltip>
                        <Tooltip content={item.status === 'online' ? '下线（需二次确认）' : '上线'}>
                          <button
                            type="button"
                            onClick={() => requestToggleStatus(item)}
                            disabled={togglingId === item.id}
                            aria-label={item.status === 'online' ? '下线 ' + item.name : '上线 ' + item.name}
                            className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-border text-muted-foreground transition-colors hover:bg-muted disabled:opacity-40"
                          >
                            <Power size={13} className={item.status === 'online' ? 'text-brand-ink' : ''} />
                          </button>
                        </Tooltip>
                        <Tooltip content="复制完整调用地址（含协议、主机与路径）">
                          <button
                            type="button"
                            onClick={() => copyEndpoint(item)}
                            aria-label={'复制 ' + item.name + ' 调用端点'}
                            className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-border text-muted-foreground transition-colors hover:bg-muted"
                          >
                            {copiedId === item.id ? <Check size={13} className="text-brand-ink" /> : <Copy size={13} />}
                          </button>
                        </Tooltip>
                      </span>
                    </td>
                  </motion.tr>
                ))}
              </tbody>
            </table>
            <div className="flex items-center justify-between border-t border-border px-4 py-2.5 text-xs text-muted-foreground">
              <span>共 {total} 条</span>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setPage(p => Math.max(1, p - 1))}
                  disabled={page <= 1}
                  aria-label="上一页"
                  className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-border disabled:opacity-40"
                >
                  <ChevronLeft size={14} />
                </button>
                <span className="tabular-nums">{page} / {totalPages}</span>
                <button
                  type="button"
                  onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                  disabled={page >= totalPages}
                  aria-label="下一页"
                  className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-border disabled:opacity-40"
                >
                  <ChevronRight size={14} />
                </button>
              </div>
            </div>
          </>
        )}
      </section>

      {/* 试调用弹窗 */}
      <Modal
        open={!!invokeTarget}
        onClose={() => !invoking && setInvokeTarget(null)}
        title="试调用推演服务"
        description={invokeTarget?.endpoint_path ? 'POST ' + fullEndpointUrl(invokeTarget.endpoint_path) : ''}
        headerIcon={<Play size={17} />}
        size="lg"
        footer={(
          <>
            <Button variant="ghost" onClick={() => setInvokeTarget(null)} disabled={invoking}>关闭</Button>
            <Button
              onClick={() => void submitInvoke()}
              loading={invoking}
              disabled={!invokeTarget || invokeTarget.status !== 'online'}
              className="bg-brand text-white hover:bg-brand-deep"
            >
              调用
            </Button>
          </>
        )}
      >
        <div className="space-y-4">
          <div>
            <p className="mb-1.5 flex items-center justify-between gap-2 text-sm font-medium text-foreground">
              <span>测试入参（context / actions / horizon）</span>
              {exampleInput && (
                <button
                  type="button"
                  onClick={() => { setInvokeInput(exampleInput); setJsonIssue(null) }}
                  className="rounded-md border border-border px-2 py-0.5 text-[11px] font-normal text-brand-ink hover:bg-brand-soft"
                >
                  填入示例
                </button>
              )}
            </p>
            <p className="mb-1.5 text-[11px] leading-5 text-muted-foreground">
              context 为观测数据（如时序服务需传 series 数值列表，长度不足会被边界拒绝）、actions 为干预动作、horizon 为预测步数；
              示例取自该服务发布版本保存时的调试入参。
            </p>
            <textarea
              value={invokeInput}
              onChange={event => handleInvokeInputChange(event.target.value)}
              spellCheck={false}
              rows={8}
              className="w-full resize-none rounded-lg border border-border bg-card px-3 py-2 font-mono text-xs leading-5 text-foreground focus-visible:border-ring focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              aria-label="试调用测试入参 JSON"
              aria-invalid={!!jsonIssue}
            />
            {jsonIssue && (
              <p className="mt-1 text-[11px] text-destructive" role="alert">
                JSON 语法错误
                {jsonIssue.line !== null && jsonIssue.column !== null
                  ? `（第 ${jsonIssue.line} 行第 ${jsonIssue.column} 列附近）`
                  : ''}：{jsonIssue.message}
              </p>
            )}
          </div>
          {invokeResult && (
            <div aria-live="polite">
              {invokeResult.ok ? (() => {
                const payload = invokeResult.payload as { trajectory?: unknown; boundary?: unknown } | null
                const emptyTrajectory = !!payload && Array.isArray(payload.trajectory) && payload.trajectory.length === 0
                const boundaryNote = typeof payload?.boundary === 'string' ? payload.boundary : ''
                return (
                  <div>
                    {emptyTrajectory ? (
                      <div className="mb-1 rounded-lg border border-[var(--color-warning)] bg-[var(--color-warning-bg)] px-2.5 py-1.5">
                        <p className="flex items-center gap-1 text-[11px] font-medium text-[var(--color-warning)]">
                          <AlertTriangle size={12} /> 调用成功 · {invokeResult.duration_ms} ms · 未产生预测输出
                        </p>
                        {boundaryNote && (
                          <p className="mt-0.5 text-[11px] leading-4 text-[var(--color-warning)]">边界说明：{boundaryNote}</p>
                        )}
                      </div>
                    ) : (
                      <p className="mb-1 flex items-center gap-1 text-[11px] font-medium text-brand-ink">
                        <CheckCircle2 size={12} /> 调用成功 · {invokeResult.duration_ms} ms
                      </p>
                    )}
                    <pre className="max-h-64 overflow-auto rounded-lg bg-muted p-2.5 text-xs leading-5 text-foreground">
                      {JSON.stringify(invokeResult.payload, null, 2)}
                    </pre>
                  </div>
                )
              })() : (
                <div>
                  <p className="mb-1 flex items-center gap-1 text-[11px] font-medium text-destructive">
                    <XCircle size={12} /> 调用失败
                  </p>
                  <p className="text-xs text-destructive">{invokeResult.error}</p>
                </div>
              )}
            </div>
          )}
          {invokeTarget?.endpoint_path && (
            <div>
              <p className="mb-1.5 flex items-center justify-between text-sm font-medium text-foreground">
                <span className="flex items-center gap-1"><Terminal size={14} /> 外部调用示例（curl）</span>
                <button
                  type="button"
                  onClick={() => copyCurlExample(buildCurlExample(fullEndpointUrl(invokeTarget.endpoint_path!), invokeInput))}
                  aria-label="复制 curl 示例"
                  title="复制 curl 调用示例"
                  className="inline-flex h-6 items-center gap-1 rounded-md border border-border px-2 text-[11px] font-normal text-muted-foreground hover:bg-muted"
                >
                  {copiedCurl ? <Check size={12} className="text-brand-ink" /> : <Copy size={12} />} 复制
                </button>
              </p>
              <pre className="overflow-x-auto rounded-lg bg-muted p-2.5 font-mono text-[11px] leading-5 text-muted-foreground">
                {buildCurlExample(fullEndpointUrl(invokeTarget.endpoint_path), invokeInput)}
              </pre>
              <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
                「访问令牌」为平台登录账号的 JWT，可通过 POST /api/v1/auth/login 获取（与页面登录同源）；未带令牌或令牌失效将返回 401/403。
              </p>
            </div>
          )}
        </div>
      </Modal>

      {/* 详情抽屉 */}
      {detailTarget && (
        <div className="fixed inset-0 z-40 flex justify-end" role="dialog" aria-label="推演服务详情">
          <div className="absolute inset-0 bg-[var(--color-bg-overlay)]" onClick={() => setDetailTarget(null)} />
          <aside className="relative z-10 flex h-full w-[min(560px,100%)] flex-col bg-card shadow-2xl">
            <header className="flex items-center justify-between border-b border-border px-5 py-3.5">
              <div>
                <h2 className="text-sm font-semibold text-foreground">{detailTarget.name}</h2>
                <p className="mt-0.5 text-[11px] text-muted-foreground">
                  {detailTarget.project_name} · v{detailTarget.version_no ?? '-'} · 更新于 {formatDateTime(detailTarget.updated_at)}
                </p>
              </div>
              <button
                type="button"
                onClick={() => setDetailTarget(null)}
                aria-label="关闭详情"
                className="inline-flex h-8 w-8 items-center justify-center rounded-lg text-muted-foreground hover:bg-muted hover:text-muted-foreground"
              >
                <X size={16} />
              </button>
            </header>

            <div className="min-h-0 flex-1 overflow-auto px-5 py-4">
              <div className="space-y-4 text-xs leading-6 text-muted-foreground">
                <section>
                  <p className="mb-1.5 flex items-center justify-between text-xs font-semibold text-foreground">
                    <span>基本状态</span>{statusBadge(detailTarget.status)}
                  </p>
                  <dl className="space-y-1.5 rounded-lg border border-border bg-muted/50 p-3">
                    <div className="flex items-start justify-between gap-2">
                      <dt className="shrink-0 text-muted-foreground">调用端点</dt>
                      <dd className="flex min-w-0 items-start justify-end gap-1.5">
                        <span className="break-all text-right font-mono text-[11px] leading-5 text-foreground">
                          {detailTarget.endpoint_path ?? '—'}
                        </span>
                        {detailTarget.endpoint_path && (
                          <button
                            type="button"
                            onClick={() => copyEndpoint(detailTarget)}
                            aria-label="复制调用端点"
                            title="复制完整调用地址（含协议、主机与路径）"
                            className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded text-muted-foreground transition-colors hover:bg-muted hover:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          >
                            {copiedId === detailTarget.id ? <Check size={12} className="text-brand-ink" /> : <Copy size={12} />}
                          </button>
                        )}
                      </dd>
                    </div>
                    <div className="flex justify-between"><dt className="text-muted-foreground">服务描述</dt><dd className="max-w-[320px] text-right text-foreground">{detailTarget.description || '—'}</dd></div>
                    <div className="flex justify-between"><dt className="text-muted-foreground">调用统计</dt><dd className="tabular-nums text-foreground">成功 {detailTarget.call_count - detailTarget.failed_count} / 共 {detailTarget.call_count}（成功率 {formatSuccessRate(detailTarget.call_count - detailTarget.failed_count, detailTarget.call_count)}）</dd></div>
                  </dl>
                </section>

                <section>
                  <p className="mb-1.5 text-xs font-semibold text-foreground">本体语义注册</p>
                  <dl className="space-y-1.5 rounded-lg border border-border bg-muted/50 p-3">
                    <div className="flex justify-between">
                      <dt className="text-muted-foreground">所属本体</dt>
                      <dd className="max-w-[320px] text-right text-foreground">{ontologyName || detailTarget.applicable_object_types?.ontology_id || '—'}</dd>
                    </div>
                    <div className="flex justify-between">
                      <dt className="text-muted-foreground">适用对象类型</dt>
                      <dd className="max-w-[320px] text-right text-foreground">
                        {(detailTarget.applicable_object_types?.object_type_ids ?? [])
                          .map(id => objectTypeLabels[id] || id)
                          .join('、') || '—'}
                      </dd>
                    </div>
                    <div className="flex justify-between">
                      <dt className="text-muted-foreground">前置条件</dt>
                      <dd className="max-w-[320px] text-right text-foreground">
                        {(detailTarget.preconditions ?? [])
                          .map(item => (objectTypeLabels[item.object_type_id] || item.object_type_id) + ' ≥ ' + item.min_count)
                          .join('；') || '无'}
                      </dd>
                    </div>
                  </dl>
                </section>

                <section>
                  <p className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold text-foreground">
                    <Activity size={13} /> 最近调用（共 {serviceCallsTotal} 条）
                    {serviceCallsTotal > 0 && (
                      <button
                        type="button"
                        onClick={() => {
                          const target = detailTarget
                          setDetailTarget(null)
                          navigate(`/world-model/calls?service_id=${target.id}&service_name=${encodeURIComponent(target.name)}`)
                        }}
                        className="ml-auto inline-flex items-center gap-1 text-[11px] font-normal text-brand-ink underline-offset-2 hover:underline"
                        title="进入调用记录页并按本服务过滤"
                      >
                        查看全部 <ExternalLink size={11} />
                      </button>
                    )}
                  </p>
                  {serviceCallsLoading ? (
                    <p className="py-6 text-center text-xs text-muted-foreground">加载调用记录…</p>
                  ) : serviceCalls.length === 0 ? (
                    <p className="py-6 text-center text-xs text-muted-foreground">该服务暂无调用记录</p>
                  ) : (
                    <div className="overflow-hidden rounded-lg border border-border">
                      <table className="w-full text-left text-xs">
                        <thead>
                          <tr className="border-b border-border text-[11px] text-muted-foreground">
                            <th className="px-3 py-2 font-medium">时间</th>
                            <th className="px-3 py-2 font-medium">调用方</th>
                            <th className="px-3 py-2 font-medium">结果</th>
                            <th className="px-3 py-2 text-right font-medium">耗时</th>
                          </tr>
                        </thead>
                        <tbody>
                          {serviceCalls.map(call => (
                            <tr key={call.id} className="border-b border-border">
                              <td className="px-3 py-2 tabular-nums text-muted-foreground">{formatDateTime(call.created_at, { fallback: '-' })}</td>
                              <td className="px-3 py-2 text-muted-foreground">{call.caller || '—'}</td>
                              <td className="px-3 py-2">
                                {call.ok
                                  ? <span className="inline-flex items-center gap-1 text-brand-ink"><CheckCircle2 size={12} /> 成功</span>
                                  : <span className="inline-flex items-center gap-1 text-destructive"><XCircle size={12} /> 失败</span>}
                              </td>
                              <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">{call.duration_ms} ms</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </section>
              </div>
            </div>
          </aside>
        </div>
      )}

      {/* 下线二次确认：端点将立即不可访问，误触可在确认层拦截；上线不打扰 */}
      <ConfirmModal
        open={!!offlineTarget}
        onClose={() => { if (!togglingId) setOfflineTarget(null) }}
        onConfirm={() => offlineTarget && void toggleStatus(offlineTarget)}
        title={offlineTarget ? `下线「${offlineTarget.name}」？` : '下线推演服务？'}
        description="下线后该服务的调用端点将立即不可访问，正在集成的调用方会调用失败。此操作不影响已保存的模型与版本，可随时重新上线。"
        confirmText="确认下线"
        variant="danger"
        loading={!!togglingId}
      />
    </div>
  )
}
