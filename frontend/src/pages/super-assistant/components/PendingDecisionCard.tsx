import { useEffect, useState } from 'react'
import { Bell, BellOff, ListChecks } from 'lucide-react'
import { toast } from 'sonner'
import { setDecisionAlertsMuted, useDecisionAlertStore } from '../decisionAlerts'
import { attentionCountOf, waitingLabel } from '../decisionAlertModel'
import { desktopNotifyPermission, requestDesktopNotifyPermission } from '../desktopNotify'

const MINUTE_MS = 60_000
const FOCUS_RING = 'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring'
const HOVER_TINT = 'hover:bg-[color-mix(in_srgb,var(--color-warning)_12%,transparent)]'
// 徽标文字取页面底色令牌：浅色主题下近白压在琥珀底上、深色主题下深色压在亮琥珀底上，两套主题均过对比度
const BADGE_CLASS = 'relative flex h-[18px] min-w-[18px] items-center justify-center rounded-full bg-[var(--color-warning)] px-1 text-[11px] font-semibold tabular-nums text-background'

type NotifyState = 'default' | 'granted' | 'muted' | 'denied'

const NOTIFY_COPY: Record<NotifyState, { label: string; hint: string }> = {
  default: { label: '开启桌面通知', hint: '开启桌面通知：有新的决策事项时在系统层面提醒你' },
  granted: { label: '桌面通知已开启，点击静音', hint: '桌面通知已开启 · 点击静音' },
  muted: { label: '桌面通知已静音，点击开启', hint: '桌面通知已静音 · 点击开启' },
  denied: { label: '浏览器已阻止桌面通知', hint: '浏览器已阻止本站通知：请在地址栏站点设置中允许后刷新页面' },
}

/** 等待时长按分钟推进：挂载期间每分钟重算一次「已等待」 */
function useMinuteClock() {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), MINUTE_MS)
    return () => window.clearInterval(timer)
  }, [])
  return now
}

/** 新事项到达后的注意力动效窗口：到期自动静止 */
function useAttentionPulse(until: number) {
  const [active, setActive] = useState(() => Date.now() < until)
  useEffect(() => {
    const remaining = until - Date.now()
    if (remaining <= 0) {
      setActive(false)
      return
    }
    setActive(true)
    const timer = window.setTimeout(() => setActive(false), remaining)
    return () => window.clearTimeout(timer)
  }, [until])
  return active
}

function CountBadge({ value, ping = false, testId }: { value: number; ping?: boolean; testId?: string }) {
  return (
    <span className="relative flex h-[18px] shrink-0 items-center">
      {/* 新事项到达后的短时脉冲：把视线引向卡片，到点即静止 */}
      {ping && <span aria-hidden="true" className="absolute inset-0 animate-ping rounded-full bg-[var(--color-warning)] motion-reduce:animate-none" />}
      <span data-testid={testId} className={BADGE_CLASS}>{value > 99 ? '99+' : value}</span>
    </span>
  )
}

/**
 * 聊天区右上角的待决策提醒卡片：悬浮在消息区之上、不随消息滚动，处理完即消失。
 * 只统计「需要你处理」的事项（待决策 + 失败/未知），数据来自全局提醒状态
 * （见 decisionAlerts.ts），点击走页面既有的 openDecisionItems 打开决策弹窗。
 */
export function PendingDecisionCard({ onOpen }: { onOpen: () => void }) {
  const openCount = useDecisionAlertStore(state => state.openCount)
  const failedCount = useDecisionAlertStore(state => state.failedCount)
  const pulseUntil = useDecisionAlertStore(state => state.pulseUntil)
  const muted = useDecisionAlertStore(state => state.muted)
  const items = useDecisionAlertStore(state => state.items)
  const [permission, setPermission] = useState(desktopNotifyPermission)
  const now = useMinuteClock()
  const pulsing = useAttentionPulse(pulseUntil)

  // 浏览器站点设置里的通知权限会话内可变：回到页面时重读，开关不僵死在旧值
  useEffect(() => {
    const sync = () => setPermission(desktopNotifyPermission())
    window.addEventListener('focus', sync)
    document.addEventListener('visibilitychange', sync)
    return () => {
      window.removeEventListener('focus', sync)
      document.removeEventListener('visibilitychange', sync)
    }
  }, [])

  async function toggleNotify() {
    if (permission === 'default') {
      const next = await requestDesktopNotifyPermission()
      setPermission(next)
      if (next === 'granted') {
        setDecisionAlertsMuted(false)
        toast.success('桌面通知已开启', { description: '有新的决策事项时会在系统层面提醒你。' })
      } else if (next === 'denied') {
        toast.info('未获得通知授权', { description: '如需开启，请在浏览器地址栏的站点设置中允许通知。' })
      }
      return
    }
    if (permission === 'granted') {
      setDecisionAlertsMuted(!muted)
      return
    }
    toast.info('浏览器已阻止本站通知', { description: '请在地址栏站点设置中把「通知」改为允许，然后刷新页面。' })
  }

  if (attentionCountOf({ openCount, failedCount }) === 0) return null
  const notify: NotifyState | 'unsupported' = permission === 'unsupported' ? 'unsupported'
    : permission === 'granted' ? (muted ? 'muted' : 'granted')
    : permission
  const waiting = waitingLabel(items, now)
  const overview = `待决策 ${openCount} 项${failedCount ? `，需关注 ${failedCount} 项` : ''}`
  const bellOff = notify === 'muted' || notify === 'denied'

  // 外层实色卡片底承托内层浅底：深色主题下 --color-warning-bg 为半透明，直接叠在消息文字上会透底
  return (
    <div
      data-testid="super-assistant-pending-decisions"
      data-pulsing={pulsing ? 'true' : 'false'}
      className="absolute right-3 top-3 z-10 max-w-[calc(100%-1.5rem)] animate-slide-up rounded-full bg-card [box-shadow:var(--shadow-md)] motion-reduce:animate-none"
    >
      <div className="flex items-center rounded-full border border-[var(--color-warning)] bg-[var(--color-warning-bg)]">
        <button
          type="button"
          onClick={onOpen}
          aria-label={`查看待决策事项：${overview}`}
          title={[overview, ...(items ?? []).slice(0, 3).map(item => `· ${item.title}`)].join('\n')}
          className={`flex min-w-0 items-center gap-1.5 rounded-full py-1.5 pl-2.5 text-xs font-medium text-[var(--color-warning)] transition-colors ${HOVER_TINT} ${FOCUS_RING} ${notify === 'unsupported' ? 'pr-3' : 'pr-2.5'}`}
        >
          <ListChecks size={14} className="shrink-0" aria-hidden="true" />
          {openCount > 0 && (
            <>
              <span className="shrink-0">待决策</span>
              <CountBadge value={openCount} ping={pulsing} testId="super-assistant-pending-decisions-count" />
            </>
          )}
          {failedCount > 0 && (
            <>
              {openCount > 0 && <span aria-hidden="true" className="shrink-0 opacity-50">·</span>}
              <span className="shrink-0">需关注</span>
              <CountBadge value={failedCount} testId="super-assistant-pending-decisions-failed" />
            </>
          )}
          {waiting && (
            <span data-testid="super-assistant-pending-decisions-waiting" className="hidden min-w-0 truncate opacity-80 sm:inline">· 已等待 {waiting}</span>
          )}
        </button>
        {notify !== 'unsupported' && (
          <>
            <span aria-hidden="true" className="h-4 w-px shrink-0 bg-[color-mix(in_srgb,var(--color-warning)_35%,transparent)]" />
            <button
              type="button"
              data-testid="super-assistant-decision-notify"
              data-state={notify}
              onClick={() => void toggleNotify()}
              aria-label={NOTIFY_COPY[notify].label}
              title={NOTIFY_COPY[notify].hint}
              className={`mr-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-[var(--color-warning)] transition-colors ${HOVER_TINT} ${FOCUS_RING}${notify === 'denied' ? ' opacity-60' : ''}`}
            >
              {bellOff ? <BellOff size={14} /> : <Bell size={14} fill={notify === 'granted' ? 'currentColor' : 'none'} />}
            </button>
          </>
        )}
      </div>
    </div>
  )
}
