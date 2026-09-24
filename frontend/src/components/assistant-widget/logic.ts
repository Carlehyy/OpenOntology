// 悬浮 AI 助手的纯逻辑：会话挑选、SSE 流事件归约、思考链视图模型、错误文案提取。
// 本模块被单元测试在 Node --experimental-strip-types 下直接执行，
// 只允许类型级 import（运行时装载前会被擦除），禁止引入任何运行时依赖。

import type { StreamEvent, SuperMessage, ToolStep } from '../../api/superAssistant'

export interface PendingConfirmation {
  toolRunId: string
  toolName: string
  serverName: string
  arguments: Record<string, unknown>
}

/**
 * 会话初始选中：优先 URL/调用方指定的会话（须存在于列表中），否则取最新一条。
 * 超级助手页面（?conversation= 参数）与悬浮窗共用同一套挑选语义。
 */
export function pickInitialConversationId(
  conversations: ReadonlyArray<{ id: string }>,
  requestedId?: string | null,
): string | null {
  if (requestedId && conversations.some(item => item.id === requestedId)) return requestedId
  return conversations[0]?.id ?? null
}

/** 提取接口/流式错误的可读文案。apiClient 拦截器会把后端响应体（{detail}）直接作为拒绝值。 */
export function errorMessage(error: unknown, fallback = '操作失败'): string {
  const detail = (error as { detail?: unknown } | null)?.detail
  if (typeof detail === 'string' && detail) return detail
  if (detail && typeof detail === 'object') {
    const message = (detail as { message?: unknown }).message
    if (typeof message === 'string' && message) return message
  }
  const message = (error as { message?: unknown } | null)?.message
  return typeof message === 'string' && message ? message : fallback
}

/** 后端 menu_guard 的无权限拒绝（403 + detail.code = MENU_ACCESS_DENIED）。 */
export function isMenuAccessDenied(error: unknown): boolean {
  const detail = (error as { detail?: unknown } | null)?.detail
  return Boolean(
    detail
    && typeof detail === 'object'
    && (detail as { code?: unknown }).code === 'MENU_ACCESS_DENIED',
  )
}

export interface StreamReduceResult {
  message: SuperMessage
  /** tool_confirmation_required 事件给出的待确认卡片信息 */
  pendingConfirmation?: PendingConfirmation
  /** tool_result 事件给出应解除待确认卡片的 toolRunId */
  clearPendingFor?: string
  /** error 事件的可读文案（供全局提示使用） */
  errorText?: string
}

/**
 * 把单个 SSE 事件归约到乐观插入的助手消息上。
 * 语义与 SuperAssistantPage 的内联处理保持一致，修改时需两侧同步验证。
 * thinking / done 事件不改变消息本体（thinkingRound 由调用方单独维护），返回原引用。
 */
export function reduceStreamEvent(message: SuperMessage, event: StreamEvent): StreamReduceResult {
  const { event: name, data } = event
  if (name === 'text_delta') {
    return { message: { ...message, content: message.content + String(data.delta || '') } }
  }
  if (name === 'tool_start') {
    const step: ToolStep = { toolName: data.toolName, status: 'running', arguments: data.arguments }
    return { message: { ...message, steps: [...message.steps, step] } }
  }
  if (name === 'tool_confirmation_required') {
    const steps = message.steps.map((step, index) => (
      index === message.steps.length - 1 ? { ...step, status: 'awaiting_confirmation' } : step
    ))
    return {
      message: { ...message, steps },
      pendingConfirmation: {
        toolRunId: String(data.toolRunId ?? ''),
        toolName: String(data.toolName ?? ''),
        serverName: String(data.serverName ?? ''),
        arguments: (data.arguments as Record<string, unknown>) || {},
      },
    }
  }
  if (name === 'tool_result') {
    const steps = message.steps.map((step, index) => (
      index === message.steps.length - 1
        ? { ...step, status: String(data.status || step.status), preview: data.preview }
        : step
    ))
    return { message: { ...message, steps }, clearPendingFor: String(data.toolRunId ?? '') }
  }
  if (name === 'message_end') {
    return {
      message: {
        ...message,
        content: data.message?.content || message.content,
        steps: data.message?.steps || message.steps,
        token_usage: data.message?.tokenUsage || {},
        status: 'complete',
      },
    }
  }
  if (name === 'cancelled') {
    return { message: { ...message, status: 'cancelled' } }
  }
  if (name === 'error') {
    const text = typeof data.message === 'string' && data.message ? data.message : '生成失败'
    return { message: { ...message, content: text, status: 'error' }, errorText: text }
  }
  return { message }
}

/** 思考链节点的展示状态（与 @ant-design/x ThoughtChainItemType.status 对齐）。 */
export type ChainStepStatus = 'loading' | 'success' | 'error' | 'abort'

export interface ChainStepView {
  key: string
  title: string
  status: ChainStepStatus
  /** 工具结果摘要（截断后由后端给出，≤800 字符） */
  previewText?: string
  /** 工具入参的 JSON 文本，可折叠展示 */
  argumentsText?: string
}

export function mapToolStepStatus(status: string): ChainStepStatus {
  if (status === 'success') return 'success'
  if (status === 'running' || status === 'awaiting_confirmation') return 'loading'
  if (status === 'cancelled') return 'abort'
  return 'error'
}

/**
 * 悬浮球/面板的锚定与层级策略。平台自身占用右下角固定位的页面需要避让：
 * - 本体图谱编辑器（/ontologies/:id/graph）：全屏覆盖层 z-[9999] + FloatingMenu 悬浮菜单，
 *   需要同时“抬层级”（压过覆盖层）和“上移”（让开菜单按钮）→ overlay
 * - 同步任务（/data/pipelines/sync-tasks）：长表滚到底部时底部分页条位于右下角 → lifted
 * - 事件登记（/events）：移动端右下角新建 FAB（仅小屏）→ liftedMobileOnly
 * - 场景助手（/scenes/modeling）：edge-to-edge 双卡布局，右卡对话输入区贴到视口
 *   右下角，「发送」按钮正处悬浮球默认位 → aboveComposer（整体抬到输入区上方）
 * 其余页面保持 z-40：高于普通页面内容，但让抽屉/模态（z-50+）与 toast（z-500）正常覆盖，
 * 避免悬浮球遮挡它们的右下角控件。新增页面若在右下角放置固定控件导致点击被遮挡，在此登记。
 */
export type WidgetAnchor = 'default' | 'overlay' | 'lifted' | 'liftedMobileOnly' | 'aboveComposer'

export function widgetAnchor(pathname: string): WidgetAnchor {
  if (pathname === '/events' || pathname.startsWith('/events/')) return 'liftedMobileOnly'
  if (/^\/ontologies\/[^/]+\/graph$/.test(pathname)) return 'overlay'
  if (pathname === '/data/pipelines/sync-tasks' || pathname.startsWith('/data/pipelines/sync-tasks/')) return 'lifted'
  if (pathname === '/scenes/modeling') return 'aboveComposer'
  return 'default'
}

/**
 * 没有浏览器本地位置时，悬浮球贴右缘，只按页面改 bottom。
 * 用户拖动后的坐标见 WIDGET_POSITION_STORAGE_KEY，会盖过这里的 bottom/right，
 * 但不盖过 WIDGET_Z（图谱页仍要压过全屏层）。
 * Tailwind 扫描需要字面量类名。
 */
export const WIDGET_FAB_BOTTOM: Record<WidgetAnchor, string> = {
  default: 'bottom-5',
  overlay: 'bottom-20',
  lifted: 'bottom-20',
  liftedMobileOnly: 'bottom-20 md:bottom-5',
  // 输入栏已对齐本体助手页（单行胶囊，高 ≈67px）+ 卡片间距 4px ≈ 4.5rem
  aboveComposer: 'bottom-[4.75rem]',
}

/** 悬浮球/面板层级（见 widgetAnchor 注释）。面板是球的绝对定位子节点，跟着球走，不再单独写 bottom。 */
export const WIDGET_Z: Record<WidgetAnchor, string> = {
  default: 'z-40',
  overlay: 'z-[10000]',
  lifted: 'z-40',
  liftedMobileOnly: 'z-40',
  aboveComposer: 'z-40',
}

/** 悬浮球位置只存在本机，不入库。换浏览器或清站点数据后回到默认锚点。 */
export const WIDGET_POSITION_STORAGE_KEY = 'ob:assistant-widget-position:v1'
export const WIDGET_FAB_SIZE = 48
export const WIDGET_DRAG_THRESHOLD_PX = 4

export interface WidgetViewportPoint {
  left: number
  top: number
}

export interface WidgetFabRect {
  left: number
  top: number
  width: number
  height: number
}

export function parseWidgetPosition(raw: string | null): WidgetViewportPoint | null {
  if (!raw) return null
  try {
    const value = JSON.parse(raw) as { left?: unknown; top?: unknown }
    if (typeof value?.left !== 'number' || typeof value?.top !== 'number') return null
    if (!Number.isFinite(value.left) || !Number.isFinite(value.top)) return null
    return { left: value.left, top: value.top }
  } catch {
    return null
  }
}

/** 把球的左上角限制在视口内，避免拖出屏幕或换一台更小的窗口后找不到。 */
export function clampWidgetPosition(
  point: WidgetViewportPoint,
  viewport: { width: number; height: number },
  fabSize = WIDGET_FAB_SIZE,
): WidgetViewportPoint {
  const maxLeft = Math.max(0, viewport.width - fabSize)
  const maxTop = Math.max(0, viewport.height - fabSize)
  return {
    left: Math.min(maxLeft, Math.max(0, point.left)),
    top: Math.min(maxTop, Math.max(0, point.top)),
  }
}

/**
 * 面板贴着球展开，并翻到视口里面。
 * align=right：面板右缘对齐球的右缘（默认右下角的历史行为）。
 * align=left：球靠近左缘时改为向右展开。
 */
export function widgetPanelPlacement(
  fab: WidgetFabRect,
  viewport: { width: number; height: number },
  panel: { width: number; height: number },
  gap = 8,
): { vertical: 'above' | 'below'; align: 'left' | 'right' } {
  const spaceAbove = fab.top
  const spaceBelow = viewport.height - (fab.top + fab.height)
  const need = panel.height + gap
  const vertical: 'above' | 'below' = spaceAbove >= need || spaceAbove >= spaceBelow ? 'above' : 'below'
  const fitsAlignRight = fab.left + fab.width >= panel.width
  const fitsAlignLeft = fab.left + panel.width <= viewport.width
  const align: 'left' | 'right' = fitsAlignRight || !fitsAlignLeft ? 'right' : 'left'
  return { vertical, align }
}

/**
 * 页面可见范围配置（系统设置 → 超级助手）使用的导航节点结构。
 * 与 config/navigation 的 PlatformNavItem 结构对齐，仅取判定所需字段；
 * 本模块保持零运行时依赖，不能 import navigation.ts（它会加载 lucide 等运行时模块），
 * 由调用方把 PLATFORM_NAV_ITEMS 传入。
 */
export interface WidgetNavNode {
  key: string
  to: string
  subItems?: readonly WidgetNavNode[]
}

/**
 * 解析路径命中的导航菜单键：取 `to` 与路径匹配（精确或 to + '/' 前缀）的最深节点，
 * 使 /data/pipelines/sync-tasks 命中 data.sync_tasks 而非 data.pipelines，
 * /ontologies/:id/graph 等详情页归属其所属的叶子菜单（ontologies）。
 * 一级目录的 to（如 /data）是重定向页，天然在最深匹配中输给子节点。
 * 未命中任何节点返回 null。
 */
export function widgetNavLeafKey(pathname: string, items: readonly WidgetNavNode[]): string | null {
  let bestKey: string | null = null
  let bestLength = -1
  const visit = (nodes: readonly WidgetNavNode[]) => {
    for (const node of nodes) {
      const matched = pathname === node.to || pathname.startsWith(node.to + '/')
      if (matched && node.to.length > bestLength) {
        bestKey = node.key
        bestLength = node.to.length
      }
      if (node.subItems) visit(node.subItems)
    }
  }
  visit(items)
  return bestKey
}

/**
 * 悬浮助手在指定路径是否可见：命中导航目录的按隐藏名单判定；
 * 未命中导航目录的路径（/inbox、/overview、公开分享页等）不在可配置范围内，
 * 保持功能上线前行为——始终可见。
 */
export function widgetVisibleOnPath(
  pathname: string,
  items: readonly WidgetNavNode[],
  hiddenMenuKeys: ReadonlySet<string>,
): boolean {
  const key = widgetNavLeafKey(pathname, items)
  if (!key) return true
  return !hiddenMenuKeys.has(key)
}

/**
 * 把一条助手消息的工具步骤映射为 ThoughtChain 视图项。
 * 流式进行中、尚无可见正文且没有正在执行/等待确认的工具时，
 * 末尾追加“正在思考”占位项（消费后端已发出但页面此前未使用的 thinking 事件轮次）。
 */
export function buildChainSteps(
  steps: readonly ToolStep[],
  opts: { streaming?: boolean; thinkingRound?: number | null; hasContent?: boolean },
): ChainStepView[] {
  const items: ChainStepView[] = steps.map((step, index) => ({
    key: `tool-${index}`,
    title: step.toolName || `工具调用 ${index + 1}`,
    status: mapToolStepStatus(step.status),
    previewText: step.preview || undefined,
    argumentsText: step.arguments && Object.keys(step.arguments).length
      ? JSON.stringify(step.arguments, null, 2)
      : undefined,
  }))
  const anyActive = steps.some(step => step.status === 'running' || step.status === 'awaiting_confirmation')
  if (opts.streaming && !anyActive && !opts.hasContent) {
    items.push({
      key: 'thinking',
      title: opts.thinkingRound ? `正在思考（第 ${opts.thinkingRound} 轮推理）` : '正在思考…',
      status: 'loading',
    })
  }
  return items
}
