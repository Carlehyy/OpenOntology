import { useEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent } from 'react'
import { keepPreviousData, useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import ReactECharts from 'echarts-for-react'
import type { EChartsOption } from 'echarts'
import {
  Archive, ArchiveRestore, ArrowLeft, BarChart3, Bell, FileText, Forward, Inbox, Loader2,
  Mail, MailOpen, Paperclip, Plug, Send, Star,
} from 'lucide-react'
import { toast } from 'sonner'

import {
  NOTIFICATION_TABS,
  notificationsApi,
  type NotificationAttachment,
  type NotificationMessage,
  type NotificationPriority,
  type NotificationTab,
} from '@/api/notifications'
import { Button } from '@/components/ui/Button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import {
  baseChartOption, categoryAxisBase, valueAxisBase,
  CHART_TEAL, CHART_TEXT_STRONG,
} from '@/lib/echartsTheme'
import { formatDateTime } from '@/utils/datetime'
import { useAuthStore } from '@/stores/authStore'

import { DialogShell } from './AssistantConfiguration'
import { assistantMarkdownComponents } from './AssistantConversation'
import { errorText } from './assistantPanelUtils'
import NotificationInboundSection, { type InboundSectionHandle } from './NotificationsInboundSection'
import NotificationOutboundSection, { type OutboundSectionHandle } from './NotificationsOutboundSection'
import NotificationMedia from './NotificationMedia'
import { iconButtonClass, inputClass, sectionHeadingClass } from './notificationsConfigShared'
import {
  formatFileSize, forwardButtonLabel, listEmptyCopy, priorityStyle, sourceTypeLabel, stripMarkdownPreview,
} from './notificationsFormat'

/**
 * 消息中转站弹窗：左侧分区导航（消息列表 / 发送消息 / 渠道配置 / 消息统计）+ 右侧内容。
 * - 消息列表：列表列自带筛选头（全部消息默认 → 已标记 → 已归档；全部消息=未归档收件箱，与已归档互补）+ 图标化「全部已读」，
 *   右区只有「列表 / 详情」两块；窄屏由选中态驱动单栏切换（Esc 先回列表再关弹窗）。
 * - 渠道配置：拆分为 Inbound（消息入口：投递接入/密钥）与 Outbound（消息出口：发件邮箱+转发渠道）
 *   两个子组件；本组件持有共用渠道 query 与 Esc 分层回退句柄。
 * - 消息统计：ECharts（平台唯一图表标准，svg 渲染器），渠道口径与测试直发在卡片上明示。
 */
type NavSection = 'messages' | 'compose' | 'config' | 'stats'

const NAV_ITEMS: { key: NavSection; label: string; icon: typeof Bell }[] = [
  { key: 'messages', label: '消息列表', icon: Bell },
  { key: 'compose', label: '发送消息', icon: Send },
  { key: 'config', label: '渠道配置', icon: Plug },
  { key: 'stats', label: '消息统计', icon: BarChart3 },
]

/** 通知正文沿用会话排版映射，仅覆盖 img：内嵌媒体走鉴权拉取与音视频原生控件 */
const notificationMarkdownComponents = {
  ...assistantMarkdownComponents,
  img: ({ src, alt }: { src?: string; alt?: string }) => (
    <NotificationMedia src={src} alt={alt} />
  ),
}

function NotificationMarkdown({ content }: { content: string }) {
  return (
    <div className="min-w-0 break-words [overflow-wrap:anywhere]">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={notificationMarkdownComponents}>
        {content}
      </ReactMarkdown>
    </div>
  )
}

/** 附件鉴权下载：原生标签带不了 Bearer，先取 blob 再触发浏览器另存 */
async function downloadAttachment(messageId: string, attachment: NotificationAttachment): Promise<void> {
  const token = useAuthStore.getState().token
  const response = await fetch(notificationsApi.attachmentUrl(messageId, attachment.id), {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  if (!response.ok) throw new Error(`附件下载失败（${response.status}）`)
  const blob = await response.blob()
  const objectUrl = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = objectUrl
  anchor.download = attachment.filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  // 同步 click 已启动下载；revoke 需给慢浏览器/大附件留出建流时间（过早回收会截断）
  setTimeout(() => URL.revokeObjectURL(objectUrl), 10_000)
}

export default function NotificationsDialog({
  open,
  onClose,
}: {
  open: boolean
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [nav, setNav] = useState<NavSection>('messages')
  const [tab, setTab] = useState<NotificationTab>('all')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  // 手动转发面板
  const [forwardOpen, setForwardOpen] = useState(false)
  const [forwardChannelIds, setForwardChannelIds] = useState<Set<string>>(new Set())

  // 手动发送表单（独立分区）
  const [composeTitle, setComposeTitle] = useState('')
  const [composeBody, setComposeBody] = useState('')
  const [composePriority, setComposePriority] = useState<NotificationPriority>('normal')
  // 消息统计：时间窗（7/30 天，默认 30）
  const [statsDays, setStatsDays] = useState<7 | 30>(30)
  // 投递去向：null = 默认全选（跟随启用渠道）；Set = 用户改选后的显式集合
  const [composeChannelIds, setComposeChannelIds] = useState<Set<string> | null>(null)

  // 只失效列表与徽章；detail 缓存由 setQueryData 精确写入——
  // 若前缀失效命中 detail，会触发「GET 详情即自动已读」把刚标回的未读打回
  const invalidateLists = () => {
    void queryClient.invalidateQueries({ queryKey: ['notifications', 'page'] })
    void queryClient.invalidateQueries({ queryKey: ['notifications', 'summary'] })
  }

  const summary = useQuery({
    queryKey: ['notifications', 'summary'],
    queryFn: notificationsApi.summary,
    enabled: open,
    // 15s 轮询由工作台侧栏那路常驻 observer 负责（同一 queryKey 会合并通知），
    // 弹窗内不再挂第二个 interval，避免开启期间轮询频率翻倍
    refetchInterval: false,
  })

  const list = useInfiniteQuery({
    queryKey: ['notifications', 'page', tab],
    enabled: open,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) => notificationsApi.list({ tab, cursor: pageParam, limit: 30 }),
    getNextPageParam: page => page.nextCursor || undefined,
    // 切 tab 期间保留上一屏列表，避免闪一帧空态
    placeholderData: keepPreviousData,
  })

  const detail = useQuery({
    queryKey: ['notifications', 'detail', selectedId],
    enabled: open && !!selectedId,
    queryFn: () => notificationsApi.get(selectedId as string),
    // 服务端「GET 详情即已读」；短暂 stale 期内复用缓存：
    // ① 避免刚「标为未读」的消息因切 tab 重挂载被再次自动已读；
    // ② 刚发送的消息复用 create 响应（本身即完整 detail 形状），不会立刻 GET 被置读
    staleTime: 30_000,
  })

  // 渠道数据在三个分区都用（配置/测试/消息分区的转发面板）：弹窗打开即拉取
  const channels = useQuery({
    queryKey: ['notifications', 'channels'],
    queryFn: notificationsApi.channels.list,
    enabled: open,
  })

  const stats = useQuery({
    queryKey: ['notifications', 'stats', statsDays],
    queryFn: () => notificationsApi.stats(statsDays),
    enabled: open && nav === 'stats',
    placeholderData: previous => previous,
  })

  // 渠道配置两个子分区的 Esc 分层回退句柄（签发表单/渠道表单收起优先于关弹窗）
  const inboundSectionRef = useRef<InboundSectionHandle>(null)
  const outboundSectionRef = useRef<OutboundSectionHandle>(null)

  // 弹窗关闭即回消息分区并清空转发/发送临时态；密钥明文与渠道表单的清理随子组件各自收口
  useEffect(() => {
    if (!open) {
      setNav('messages')
      setForwardOpen(false)
      setForwardChannelIds(new Set())
      setComposeTitle('')
      setComposeBody('')
      setComposePriority('normal')
      setComposeChannelIds(null)
    }
  }, [open])

  // 详情接口在服务端把消息标为已读：同步刷新列表与徽章（不刷详情自身，避免循环）。
  // 键含 isRead：同一条消息「标为未读后重新查阅」的已读回翻也要即时刷——
  // 只看 id 会漏掉翻转，未读底色与徽章要等 15s 轮询才纠正（观感即"查阅了标识还在"）
  const syncedReadKey = useRef<string | null>(null)
  useEffect(() => {
    const message = detail.data
    if (!message) return
    const key = `${message.id}:${message.isRead}`
    if (syncedReadKey.current === key) return
    syncedReadKey.current = key
    void queryClient.invalidateQueries({ queryKey: ['notifications', 'page'] })
    void queryClient.invalidateQueries({ queryKey: ['notifications', 'summary'] })
  }, [detail.data, queryClient])

  // 点击即查阅：detail 缓存在 staleTime 内被视为新鲜不会发 GET，但用户主动点开的
  // 未读消息必须真实请求一次让服务端落已读；无缓存时挂载本身就会 GET，无需干预。
  // 非点击路径（切 tab 重挂载/弹窗重开沿用选中）不强制——保住"标为未读稍后处理"不被自动打回
  const openMessage = (id: string, isRead: boolean) => {
    setSelectedId(id)
    if (isRead) return
    const cached = queryClient.getQueryData<NotificationMessage>(['notifications', 'detail', id])
    if (cached && !cached.isRead) {
      void queryClient.refetchQueries({ queryKey: ['notifications', 'detail', id] })
    }
  }

  // 转发面板是消息分区的临时态：离开该分区即收起，避免 Esc/返回语义被不可见面板吃掉
  useEffect(() => {
    if (nav !== 'messages') {
      setForwardOpen(false)
      setForwardChannelIds(new Set())
    }
  }, [nav])

  const stateMutation = useMutation({
    mutationFn: ({ id, fields }: { id: string; fields: { isRead?: boolean; isStarred?: boolean; isArchived?: boolean } }) =>
      notificationsApi.updateState(id, fields),
    onSuccess: data => {
      queryClient.setQueryData(['notifications', 'detail', data.id], data)
      invalidateLists()
    },
    onError: error => toast.error(errorText(error)),
  })

  const readAllMutation = useMutation({
    mutationFn: notificationsApi.readAll,
    onSuccess: data => {
      toast.success(`已将 ${data.updated} 条消息标为已读`)
      invalidateLists()
    },
    onError: error => toast.error(errorText(error)),
  })

  const createMutation = useMutation({
    mutationFn: notificationsApi.create,
    onSuccess: data => {
      toast.success('消息已发送')
      setComposeTitle('')
      setComposeBody('')
      setComposePriority('normal')
      setComposeChannelIds(null)
      setSelectedId(data.id)
      // 新消息未读未归档：切到全部消息视图保证列表与选中详情同屏可见
      setTab('all')
      setNav('messages')
      queryClient.setQueryData(['notifications', 'detail', data.id], data)
      invalidateLists()
    },
    onError: error => toast.error(errorText(error)),
  })

  const forwardMutation = useMutation({
    mutationFn: ({ messageId, channelIds }: { messageId: string; channelIds: string[] }) =>
      notificationsApi.forward(messageId, channelIds),
    onSuccess: results => {
      const okCount = results.filter(r => r.ok).length
      const failed = results.filter(r => !r.ok)
      if (okCount) toast.success(`已转发到 ${okCount} 个渠道`)
      for (const item of failed) toast.error(`转发失败：${item.message}`)
      setForwardOpen(false)
      setForwardChannelIds(new Set())
      void queryClient.invalidateQueries({ queryKey: ['notifications', 'channels'] })
    },
    onError: error => toast.error(errorText(error)),
  })

  const items = useMemo(
    () => list.data?.pages.flatMap(page => page.items) ?? [],
    [list.data],
  )
  const selected = detail.data ?? null
  const unreadCount = summary.data?.unreadCount ?? 0

  // 入站趋势（ECharts 平台标准；svg 渲染器保证容器内渲染出 <svg> 供 e2e 锚定）
  const trendOption = useMemo<EChartsOption | null>(() => {
    const trend = stats.data?.trend ?? []
    if (trend.length === 0) return null
    const base = baseChartOption()
    return {
      ...base,
      aria: { enabled: true, description: '每日入站消息量柱状趋势' },
      grid: { left: 4, right: 8, top: 16, bottom: 2, containLabel: true },
      tooltip: {
        ...base.tooltip,
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        textStyle: { color: CHART_TEXT_STRONG, fontSize: 12 },
      },
      xAxis: {
        ...categoryAxisBase,
        type: 'category',
        data: trend.map(item => item.date.slice(5)),
      },
      yAxis: { ...valueAxisBase, type: 'value', minInterval: 1 },
      series: [{
        name: '入站',
        type: 'bar',
        barMaxWidth: 18,
        itemStyle: { color: CHART_TEAL, borderRadius: [3, 3, 0, 0] },
        emphasis: { focus: 'series' },
        data: trend.map(item => item.count),
      }],
    }
  }, [stats.data?.trend])

  const handleDownload = async (messageId: string, attachment: NotificationAttachment) => {
    try {
      await downloadAttachment(messageId, attachment)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : '附件下载失败')
    }
  }

  const enabledChannels = (channels.data ?? []).filter(channel => channel.enabled)

  const handleCompose = () => {
    if (!composeTitle.trim()) {
      toast.error('标题不能为空')
      return
    }
    // 渠道列表加载失败：去向不明，拒绝发送（界面提示错误并给重试入口）
    if (channels.isError) {
      toast.error('渠道列表加载失败，无法确定投递去向，请重试')
      return
    }
    // 无启用渠道：消息仅入站内（后端缺省扇出到空集，语义一致）
    if (enabledChannels.length === 0) {
      createMutation.mutate({ title: composeTitle.trim(), body: composeBody, priority: composePriority })
      return
    }
    // 勾选集与当前启用渠道求交集：同会话内渠道被停用/删除后，失效 id 不再随请求发出
    const enabledIds = new Set(enabledChannels.map(channel => channel.id))
    const selectedIds = composeChannelIds
      ? [...composeChannelIds].filter(id => enabledIds.has(id))
      : [...enabledIds]
    if (selectedIds.length === 0) {
      toast.error('至少选择一个投递渠道')
      return
    }
    createMutation.mutate({
      title: composeTitle.trim(),
      body: composeBody,
      priority: composePriority,
      // 勾选集恰为全量时省略字段：走后端缺省语义（无渠道数上限、渠道新增即覆盖）
      ...(selectedIds.length === enabledIds.size ? {} : { channelIds: selectedIds }),
    })
  }

  /** 发送分区的渠道勾选集：null 视为全选，与启用渠道列表求最终勾选态 */
  const composeSelection = (channelId: string): boolean => {
    if (!composeChannelIds) return true
    return composeChannelIds.has(channelId)
  }

  const toggleComposeChannel = (channelId: string) => {
    setComposeChannelIds(current => {
      const base = current ?? new Set(enabledChannels.map(channel => channel.id))
      const next = new Set(base)
      if (next.has(channelId)) next.delete(channelId)
      else next.add(channelId)
      return next
    })
  }

  // Esc 分层回退：内层临时态先关（转发面板/渠道表单/签发表单），窄屏详情态先回列表；
  // 其余交给 Radix 关闭弹窗。密钥明文不参与回退——「仅此一次」，误触 Esc 不应吞掉它。
  const handleDialogEscape = (event: KeyboardEvent) => {
    if (nav === 'messages' && forwardOpen) {
      event.preventDefault()
      setForwardOpen(false)
      setForwardChannelIds(new Set())
      return
    }
    if (nav === 'config') {
      // 渠道表单/类型网格先于签发表单回退（与拆分前的收起顺序一致）
      if (outboundSectionRef.current?.collapseTransient()) {
        event.preventDefault()
        return
      }
      if (inboundSectionRef.current?.collapseTransient()) {
        event.preventDefault()
        return
      }
    }
    if (nav === 'messages' && selectedId && window.matchMedia('(max-width: 767px)').matches) {
      event.preventDefault()
      setSelectedId(null)
    }
  }

  // 分区导航 tablist 的方向键漫游（WAI-ARIA tabs 模式；窄屏横排/宽屏竖排都支持四向 + Home/End）
  const navItemRefs = useRef<(HTMLButtonElement | null)[]>([])
  const handleNavKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>, index: number) => {
    let next: number | null = null
    if (event.key === 'ArrowDown' || event.key === 'ArrowRight') next = (index + 1) % NAV_ITEMS.length
    else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') next = (index - 1 + NAV_ITEMS.length) % NAV_ITEMS.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = NAV_ITEMS.length - 1
    if (next === null) return
    event.preventDefault()
    navItemRefs.current[next]?.focus()
    setNav(NAV_ITEMS[next].key)
  }

  if (!open) return null

  return (
    <TooltipProvider delayDuration={300}>
      <DialogShell
        title="消息通知"
        description="消息中转站：多入口接收，站内查看处置，按配置的渠道自动或手动转发"
        size="xwide"
        icon={<Bell size={18} />}
        onClose={onClose}
        onEscapeKeyDown={handleDialogEscape}
        // 误触遮罩不应关弹窗：发送分区的草稿与列表滚动位置会随关闭一并丢失。
        // 只挡遮罩——Esc 分层回退与右上关闭按钮保持可用（工作台型弹窗惯例，
        // 不用 DialogContent 的 dismissible，它会连 Esc 一起禁掉）
        onInteractOutside={event => event.preventDefault()}
        contentClassName="h-[min(88dvh,52rem)]"
      >
        <div className="grid min-h-0 flex-1 grid-cols-1 md:grid-cols-[9.5rem_minmax(0,1fr)]">
          {/* 左侧分区导航：宽屏竖排；窄屏横排单行可横滚（避免纵向按钮列吞掉首屏） */}
          <nav
            role="tablist"
            aria-label="消息通知分区"
            className="scrollbar-none flex min-h-0 shrink-0 flex-row gap-1 overflow-x-auto border-b border-[var(--color-border)] p-2 md:flex-col md:overflow-x-visible md:border-b-0 md:border-r"
          >
            {NAV_ITEMS.map(({ key, label, icon: Icon }, index) => {
              const active = nav === key
              return (
                <button
                  key={key}
                  type="button"
                  role="tab"
                  id={`notifications-tab-${key}`}
                  aria-selected={active}
                  aria-controls={`notifications-panel-${key}`}
                  tabIndex={active ? 0 : -1}
                  onClick={() => setNav(key)}
                  onKeyDown={event => handleNavKeyDown(event, index)}
                  ref={element => { navItemRefs.current[index] = element }}
                  data-notifications-nav={key}
                  className={`flex min-h-10 shrink-0 items-center gap-1.5 rounded-lg px-2.5 text-left text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                    active
                      ? 'bg-brand-soft font-medium text-brand-ink'
                      : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)]'
                  }`}
                >
                  <Icon size={15} className="shrink-0" aria-hidden="true" />
                  <span className="min-w-0 flex-1 whitespace-nowrap md:truncate">{label}</span>
                  {key === 'messages' && unreadCount > 0 && (
                    <span className="flex h-[18px] min-w-[18px] shrink-0 items-center justify-center rounded-full bg-[var(--color-danger)] px-1 text-xs font-semibold tabular-nums text-white">
                      {unreadCount > 99 ? '99+' : unreadCount}
                    </span>
                  )}
                </button>
              )
            })}
          </nav>

          {nav === 'messages' && (
            <div
              role="tabpanel"
              id="notifications-panel-messages"
              aria-labelledby="notifications-tab-messages"
              className="flex min-h-0 flex-col"
            >
              {/* —— 列表 / 详情两块；窄屏由选中态驱动单栏切换 —— */}
              <div className="flex min-h-0 flex-1">
                  <div
                    className={`${selectedId ? 'hidden md:flex' : 'flex'} w-full shrink-0 flex-col border-r border-[var(--color-border)] md:w-[22rem]`}
                    data-notifications-list
                  >
                    {/* 列表列头：筛选只作用于本列（控件贴作用对象）+ 图标化「全部已读」；
                        h-10 与右栏详情头同行高钉死——两列分割线必须落在同一水平线 */}
                    <div className="scrollbar-none flex h-10 shrink-0 items-center gap-0.5 overflow-x-auto border-b border-[var(--color-border)] px-1.5">
                      {NOTIFICATION_TABS.map(({ key, label }) => {
                        const active = tab === key
                        return (
                          <button
                            key={key}
                            type="button"
                            onClick={() => { setTab(key); setSelectedId(null) }}
                            aria-pressed={active}
                            data-notifications-tab={key}
                            className={`flex shrink-0 items-center gap-1 whitespace-nowrap rounded-md px-1.5 py-1 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                              active
                                ? 'bg-brand-soft font-medium text-brand-ink'
                                : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)]'
                            }`}
                          >
                            {label}
                          </button>
                        )
                      })}
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <button
                            type="button"
                            onClick={() => readAllMutation.mutate()}
                            disabled={unreadCount === 0 || readAllMutation.isPending}
                            aria-label="全部已读"
                            // 禁用态不派发 pointer 事件，Radix tooltip 悬停不出来；
                            // 原生 title 走浏览器 hit-testing 仍可显示，补一句禁用原因
                            title={unreadCount === 0 ? '暂无未读消息' : undefined}
                            className="ml-auto flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
                            data-notifications-read-all
                          >
                            <MailOpen size={15} />
                          </button>
                        </TooltipTrigger>
                        <TooltipContent>将全部未读消息标为已读</TooltipContent>
                      </Tooltip>
                    </div>
                    <div className="scrollbar-none min-h-0 flex-1 overflow-y-auto p-2">
                      {list.isLoading && (
                        <div className="flex items-center justify-center gap-2 py-8 text-xs text-[var(--color-text-tertiary)]">
                          <Loader2 size={12} className="animate-spin" /> 加载中…
                        </div>
                      )}
                      {list.isError && (
                        <div role="alert" className="flex flex-col items-center gap-2 px-3 py-8 text-center">
                          <p className="text-xs leading-5 text-[var(--color-text-tertiary)]">消息列表加载失败：{errorText(list.error)}</p>
                          <button
                            type="button"
                            onClick={() => void list.refetch()}
                            className="rounded-md border border-[var(--color-border)] px-2.5 py-1 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          >
                            重试
                          </button>
                        </div>
                      )}
                      {!list.isLoading && !list.isError && !list.isPlaceholderData && items.length === 0 && (() => {
                        const empty = listEmptyCopy(tab)
                        return (
                          <div className="flex flex-col items-center gap-1.5 px-4 py-10 text-center" data-notifications-list-empty={tab}>
                            <span className="flex h-10 w-10 items-center justify-center rounded-full bg-[var(--color-bg-hover)] text-[var(--color-text-tertiary)]" aria-hidden="true">
                              {tab === 'starred' ? <Star size={18} /> : tab === 'archived' ? <Archive size={18} /> : <Inbox size={18} />}
                            </span>
                            <p className="mt-1 text-xs font-medium text-[var(--color-text-secondary)]">{empty.title}</p>
                            <p className="max-w-60 text-xs leading-5 text-[var(--color-text-tertiary)]">{empty.description}</p>
                            {empty.composeCta ? (
                              <button
                                type="button"
                                onClick={() => setNav('compose')}
                                className="mt-2 rounded-md border border-[var(--color-border)] px-2.5 py-1 text-xs text-brand-ink transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                                data-notifications-empty-compose
                              >
                                去发送一条
                              </button>
                            ) : null}
                          </div>
                        )
                      })()}
                      <div className="space-y-1">
                        {items.map(item => {
                          const active = item.id === selectedId
                          const priority = priorityStyle(item.priority)
                          const previewText = item.bodyPreview ? stripMarkdownPreview(item.bodyPreview) : ''
                          return (
                            <button
                              key={item.id}
                              type="button"
                              onClick={() => { if (!list.isPlaceholderData) openMessage(item.id, item.isRead) }}
                              data-notifications-item={item.id}
                              data-notifications-priority={item.priority}
                              // 边框色必须各分支自带（含透明档）：与基线类并存的 border-transparent
                              // 在产物样式表中排在任意值 border-[…] 之后，会压掉未读描边（线上实测踩坑）
                              className={`relative w-full rounded-lg border px-2.5 py-2 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                                active
                                  ? 'border-transparent bg-brand-soft'
                                  : item.isRead
                                    ? 'border-transparent hover:bg-[var(--color-bg-hover)]'
                                    // 品牌软底令牌不带 alpha 通道（tailwind 的 /N 修饰符对裸 var 不生效），
                                    // 半透明档用 color-mix 现场合成；未读底 35% / 悬停 60% / 选中 100%
                                    // 三档递进——悬停与选中必须可区分（窄屏单栏下 hover≠选中）；
                                    // 未读再加 70% 描边强化辨识（统一 1px 边框占位，已读↔未读
                                    // 翻转不引起内容跳动；选中态自身已足够醒目，不叠描边）
                                    : 'border-[color-mix(in_srgb,var(--color-accent-soft)_70%,transparent)] bg-[color-mix(in_srgb,var(--color-accent-soft)_35%,transparent)] hover:bg-[color-mix(in_srgb,var(--color-accent-soft)_60%,transparent)]'
                              }`}
                            >
                              {/* urgent 左缘色条：优先级不止靠 6px 圆点的颜色编码（色弱不可辨），有无色条是可扫视的二值信号 */}
                              {item.priority === 'urgent' && (
                                <span className="absolute inset-y-1.5 left-0 w-[2px] rounded-full bg-[var(--color-danger)]" aria-hidden="true" />
                              )}
                              <span className="flex items-center gap-1.5">
                                <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${priority.dotClass}`} aria-hidden />
                                <span className={`min-w-0 flex-1 truncate text-sm ${item.isRead ? 'text-[var(--color-text-primary)]' : 'font-semibold text-[var(--color-text-primary)]'}`}>
                                  {item.title}
                                </span>
                                {item.isStarred && <Star size={12} className="shrink-0 fill-current text-[var(--color-warning)]" aria-label="已标记" />}
                                {!!item.attachmentCount && item.attachmentCount > 0 && (
                                  <span className="flex shrink-0 items-center gap-0.5 text-xs tabular-nums text-[var(--color-text-tertiary)]">
                                    <Paperclip size={12} />{item.attachmentCount}
                                  </span>
                                )}
                              </span>
                              {previewText && (
                                <span data-notifications-preview className={`mt-0.5 block truncate text-xs ${item.isRead ? 'text-[var(--color-text-tertiary)]' : 'text-[var(--color-text-secondary)]'}`}>
                                  {previewText}
                                </span>
                              )}
                              <span className="mt-1 block truncate text-xs text-[var(--color-text-tertiary)]">
                                {item.sourceSystem} · {formatDateTime(item.createdAt)}
                              </span>
                            </button>
                          )
                        })}
                      </div>
                      {list.hasNextPage && (
                        <button
                          type="button"
                          onClick={() => void list.fetchNextPage()}
                          disabled={list.isFetchingNextPage}
                          className="mt-1 flex w-full items-center justify-center gap-1 rounded-lg px-2 py-1.5 text-xs text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-brand-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          data-notifications-load-more
                        >
                          {list.isFetchingNextPage && <Loader2 size={12} className="animate-spin" />}
                          加载更多
                        </button>
                      )}
                    </div>
                  </div>

                  <div
                    // 阅读栏宽度 = 列表两倍（22rem×2=44rem）：弹窗保持 xwide，富余空间
                    // 由 auto 边距对称留白（窄于 44rem 的视口不封顶、行为不变）
                    className={`${selectedId ? 'flex' : 'hidden md:flex'} min-w-0 flex-1 flex-col md:max-w-[44rem] md:mx-auto`}
                    data-notifications-detail
                  >
                    {/* 窄屏单栏：详情视图统一返回条（成功/加载/错误三分支都可回列表） */}
                    {selectedId && (
                      <div className="flex shrink-0 border-b border-[var(--color-border)] md:hidden">
                        <button
                          type="button"
                          onClick={() => setSelectedId(null)}
                          aria-label="返回消息列表"
                          data-notifications-back-detail
                          className="flex items-center gap-1 px-3 py-2 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        >
                          <ArrowLeft size={12} /> 返回消息列表
                        </button>
                      </div>
                    )}
                    {selectedId ? (
                      detail.isError ? (
                        <div role="alert" className="flex flex-1 flex-col items-center justify-center gap-2 p-6 text-center">
                          <p className="text-xs leading-5 text-[var(--color-text-tertiary)]">消息详情加载失败：{errorText(detail.error)}</p>
                          <button
                            type="button"
                            onClick={() => void detail.refetch()}
                            className="rounded-md border border-[var(--color-border)] px-2.5 py-1 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          >
                            重试
                          </button>
                        </div>
                      ) : !selected ? (
                        <div className="flex flex-1 items-center justify-center gap-2 text-xs text-[var(--color-text-tertiary)]">
                          <Loader2 size={12} className="animate-spin" /> 加载中…
                        </div>
                      ) : (
                        <>
                          {/* 详情头：≥lg 单行收纳（优先级章 + 标题 + 来源/时间 + 操作图标同排），
                              h-10 与左栏过滤行钉同高——两列分割线落在同一水平线；
                              <lg 右栏窄（md 带仅 ~233px），标题整行换到第二行截断，避免被图标挤到不可读 */}
                          <div className="flex min-h-10 shrink-0 flex-wrap items-center gap-x-2 gap-y-0.5 border-b border-[var(--color-border)] px-3 py-1 lg:h-10 lg:flex-nowrap lg:py-0">
                            <span className={`shrink-0 rounded px-1.5 py-0.5 text-xs font-medium ${priorityStyle(selected.priority).chipClass}`}>
                              {priorityStyle(selected.priority).label}
                            </span>
                            <h3 className="order-last mb-0.5 w-full truncate text-sm font-semibold text-[var(--color-text-primary)] lg:order-none lg:mb-0 lg:min-w-0 lg:w-auto lg:flex-1" title={selected.title}>
                              {selected.title}
                            </h3>
                            <span className="hidden shrink-0 text-xs text-[var(--color-text-tertiary)] lg:inline" data-notifications-detail-meta>
                              {sourceTypeLabel(selected.sourceType)} · {selected.sourceSystem} · {formatDateTime(selected.createdAt)}
                            </span>
                            <div className="ml-auto flex shrink-0 items-center gap-0.5 lg:ml-0">
                                {/* 已归档 = 处置终态：只留恢复/转发；标记与未读是收件箱态操作 */}
                                {!selected.isArchived && (
                                  <Tooltip>
                                    <TooltipTrigger asChild>
                                      <button
                                        type="button"
                                        onClick={() => stateMutation.mutate({ id: selected.id, fields: { isStarred: !selected.isStarred } })}
                                        aria-label={selected.isStarred ? '取消标记' : '标记为重要'}
                                        className={`${iconButtonClass} ${selected.isStarred ? 'text-[var(--color-warning)]' : ''}`}
                                        data-notifications-star
                                      >
                                        <Star size={15} className={selected.isStarred ? 'fill-current' : ''} />
                                      </button>
                                    </TooltipTrigger>
                                    <TooltipContent align="end">{selected.isStarred ? '取消标记' : '标记为重要'}</TooltipContent>
                                  </Tooltip>
                                )}
                                <Tooltip>
                                  <TooltipTrigger asChild>
                                    <button
                                      type="button"
                                      onClick={() => stateMutation.mutate({ id: selected.id, fields: { isArchived: !selected.isArchived } })}
                                      aria-label={selected.isArchived ? '恢复消息' : '归档消息'}
                                      className={iconButtonClass}
                                      data-notifications-archive
                                    >
                                      {selected.isArchived ? <ArchiveRestore size={15} /> : <Archive size={15} />}
                                    </button>
                                  </TooltipTrigger>
                                  <TooltipContent align="end">{selected.isArchived ? '恢复消息' : '归档消息'}</TooltipContent>
                                </Tooltip>
                                <Tooltip>
                                  <TooltipTrigger asChild>
                                    <button
                                      type="button"
                                      onClick={() => { setForwardOpen(value => !value); setForwardChannelIds(new Set()) }}
                                      aria-label="转发到外部渠道"
                                      className={iconButtonClass}
                                      data-notifications-forward
                                    >
                                      <Forward size={15} />
                                    </button>
                                  </TooltipTrigger>
                                  <TooltipContent align="end">转发到外部渠道</TooltipContent>
                                </Tooltip>
                                {selected.isRead && !selected.isArchived && (
                                  <Tooltip>
                                    <TooltipTrigger asChild>
                                      <button
                                        type="button"
                                        onClick={() => stateMutation.mutate({ id: selected.id, fields: { isRead: false } })}
                                        aria-label="标为未读"
                                        className={iconButtonClass}
                                        data-notifications-unread
                                      >
                                        <Mail size={15} />
                                      </button>
                                    </TooltipTrigger>
                                    <TooltipContent align="end">标为未读</TooltipContent>
                                  </Tooltip>
                                )}
                              </div>
                          </div>
                          {forwardOpen && (
                            <div className="shrink-0 border-b border-[var(--color-border)] bg-[var(--color-bg-base)] p-3" data-notifications-forward-panel>
                              <p className="text-xs font-medium text-[var(--color-text-secondary)]">转发到外部渠道</p>
                              <div className="mt-2 flex flex-col gap-1">
                                {enabledChannels.length === 0 ? (
                                  <p className="text-xs leading-5 text-[var(--color-text-tertiary)]">
                                    没有启用的渠道，请先到「渠道配置」新建。
                                  </p>
                                ) : enabledChannels.map(channel => (
                                  <label key={channel.id} className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-xs text-[var(--color-text-primary)] transition-colors hover:bg-[var(--color-bg-hover)]">
                                    <input
                                      type="checkbox"
                                      checked={forwardChannelIds.has(channel.id)}
                                      onChange={event => setForwardChannelIds(current => {
                                        const next = new Set(current)
                                        if (event.target.checked) next.add(channel.id)
                                        else next.delete(channel.id)
                                        return next
                                      })}
                                      className="h-3.5 w-3.5 accent-[var(--color-nav-bg)]"
                                      data-notifications-forward-channel={channel.id}
                                    />
                                    <span className="min-w-0 flex-1 truncate">{channel.name}<span className="ml-1.5 text-xs text-[var(--color-text-tertiary)]">{channel.templateName}</span></span>
                                  </label>
                                ))}
                              </div>
                              <div className="mt-2 flex justify-end gap-2">
                                <button type="button" onClick={() => setForwardOpen(false)} className="rounded-lg border border-[var(--color-border)] px-3 py-1.5 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                                  取消
                                </button>
                                <Button
                                  size="sm"
                                  onClick={() => forwardMutation.mutate({ messageId: selected.id, channelIds: [...forwardChannelIds] })}
                                  disabled={forwardChannelIds.size === 0}
                                  loading={forwardMutation.isPending}
                                  className="rounded-lg"
                                  data-notifications-forward-confirm
                                >
                                  <Forward size={12} /> {forwardButtonLabel(forwardChannelIds.size)}
                                </Button>
                              </div>
                            </div>
                          )}
                          <div className="min-h-0 flex-1 overflow-y-auto p-4">
                            <NotificationMarkdown content={selected.body || ''} />
                            {!!selected.attachments?.length && (
                              <div className="mt-5 border-t border-[var(--color-border)] pt-3">
                                <p className="mb-2 flex items-center gap-1.5 text-xs font-medium text-[var(--color-text-secondary)]">
                                  <Paperclip size={12} /> 附件（{selected.attachments.length}）
                                </p>
                                <ul className="space-y-1">
                                  {selected.attachments.map(attachment => (
                                    <li key={attachment.id}>
                                      <button
                                        type="button"
                                        onClick={() => void handleDownload(selected.id, attachment)}
                                        className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                                        data-notifications-attachment={attachment.id}
                                      >
                                        <FileText size={12} className="shrink-0 text-[var(--color-text-tertiary)]" />
                                        <span className="min-w-0 flex-1 truncate text-sm text-[var(--color-text-primary)]">
                                          {attachment.filename}
                                        </span>
                                        <span className="shrink-0 text-xs tabular-nums text-[var(--color-text-tertiary)]">
                                          {formatFileSize(attachment.fileSize)}
                                        </span>
                                      </button>
                                    </li>
                                  ))}
                                </ul>
                              </div>
                            )}
                          </div>
                        </>
                      )
                    ) : (
                      <div className="flex flex-1 items-center justify-center p-6">
                        <p className="max-w-56 text-center text-xs leading-5 text-[var(--color-text-tertiary)]">
                          选择左侧消息查看详情；标记与归档只影响你自己的视图。
                        </p>
                      </div>
                    )}
                  </div>
                </div>
            </div>
          )}

          {nav === 'compose' && (
            <div
              role="tabpanel"
              id="notifications-panel-compose"
              aria-labelledby="notifications-tab-compose"
              className="scrollbar-none flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-4 sm:p-5"
              data-notifications-compose-panel
            >
              <div>
                <h3 className={sectionHeadingClass}>发送消息</h3>
                <p className="mt-1 text-xs leading-5 text-[var(--color-text-tertiary)]">
                  消息进入站内列表，并立即转发到下方所选的启用渠道。
                </p>
              </div>

              {/* 投递去向：启用渠道 chips（默认全选，可点选增减） */}
              <div className="rounded-lg border border-[var(--color-border)] p-3" data-notifications-compose-destinations>
                <p className="text-xs font-medium text-[var(--color-text-secondary)]">
                  投递渠道
                  <span className="ml-1.5 font-normal text-xs text-[var(--color-text-tertiary)]">
                    默认全选，点击可增减
                  </span>
                </p>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {channels.isLoading ? (
                    <span className="flex items-center gap-1.5 text-xs text-[var(--color-text-tertiary)]">
                      <Loader2 size={12} className="animate-spin" /> 渠道加载中…
                    </span>
                  ) : channels.isError ? (
                    <span role="alert" className="flex flex-wrap items-center gap-2 text-xs leading-5 text-[var(--color-text-tertiary)]">
                      渠道列表加载失败：{errorText(channels.error)}
                      <button
                        type="button"
                        onClick={() => void channels.refetch()}
                        className="rounded-md border border-[var(--color-border)] px-2 py-0.5 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        重试
                      </button>
                    </span>
                  ) : enabledChannels.length === 0 ? (
                    <span className="text-xs leading-5 text-[var(--color-text-tertiary)]">
                      暂无启用的渠道，消息将仅进入站内列表；可到「渠道配置」新建。
                    </span>
                  ) : (
                    enabledChannels.map(channel => {
                      const checked = composeSelection(channel.id)
                      return (
                        <button
                          key={channel.id}
                          type="button"
                          role="switch"
                          aria-checked={checked}
                          onClick={() => toggleComposeChannel(channel.id)}
                          data-notifications-compose-channel={channel.id}
                          className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                            checked
                              ? 'border-transparent bg-brand-soft font-medium text-brand-ink'
                              : 'border-[var(--color-border)] text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)]'
                          }`}
                        >
                          {checked && <Send size={12} className="shrink-0" />}
                          <span className="max-w-40 truncate">{channel.name}</span>
                          <span className="text-xs font-normal opacity-70">{channel.templateName}</span>
                        </button>
                      )
                    })
                  )}
                </div>
              </div>

              <div className="flex flex-col gap-4 sm:flex-row">
                <label className="flex flex-1 flex-col gap-1 text-xs text-[var(--color-text-secondary)]">
                  标题
                  <input
                    value={composeTitle}
                    onChange={event => setComposeTitle(event.target.value)}
                    maxLength={300}
                    placeholder="消息标题"
                    className={inputClass}
                    data-notifications-compose-title
                  />
                </label>
                <div className="flex w-full flex-col gap-1 text-xs text-[var(--color-text-secondary)] sm:w-40">
                  优先级
                  <Select
                    value={composePriority}
                    onValueChange={value => setComposePriority(value as NotificationPriority)}
                  >
                    <SelectTrigger className="h-9 w-full text-sm" data-notifications-compose-priority>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="urgent">紧急</SelectItem>
                      <SelectItem value="high">高优</SelectItem>
                      <SelectItem value="normal">普通</SelectItem>
                      <SelectItem value="low">低优</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>
              <label className="flex flex-1 flex-col gap-1 text-xs text-[var(--color-text-secondary)]">
                正文（Markdown）
                <textarea
                  value={composeBody}
                  onChange={event => setComposeBody(event.target.value)}
                  placeholder="支持 Markdown：图片、表格、代码块…"
                  className={`${inputClass} min-h-48 flex-1 resize-none font-mono text-xs leading-6`}
                  data-notifications-compose-body
                />
              </label>
              <div className="flex items-center justify-between gap-2">
                <p className="text-xs leading-5 text-[var(--color-text-tertiary)]">
                  {enabledChannels.length > 0 && !channels.isError && `将投递到 ${
                    composeChannelIds
                      ? [...composeChannelIds].filter(id => enabledChannels.some(channel => channel.id === id)).length
                      : enabledChannels.length
                  } 个渠道`}
                </p>
                <Button
                  size="sm"
                  onClick={handleCompose}
                  loading={createMutation.isPending}
                  className="rounded-lg"
                  data-notifications-compose-send
                >
                  <Send size={12} /> 发送
                </Button>
              </div>
            </div>
          )}

          {nav === 'config' && (
            <div
              role="tabpanel"
              id="notifications-panel-config"
              aria-labelledby="notifications-tab-config"
              className="scrollbar-none min-h-0 overflow-y-auto overscroll-contain p-4 sm:p-5"
            >
              <NotificationInboundSection ref={inboundSectionRef} open={open} invalidateLists={invalidateLists} />
              <hr className="my-6 border-[var(--color-border)]" aria-hidden />
              <NotificationOutboundSection ref={outboundSectionRef} open={open} channels={channels} />
            </div>
          )}
          {nav === 'stats' && (
            <div
              role="tabpanel"
              id="notifications-panel-stats"
              aria-labelledby="notifications-tab-stats"
              className="scrollbar-none min-h-0 overflow-y-auto overscroll-contain p-4 sm:p-5"
              data-notifications-stats-panel
            >
              {stats.isLoading ? (
                <div className="flex items-center justify-center gap-2 py-16 text-xs text-[var(--color-text-tertiary)]">
                  <Loader2 size={12} className="animate-spin" /> 统计加载中…
                </div>
              ) : stats.isError ? (
                <div role="alert" className="flex flex-col items-center gap-2 py-16 text-center">
                  <p className="text-xs leading-5 text-[var(--color-text-tertiary)]">统计加载失败：{errorText(stats.error)}</p>
                  <button
                    type="button"
                    onClick={() => void stats.refetch()}
                    className="rounded-md border border-[var(--color-border)] px-2.5 py-1 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    重试
                  </button>
                </div>
              ) : !stats.data ? null : (
                <>
                  {/* —— 概览卡 —— */}
                  <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-4" data-notifications-stats-overview>
                    {[
                      { label: '消息总量', value: String(stats.data.overview.totalCount) },
                      { label: '未读', value: String(stats.data.overview.unreadCount) },
                      { label: '今日入站', value: String(stats.data.overview.todayInbound) },
                      {
                        label: '转发成功率',
                        value: stats.data.overview.forwardSuccessRate === null
                          ? '—'
                          : `${Math.round(stats.data.overview.forwardSuccessRate * 1000) / 10}%`,
                        // 渠道卡的「投递失败」含测试直发，与这里口径不同——写明避免两边数字互相打架
                        note: '仅计正式投递单，不含渠道测试直发',
                      },
                    ].map(card => (
                      <div key={card.label} className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-base)] p-3" data-notifications-stats-card={card.label}>
                        <p className="text-xs text-[var(--color-text-tertiary)]">{card.label}</p>
                        <p className="mt-1 text-lg font-semibold tabular-nums leading-7 text-[var(--color-text-primary)]">{card.value}</p>
                        {card.note && <p className="mt-0.5 text-xs leading-4 text-[var(--color-text-tertiary)]">{card.note}</p>}
                      </div>
                    ))}
                  </div>

                  {/* —— 入站趋势（ECharts 平台标准；svg 渲染器保证容器内渲染出 <svg>） —— */}
                  <section className="mt-4 rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-base)] p-4" aria-label="入站趋势" data-notifications-stats-trend>
                    <div className="flex items-center justify-between gap-2">
                      <h3 className="text-sm font-semibold text-[var(--color-text-primary)]">入站趋势</h3>
                      <div className="flex items-center gap-1" role="group" aria-label="统计时间窗">
                        {([7, 30] as const).map(d => (
                          <button
                            key={d}
                            type="button"
                            onClick={() => setStatsDays(d)}
                            aria-pressed={statsDays === d}
                            data-notifications-stats-range={d}
                            className={`rounded-md px-2 py-1 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                              statsDays === d
                                ? 'bg-brand-soft font-medium text-brand-ink'
                                : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)]'
                            }`}
                          >
                            近 {d} 天
                          </button>
                        ))}
                      </div>
                    </div>
                    <div className="mt-3 h-44" data-notifications-stats-trend-chart>
                      {trendOption && (
                        <ReactECharts
                          option={trendOption}
                          style={{ width: '100%', height: '100%' }}
                          opts={{ renderer: 'svg' }}
                          notMerge
                        />
                      )}
                    </div>
                  </section>

                  <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-2">
                    {/* —— 来源分布 —— */}
                    <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-base)] p-4" aria-label="来源分布" data-notifications-stats-sources>
                      <h3 className="text-sm font-semibold text-[var(--color-text-primary)]">来源分布</h3>
                      {stats.data.sources.length === 0 ? (
                        <p className="py-8 text-center text-xs text-[var(--color-text-tertiary)]">还没有消息来源。</p>
                      ) : (
                        <div className="mt-3 space-y-2" data-notifications-stats-sources-list>
                          {(() => {
                            const max = Math.max(...stats.data.sources.map(s => s.count), 1)
                            return stats.data.sources.map(item => (
                              <div key={item.source} className="flex items-center gap-2" data-notifications-stats-source={item.source}>
                                <span className="w-28 shrink-0 truncate text-xs text-[var(--color-text-secondary)]" title={item.source}>{item.source}</span>
                                <span className="h-4 min-w-1 flex-1 overflow-hidden rounded bg-[var(--color-bg-hover)]">
                                  <span className="block h-full rounded bg-brand-soft" style={{ width: `${Math.max((item.count / max) * 100, 2)}%` }} />
                                </span>
                                <span className="w-8 shrink-0 text-right text-xs tabular-nums text-[var(--color-text-tertiary)]">{item.count}</span>
                              </div>
                            ))
                          })()}
                        </div>
                      )}
                    </section>

                    {/* —— 渠道投递结果 —— */}
                    <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-base)] p-4" aria-label="渠道投递结果" data-notifications-stats-channels>
                      <h3 className="text-sm font-semibold text-[var(--color-text-primary)]">渠道投递结果</h3>
                      {stats.data.channels.length === 0 ? (
                        <p className="py-8 text-center text-xs text-[var(--color-text-tertiary)]">还没有投递记录。到「渠道配置」新建渠道后开始转发。</p>
                      ) : (
                        <div className="mt-3 space-y-2.5" data-notifications-stats-channels-list>
                          {stats.data.channels.map(ch => {
                            const total = ch.sent + ch.failed + ch.pending
                            return (
                              <div key={ch.channelId} data-notifications-stats-channel={ch.channelId}>
                                <div className="flex items-center justify-between gap-2 text-xs">
                                  <span className="min-w-0 flex-1 truncate text-[var(--color-text-secondary)]" title={ch.name}>{ch.name}</span>
                                  <span className="shrink-0 tabular-nums text-[var(--color-text-tertiary)]">
                                    成功 <span className="text-[var(--color-success)]">{ch.sent}</span>
                                    {' · '}失败 <span className="text-[var(--color-danger)]">{ch.failed}</span>
                                    {ch.pending > 0 ? ` · 待发 ${ch.pending}` : ''} · 共 {total}
                                  </span>
                                </div>
                                <div className="mt-1 flex h-2 w-full overflow-hidden rounded bg-[var(--color-bg-hover)]">
                                  {total > 0 && (
                                    <>
                                      <span className="h-full bg-[var(--color-success)]" style={{ width: `${(ch.sent / total) * 100}%` }} />
                                      <span className="h-full bg-[var(--color-danger)]" style={{ width: `${(ch.failed / total) * 100}%` }} />
                                      <span className="h-full bg-[var(--color-border)]" style={{ width: `${(ch.pending / total) * 100}%` }} />
                                    </>
                                  )}
                                </div>
                              </div>
                            )
                          })}
                        </div>
                      )}
                    </section>
                  </div>
                </>
              )}
            </div>
          )}
        </div>
      </DialogShell>

    </TooltipProvider>
  )
}
