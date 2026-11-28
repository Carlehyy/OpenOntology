import type { AutoLoopOutcome, AutoLoopRoute, AutoLoopTraceFilters } from '@/api/autoloop'
import type { BadgeProps } from '@/components/ui/Badge'
// 相对路径 + .ts 扩展名（与同目录 autoloopLogic.ts / ruleRoutingForm.ts 同例）：
// 本模块是无 DOM 纯函数层，供 node --experimental-strip-types 单测直接加载；@/ 别名在 node 下不可解析。
import { formatDateTime, parseServerTime, type DateInput } from '../../../utils/datetime.ts'

export const EVENT_SOURCE_LABEL: Record<string, string> = { notification: '外部通知', schedule: '定时任务' }

export const EVENT_ROUTE_LABEL: Record<AutoLoopRoute, string> = {
  pending: '等待匹配', rule: '规则命中', assistant: '未命中规则',
}

export const EVENT_OUTCOME: Record<AutoLoopOutcome, { label: string; variant: BadgeProps['variant'] }> = {
  pending: { label: '待处理', variant: 'secondary' },
  queued: { label: '待派发', variant: 'secondary' },
  sending: { label: '写入中', variant: 'info' },
  review: { label: '待人工处理', variant: 'warning' },
  confirmed: { label: 'Multica 已确认', variant: 'success' },
  recorded: { label: '决定已记录', variant: 'success' },
  failed: { label: '处理失败', variant: 'danger' },
  unknown: { label: '写入结果未知', variant: 'warning' },
  ignored: { label: '已忽略', variant: 'secondary' },
  handled: { label: '已处理', variant: 'secondary' },
  cancelled: { label: '已取消', variant: 'secondary' },
}

/** 结论的一句话说明：只陈述服务端已记录的事实，与徽章文案同源，不推测后续结果。 */
export const EVENT_OUTCOME_NOTE: Record<AutoLoopOutcome, string> = {
  pending: '路由尚未完成，稍后可刷新查看最新进展。',
  queued: '处理动作已生成，等待派发。',
  sending: '正在写入 Multica。',
  confirmed: 'Multica 已确认写入。',
  recorded: '人工决定已记录。',
  review: '需要人工处理，有待决定的事项。',
  ignored: '事件已忽略，不再继续处理。',
  handled: '待决事项已人工处理。',
  cancelled: '处理已取消。',
  failed: '处理失败，原因见下方说明。',
  unknown: '远端未返回确认，请先核对 Multica 中的记录，避免重复创建。',
}

/** 「待处理」按路由阶段细分：路由结果比处理状态更能说明卡在哪一步。 */
export const EVENT_PENDING_ROUTE_NOTE: Record<AutoLoopRoute, string> = {
  pending: '事件已接收，等待路由处理。',
  rule: '规则已命中，等待执行。',
  assistant: '未命中已启用的规则，等待助手研判。',
}

export const EVENT_ACTION_LABEL: Record<string, string> = {
  create_task: '创建 Multica 任务', append_task: '追加 Multica 评论', create_decision: '转人工处理',
  ignore: '忽略事件', kernel_fallback: '转人工核查',
  'issue.create': '创建 Multica 任务', 'issue.comment': '追加 Multica 评论', 'decision.answered': '记录人工决定',
}

export const EVENT_ERROR_LABEL: Record<string, string> = {
  model_unavailable: '尚未配置可用的研判模型。',
  explicit_model_unavailable: '指定的研判模型不可用。',
  invalid_model_output: '助手返回的研判内容无法使用。',
  invalid_watchdog_action: '看门狗提出了超出只读研判范围的操作，请人工核查。',
  invalid_readonly_action: '助手提出了超出只读研判范围的操作，请人工核查。',
  untrusted_readonly_plan: '助手的研判方案包含未获授权的动作或目标，请人工核查。',
  decision_answered_without_rule: '已记录人工决定，没有匹配的启用规则。',
  multica_unavailable: '当前账号没有可用的 Multica 连接。',
  untrusted_issue_target: '无法验证关联任务，请核对规则和 Multica 工作区。',
  issue_not_found: '未找到关联的 Multica 任务。',
  issue_workspace_unverified: '无法确认任务属于当前 Multica 工作区。',
  issue_identity_mismatch: '返回的 Multica 任务与规则指定的任务不一致。',
  workspace_changed: 'Multica 工作区已变更，请核对执行目标。',
  connector_changed: 'Multica 连接地址已变更，请核对执行目标。',
  binding_connector_changed: '历史关联使用了不同的 Multica 连接，请核对执行目标。',
  binding_target_missing: '事件内容中未找到规则指定的关联任务。',
  binding_requires_ingest_key_scope: '关联任务前须在规则中指定已验证的来源 Key。',
  template_render_limit: '任务文本为空或超出长度限制，请检查规则模板。',
  multica_request_rejected: 'Multica 拒绝了这次请求，请核对任务和连接配置。',
  multica_request_unknown: '无法确认 Multica 是否收到这次请求，请先核对远端记录。',
  writeback_unknown: '远端写入结果未确认，请先核对 Multica 中的任务或评论。',
  writeback_validation_failed: '写入前校验未通过，请核对 Multica 连接和执行目标。',
  routing_validation_failed: '路由处理未通过校验，请查看动作步骤和原始记录。',
  hop_limit_exceeded: '事件关联层数已达上限，请核对来源。',
  owner_inactive: '事件所属账号当前不可用。',
  awaiting_business_router: '事件已接收，正在等待路由处理。',
}

/**
 * 研判提示词版本溯源：只显示版本号，不涉及提示词正文。
 * null = 早于版本冻结的历史事实（未记录）；0 = 账号从未自定义过提示词（平台默认），与
 * RoutingPolicyEditor.routingPromptLabel 对 0 的口径一致，不写成会误导的「v0」。
 */
export function promptProvenance(version: number | null | undefined): string {
  if (version == null) return '提示词版本未记录'
  return version > 0 ? `提示词 v${version}` : '平台默认提示词'
}

function toDate(value: DateInput): Date | null {
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value
  return typeof value === 'string' && value.trim() ? parseServerTime(value) : null
}

/** 列表与条件摘要用：YYYY-MM-DD HH:mm，到分钟。 */
export function formatEventTime(value: DateInput): string {
  return formatDateTime(value, { fallback: '时间未知' })
}

/** 本地时区标签（如 UTC+08:00），取该时刻的实际偏移（含夏令时）；不经 Intl，输出与浏览器语言无关。 */
export function timezoneLabel(value: DateInput): string {
  const date = toDate(value)
  if (!date) return ''
  const offset = -date.getTimezoneOffset()
  const minutes = Math.abs(offset)
  const sign = offset < 0 ? '-' : '+'
  return `UTC${sign}${String(Math.floor(minutes / 60)).padStart(2, '0')}:${String(minutes % 60).padStart(2, '0')}`
}

/** 详情与统计窗口用：YYYY-MM-DD HH:mm:ss（UTC±HH:MM）。 */
export function formatEventTimeFull(value: DateInput): string {
  const date = toDate(value)
  return date ? `${formatDateTime(date, { seconds: true })}（${timezoneLabel(date)}）` : '时间未知'
}

export type AppliedConditionKey =
  | 'source' | 'outcome' | 'route' | 'ruleId' | 'sourceSystem' | 'scheduledTaskId' | 'scheduledRunId' | 'timeRange'

export interface AppliedCondition {
  key: AppliedConditionKey
  /** 条件名，同时用于「移除筛选」按钮的可访问名称 */
  label: string
  /** 展示值（时间到分钟） */
  text: string
  /** 展示值被截短时的完整值（时间到秒并带时区） */
  full?: string
}

/** 已应用条件摘要：只读驱动请求的 applied 状态，不读输入草稿。顺序与工具栏一致，时间窗计 1 项。 */
export function appliedConditions(applied: AutoLoopTraceFilters, ruleNames: Record<string, string>): AppliedCondition[] {
  const conditions: AppliedCondition[] = []
  const add = (key: AppliedConditionKey, label: string, text: string | null | undefined, full?: string) => {
    if (!text) return
    conditions.push(full && full !== text ? { key, label, text, full } : { key, label, text })
  }
  if (applied.source) add('source', '来源类型', EVENT_SOURCE_LABEL[applied.source] ?? applied.source)
  if (applied.outcome) add('outcome', '处理状态', EVENT_OUTCOME[applied.outcome].label)
  if (applied.route) add('route', '路由结果', EVENT_ROUTE_LABEL[applied.route])
  if (applied.ruleId) add('ruleId', '流水规则', ruleNames[applied.ruleId] ?? '历史规则')
  if (applied.sourceSystem) add('sourceSystem', '来源系统', applied.sourceSystem)
  if (applied.scheduledTaskId) add('scheduledTaskId', '定时任务', applied.scheduledTaskId)
  if (applied.scheduledRunId) add('scheduledRunId', '日程执行', applied.scheduledRunId)
  if (applied.since || applied.until) {
    const range = (format: (value: string | undefined) => string) => `${format(applied.since)} 至 ${format(applied.until)}`
    add('timeRange', '接收时间', range(value => value ? formatEventTime(value) : '不限'),
      applied.since && applied.until ? range(value => formatEventTimeFull(value)) : undefined)
  }
  return conditions
}

/** 「更多筛选」旁的数量：首行两个下拉之外已应用的条件数。 */
export function advancedConditionCount(applied: AutoLoopTraceFilters): number {
  return appliedConditions(applied, {}).filter(condition => condition.key !== 'source' && condition.key !== 'outcome').length
}

/** 移除单个条件时要清掉的筛选字段（时间窗的起止一起清）。 */
export function conditionReset(key: AppliedConditionKey): Partial<AutoLoopTraceFilters> {
  if (key === 'timeRange') return { since: undefined, until: undefined }
  return { [key]: undefined } as Partial<AutoLoopTraceFilters>
}
