import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Button, Drawer, Empty, Modal, Segmented, Table, Tag, Tooltip } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import ReactECharts from 'echarts-for-react'
import type { EChartsOption } from 'echarts'
import { Activity, Copy, Gauge, Info, RefreshCw, Timer, TriangleAlert } from 'lucide-react'
import { toast } from 'sonner'
import { writeTextToClipboard } from '@/utils/clipboard'
import {
  monitoringApi,
  type MonitoringWindow,
  type SlowRequestBreakdown,
  type SlowRequestItem,
  type TopRouteItem,
  type TraceSpan,
} from '@/api/monitoring'
import {
  baseChartOption,
  CHART_AMBER,
  CHART_AXIS,
  CHART_BLUE,
  CHART_RED,
  CHART_SPLIT,
  CHART_TEAL,
  CHART_TEXT,
  CHART_TOOLTIP_BORDER,
  CHART_VIOLET,
} from '@/lib/echartsTheme'
import { buildAnalysisPrompt } from './traceAnalysisPrompt'

const REFRESH_MS = 30_000

function formatTime(value: string | null): string {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

type KpiTone = 'neutral' | 'success' | 'warning' | 'danger'

const KPI_TONE: Record<KpiTone, { value: string; icon: string; ring: string }> = {
  neutral: { value: 'text-foreground', icon: 'bg-muted text-muted-foreground', ring: 'border-border' },
  success: { value: 'text-[var(--color-success)]', icon: 'bg-[var(--color-success-bg)] text-[var(--color-success)]', ring: 'border-[var(--color-success-bg)]' },
  warning: { value: 'text-[var(--color-warning)]', icon: 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]', ring: 'border-[var(--color-warning-bg)]' },
  danger:  { value: 'text-[var(--color-danger)]',  icon: 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]',  ring: 'border-[var(--color-danger-bg)]' },
}

function Card({ icon, label, value, extra, tone = 'neutral' }: {
  icon: React.ReactNode
  label: string
  value: string
  extra?: string
  tone?: KpiTone
}) {
  const t = KPI_TONE[tone]
  return (
    <div className={`flex items-start gap-3 rounded-lg border bg-card px-4 py-3 ${t.ring}`}>
      <span className={`mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-md ${t.icon}`}>{icon}</span>
      <div className="min-w-0">
        <div className="text-xs text-muted-foreground">{label}</div>
        <div className={`text-xl font-semibold leading-tight tabular-nums ${t.value}`}>{value}</div>
        {extra && <div className="text-[11px] text-muted-foreground/80 mt-0.5">{extra}</div>}
      </div>
    </div>
  )
}

function BreakdownTags({ breakdown }: { breakdown: SlowRequestBreakdown }) {
  const layers: { key: keyof SlowRequestBreakdown; label: string; color: string }[] = [
    { key: 'db', label: 'DB', color: 'geekblue' },
    { key: 'llm', label: 'LLM', color: 'purple' },
    { key: 'http', label: 'HTTP', color: 'orange' },
  ]
  const entries = layers.filter(layer => breakdown[layer.key])
  if (!entries.length) return <span className="text-xs text-muted-foreground">无分层数据</span>
  return (
    <span className="flex flex-wrap items-center gap-1.5">
      {entries.map(layer => {
        const span = breakdown[layer.key]!
        return (
          <Tag key={layer.key} color={layer.color} className="!mb-0 !text-[11px]">
            {layer.label} {span.count} 次 · {(span.total_ms / 1000).toFixed(2)}s
          </Tag>
        )
      })}
    </span>
  )
}

const LAYER_COLORS: Record<string, string> = {
  db: CHART_BLUE,
  llm: CHART_VIOLET,
  http: CHART_AMBER,
}

const LAYER_LABELS: Record<string, string> = {
  db: 'DB',
  llm: 'LLM',
  http: 'HTTP',
}

function TraceView({ request }: { request: SlowRequestItem }) {
  const spans = request.spans ?? []
  const total = Math.max(request.duration_ms, 1)
  // 旧记录没有 spans，用分层汇总估算已归因耗时，避免把全部耗时误报为"未归因"
  const attributed = spans.length
    ? spans.reduce((sum, span) => sum + (span.duration_ms || 0), 0)
    : Object.values(request.breakdown ?? {}).reduce(
        (sum, entry) => sum + (entry?.total_ms || 0),
        0,
      )
  const unattributed = Math.max(0, request.duration_ms - attributed)

  const columns: ColumnsType<TraceSpan> = [
    {
      title: '#', dataIndex: 'seq', key: 'seq', width: 44, align: 'right',
      render: value => <span className="text-[11px] text-muted-foreground">{value}</span>,
    },
    {
      title: '层级', dataIndex: 'layer', key: 'layer', width: 72,
      render: layer => (
        <Tag color={LAYER_COLORS[layer] ?? 'default'} className="!mb-0 !text-[11px]">
          {LAYER_LABELS[layer] ?? layer}
        </Tag>
      ),
    },
    {
      title: '操作', dataIndex: 'name', key: 'name', width: 150, ellipsis: true,
      render: value => value || <span className="text-muted-foreground/60">-</span>,
    },
    {
      title: '目标', dataIndex: 'target', key: 'target', ellipsis: true,
      render: value => (
        <span className="font-mono text-[11px] text-muted-foreground">{value || '-'}</span>
      ),
    },
    {
      title: '开始', dataIndex: 'start_ms', key: 'start_ms', width: 84, align: 'right',
      render: value => <span className="font-mono text-[11px]">+{value}ms</span>,
    },
    {
      title: '耗时', dataIndex: 'duration_ms', key: 'duration_ms', width: 96, align: 'right',
      sorter: (a, b) => a.duration_ms - b.duration_ms,
      defaultSortOrder: 'descend',
      render: value => (
        <span className="font-medium text-[var(--color-danger)]">{(value / 1000).toFixed(3)}s</span>
      ),
    },
    {
      title: '占比', key: 'pct', width: 76, align: 'right',
      render: (_, span) => <span>{((span.duration_ms / total) * 100).toFixed(1)}%</span>,
    },
    {
      title: '状态', dataIndex: 'status', key: 'status', width: 86, ellipsis: true,
      render: value => value || '-',
    },
  ]

  return (
    <div className="flex flex-col gap-3 text-xs text-muted-foreground">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg bg-muted px-3 py-2">
        <span className="font-mono text-xs">
          {request.method} {request.route}
        </span>
        <span className="text-[var(--color-danger)] font-medium">{(request.duration_ms / 1000).toFixed(2)}s</span>
        <span className="text-muted-foreground font-mono text-[10px]">{request.request_id}</span>
      </div>

      {!spans.length && (
        <div className="flex items-start gap-2 rounded-lg border border-[var(--color-warning-bg)] bg-[var(--color-warning-bg)] px-3 py-2 leading-5 text-[var(--color-warning)]">
          <Info size={13} className="mt-1 shrink-0" />
          <div>
            该请求记录于调用链功能上线前的旧版本，系统未采集逐步调用链，仅保留分层耗时汇总：
            <span className="ml-1"><BreakdownTags breakdown={request.breakdown} /></span>
            <span className="ml-1 text-[var(--color-warning)]/80">
              此类历史记录会随 7 天保留期自动清除；上线后产生的新慢请求均有完整调用链。
            </span>
          </div>
        </div>
      )}

      <div>
        <div className="mb-1 flex items-center justify-between text-[11px] text-muted-foreground">
          <span>调用链时间轴（相对请求开始）</span>
          <span className="flex gap-3">
            {(['db', 'llm', 'http'] as const).map(layer => (
              <span key={layer} className="flex items-center gap-1">
                <span
                  className="inline-block h-2 w-2 rounded-sm"
                  style={{ backgroundColor: LAYER_COLORS[layer] }}
                />
                {LAYER_LABELS[layer]}
              </span>
            ))}
          </span>
        </div>
        <div className="rounded-lg border border-border bg-card p-2">
          <div className="relative mb-1 h-4 text-[9px] text-muted-foreground/60">
            {[0, 25, 50, 75, 100].map(pct => (
              <span key={pct} className="absolute -translate-x-1/2" style={{ left: pct + '%' }}>
                {pct}%
              </span>
            ))}
          </div>
          {spans.map(span => (
            <div key={span.seq} className="relative my-1 h-3.5 rounded-sm bg-muted">
              <div
                className="absolute top-0 h-full min-w-[2px] rounded-sm"
                style={{
                  left: Math.min(100, (span.start_ms / total) * 100) + '%',
                  width: Math.max(0.3, Math.min(100, (span.duration_ms / total) * 100)) + '%',
                  backgroundColor: LAYER_COLORS[span.layer] ?? CHART_TEXT,
                }}
                title={LAYER_LABELS[span.layer] ?? span.layer + ' ' + span.name + ' ' + span.target + ' · ' + span.duration_ms + 'ms'}
              />
            </div>
          ))}
          {!spans.length && (
            <div className="py-3 text-center text-[11px] text-muted-foreground">
              该请求没有调用链数据（可能为旧版本记录的慢请求）
            </div>
          )}
        </div>
        <div className="mt-1 text-[11px] text-muted-foreground">
          {unattributed > 0 && (
            <span>
              未归因耗时 {(unattributed / 1000).toFixed(2)}s（Python 计算及其他未埋点环节）·{' '}
            </span>
          )}
          已归因 {(attributed / 1000).toFixed(2)}s / 总耗时 {(request.duration_ms / 1000).toFixed(2)}s
          {request.spans_truncated && ' · 调用链过长已按耗时截断'}
        </div>
      </div>

      <Table<TraceSpan>
        rowKey="seq"
        size="small"
        columns={columns}
        dataSource={spans}
        pagination={false}
        locale={{ emptyText: <Empty description="暂无调用链数据" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
        expandable={{
          rowExpandable: span => Boolean(span.detail),
          expandedRowRender: span => (
            <pre className="m-0 whitespace-pre-wrap break-all rounded bg-muted p-2 font-mono text-[11px] leading-relaxed text-muted-foreground">
              {span.detail}
            </pre>
          ),
        }}
      />
    </div>
  )
}

export default function MonitoringTab() {
  const [window, setWindow] = useState<MonitoringWindow>('24h')
  const [topSort, setTopSort] = useState('slow_count')
  const [slowPage, setSlowPage] = useState(1)
  const [slowRoute, setSlowRoute] = useState('')
  const [traceRequest, setTraceRequest] = useState<SlowRequestItem | null>(null)
  const [promptText, setPromptText] = useState<string | null>(null)

  const copyAnalysisPrompt = async () => {
    if (!traceRequest) return
    const text = buildAnalysisPrompt(traceRequest)
    // 始终弹出全文：普通 HTTP 部署没有 Clipboard API，execCommand 回退在
    // 非聚焦页面等场景会静默失败且页面侧无法验证，弹出全文（已全选）与
    // 下载入口保证用户一定能拿到内容，绝不只依赖剪贴板单一路径。
    setPromptText(text)
    try {
      await writeTextToClipboard(text)
      toast.success('已尝试写入剪贴板', { description: '若粘贴没有内容，请在弹出框中按 ⌘C / Ctrl+C 手动复制（文本已全选）' })
    } catch {
      toast.warning('自动复制不可用', { description: '请在弹出的文本框中使用 ⌘C / Ctrl+C 复制，或直接下载 .md 文件' })
    }
  }

  const downloadPrompt = () => {
    if (!traceRequest || !promptText) return
    const blob = new Blob([promptText], { type: 'text/markdown;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `slow-request-${traceRequest.request_id || traceRequest.id}.md`
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    URL.revokeObjectURL(url)
  }

  const overview = useQuery({
    queryKey: ['monitoring', 'overview', window],
    queryFn: () => monitoringApi.overview(window),
    refetchInterval: REFRESH_MS,
  })
  const trend = useQuery({
    queryKey: ['monitoring', 'trend', window],
    queryFn: () => monitoringApi.trend(window),
    refetchInterval: REFRESH_MS,
  })
  const top = useQuery({
    queryKey: ['monitoring', 'top', window, topSort],
    queryFn: () => monitoringApi.top(window, topSort, 20),
    refetchInterval: REFRESH_MS,
  })
  const slow = useQuery({
    queryKey: ['monitoring', 'slow', slowPage, slowRoute],
    queryFn: () =>
      monitoringApi.slowRequests({ route: slowRoute, page: slowPage, size: 10 }),
    refetchInterval: REFRESH_MS,
  })

  const chartOption = useMemo<EChartsOption>(() => {
    const points = trend.data?.points ?? []
    return {
      ...baseChartOption(),
      aria: { enabled: true, description: '接口请求量、p95 耗时与错误率趋势' },
      grid: { left: 8, right: 10, top: 34, bottom: 4, containLabel: true },
      legend: {
        top: 0,
        right: 0,
        icon: 'roundRect',
        itemWidth: 8,
        itemHeight: 8,
        itemGap: 12,
        textStyle: { color: CHART_TEXT, fontSize: 10 },
      },
      tooltip: { trigger: 'axis', borderColor: CHART_TOOLTIP_BORDER },
      xAxis: {
        type: 'category',
        data: points.map(point => point.t),
        axisLabel: {
          color: CHART_TEXT,
          fontSize: 9,
          formatter: (value: string) => {
            const date = new Date(value)
            if (Number.isNaN(date.getTime())) return ''
            return window === '24h'
              ? date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
              : date.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' })
          },
          hideOverlap: true,
        },
        axisLine: { lineStyle: { color: CHART_AXIS } },
      },
      yAxis: [
        { type: 'value', name: '请求', nameTextStyle: { color: CHART_TEXT, fontSize: 9 }, splitLine: { lineStyle: { color: CHART_SPLIT } } },
        { type: 'value', name: 'ms', nameTextStyle: { color: CHART_TEXT, fontSize: 9 }, splitLine: { show: false } },
        { type: 'value', name: '%', nameTextStyle: { color: CHART_TEXT, fontSize: 9 }, splitLine: { show: false }, max: 100 },
      ],
      series: [
        {
          name: '请求量',
          type: 'bar',
          data: points.map(point => point.count),
          itemStyle: { color: CHART_TEAL },
          barMaxWidth: 8,
        },
        {
          name: 'p95 耗时',
          type: 'line',
          yAxisIndex: 1,
          data: points.map(point => point.p95_ms),
          smooth: true,
          symbol: 'none',
          lineStyle: { color: CHART_AMBER, width: 1.4 },
        },
        {
          name: '错误率',
          type: 'line',
          yAxisIndex: 2,
          data: points.map(point => point.error_rate),
          smooth: true,
          symbol: 'none',
          lineStyle: { color: CHART_RED, width: 1.4 },
        },
      ],
    }
  }, [trend.data, window])

  const topColumns: ColumnsType<TopRouteItem> = [
    { title: '接口', dataIndex: 'route', key: 'route', ellipsis: true, render: (_, row) => <span className="font-mono text-xs">{row.method} {row.route}</span> },
    { title: '请求量', dataIndex: 'requests', key: 'requests', width: 90, align: 'right' },
    { title: '错误率', dataIndex: 'error_rate', key: 'error_rate', width: 90, align: 'right', render: value => value + '%' },
    { title: '平均', dataIndex: 'avg_ms', key: 'avg_ms', width: 90, align: 'right', render: value => (value == null ? '-' : value + 'ms') },
    { title: 'p95', dataIndex: 'p95_ms', key: 'p95_ms', width: 90, align: 'right', render: value => (value == null ? '-' : value + 'ms') },
    { title: '最大', dataIndex: 'max_ms', key: 'max_ms', width: 90, align: 'right', render: value => value + 'ms' },
    { title: '慢请求', dataIndex: 'slow_count', key: 'slow_count', width: 90, align: 'right', render: value => <span className={value ? 'text-[var(--color-danger)] font-medium' : ''}>{value}</span> },
  ]

  const slowColumns: ColumnsType<SlowRequestItem> = [
    { title: '时间', dataIndex: 'created_at', key: 'created_at', width: 150, render: formatTime },
    { title: '接口', dataIndex: 'route', key: 'route', ellipsis: true, render: (_, row) => <span className="font-mono text-xs">{row.method} {row.route}</span> },
    { title: '耗时', dataIndex: 'duration_ms', key: 'duration_ms', width: 110, align: 'right', sorter: (a, b) => a.duration_ms - b.duration_ms, render: value => <span className="text-[var(--color-danger)] font-medium">{(value / 1000).toFixed(2)}s</span> },
    { title: '状态', dataIndex: 'status_code', key: 'status_code', width: 70, align: 'center' },
    { title: '用户', dataIndex: 'username', key: 'username', width: 110, ellipsis: true, render: value => value || '-' },
    { title: '来源', dataIndex: 'source_ip', key: 'source_ip', width: 120, render: value => value || '-' },
    { title: 'request_id', dataIndex: 'request_id', key: 'request_id', width: 120, render: value => <span className="font-mono text-[10px] text-muted-foreground">{value || '-'}</span> },
    {
      title: '调用链',
      key: 'trace',
      width: 110,
      align: 'center',
      render: (_, row) => row.spans?.length ? (
        <Button
          type="link"
          size="small"
          className="!text-xs !text-[var(--color-accent-ink)]"
          onClick={() => setTraceRequest(row)}
        >
          调用链
        </Button>
      ) : (
        <Tooltip
          title="该请求产生于调用链功能上线前的旧版本，未采集逐步调用链，仅保留 DB/LLM/HTTP 分层汇总；此类记录会随 7 天保留期自动清除"
        >
          <Button
            type="link"
            size="small"
            className="!text-xs !text-muted-foreground hover:!text-foreground"
            onClick={() => setTraceRequest(row)}
          >
            <Info size={12} className="mr-0.5" />
            历史记录
          </Button>
        </Tooltip>
      ),
    },
  ]

  const overviewData = overview.data
  const slowThreshold = overviewData?.slow_threshold_ms ?? 1000

  // p95 耗时健康度：以慢请求阈值为基准（< 阈值 健康 / ≥ 阈值且 < 2× 警告 / ≥ 2× 危险）
  const p95Tone: KpiTone = overviewData?.p95_ms == null
    ? 'neutral'
    : overviewData.p95_ms >= slowThreshold * 2
      ? 'danger'
      : overviewData.p95_ms >= slowThreshold
        ? 'warning'
        : 'success'

  // 成功率健康度：≥ 99% 健康 / 95–99% 警告 / < 95% 危险
  const successTone: KpiTone = overviewData == null
    ? 'neutral'
    : overviewData.success_rate >= 99
      ? 'success'
      : overviewData.success_rate >= 95
        ? 'warning'
        : 'danger'

  // 慢请求数健康度：0 健康 / >0 危险（信号放大，便于一眼定位）
  const slowTone: KpiTone = overviewData == null
    ? 'neutral'
    : overviewData.slow_requests > 0
      ? 'danger'
      : 'success'

  const handleRefresh = () => {
    void overview.refetch()
    void trend.refetch()
    void top.refetch()
    void slow.refetch()
  }

  return (
    <div className="flex flex-col gap-4">
      {/* 页头：标题 + 时间窗 + 刷新 */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex flex-col gap-0.5">
          <h2 className="text-base font-semibold text-foreground leading-none">运行监控</h2>
          <span className="text-[11px] text-muted-foreground">
            数据每 30 秒自动刷新 · 慢接口阈值 {slowThreshold}ms · 明细保留 7 天
          </span>
        </div>
        <div className="flex items-center gap-2">
          <Segmented
            value={window}
            onChange={value => setWindow(value as MonitoringWindow)}
            options={[{ label: '近 24 小时', value: '24h' }, { label: '近 7 天', value: '7d' }]}
          />
          <Tooltip title="立即刷新">
            <Button
              size="small"
              icon={<RefreshCw size={13} />}
              onClick={handleRefresh}
              loading={overview.isFetching || trend.isFetching}
            />
          </Tooltip>
        </div>
      </div>

      <div className="grid grid-cols-2 xl:grid-cols-4 gap-3">
        <Card icon={<Activity size={16} />} label="请求总量" value={overviewData ? overviewData.requests.toLocaleString() : '-'} extra={window === '24h' ? '近 24 小时' : '近 7 天'} />
        <Card icon={<Gauge size={16} />} label="成功率" value={overviewData ? overviewData.success_rate + '%' : '-'} extra={'服务器错误率 ' + (overviewData?.server_error_rate ?? '-') + '%'} tone={successTone} />
        <Card icon={<Timer size={16} />} label="p95 耗时" value={overviewData?.p95_ms != null ? overviewData.p95_ms + 'ms' : '-'} extra={'平均 ' + (overviewData?.avg_ms ?? '-') + 'ms · 阈值 ' + slowThreshold + 'ms'} tone={p95Tone} />
        <Card icon={<TriangleAlert size={16} />} label="慢请求" value={overviewData ? String(overviewData.slow_requests) : '-'} extra={'≥ ' + slowThreshold + 'ms 的请求次数'} tone={slowTone} />
      </div>

      <div className="rounded-lg border border-border bg-card p-4">
        <div className="text-sm font-medium text-foreground mb-2">请求趋势（请求量 / p95 耗时 / 错误率）</div>
        <ReactECharts option={chartOption} style={{ height: 240 }} notMerge />
      </div>

      <div className="rounded-lg border border-border bg-card p-4">
        <div className="flex items-center justify-between mb-2 flex-wrap gap-2">
          <span className="text-sm font-medium text-foreground">接口排行（针对性优化依据）</span>
          <Segmented
            size="small"
            value={topSort}
            onChange={value => setTopSort(value as string)}
            options={[
              { label: '慢请求数', value: 'slow_count' },
              { label: 'p95', value: 'p95_ms' },
              { label: '错误率', value: 'error_rate' },
              { label: '请求量', value: 'requests' },
            ]}
          />
        </div>
        <Table
          rowKey="route"
          size="small"
          loading={top.isLoading}
          columns={topColumns}
          dataSource={top.data?.items ?? []}
          pagination={false}
          onRow={row => ({ onClick: () => { setSlowRoute(row.route); setSlowPage(1) }, style: { cursor: 'pointer' } })}
          locale={{ emptyText: <Empty description="该时间窗暂无请求记录" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
        />
      </div>

      <div className="rounded-lg border border-border bg-card p-4">
        <div className="flex items-center justify-between mb-2 flex-wrap gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <span className="text-sm font-medium text-foreground">慢请求明细</span>
            {slowRoute && (
              <Tag
                closable
                onClose={() => { setSlowRoute(''); setSlowPage(1) }}
                className="!mb-0 !text-[11px]"
              >
                筛选：{slowRoute}
              </Tag>
            )}
          </div>
          <Tooltip title="调用链采集自功能上线版本开始；「历史记录」为上线前的慢请求，仅保留分层汇总，随 7 天保留期自动清除">
            <span className="inline-flex items-center gap-0.5 text-[11px] font-normal text-muted-foreground cursor-help">
              <Info size={12} />
              调用链自上线版本开始采集
            </span>
          </Tooltip>
          <input
            value={slowRoute}
            onChange={event => {
              setSlowRoute(event.target.value)
              setSlowPage(1)
            }}
            placeholder="按接口路径筛选"
            className="px-2.5 py-1 border border-border rounded-lg text-xs w-56 bg-card text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </div>
        <Table
          rowKey="id"
          size="small"
          loading={slow.isLoading}
          columns={slowColumns}
          dataSource={slow.data?.items ?? []}
          expandable={{
            expandedRowRender: row => (
              <div className="text-xs text-muted-foreground">
                <div className="mb-1"><span className="text-muted-foreground">UA：</span>{row.user_agent || '-'}</div>
                <BreakdownTags breakdown={row.breakdown} />
              </div>
            ),
          }}
          pagination={{
            current: slowPage,
            pageSize: slow.data?.size ?? 10,
            total: slow.data?.total ?? 0,
            showSizeChanger: false,
            onChange: page => setSlowPage(page),
          }}
          locale={{ emptyText: <Empty description="暂无慢请求记录" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
        />
      </div>

      <Drawer
        title={
          <span className="text-sm font-medium">
            调用链
            <span className="ml-2 text-xs font-normal text-muted-foreground">
              慢请求内部步骤耗时分解（定位具体慢环节）
            </span>
          </span>
        }
        width={860}
        open={Boolean(traceRequest)}
        onClose={() => setTraceRequest(null)}
        destroyOnClose
        footer={
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <span className="text-xs text-muted-foreground">
              将本次慢请求整理成完整分析提示词，可交给其他 Agent 深入分析优化空间
            </span>
            <Button type="primary" size="small" icon={<Copy size={13} />} onClick={copyAnalysisPrompt}>
              复制分析提示词
            </Button>
          </div>
        }
      >
        {traceRequest && <TraceView request={traceRequest} />}
      </Drawer>

      <Modal
        title="慢请求分析提示词"
        open={Boolean(promptText)}
        onCancel={() => setPromptText(null)}
        width={760}
        footer={
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <span className="text-xs text-muted-foreground">
              文本框已自动全选：粘贴无内容时按 ⌘C / Ctrl+C 复制，或直接下载文件
            </span>
            <Button size="small" onClick={downloadPrompt}>
              下载 .md 文件
            </Button>
          </div>
        }
      >
        <textarea
          readOnly
          value={promptText ?? ''}
          autoFocus
          onFocus={event => event.currentTarget.select()}
          className="h-96 w-full resize-none rounded-lg border border-border bg-muted p-3 font-mono text-[11px] leading-relaxed text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      </Modal>
    </div>
  )
}

