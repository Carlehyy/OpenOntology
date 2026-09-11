/**
 * PipelineOverviewBar — 数据流水线列表页头部运行概况。
 *
 * 布局对齐同域「数据任务池」页（SyncTasksTab）：单行五张紧凑 KPI 统计卡
 * （grid-cols-2 → sm:3 → xl:5），卡样式为全站共享 KpiStatCard；所有视口
 * 宽度下均保持此单一形态（不再随 ≥2xl 断点切换右侧栏布局），表格始终
 * 全宽展示。
 *
 * 数据来自列表接口 paginated 响应里的 overview 字段（全量口径、不受筛选
 * 影响，见 backend pipeline_overview）。
 */
import { GitBranch, CheckCircle2, Activity, AlertCircle, Waves } from 'lucide-react'
import type { PipelineOverview } from '@/api/v2/pipelines'
import { KpiStatCard } from '@/components/KpiStatCard'

/** 头部 KPI 行：单行五张紧凑统计卡。语义色仅在坏消息为真时着色（0 恒中性）。 */
export default function PipelineOverviewBar({ overview }: { overview: PipelineOverview }) {
  const source = overview.trend_7d ?? []
  const successTotal = source.reduce((sum, item) => sum + Math.max(item.runs - item.errors, 0), 0)
  const failureTotal = source.reduce((sum, item) => sum + Math.min(Math.max(item.errors, 0), Math.max(item.runs, 0)), 0)
  const total7d = successTotal + failureTotal
  const hasTrend = Array.isArray(overview.trend_7d)

  return (
    <div
      data-testid="pipeline-overview-bar"
      className={`grid shrink-0 grid-cols-2 gap-2 sm:grid-cols-3 ${hasTrend ? 'xl:grid-cols-5' : 'xl:grid-cols-4'}`}
    >
      <KpiStatCard label="流水线总数" value={overview.total} note="不含已归档" icon={<GitBranch size={13} />} />
      <KpiStatCard label="已发布" value={overview.published} note="契约封版可挂接任务" icon={<CheckCircle2 size={13} />} tone="success" />
      <KpiStatCard label="已启用" value={overview.enabled} note="可被任务池调度" icon={<Activity size={13} />} tone="brand" />
      <KpiStatCard
        label="最近执行失败"
        value={overview.latest_failed}
        note="按各流水线最近一次运行"
        icon={<AlertCircle size={13} />}
        tone="danger"
        toneActive={overview.latest_failed > 0}
        pulse
      />
      {hasTrend && (
        <KpiStatCard label="近7日执行" value={total7d} note={`成功 ${successTotal} · 失败 ${failureTotal}`} icon={<Waves size={13} />} tone="info" />
      )}
    </div>
  )
}
