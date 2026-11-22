/**
 * 待处理决策事项提醒的纯规则层：无 DOM、Store、API 依赖，由 node 单测直接覆盖。
 *
 * 决策事项由后台看门狗与事件路由创建，后端没有任何推送给浏览器的事件，提醒只能
 * 建立在对 summary 的周期轮询之上：
 * - 「需要你处理」= 待决策(open) + 失败/未知(writeback_failed + writeback_unknown)；
 *   处理中(pending_dispatch/sending)由系统自动推进，不计入提醒，处理完成即自然消失。
 * - 首次观测只建立基线：页面打开前就已存在的事项不当作「新到达」打扰用户，由卡片与
 *   标签页标题角标持续可见。
 * - 长期未处理不做超时丢弃（服务端没有过期语义，静默丢弃风险高于提醒）：按最老待决策
 *   事项的等待天数进位提醒，每满一天提醒一次，加载当下不提醒。
 */
import type { DecisionItem, DecisionSummary } from '../../api/autoloop.ts'
import type { SnapshotStorage } from './conversationCache.ts'

const MINUTE_MS = 60_000
const HOUR_MS = 60 * MINUTE_MS
export const DAY_MS = 24 * HOUR_MS

/** 轮询周期：与聊天区、收件箱既有徽标轮询同节奏（后台标签页会被浏览器节流到约 1 分钟） */
export const DECISION_ALERT_POLL_MS = 15_000

/** 等待满 1 小时才在卡片上显示「已等待」，避免刚到达的事项带出「已等待 1 分钟」 */
export const WAITING_LABEL_AFTER_MS = HOUR_MS

/** 新事项到达后卡片徽标的脉冲时长：只负责把视线引过去，到点即静止——用户看到了但打算稍后处理时不被持续打扰 */
export const ATTENTION_PULSE_MS = 15_000

export interface DecisionAlertItem {
  id: string
  title: string
  createdAt: string
}

export interface DecisionAlertModel {
  openCount: number
  failedCount: number
  /** 最近一次对齐的待决策事项明细；null = 尚未对齐 */
  items: DecisionAlertItem[] | null
  /** 明细拉取失败：下一轮即使计数未变也补拉 */
  needsItems: boolean
  /** 已完成首次观测，此后出现的事项才算新到达 */
  primed: boolean
  /** 上一次观测时最老待决策事项的等待整天数；null = 当时没有待决策事项 */
  waitingDays: number | null
}

export const INITIAL_DECISION_ALERT_MODEL: DecisionAlertModel = {
  openCount: 0, failedCount: 0, items: null, needsItems: false, primed: false, waitingDays: null,
}

export type DecisionAlertEvent =
  | { type: 'new-open'; latest: DecisionAlertItem; count: number }
  | { type: 'new-failed'; count: number; at: number }
  | { type: 'stale'; openCount: number; days: number; oldestId: string; at: number }

/** 提醒文案；toast 与系统通知共用，保证两条通道对同一事件的说法一致 */
export interface DecisionAlertContent {
  title: string
  body: string
  /** 系统通知去重标签：同标签的通知被浏览器合并替换，多标签页同时提醒只留一条 */
  tag: string
  /** 打开决策弹窗时定位到的事项；null = 只打开弹窗 */
  focusId: string | null
}

export const failedCountOf = (summary: DecisionSummary) => summary.unknownCount + summary.failedCount

export const attentionCountOf = (model: Pick<DecisionAlertModel, 'openCount' | 'failedCount'>) =>
  model.openCount + model.failedCount

/** 本轮是否需要补拉待决策明细：有待决策事项，且从未对齐、计数变化或上次拉取失败 */
export function needsOpenDecisionItems(model: DecisionAlertModel, summary: DecisionSummary): boolean {
  return summary.openCount > 0 && (model.items === null || model.openCount !== summary.openCount || model.needsItems)
}

/** 只保留提醒需要的字段，不带问题正文与不可信原始负载 */
export function toDecisionAlertItems(items: DecisionItem[]): DecisionAlertItem[] {
  return items.map(({ id, title, createdAt }) => ({ id, title, createdAt }))
}

export function oldestOpenItem(items: DecisionAlertItem[] | null): DecisionAlertItem | null {
  return (items ?? []).reduce<DecisionAlertItem | null>(
    (oldest, item) => !oldest || item.createdAt < oldest.createdAt || (item.createdAt === oldest.createdAt && item.id < oldest.id) ? item : oldest,
    null,
  )
}

export function waitingDaysOf(createdAt: string, now: number): number {
  const age = now - Date.parse(createdAt)
  return Number.isFinite(age) ? Math.max(0, Math.floor(age / DAY_MS)) : 0
}

/**
 * 一次轮询结果并入提醒状态。
 *
 * items 为 null 表示本轮未拉取明细（计数未变或拉取失败）：沿用上一次对齐结果，只推进
 * 计数与等待天数；待决策数归零时无需明细即可清空。只有完成首次观测（primed）后出现的
 * 事项才产生事件。
 */
export function reduceDecisionAlerts(
  prev: DecisionAlertModel,
  summary: DecisionSummary,
  items: DecisionAlertItem[] | null,
  now: number,
): { model: DecisionAlertModel; events: DecisionAlertEvent[] } {
  const failedCount = failedCountOf(summary)
  const events: DecisionAlertEvent[] = []
  let nextItems = prev.items
  let needsItems = prev.needsItems
  const added: DecisionAlertItem[] = []
  if (items) {
    const known = new Set((prev.items ?? []).map(item => item.id))
    for (const item of items) if (!known.has(item.id)) added.push(item)
    nextItems = items
    needsItems = false
  } else if (summary.openCount === 0) {
    nextItems = []
    needsItems = false
  }
  if (prev.primed && added.length) {
    // 服务端按「优先级降序 → 等待最久优先」返回；提醒定位到最新到达的一项
    const latest = [...added].sort((a, b) => b.createdAt.localeCompare(a.createdAt) || b.id.localeCompare(a.id))[0]
    events.push({ type: 'new-open', latest, count: added.length })
  }
  if (prev.primed && failedCount > prev.failedCount) events.push({ type: 'new-failed', count: failedCount - prev.failedCount, at: now })

  const oldest = oldestOpenItem(nextItems)
  const waitingDays = oldest ? waitingDaysOf(oldest.createdAt, now) : null
  // 等待天数进位才提醒；同一轮已有新到达时由「新到达」提醒覆盖，不叠加两条
  if (prev.waitingDays !== null && waitingDays !== null && waitingDays > prev.waitingDays && !added.length && oldest) {
    events.push({ type: 'stale', openCount: summary.openCount, days: waitingDays, oldestId: oldest.id, at: now })
  }
  return {
    model: {
      openCount: summary.openCount,
      failedCount,
      items: nextItems,
      needsItems,
      primed: true,
      waitingDays,
    },
    events,
  }
}

export function formatWaiting(ageMs: number): string {
  if (ageMs < HOUR_MS) return `${Math.max(1, Math.floor(ageMs / MINUTE_MS))} 分钟`
  if (ageMs < DAY_MS) return `${Math.floor(ageMs / HOUR_MS)} 小时`
  return `${Math.floor(ageMs / DAY_MS)} 天`
}

/** 卡片上的「已等待」文案；不满 1 小时返回 null */
export function waitingLabel(items: DecisionAlertItem[] | null, now: number): string | null {
  const oldest = oldestOpenItem(items)
  if (!oldest) return null
  const age = now - Date.parse(oldest.createdAt)
  return age >= WAITING_LABEL_AFTER_MS ? formatWaiting(age) : null
}

export function decisionAlertContent(event: DecisionAlertEvent): DecisionAlertContent {
  if (event.type === 'new-open') {
    return {
      title: '超级助手：新的决策事项待处理',
      body: event.count > 1 ? `「${event.latest.title}」等 ${event.count} 项` : `「${event.latest.title}」`,
      tag: `openontology-decision-${event.latest.id}`,
      focusId: event.latest.id,
    }
  }
  if (event.type === 'new-failed') {
    return {
      title: '超级助手：决策处理结果需确认',
      body: `${event.count} 项决策的写回失败或结果未知`,
      // 失败没有稳定标识：按小时分桶，多标签页同一时段只留一条
      tag: `openontology-decision-failed-${Math.floor(event.at / HOUR_MS)}`,
      focusId: null,
    }
  }
  return {
    title: '超级助手：决策事项长期未处理',
    body: `${event.openCount} 项决策已等待 ${event.days} 天，请及时处理或标记「已人工处理」`,
    tag: `openontology-decision-stale-${event.days}`,
    focusId: event.oldestId,
  }
}

const TITLE_BADGE = /^\(\d+\) /

/** 去掉既有角标后的基础标题：提醒撤销时恢复，而不是把上一轮角标叠进基础标题 */
export const baseDocumentTitle = (title: string) => title.replace(TITLE_BADGE, '')

/** 标签页标题角标：计数放最前，标签栏截断时仍可见 */
export const decisionDocumentTitle = (base: string, attention: number) => (attention > 0 ? `(${attention}) ${base}` : base)

/** 与决策弹窗互斥的 URL 参数，和 SuperAssistantPage.openDecisionItems 的清理保持一致 */
const DECISION_DIALOG_PARAMS = ['autoloopEvent', 'ruleRouting', 'decision', 'decisions', 'schedule', 'scheduleRun']

/**
 * 提醒点击的落点：超级助手页既有的 ?decision=<id> / ?decisions=1 深链（页面据此打开弹窗）。
 * 已在超级助手页时保留会话等其它参数，只切换弹窗；在其它页面时不携带该页参数。
 */
export function decisionDeepLink(hash: string, focusId: string | null): { pathname: string; search: string } {
  const [path, search = ''] = hash.replace(/^#/, '').split('?')
  const params = new URLSearchParams(path === '/super-assistant' ? search : '')
  for (const key of DECISION_DIALOG_PARAMS) params.delete(key)
  params.set(focusId ? 'decision' : 'decisions', focusId ?? '1')
  return { pathname: '/super-assistant', search: `?${params.toString()}` }
}

export const DECISION_ALERT_PREFS_KEY_PREFIX = 'ob:sa-decision-alerts:v1'

export interface DecisionAlertPrefs {
  /** 用户在卡片上静音了桌面通知（浏览器授权无法由页面撤销，故保留应用内开关） */
  muted: boolean
}

export const decisionAlertPrefsKey = (userId: string) => `${DECISION_ALERT_PREFS_KEY_PREFIX}:${userId}`

/** 读端严格校验：形状不合法整体回到默认，不猜测用户意图 */
export function parseDecisionAlertPrefs(raw: string | null): DecisionAlertPrefs {
  if (!raw) return { muted: false }
  try {
    return { muted: (JSON.parse(raw) as { muted?: unknown } | null)?.muted === true }
  } catch {
    return { muted: false }
  }
}

export function readDecisionAlertPrefs(storage: SnapshotStorage, userId: string): DecisionAlertPrefs {
  try {
    return parseDecisionAlertPrefs(storage.getItem(decisionAlertPrefsKey(userId)))
  } catch {
    return { muted: false }
  }
}

/** 写入失败（配额满/私有模式）静默：本次会话内开关仍生效，只是不落盘 */
export function writeDecisionAlertPrefs(storage: SnapshotStorage, userId: string, prefs: DecisionAlertPrefs): void {
  try {
    storage.setItem(decisionAlertPrefsKey(userId), JSON.stringify(prefs))
  } catch { /* 存储不可用时静默 */ }
}
