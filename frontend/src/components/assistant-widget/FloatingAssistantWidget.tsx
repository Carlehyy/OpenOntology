import { lazy, Suspense, useMemo } from 'react'
import { useLocation } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Loader2, Sparkles } from 'lucide-react'

import { superAssistantApi } from '@/api/superAssistant'
import { PLATFORM_NAV_ITEMS } from '@/config/navigation'
import { useAssistantWidgetStore } from '@/stores/assistantWidgetStore'
import {
  WIDGET_FAB_BOTTOM,
  WIDGET_PANEL_BOTTOM,
  WIDGET_Z,
  widgetAnchor,
  widgetVisibleOnPath,
} from '@/components/assistant-widget/logic'

// 面板依赖 antd / @ant-design/x，体积大，懒加载分包：首屏只承载这个轻量悬浮球
const AssistantWidgetPanel = lazy(() => import('./AssistantWidgetPanel'))

/** 面板首次加载（拉取 antd 分包）时的占位骨架，保持点击反馈即时可见 */
function PanelSkeleton({ bottomClass, zClass }: { bottomClass: string; zClass: string }) {
  return (
    <div className={`fixed ${bottomClass} right-5 ${zClass} flex h-[min(600px,calc(100dvh-7rem))] w-[min(384px,calc(100vw-2.5rem))] items-center justify-center rounded-2xl border border-[var(--color-border)] bg-[var(--color-bg-elevated)] shadow-[0_24px_64px_rgba(15,23,42,0.22)]`}>
      <Loader2 size={22} className="animate-spin text-teal-600" />
    </div>
  )
}

/**
 * 全局悬浮 AI 助手入口：任何登录后页面的右下角常驻图标，
 * 点击展开迷你对话框（底层复用超级助手会话与 SSE 流式能力）。
 * 挂载于 Layout，自动排除登录页与公开分享页。
 * 层级与位置按 widgetAnchor 策略分级（见 logic.ts）：常规页面 z-40
 * （让抽屉/模态/toast 正常覆盖），图谱编辑器等全屏覆盖层页面抬升至
 * z-[10000] 并上移避让页面自带控件。
 * 页面可见范围由管理员在 系统设置 → 超级助手 平台级配置（隐藏名单）；
 * 配置拉取失败或未配置时保持全部页面可见（与功能上线前一致）。
 */
export default function FloatingAssistantWidget() {
  const open = useAssistantWidgetStore(state => state.open)
  const streaming = useAssistantWidgetStore(state => state.streaming)
  const awaitingDecision = useAssistantWidgetStore(state => state.pending !== null)
  const toggle = useAssistantWidgetStore(state => state.toggle)
  const location = useLocation()
  const anchor = widgetAnchor(location.pathname)

  const { data: widgetConfig } = useQuery({
    queryKey: ['assistant-widget-config'],
    queryFn: () => superAssistantApi.widgetConfig(),
    staleTime: 60_000,
    retry: 1,
  })
  const hiddenMenuKeys = useMemo(
    () => new Set(Array.isArray(widgetConfig?.hidden_menu_keys) ? widgetConfig.hidden_menu_keys : []),
    [widgetConfig],
  )
  if (!widgetVisibleOnPath(location.pathname, PLATFORM_NAV_ITEMS, hiddenMenuKeys)) return null

  return (
    <>
      {open && (
        <Suspense fallback={<PanelSkeleton bottomClass={WIDGET_PANEL_BOTTOM[anchor]} zClass={WIDGET_Z[anchor]} />}>
          <AssistantWidgetPanel />
        </Suspense>
      )}
      <button
        type="button"
        onClick={toggle}
        aria-label={open ? '关闭 AI 助手' : '打开 AI 助手'}
        aria-expanded={open}
        title="AI 助手"
        data-testid="assistant-widget-fab"
        className={`fixed right-5 ${WIDGET_FAB_BOTTOM[anchor]} ${WIDGET_Z[anchor]} flex h-12 w-12 items-center justify-center rounded-full bg-[var(--color-nav-bg)] text-white shadow-[0_10px_30px_rgba(13,148,136,0.35)] transition-all hover:scale-105 hover:shadow-[0_14px_36px_rgba(13,148,136,0.45)] active:scale-95 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2`}
      >
        <Sparkles size={22} />
        {(streaming || awaitingDecision) && !open && (
          <span className="absolute -right-0.5 -top-0.5 flex h-3 w-3" aria-hidden="true">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-amber-400 opacity-75" />
            <span className="relative inline-flex h-3 w-3 rounded-full bg-amber-500" />
          </span>
        )}
      </button>
    </>
  )
}
