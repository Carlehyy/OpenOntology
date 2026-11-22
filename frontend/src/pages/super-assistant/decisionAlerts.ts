/**
 * 待处理决策事项提醒的运行时：全局唯一轮询器 + 跨页面共享的提醒状态。
 *
 * 由 App 根部的 DecisionAlertHub 启停，超级助手页的提醒卡片与决策弹窗的摘要刷新共用
 * 同一份状态。与 DecisionItemsDialog.useDecisionSummary（仅前台、仅超级助手页）的差异是
 * 刻意的：提醒最需要的恰恰是标签页在后台、用户停在平台其它页面的时刻。
 *
 * 同一事件按用户此刻是否在看本页面只走一条通道，避免重复打扰：
 * - 可见且持有焦点：应用内 toast（决策弹窗打开期间不叠加——用户正在看）；
 * - 其它（后台标签页，或窗口可见但焦点在别的应用）：系统通知（需已授权且未静音）；
 *   不可用时在用户回到页面后补一条 toast。
 */
import { create } from 'zustand'
import { toast } from 'sonner'
import { autoloopApi, type DecisionItem } from '@/api/autoloop'
import { showDesktopNotification } from './desktopNotify.ts'
import {
  ATTENTION_PULSE_MS,
  DECISION_ALERT_POLL_MS,
  INITIAL_DECISION_ALERT_MODEL,
  attentionCountOf,
  baseDocumentTitle,
  decisionAlertContent,
  decisionDocumentTitle,
  needsOpenDecisionItems,
  readDecisionAlertPrefs,
  reduceDecisionAlerts,
  toDecisionAlertItems,
  writeDecisionAlertPrefs,
  type DecisionAlertContent,
  type DecisionAlertItem,
  type DecisionAlertModel,
  type DecisionAlertPrefs,
} from './decisionAlertModel.ts'

interface DecisionAlertState extends DecisionAlertModel {
  muted: boolean
  /** 新事项到达后的卡片注意力动效截止时刻（毫秒时间戳）；0 = 未在动效窗口内 */
  pulseUntil: number
}

/** 提醒状态（卡片、标题角标与提醒通道的共同数据源） */
export const useDecisionAlertStore = create<DecisionAlertState>(() => ({ ...INITIAL_DECISION_ALERT_MODEL, muted: false, pulseUntil: 0 }))

let timer: number | null = null
// 每次刷新自增；只有最后一次刷新的结果可以落地，迟到的旧响应不得回滚新状态
let generation = 0
let userId: string | null = null
let prefs: DecisionAlertPrefs = { muted: false }
let dialogOpen = false
// 平台标题是静态的（index.html），任何页面都不改写；这里记下基础标题以便撤销角标
let baseTitle: string | null = null
let openHandler: ((focusId: string | null) => void) | null = null
let missedAlert: DecisionAlertContent | null = null

function showDecisionToast(content: DecisionAlertContent) {
  toast(content.title, {
    description: content.body,
    duration: 8000,
    action: { label: '查看', onClick: () => openHandler?.(content.focusId) },
  })
}

function syncDocumentTitle(attention: number) {
  baseTitle ??= baseDocumentTitle(document.title)
  document.title = decisionDocumentTitle(baseTitle, attention)
}

/** 用户此刻是否正在看本页面。窗口没最小化、只是焦点在别的应用时页面仍「可见」，
 *  但应用内提示同样看不到——桌面办公最常见的离开方式，只有系统通知能触达。 */
function pageHasAttention() {
  return document.visibilityState === 'visible' && document.hasFocus()
}

function emitDecisionAlert(content: DecisionAlertContent) {
  if (pageHasAttention()) {
    if (!dialogOpen) showDecisionToast(content)
    return
  }
  if (!prefs.muted && showDesktopNotification({ ...content, onClick: () => openHandler?.(content.focusId) })) return
  missedAlert = content
}

/** 回到页面（标签页转可见或窗口重新获得焦点）：立即对齐一次，并补上未能送达的提醒 */
function handleAttentionRegained() {
  if (!pageHasAttention()) return
  void refreshDecisionAlerts()
  if (missedAlert) {
    const content = missedAlert
    missedAlert = null
    if (!dialogOpen) showDecisionToast(content)
  }
}

/** 拉全待决策明细：单页取接口上限 100，超出按游标续拉，最多 5 页防止异常数据拖垮提醒 */
async function loadOpenDecisions(): Promise<DecisionItem[]> {
  const items: DecisionItem[] = []
  let cursor: string | null = null
  for (let page = 0; page < 5; page++) {
    const result = await autoloopApi.listDecisions('open', cursor, 100)
    items.push(...result.items)
    if (!result.hasMore || !result.nextCursor) break
    cursor = result.nextCursor
  }
  return items
}

/**
 * 执行一次轮询（定时器、回到前台、弹窗内刷新共用）。
 * now 仅由测试传入以推进「等待天数进位」，生产调用不传。
 */
export async function refreshDecisionAlerts(now: number = Date.now()): Promise<void> {
  if (userId === null || timer === null) return
  const request = ++generation
  try {
    const summary = await autoloopApi.decisionSummary()
    let items: DecisionAlertItem[] | null = null
    if (needsOpenDecisionItems(useDecisionAlertStore.getState(), summary)) {
      try {
        items = toDecisionAlertItems(await loadOpenDecisions())
      } catch {
        if (request === generation) useDecisionAlertStore.setState({ needsItems: true })
      }
    }
    if (request !== generation) return
    const { model, events } = reduceDecisionAlerts(useDecisionAlertStore.getState(), summary, items, now)
    useDecisionAlertStore.setState(model)
    syncDocumentTitle(attentionCountOf(model))
    for (const event of events) {
      // 新到达才驱动卡片的注意力动效；长期未处理的进位提醒由提示通道承担，卡片本就在那里
      if (event.type !== 'stale') useDecisionAlertStore.setState({ pulseUntil: Date.now() + ATTENTION_PULSE_MS })
      emitDecisionAlert(decisionAlertContent(event))
    }
  } catch { /* 保留最后一次已确认计数；下一轮轮询重试，弹窗内刷新可看到服务错误 */ }
}

function resetRuntime() {
  generation++
  if (timer !== null) {
    window.clearInterval(timer)
    timer = null
  }
  window.removeEventListener('focus', handleAttentionRegained)
  document.removeEventListener('visibilitychange', handleAttentionRegained)
  missedAlert = null
  dialogOpen = false
  useDecisionAlertStore.setState({ ...INITIAL_DECISION_ALERT_MODEL, pulseUntil: 0 })
  if (baseTitle !== null) document.title = baseTitle
}

/** 开始提醒；同一用户重复调用只更新回调，换账号则整体重置后重新建立基线。 */
export function startDecisionAlerts(options: { userId: string | null; onOpen: (focusId: string | null) => void }) {
  if (options.userId !== userId) {
    resetRuntime()
    userId = options.userId
    prefs = userId ? readDecisionAlertPrefs(window.localStorage, userId) : { muted: false }
    useDecisionAlertStore.setState({ muted: prefs.muted })
  }
  openHandler = options.onOpen
  if (timer !== null) return
  window.addEventListener('focus', handleAttentionRegained)
  document.addEventListener('visibilitychange', handleAttentionRegained)
  timer = window.setInterval(() => { void refreshDecisionAlerts() }, DECISION_ALERT_POLL_MS)
  void refreshDecisionAlerts()
}

/** 停止提醒并清空状态（退出登录、失去菜单授权或应用卸载）。 */
export function stopDecisionAlerts() {
  resetRuntime()
  userId = null
  openHandler = null
}

/** 决策弹窗开合：打开期间不叠加应用内提示（新事项已在列表中呈现，用户正在看）。 */
export function noteDecisionDialogState(open: boolean) {
  dialogOpen = open
}

export function setDecisionAlertsMuted(muted: boolean) {
  prefs.muted = muted
  if (userId) writeDecisionAlertPrefs(window.localStorage, userId, prefs)
  useDecisionAlertStore.setState({ muted })
}

// E2E 与现场排障入口（同 main.tsx 的 __openOntologyReloadForAssets）：等价于手动触发一次轮询
;(window as unknown as Record<string, unknown>).__openOntologyRefreshDecisionAlerts = refreshDecisionAlerts
