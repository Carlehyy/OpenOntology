import { useEffect, useMemo, useRef, useState } from 'react'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  Archive, ArchiveRestore, ArrowLeft, BarChart3, Bell, ChevronDown, Copy, FileText, Forward, KeyRound, Loader2,
  Mail, MailOpen, MessageSquare, Paperclip, Pencil, Plug, Plus, Send, Star, Trash2, Users, Webhook, Zap,
} from 'lucide-react'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { toast } from 'sonner'

import {
  NOTIFICATION_TABS,
  notificationsApi,
  type NotificationChannel,
  type NotificationIngestKey,
  type NotificationAttachment,
  type NotificationMessage,
  type NotificationPriority,
  type NotificationTab,
} from '@/api/notifications'
import { absoluteFileUrl } from '@/api/fileAssets'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { formatDateTime } from '@/utils/datetime'
import { writeTextToClipboard } from '@/utils/clipboard'
import { useAuthStore } from '@/stores/authStore'

import { DialogShell } from './AssistantConfiguration'
import { assistantMarkdownComponents } from './AssistantConversation'
import { errorText } from './assistantPanelUtils'
import NotificationMedia from './NotificationMedia'
import { formatFileSize, priorityStyle, sourceTypeLabel } from './notificationsFormat'

/**
 * 布局参考个人资料弹窗：左侧分区导航（消息列表 / 发送消息 / 渠道配置）+ 右侧内容。
 * - 消息列表：筛选 tab（未读默认 → 已标记 → 已归档 → 全部=真全量）+ 列表/详情两栏；
 *   窄屏由选中态驱动单栏切换。
 * - 发送消息：独立分区——向所选启用渠道发布通知（默认全选），消息同时进入站内列表。
 * - 渠道配置：入口（投递密钥 + 接口调用示例）与出口（发件邮箱 + 转发渠道）两个区块，
 *   每个密钥/渠道旁内嵌连通测试。
 */
type NavSection = 'messages' | 'compose' | 'config' | 'stats'

/** 渠道类型的图形语义（图标盒统一品牌软底，类型间靠图形区分而非颜色） */
const TEMPLATE_ICONS: Record<string, typeof Bell> = {
  dingtalk: Bell,
  feishu: MessageSquare,
  wecom: Users,
  tgram: Send,
  email: Mail,
  webhook: Webhook,
  custom: Plug,
}

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

const inputClass = 'w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 py-2 text-sm text-[var(--color-text-primary)] placeholder:text-[var(--color-text-tertiary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring'

const iconButtonClass = 'flex h-8 w-8 items-center justify-center rounded-lg text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60'

const sectionHeadingClass = 'text-sm font-semibold text-[var(--color-text-primary)]'

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
  // 同步 click 已启动下载；revoke 延后一拍避免个别浏览器截断流
  setTimeout(() => URL.revokeObjectURL(objectUrl), 0)
}

function ingestEndpoint(): string {
  try {
    return absoluteFileUrl('/api/v2/notifications/ingest')
  } catch {
    return '/api/v2/notifications/ingest'
  }
}

function ingestCurlSample(): string {
  return [
    `curl -X POST ${ingestEndpoint()} \\`,
    '  -H "X-API-Key: <你的密钥>" \\',
    '  -H "Content-Type: application/json" \\',
    `  -d '{"eventId":"order-123","title":"订单异常","body":"# 详情\\n金额超限","priority":"high"}'`,
  ].join('\n')
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
  const [tab, setTab] = useState<NotificationTab>('unread')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [deleting, setDeleting] = useState<NotificationMessage | null>(null)
  const [keyName, setKeyName] = useState('')
  const [keyScope, setKeyScope] = useState('')
  const [mintedKey, setMintedKey] = useState<NotificationIngestKey | null>(null)
  const [revokingKey, setRevokingKey] = useState<NotificationIngestKey | null>(null)
  const [channelName, setChannelName] = useState('')
  const [channelNote, setChannelNote] = useState('')
  const [templateId, setTemplateId] = useState('dingtalk')
  const [templateParams, setTemplateParams] = useState<Record<string, string>>({})
  const [editingChannel, setEditingChannel] = useState<NotificationChannel | null>(null)
  // 渠道表单阶段：idle=列表；pick=模板网格选择；form=字段表单（编辑直达）
  const [channelFormStage, setChannelFormStage] = useState<'idle' | 'pick' | 'form'>('idle')
  const [deletingChannel, setDeletingChannel] = useState<NotificationChannel | null>(null)
  const [testingChannelId, setTestingChannelId] = useState<string | null>(null)
  const [testingKeyId, setTestingKeyId] = useState<string | null>(null)
  // 手动转发面板
  const [forwardOpen, setForwardOpen] = useState(false)
  const [forwardChannelIds, setForwardChannelIds] = useState<Set<string>>(new Set())
  // 平台发件设置（SMTP）
  const [smtpHost, setSmtpHost] = useState('')
  const [smtpPort, setSmtpPort] = useState('465')
  const [smtpUsername, setSmtpUsername] = useState('')
  const [smtpPassword, setSmtpPassword] = useState('')
  const [smtpSender, setSmtpSender] = useState('')
  const [smtpTestTo, setSmtpTestTo] = useState('')

  // 手动发送表单（独立分区）
  const [composeTitle, setComposeTitle] = useState('')
  const [composeBody, setComposeBody] = useState('')
  const [composePriority, setComposePriority] = useState<NotificationPriority>('normal')
  // 消息统计：时间窗（7/30 天，默认 30）
  const [statsDays, setStatsDays] = useState<7 | 30>(30)
  // 投递去向：null = 默认全选（跟随启用渠道）；Set = 用户改选后的显式集合
  const [composeChannelIds, setComposeChannelIds] = useState<Set<string> | null>(null)

  // 只失效列表与徽章；detail 缓存由 setQueryData 精确写入——
  // 若前缀失效命中 detail，会触发「GET 详情即自动已读」把刚标回的未读打回，
  // 删除场景还会对已删消息发起 404 请求
  const invalidateLists = () => {
    void queryClient.invalidateQueries({ queryKey: ['notifications', 'page'] })
    void queryClient.invalidateQueries({ queryKey: ['notifications', 'summary'] })
  }

  const summary = useQuery({
    queryKey: ['notifications', 'summary'],
    queryFn: notificationsApi.summary,
    enabled: open,
    refetchInterval: 15_000,
  })

  const list = useInfiniteQuery({
    queryKey: ['notifications', 'page', tab],
    enabled: open,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) => notificationsApi.list({ tab, cursor: pageParam, limit: 30 }),
    getNextPageParam: page => page.nextCursor || undefined,
  })

  const detail = useQuery({
    queryKey: ['notifications', 'detail', selectedId],
    enabled: open && !!selectedId,
    queryFn: () => notificationsApi.get(selectedId as string),
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

  const channelTemplates = useQuery({
    queryKey: ['notifications', 'channel-templates'],
    queryFn: notificationsApi.channelTemplates,
    enabled: open && nav === 'config',
    staleTime: Infinity,
  })

  const smtp = useQuery({
    queryKey: ['notifications', 'smtp'],
    queryFn: notificationsApi.smtp.get,
    enabled: open && nav === 'config',
  })

  // SMTP 表单首载回填（保存后不覆盖正在编辑的输入）
  const smtpLoadedRef = useRef(false)
  useEffect(() => {
    if (!smtp.data || smtpLoadedRef.current) return
    smtpLoadedRef.current = true
    setSmtpHost(smtp.data.host)
    setSmtpPort(String(smtp.data.port || 465))
    setSmtpUsername(smtp.data.username)
    setSmtpSender(smtp.data.sender)
  }, [smtp.data])

  const ingestKeys = useQuery({
    queryKey: ['notifications', 'ingest-keys'],
    queryFn: notificationsApi.ingestKeys.list,
    enabled: open && nav === 'config',
  })

  // 弹窗关闭即回消息分区并清除密钥明文与表单临时态（“仅此一次”承诺不因重开而失效）
  useEffect(() => {
    if (!open) {
      setNav('messages')
      setMintedKey(null)
      setKeyName('')
      setKeyScope('')
      setChannelName('')
      setChannelNote('')
      setTemplateParams({})
      setEditingChannel(null)
      setChannelFormStage('idle')
      setForwardOpen(false)
      setForwardChannelIds(new Set())
      setComposeTitle('')
      setComposeBody('')
      setComposePriority('normal')
      setComposeChannelIds(null)
    }
  }, [open])

  // 详情接口在服务端把消息标为已读：同步刷新列表与徽章（不刷详情自身，避免循环）
  const syncedReadId = useRef<string | null>(null)
  useEffect(() => {
    const id = detail.data?.id
    if (!id || syncedReadId.current === id) return
    syncedReadId.current = id
    void queryClient.invalidateQueries({ queryKey: ['notifications', 'page'] })
    void queryClient.invalidateQueries({ queryKey: ['notifications', 'summary'] })
  }, [detail.data?.id, queryClient])

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
      // 新消息未读未归档：切到未读视图保证列表与选中详情同屏可见
      setTab('unread')
      setNav('messages')
      queryClient.setQueryData(['notifications', 'detail', data.id], data)
      invalidateLists()
    },
    onError: error => toast.error(errorText(error)),
  })

  const currentTemplate = (channelTemplates.data ?? []).find(t => t.id === templateId)

  const resetChannelForm = () => {
    setChannelName('')
    setChannelNote('')
    setTemplateParams({})
    setEditingChannel(null)
    setChannelFormStage('idle')
  }

  const submitChannelForm = () => {
    if (!channelName.trim()) {
      toast.error('渠道名称不能为空')
      return
    }
    if (!currentTemplate) {
      toast.error('请选择渠道类型')
      return
    }
    // 空值字段不提交（编辑态敏感字段留空 = 保持原值）
    const params = Object.fromEntries(
      Object.entries(templateParams).filter(([, value]) => value.trim() !== ''),
    )
    const missing = currentTemplate.fields.find(f => f.required && !params[f.key])
    if (missing) {
      toast.error(`「${missing.label}」不能为空`)
      return
    }
    if (editingChannel) {
      saveChannelMutation.mutate({ id: editingChannel.id, fields: { name: channelName.trim(), note: channelNote.trim() || null, params } })
    } else {
      createChannelMutation.mutate({ name: channelName.trim(), template: templateId, params, note: channelNote.trim() || null })
    }
  }

  const createChannelMutation = useMutation({
    mutationFn: notificationsApi.channels.create,
    onSuccess: () => {
      toast.success('渠道已创建')
      resetChannelForm()
      void queryClient.invalidateQueries({ queryKey: ['notifications', 'channels'] })
    },
    onError: error => toast.error(errorText(error)),
  })

  const saveChannelMutation = useMutation({
    mutationFn: ({ id, fields }: { id: string; fields: { name?: string; note?: string | null; params?: Record<string, string> } }) =>
      notificationsApi.channels.update(id, fields),
    onSuccess: () => {
      toast.success('渠道已更新')
      resetChannelForm()
      void queryClient.invalidateQueries({ queryKey: ['notifications', 'channels'] })
    },
    onError: error => toast.error(errorText(error)),
  })

  const smtpPutMutation = useMutation({
    mutationFn: notificationsApi.smtp.put,
    onSuccess: () => {
      toast.success('发件设置已保存')
      setSmtpPassword('')
      void queryClient.invalidateQueries({ queryKey: ['notifications', 'smtp'] })
    },
    onError: error => toast.error(errorText(error)),
  })

  const smtpTestMutation = useMutation({
    mutationFn: notificationsApi.smtp.test,
    onSuccess: data => {
      if (data.ok) toast.success(data.message)
      else toast.error(data.message)
    },
    onError: error => toast.error(errorText(error)),
  })

  const updateChannelMutation = useMutation({
    mutationFn: ({ id, fields }: { id: string; fields: { enabled?: boolean; name?: string; note?: string | null; params?: Record<string, string> } }) =>
      notificationsApi.channels.update(id, fields),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['notifications', 'channels'] })
    },
    onError: error => toast.error(errorText(error)),
  })

  const deleteChannelMutation = useMutation({
    mutationFn: notificationsApi.channels.remove,
    onSuccess: () => {
      toast.success('渠道已删除')
      setDeletingChannel(null)
      void queryClient.invalidateQueries({ queryKey: ['notifications', 'channels'] })
    },
    onError: error => toast.error(errorText(error)),
  })

  const testChannelMutation = useMutation({
    mutationFn: notificationsApi.channels.test,
    onSuccess: data => {
      if (data.ok) toast.success(data.message)
      else toast.error(data.message)
      void queryClient.invalidateQueries({ queryKey: ['notifications', 'channels'] })
    },
    onError: error => toast.error(errorText(error)),
    onSettled: () => setTestingChannelId(null),
  })

  const mintKeyMutation = useMutation({
    mutationFn: notificationsApi.ingestKeys.create,
    onSuccess: data => {
      setMintedKey(data)
      setKeyName('')
      setKeyScope('')
      void queryClient.invalidateQueries({ queryKey: ['notifications', 'ingest-keys'] })
    },
    onError: error => toast.error(errorText(error)),
  })

  const revokeKeyMutation = useMutation({
    mutationFn: notificationsApi.ingestKeys.revoke,
    onSuccess: () => {
      toast.success('密钥已吊销')
      void queryClient.invalidateQueries({ queryKey: ['notifications', 'ingest-keys'] })
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

  const keyTestMutation = useMutation({
    mutationFn: notificationsApi.ingestKeys.test,
    onSuccess: data => {
      if (data.ok) {
        toast.success(data.message)
        invalidateLists()
        void queryClient.invalidateQueries({ queryKey: ['notifications', 'ingest-keys'] })
        void queryClient.invalidateQueries({ queryKey: ['notifications', 'channels'] })
      } else {
        toast.error(data.message)
      }
    },
    onError: error => toast.error(errorText(error)),
    onSettled: () => setTestingKeyId(null),
  })

  const deleteMutation = useMutation({
    mutationFn: notificationsApi.remove,
    onSuccess: (_result, messageId) => {
      toast.success('消息已删除')
      if (selectedId === messageId) setSelectedId(null)
      setDeleting(null)
      queryClient.removeQueries({ queryKey: ['notifications', 'detail', messageId] })
      invalidateLists()
    },
    onError: error => toast.error(errorText(error)),
  })

  const items = useMemo(
    () => list.data?.pages.flatMap(page => page.items) ?? [],
    [list.data],
  )
  const selected = detail.data ?? null
  const unreadCount = summary.data?.unreadCount ?? 0

  const tabCount = (key: NotificationTab): number | null => {
    if (key === 'unread') return summary.data?.unreadCount ?? null
    if (key === 'starred') return summary.data?.starredCount ?? null
    if (key === 'archived') return summary.data?.archivedCount ?? null
    return null
  }

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
    const selected = composeChannelIds
      ? [...composeChannelIds].filter(id => enabledIds.has(id))
      : [...enabledIds]
    if (selected.length === 0) {
      toast.error('至少选择一个投递渠道')
      return
    }
    createMutation.mutate({
      title: composeTitle.trim(),
      body: composeBody,
      priority: composePriority,
      // 勾选集恰为全量时省略字段：走后端缺省语义（无渠道数上限、渠道新增即覆盖）
      ...(selected.length === enabledIds.size ? {} : { channelIds: selected }),
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

  if (!open) return null

  return (
    <>
      <DialogShell
        title="消息通知"
        description="消息中转站：多入口接收，站内查看处置，按配置的渠道自动或手动转发"
        size="wide"
        icon={<Bell size={18} />}
        onClose={onClose}
        contentClassName="h-[min(85dvh,46rem)]"
      >
        <div className="grid min-h-0 flex-1 grid-cols-1 md:grid-cols-[9.5rem_minmax(0,1fr)]">
          {/* 左侧分区导航（参考个人资料弹窗） */}
          <nav
            role="tablist"
            aria-label="消息通知分区"
            aria-orientation="vertical"
            className="flex min-h-0 flex-col gap-1 border-b border-[var(--color-border)] p-2 md:border-b-0 md:border-r"
          >
            {NAV_ITEMS.map(({ key, label, icon: Icon }) => {
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
                  data-notifications-nav={key}
                  className={`flex min-h-10 items-center gap-1.5 rounded-lg px-2.5 text-left text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                    active
                      ? 'bg-brand-soft font-medium text-brand-ink'
                      : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)]'
                  }`}
                >
                  <Icon size={14} className="shrink-0" aria-hidden="true" />
                  <span className="min-w-0 flex-1 truncate">{label}</span>
                  {key === 'messages' && unreadCount > 0 && (
                    <span className="flex h-[18px] min-w-[18px] shrink-0 items-center justify-center rounded-full bg-[var(--color-danger)] px-1 text-[10px] font-semibold tabular-nums text-white">
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
              {/* 消息分区内工具栏：筛选 + 全部已读 */}
              <div className="scrollbar-none flex shrink-0 items-center gap-1 overflow-x-auto border-b border-[var(--color-border)] px-2 py-1.5">
                {NOTIFICATION_TABS.map(({ key, label }) => {
                  const count = tabCount(key)
                  const active = tab === key
                  return (
                    <button
                      key={key}
                      type="button"
                      onClick={() => { setTab(key); setSelectedId(null) }}
                      aria-pressed={active}
                      data-notifications-tab={key}
                      className={`flex shrink-0 items-center gap-1 rounded-md px-2 py-1 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                        active
                          ? 'bg-brand-soft font-medium text-brand-ink'
                          : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)]'
                      }`}
                    >
                      {label}
                      {count !== null && count > 0 && (
                        <span className="rounded-full bg-[var(--color-bg-hover)] px-1.5 text-[10px] tabular-nums text-[var(--color-text-tertiary)]">
                          {count > 99 ? '99+' : count}
                        </span>
                      )}
                    </button>
                  )
                })}
                <div className="ml-auto flex shrink-0 items-center gap-1 pl-2">
                  <button
                    type="button"
                    onClick={() => readAllMutation.mutate()}
                    disabled={unreadCount === 0 || readAllMutation.isPending}
                    aria-label="全部已读"
                    className="flex shrink-0 items-center gap-1 rounded-md px-2 py-1.5 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
                    data-notifications-read-all
                  >
                    <MailOpen size={13} /> <span className="hidden sm:inline">全部已读</span>
                  </button>
                </div>
              </div>

              {/* —— 列表 / 详情两栏；窄屏由选中态驱动单栏切换 —— */}
              <div className="flex min-h-0 flex-1">
                  <div
                    className={`${selectedId ? 'hidden md:flex' : 'flex'} w-full shrink-0 flex-col border-r border-[var(--color-border)] md:w-[22rem]`}
                    data-notifications-list
                  >
                    <div className="scrollbar-none min-h-0 flex-1 overflow-y-auto p-2">
                      {list.isLoading && (
                        <div className="flex items-center justify-center gap-2 py-8 text-xs text-[var(--color-text-tertiary)]">
                          <Loader2 size={14} className="animate-spin" /> 加载中…
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
                      {!list.isLoading && !list.isError && items.length === 0 && (
                        <p className="px-2 py-8 text-center text-xs leading-5 text-[var(--color-text-tertiary)]">
                          {tab === 'archived' ? '没有已归档的消息。' : '没有消息。外部系统可通过投递接口把消息送到这里。'}
                        </p>
                      )}
                      <div className="space-y-1">
                        {items.map(item => {
                          const active = item.id === selectedId
                          const priority = priorityStyle(item.priority)
                          return (
                            <button
                              key={item.id}
                              type="button"
                              onClick={() => setSelectedId(item.id)}
                              data-notifications-item={item.id}
                              className={`w-full rounded-lg px-2.5 py-2 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                                active
                                  ? 'bg-brand-soft/70'
                                  : 'hover:bg-[var(--color-bg-hover)]'
                              }`}
                            >
                              <span className="flex items-center gap-1.5">
                                <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${priority.dotClass}`} aria-hidden />
                                <span className={`min-w-0 flex-1 truncate text-sm ${item.isRead ? 'text-[var(--color-text-primary)]' : 'font-medium text-[var(--color-text-primary)]'}`}>
                                  {item.title}
                                </span>
                                {item.isStarred && <Star size={12} className="shrink-0 fill-current text-[var(--color-warning)]" aria-label="已标记" />}
                                {!!item.attachmentCount && item.attachmentCount > 0 && (
                                  <span className="flex shrink-0 items-center gap-0.5 text-[10px] tabular-nums text-[var(--color-text-tertiary)]">
                                    <Paperclip size={10} />{item.attachmentCount}
                                  </span>
                                )}
                              </span>
                              {item.bodyPreview && (
                                <span className={`mt-0.5 block truncate text-xs ${item.isRead ? 'text-[var(--color-text-tertiary)]' : 'text-[var(--color-text-secondary)]'}`}>
                                  {item.bodyPreview}
                                </span>
                              )}
                              <span className="mt-1 block truncate text-[10px] text-[var(--color-text-tertiary)]">
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
                    className={`${selectedId ? 'flex' : 'hidden md:flex'} min-w-0 flex-1 flex-col`}
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
                          <ArrowLeft size={13} /> 返回消息列表
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
                          <Loader2 size={14} className="animate-spin" /> 加载中…
                        </div>
                      ) : (
                        <>
                          <div className="shrink-0 border-b border-[var(--color-border)] p-4">
                            <div className="flex items-start gap-2">
                              <h3 className="min-w-0 flex-1 text-sm font-semibold leading-6 text-[var(--color-text-primary)]">
                                {selected.title}
                              </h3>
                              <div className="flex shrink-0 items-center gap-0.5">
                                <button
                                  type="button"
                                  onClick={() => stateMutation.mutate({ id: selected.id, fields: { isStarred: !selected.isStarred } })}
                                  title={selected.isStarred ? '取消标记' : '标记为重要'}
                                  aria-label={selected.isStarred ? '取消标记' : '标记为重要'}
                                  className={`${iconButtonClass} ${selected.isStarred ? 'text-[var(--color-warning)]' : ''}`}
                                  data-notifications-star
                                >
                                  <Star size={15} className={selected.isStarred ? 'fill-current' : ''} />
                                </button>
                                <button
                                  type="button"
                                  onClick={() => stateMutation.mutate({ id: selected.id, fields: { isArchived: !selected.isArchived } })}
                                  title={selected.isArchived ? '恢复消息' : '归档消息'}
                                  aria-label={selected.isArchived ? '恢复消息' : '归档消息'}
                                  className={iconButtonClass}
                                  data-notifications-archive
                                >
                                  {selected.isArchived ? <ArchiveRestore size={15} /> : <Archive size={15} />}
                                </button>
                                <button
                                  type="button"
                                  onClick={() => { setForwardOpen(value => !value); setForwardChannelIds(new Set()) }}
                                  title="转发到外部渠道"
                                  aria-label="转发到外部渠道"
                                  className={iconButtonClass}
                                  data-notifications-forward
                                >
                                  <Forward size={15} />
                                </button>
                                {selected.isRead && (
                                  <button
                                    type="button"
                                    onClick={() => stateMutation.mutate({ id: selected.id, fields: { isRead: false } })}
                                    title="标为未读"
                                    aria-label="标为未读"
                                    className={iconButtonClass}
                                    data-notifications-unread
                                  >
                                    <MailOpen size={15} className="rotate-180" />
                                  </button>
                                )}
                                <button
                                  type="button"
                                  onClick={() => setDeleting(selected)}
                                  title="删除消息"
                                  aria-label="删除消息"
                                  className={`${iconButtonClass} hover:text-[var(--color-danger)]`}
                                  data-notifications-delete
                                >
                                  <Trash2 size={15} />
                                </button>
                              </div>
                            </div>
                            <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-[var(--color-text-tertiary)]">
                              <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${priorityStyle(selected.priority).chipClass}`}>
                                {priorityStyle(selected.priority).label}
                              </span>
                              <span>{sourceTypeLabel(selected.sourceType)} · {selected.sourceSystem}</span>
                              <span>{formatDateTime(selected.createdAt)}</span>
                            </div>
                          </div>
                          {forwardOpen && (
                            <div className="shrink-0 border-b border-[var(--color-border)] bg-[var(--color-bg-base)] p-3" data-notifications-forward-panel>
                              <p className="text-xs font-medium text-[var(--color-text-secondary)]">转发到外部渠道</p>
                              <div className="mt-2 flex flex-col gap-1">
                                {enabledChannels.length === 0 ? (
                                  <p className="text-[11px] leading-5 text-[var(--color-text-tertiary)]">
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
                                    <span className="min-w-0 flex-1 truncate">{channel.name}<span className="ml-1.5 text-[10px] text-[var(--color-text-tertiary)]">{channel.templateName}</span></span>
                                  </label>
                                ))}
                              </div>
                              <div className="mt-2 flex justify-end gap-2">
                                <button type="button" onClick={() => setForwardOpen(false)} className="rounded-lg border border-[var(--color-border)] px-3 py-1.5 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                                  取消
                                </button>
                                <button
                                  type="button"
                                  onClick={() => forwardMutation.mutate({ messageId: selected.id, channelIds: [...forwardChannelIds] })}
                                  disabled={forwardChannelIds.size === 0 || forwardMutation.isPending}
                                  className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium text-white transition-all hover:opacity-95 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
                                  style={{ background: 'var(--color-nav-bg)' }}
                                  data-notifications-forward-confirm
                                >
                                  {forwardMutation.isPending ? <Loader2 size={13} className="animate-spin" /> : <Forward size={13} />}
                                  转发（{forwardChannelIds.size}）
                                </button>
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
                                        <FileText size={14} className="shrink-0 text-[var(--color-text-tertiary)]" />
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
                <h3 className={sectionHeadingClass}>发布通知</h3>
                <p className="mt-1 text-xs leading-5 text-[var(--color-text-tertiary)]">
                  消息进入站内列表，并立即转发到下方所选的启用渠道。
                </p>
              </div>

              {/* 投递去向：启用渠道 chips（默认全选，可点选增减） */}
              <div className="rounded-lg border border-[var(--color-border)] p-3" data-notifications-compose-destinations>
                <p className="text-xs font-medium text-[var(--color-text-secondary)]">
                  投递渠道
                  <span className="ml-1.5 font-normal text-[10px] text-[var(--color-text-tertiary)]">
                    默认全选，点击可增减
                  </span>
                </p>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {channels.isLoading ? (
                    <span className="flex items-center gap-1.5 text-xs text-[var(--color-text-tertiary)]">
                      <Loader2 size={12} className="animate-spin" /> 渠道加载中…
                    </span>
                  ) : channels.isError ? (
                    <span role="alert" className="flex flex-wrap items-center gap-2 text-[11px] leading-5 text-[var(--color-text-tertiary)]">
                      渠道列表加载失败：{errorText(channels.error)}
                      <button
                        type="button"
                        onClick={() => void channels.refetch()}
                        className="rounded-md border border-[var(--color-border)] px-2 py-0.5 text-[11px] text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        重试
                      </button>
                    </span>
                  ) : enabledChannels.length === 0 ? (
                    <span className="text-[11px] leading-5 text-[var(--color-text-tertiary)]">
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
                          {checked && <Send size={11} className="shrink-0" />}
                          <span className="max-w-40 truncate">{channel.name}</span>
                          <span className="text-[10px] font-normal opacity-70">{channel.templateName}</span>
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
                  placeholder="支持 Markdown：图片、表格、代码块……"
                  className={`${inputClass} min-h-48 flex-1 resize-none font-mono text-xs leading-6`}
                  data-notifications-compose-body
                />
              </label>
              <div className="flex items-center justify-between gap-2">
                <p className="text-[11px] leading-5 text-[var(--color-text-tertiary)]">
                  {enabledChannels.length > 0 && !channels.isError && `将投递到 ${
                    composeChannelIds
                      ? [...composeChannelIds].filter(id => enabledChannels.some(channel => channel.id === id)).length
                      : enabledChannels.length
                  } 个渠道`}
                </p>
                <button
                  type="button"
                  onClick={handleCompose}
                  disabled={createMutation.isPending}
                  className="flex items-center gap-1.5 rounded-lg px-3.5 py-2 text-xs font-medium text-white transition-all hover:opacity-95 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
                  style={{ background: 'var(--color-nav-bg)' }}
                  data-notifications-compose-send
                >
                  {createMutation.isPending ? <Loader2 size={13} className="animate-spin" /> : <Send size={13} />}
                  发送
                </button>
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
              {/* —— 设置组一：入口 · 对外投递 —— */}
              <section aria-label="入口配置" data-notifications-keys-panel className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-base)]">
                <header className="flex items-start justify-between gap-3 border-b border-[var(--color-border)] px-4 py-3">
                  <div className="min-w-0">
                    <h3 className="text-sm font-semibold text-[var(--color-text-primary)]">入口 · 对外投递</h3>
                    <p className="mt-0.5 text-xs leading-5 text-[var(--color-text-tertiary)]">
                      外部系统以 X-API-Key 调用投递接口，消息进入中转站并自动转发；密钥明文仅签发时展示一次。
                    </p>
                  </div>
                  <span className="mt-0.5 shrink-0 rounded-full bg-[var(--color-bg-hover)] px-2 py-0.5 text-[10px] tabular-nums text-[var(--color-text-tertiary)]">
                    {(ingestKeys.data ?? []).filter(key => key.enabled).length} 枚有效
                  </span>
                </header>
                <div className="space-y-3 p-4">
                  <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-3" data-notifications-endpoint-card>
                    <div className="flex items-center justify-between gap-2">
                      <code className="min-w-0 truncate font-mono text-[11px] text-[var(--color-text-primary)]">{ingestEndpoint()}</code>
                      <button
                        type="button"
                        onClick={() => {
                          writeTextToClipboard(ingestCurlSample())
                            .then(() => toast.success('调用示例已复制'))
                            .catch(() => toast.error('复制失败，请手动选择复制'))
                        }}
                        className="flex shrink-0 items-center gap-1 rounded-md border border-[var(--color-border)] bg-[var(--color-bg-base)] px-2 py-1 text-[11px] text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        data-notifications-copy-curl
                      >
                        <Copy size={11} /> 复制调用示例
                      </button>
                    </div>
                    <pre className="scrollbar-none mt-2 overflow-x-auto rounded-md bg-[var(--color-code-bg)] px-2.5 py-2 font-mono text-[10px] leading-5 text-[var(--color-code-fg)]">{ingestCurlSample()}</pre>
                  </div>

                  {mintedKey && (
                    <div className="rounded-lg border border-[var(--color-warning)] bg-[var(--color-warning-bg)] p-3" data-notifications-key-plaintext>
                      <p className="text-xs font-medium text-[var(--color-warning)]">
                        「{mintedKey.name}」密钥明文（仅此一次，请立即保存）：
                      </p>
                      <div className="mt-2 flex items-center gap-2">
                        <code className="min-w-0 flex-1 break-all rounded bg-[var(--color-bg-base)] px-2 py-1.5 font-mono text-[11px] text-[var(--color-text-primary)]">
                          {mintedKey.plaintextKey}
                        </code>
                        <button
                          type="button"
                          onClick={() => {
                            writeTextToClipboard(mintedKey.plaintextKey || '')
                              .then(() => toast.success('已复制到剪贴板'))
                              .catch(() => toast.error('复制失败，请手动选择复制'))
                          }}
                          className="flex shrink-0 items-center gap-1 rounded-md border border-[var(--color-border)] bg-[var(--color-bg-base)] px-2 py-1.5 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        >
                          <Copy size={12} /> 复制
                        </button>
                      </div>
                    </div>
                  )}

                  <div className="flex flex-wrap items-end gap-2">
                    <label className="flex min-w-40 flex-1 flex-col gap-1 text-xs text-[var(--color-text-secondary)]">
                      密钥名称（来源标识）
                      <input
                        value={keyName}
                        onChange={event => setKeyName(event.target.value)}
                        maxLength={200}
                        placeholder="如 billing"
                        className={inputClass}
                        data-notifications-key-name
                      />
                    </label>
                    <label className="flex w-44 flex-col gap-1 text-xs text-[var(--color-text-secondary)]">
                      限定来源系统（可选）
                      <input
                        value={keyScope}
                        onChange={event => setKeyScope(event.target.value)}
                        maxLength={200}
                        placeholder="留空不限定"
                        className={inputClass}
                        data-notifications-key-scope
                      />
                    </label>
                    <button
                      type="button"
                      onClick={() => {
                        if (!keyName.trim()) {
                          toast.error('密钥名称不能为空')
                          return
                        }
                        mintKeyMutation.mutate({
                          name: keyName.trim(),
                          allowedSourceSystem: keyScope.trim() || null,
                        })
                      }}
                      disabled={mintKeyMutation.isPending}
                      className="flex items-center gap-1.5 rounded-lg px-3 py-2 text-xs font-medium text-white transition-all hover:opacity-95 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
                      style={{ background: 'var(--color-nav-bg)' }}
                      data-notifications-key-mint
                    >
                      {mintKeyMutation.isPending ? <Loader2 size={13} className="animate-spin" /> : <KeyRound size={13} />}
                      签发密钥
                    </button>
                  </div>

                  {ingestKeys.isLoading ? (
                    <div className="flex items-center justify-center gap-2 py-6 text-xs text-[var(--color-text-tertiary)]">
                      <Loader2 size={14} className="animate-spin" /> 加载中…
                    </div>
                  ) : ingestKeys.isError ? (
                    <div role="alert" className="flex flex-col items-center gap-2 py-6 text-center">
                      <p className="text-xs leading-5 text-[var(--color-text-tertiary)]">密钥列表加载失败：{errorText(ingestKeys.error)}</p>
                      <button
                        type="button"
                        onClick={() => void ingestKeys.refetch()}
                        className="rounded-md border border-[var(--color-border)] px-2.5 py-1 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        重试
                      </button>
                    </div>
                  ) : (ingestKeys.data ?? []).length === 0 ? (
                    <p className="rounded-lg border border-dashed border-[var(--color-border)] px-3 py-5 text-center text-xs leading-5 text-[var(--color-text-tertiary)]">
                      还没有投递密钥。签发后把密钥与调用示例交给外部系统即可开始投递。
                    </p>
                  ) : (
                    <ul className="space-y-1">
                      {(ingestKeys.data ?? []).map(key => (
                        <li
                          key={key.id}
                          data-notifications-key-item={key.id}
                          className="flex items-center gap-2 rounded-lg px-2 py-2 transition-colors hover:bg-[var(--color-bg-hover)]"
                        >
                          <KeyRound size={14} className={`shrink-0 ${key.enabled ? 'text-[var(--color-text-secondary)]' : 'text-[var(--color-text-tertiary)]'}`} />
                          <div className="min-w-0 flex-1">
                            <p className="truncate text-sm text-[var(--color-text-primary)]">
                              {key.name}
                              <span className="ml-2 font-mono text-[10px] text-[var(--color-text-tertiary)]">{key.keyPrefix}_…</span>
                            </p>
                            <p className="truncate text-[10px] text-[var(--color-text-tertiary)]">
                              {key.allowedSourceSystem ? `限定来源：${key.allowedSourceSystem} · ` : ''}
                              {key.enabled ? `最近使用：${key.lastUsedAt ? formatDateTime(key.lastUsedAt) : '未使用'}` : `已吊销：${formatDateTime(key.revokedAt || key.createdAt)}`}
                            </p>
                          </div>
                          {key.enabled && (
                            <>
                              <button
                                type="button"
                                onClick={() => {
                                  setTestingKeyId(key.id)
                                  keyTestMutation.mutate(key.id)
                                }}
                                disabled={keyTestMutation.isPending && testingKeyId === key.id}
                                title="以该密钥真实投递一条链路测试消息"
                                aria-label={`以密钥 ${key.name} 测试入口链路`}
                                className="flex shrink-0 items-center rounded-md px-2.5 py-1.5 text-xs font-medium text-white transition-all hover:opacity-95 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
                                style={{ background: 'var(--color-nav-bg)' }}
                                data-notifications-key-test
                              >
                                {keyTestMutation.isPending && testingKeyId === key.id
                                  ? <Loader2 size={12} className="animate-spin" />
                                  : <Zap size={12} />}
                              </button>
                              <button
                                type="button"
                                onClick={() => setRevokingKey(key)}
                                disabled={revokeKeyMutation.isPending}
                                className="flex shrink-0 items-center gap-1 rounded-md px-2 py-1.5 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
                                data-notifications-key-revoke
                              >
                                吊销
                              </button>
                            </>
                          )}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </section>

              {/* —— 设置组二：发件邮箱（SMTP）—— */}
              <details className="group mt-4 rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-base)]" data-notifications-smtp-card>
                <summary className="flex cursor-pointer list-none items-center gap-2.5 border-b border-transparent px-4 py-3 transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring group-open:border-[var(--color-border)]">
                  <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-brand-soft text-brand-ink">
                    <Mail size={14} />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block text-sm font-semibold text-[var(--color-text-primary)]">发件邮箱</span>
                    <span className="block text-xs leading-5 text-[var(--color-text-tertiary)]">配置一次 SMTP，邮件渠道只需填收件人；密码加密存储。</span>
                  </span>
                  <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] ${smtp.data?.configured ? 'bg-[var(--color-success-bg)] text-[var(--color-success)]' : 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]'}`}>
                    {smtp.data?.configured ? '已配置' : '未配置'}
                  </span>
                  <ChevronDown size={14} className="shrink-0 text-[var(--color-text-tertiary)] transition-transform group-open:rotate-180" aria-hidden />
                </summary>
                <div className="border-t-0 p-4">
                  <div className="flex flex-wrap items-end gap-2">
                    <label className="flex min-w-40 flex-1 flex-col gap-1 text-xs text-[var(--color-text-secondary)]">
                      SMTP 主机
                      <input value={smtpHost} onChange={e => setSmtpHost(e.target.value)} maxLength={200} placeholder="如 smtp.qq.com" className={inputClass} data-notifications-smtp-host />
                    </label>
                    <label className="flex w-24 flex-col gap-1 text-xs text-[var(--color-text-secondary)]">
                      端口
                      <input value={smtpPort} onChange={e => setSmtpPort(e.target.value)} inputMode="numeric" className={inputClass} data-notifications-smtp-port />
                    </label>
                    <label className="flex min-w-44 flex-1 flex-col gap-1 text-xs text-[var(--color-text-secondary)]">
                      账号
                      <input value={smtpUsername} onChange={e => setSmtpUsername(e.target.value)} maxLength={200} placeholder="如 noreply@example.com" className={inputClass} data-notifications-smtp-username />
                    </label>
                    <label className="flex min-w-36 flex-1 flex-col gap-1 text-xs text-[var(--color-text-secondary)]">
                      密码/授权码
                      <input type="password" value={smtpPassword} onChange={e => setSmtpPassword(e.target.value)} maxLength={500} placeholder={smtp.data?.hasPassword ? '留空保持不变' : 'SMTP 密码或授权码'} className={inputClass} data-notifications-smtp-password />
                    </label>
                    <label className="flex w-40 flex-col gap-1 text-xs text-[var(--color-text-secondary)]">
                      发件人名称（可选）
                      <input value={smtpSender} onChange={e => setSmtpSender(e.target.value)} maxLength={200} placeholder="平台通知" className={inputClass} data-notifications-smtp-sender />
                    </label>
                    <button
                      type="button"
                      onClick={() => {
                        if (!smtpHost.trim()) { toast.error('SMTP 主机不能为空'); return }
                        const port = Number(smtpPort)
                        if (!Number.isInteger(port) || port < 1 || port > 65535) { toast.error('端口无效'); return }
                        if (!smtpUsername.trim()) { toast.error('SMTP 账号不能为空'); return }
                        smtpPutMutation.mutate({
                          host: smtpHost.trim(), port,
                          username: smtpUsername.trim(),
                          password: smtpPassword || undefined,
                          sender: smtpSender.trim(), useTls: true,
                        })
                      }}
                      disabled={smtpPutMutation.isPending}
                      className="flex items-center gap-1.5 rounded-lg px-3 py-2 text-xs font-medium text-white transition-all hover:opacity-95 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
                      style={{ background: 'var(--color-nav-bg)' }}
                      data-notifications-smtp-save
                    >
                      {smtpPutMutation.isPending ? <Loader2 size={13} className="animate-spin" /> : <Mail size={13} />}
                      保存
                    </button>
                  </div>
                  <div className="mt-2 flex flex-wrap items-end gap-2">
                    <label className="flex min-w-52 flex-1 flex-col gap-1 text-xs text-[var(--color-text-secondary)]">
                      测试收件邮箱
                      <input value={smtpTestTo} onChange={e => setSmtpTestTo(e.target.value)} maxLength={200} placeholder="your@mail.com" className={inputClass} data-notifications-smtp-test-to />
                    </label>
                    <button
                      type="button"
                      onClick={() => {
                        if (!smtpTestTo.trim()) { toast.error('请先填写测试收件邮箱'); return }
                        smtpTestMutation.mutate(smtpTestTo.trim())
                      }}
                      disabled={smtpTestMutation.isPending}
                      className="flex items-center gap-1.5 rounded-lg border border-[var(--color-border)] px-3 py-2 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
                      data-notifications-smtp-test
                    >
                      {smtpTestMutation.isPending ? <Loader2 size={13} className="animate-spin" /> : <Send size={13} />}
                      发送测试邮件
                    </button>
                  </div>
                </div>
              </details>

              {/* —— 设置组三：转发渠道 —— */}
              <section aria-label="出口配置" data-notifications-channels-panel className="mt-4 rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-base)]">
                <header className="flex items-start justify-between gap-3 border-b border-[var(--color-border)] px-4 py-3">
                  <div className="min-w-0">
                    <h3 className="text-sm font-semibold text-[var(--color-text-primary)]">转发渠道</h3>
                    <p className="mt-0.5 text-xs leading-5 text-[var(--color-text-tertiary)]">
                      消息到达即为启用渠道生成转发；渠道旁可直发测试消息验证连通。
                    </p>
                  </div>
                  {channelFormStage === 'idle' && (
                    <button
                      type="button"
                      onClick={() => { setTemplateId('dingtalk'); setTemplateParams({}); setChannelFormStage('pick') }}
                      className="mt-0.5 flex shrink-0 items-center gap-1 rounded-lg px-3 py-1.5 text-xs font-medium text-white transition-all hover:opacity-95 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      style={{ background: 'var(--color-nav-bg)' }}
                      data-notifications-channel-create-open
                    >
                      <Plus size={13} /> 新建渠道
                    </button>
                  )}
                </header>
                <div className="p-4">
                  {channelFormStage === 'pick' && (
                    /* —— 模板选择网格：选类型起手，每类一句人话 —— */
                    <div className="mb-4" data-notifications-template-grid>
                      <div className="mb-2 flex items-center justify-between">
                        <p className="text-xs font-medium text-[var(--color-text-secondary)]">选择渠道类型</p>
                        <button
                          type="button"
                          onClick={resetChannelForm}
                          className="rounded-md px-2 py-1 text-[11px] text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        >
                          取消
                        </button>
                      </div>
                      {channelTemplates.isLoading && (
                        <div className="flex items-center justify-center gap-2 rounded-lg border border-dashed border-[var(--color-border)] py-6 text-xs text-[var(--color-text-tertiary)]">
                          <Loader2 size={14} className="animate-spin" /> 渠道类型加载中…
                        </div>
                      )}
                      {channelTemplates.isError && (
                        <div role="alert" className="flex flex-col items-center gap-2 rounded-lg border border-dashed border-[var(--color-border)] py-6 text-center">
                          <p className="text-xs leading-5 text-[var(--color-text-tertiary)]">渠道类型加载失败：{errorText(channelTemplates.error)}</p>
                          <button
                            type="button"
                            onClick={() => void channelTemplates.refetch()}
                            className="rounded-md border border-[var(--color-border)] px-2.5 py-1 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          >
                            重试
                          </button>
                        </div>
                      )}
                      <div className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,13rem),1fr))] gap-2">
                      {!channelTemplates.isLoading && !channelTemplates.isError && (channelTemplates.data ?? []).length === 0 && (
                        <p className="rounded-lg border border-dashed border-[var(--color-border)] px-3 py-5 text-center text-xs text-[var(--color-text-tertiary)]">
                          渠道类型定义加载为空，请稍后重试。
                        </p>
                      )}
                      {(channelTemplates.data ?? []).map(t => {
                          const Icon = TEMPLATE_ICONS[t.id] ?? Plug
                          return (
                            <button
                              key={t.id}
                              type="button"
                              onClick={() => { setTemplateId(t.id); setTemplateParams({}); setChannelFormStage('form') }}
                              className="flex items-start gap-2.5 rounded-lg border border-[var(--color-border)] p-3 text-left transition-colors hover:border-[var(--color-nav-bg)] hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                              data-notifications-template-card={t.id}
                            >
                              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-brand-soft text-brand-ink">
                                <Icon size={14} />
                              </span>
                              <span className="min-w-0">
                                <span className="block truncate text-sm font-medium text-[var(--color-text-primary)]">{t.name}</span>
                                <span className="mt-0.5 block line-clamp-2 text-[11px] leading-4 text-[var(--color-text-tertiary)]">{t.description}</span>
                              </span>
                            </button>
                          )
                        })}
                      </div>
                    </div>
                  )}

                  {channelFormStage === 'form' && !currentTemplate && (
                    <div className="mb-4 flex items-center justify-center gap-2 rounded-lg border border-dashed border-[var(--color-border)] py-6 text-xs text-[var(--color-text-tertiary)]">
                      <Loader2 size={14} className="animate-spin" /> 正在加载渠道类型…
                    </div>
                  )}

                  {channelFormStage === 'form' && currentTemplate && (
                    /* —— 字段表单：新建/编辑共用；敏感字段留空=保持不变 —— */
                    <div className="mb-4 rounded-lg border border-[var(--color-border)] p-3" data-notifications-channel-form>
                      <div className="flex items-center gap-2">
                        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-brand-soft text-brand-ink">
                          {(() => { const Icon = TEMPLATE_ICONS[currentTemplate.id] ?? Plug; return <Icon size={14} /> })()}
                        </span>
                        <p className="min-w-0 flex-1 text-sm font-medium text-[var(--color-text-primary)]">
                          {editingChannel ? `编辑 · ${editingChannel.name}` : `新建 ${currentTemplate.name}`}
                        </p>
                        {!editingChannel && (
                          <button
                            type="button"
                            onClick={() => setChannelFormStage('pick')}
                            className="shrink-0 rounded-md px-2 py-1 text-[11px] text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          >
                            换类型
                          </button>
                        )}
                      </div>
                      <div className="mt-3 flex flex-wrap items-end gap-2">
                        <label className="flex min-w-36 flex-1 flex-col gap-1 text-xs text-[var(--color-text-secondary)]">
                          渠道名称
                          <input value={channelName} onChange={e => setChannelName(e.target.value)} maxLength={200} placeholder="如 钉钉运维群" className={inputClass} data-notifications-channel-name />
                        </label>
                        <label className="flex min-w-52 flex-1 flex-col gap-1 text-xs text-[var(--color-text-secondary)]">
                          备注（可选）
                          <input value={channelNote} onChange={e => setChannelNote(e.target.value)} maxLength={500} placeholder="渠道用途、接收人范围…" className={inputClass} data-notifications-channel-note />
                        </label>
                      </div>
                      <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
                        {(editingChannel ? editingChannel.fields : currentTemplate.fields).map(f => (
                          <label key={f.key} className="flex flex-col gap-1 text-xs text-[var(--color-text-secondary)]">
                            {f.label}{f.required ? '' : '（可选）'}
                            <input
                              type={f.secret ? 'password' : 'text'}
                              value={templateParams[f.key] ?? ''}
                              onChange={e => setTemplateParams(current => ({ ...current, [f.key]: e.target.value }))}
                              maxLength={2000}
                              placeholder={editingChannel && f.secret ? '留空保持不变' : f.placeholder}
                              className={inputClass}
                              data-notifications-channel-param={f.key}
                            />
                            {f.hint && <span className="text-[10px] leading-4 text-[var(--color-text-tertiary)]">{f.hint}</span>}
                          </label>
                        ))}
                      </div>
                      {templateId === 'email' && smtp.data && !smtp.data.configured && (
                        <p className="mt-2 rounded-md bg-[var(--color-warning-bg)] px-2.5 py-1.5 text-[11px] leading-5 text-[var(--color-warning)]" data-notifications-email-needs-smtp>
                          邮件渠道需要先配置上方「发件邮箱」。
                        </p>
                      )}
                      <div className="mt-3 flex justify-end gap-2">
                        <button type="button" onClick={resetChannelForm} className="rounded-lg border border-[var(--color-border)] px-3 py-2 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                          取消
                        </button>
                        <button
                          type="button"
                          onClick={submitChannelForm}
                          disabled={createChannelMutation.isPending || saveChannelMutation.isPending}
                          className="flex items-center gap-1.5 rounded-lg px-3 py-2 text-xs font-medium text-white transition-all hover:opacity-95 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
                          style={{ background: 'var(--color-nav-bg)' }}
                          data-notifications-channel-create
                        >
                          {(createChannelMutation.isPending || saveChannelMutation.isPending) ? <Loader2 size={13} className="animate-spin" /> : <Plug size={13} />}
                          {editingChannel ? '保存修改' : '新建渠道'}
                        </button>
                      </div>
                    </div>
                  )}

                  {channels.isLoading ? (
                    <div className="flex items-center justify-center gap-2 py-6 text-xs text-[var(--color-text-tertiary)]">
                      <Loader2 size={14} className="animate-spin" /> 加载中…
                    </div>
                  ) : channels.isError ? (
                    <div role="alert" className="flex flex-col items-center gap-2 py-6 text-center">
                      <p className="text-xs leading-5 text-[var(--color-text-tertiary)]">渠道列表加载失败：{errorText(channels.error)}</p>
                      <button
                        type="button"
                        onClick={() => void channels.refetch()}
                        className="rounded-md border border-[var(--color-border)] px-2.5 py-1 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        重试
                      </button>
                    </div>
                  ) : (channels.data ?? []).length === 0 ? (
                    <p className="rounded-lg border border-dashed border-[var(--color-border)] px-3 py-6 text-center text-xs leading-5 text-[var(--color-text-tertiary)]">
                      还没有转发渠道。{channelFormStage === 'idle' ? '点击右上「新建渠道」，' : '选择上方类型，'}新消息会自动转发到所有启用渠道。
                    </p>
                  ) : (
                    <div className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,15.5rem),1fr))] gap-2.5" data-notifications-channel-grid>
                      {(channels.data ?? []).map(channel => {
                        const Icon = TEMPLATE_ICONS[channel.template] ?? Plug
                        return (
                          <div
                            key={channel.id}
                            data-notifications-channel-item={channel.id}
                            className="flex flex-col gap-2 rounded-lg border border-[var(--color-border)] p-3 transition-colors hover:border-[var(--color-text-tertiary)]"
                          >
                            <div className="flex items-center gap-2">
                              <span className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${channel.enabled ? 'bg-brand-soft text-brand-ink' : 'bg-[var(--color-bg-hover)] text-[var(--color-text-tertiary)]'}`}>
                                <Icon size={14} />
                              </span>
                              <div className="min-w-0 flex-1">
                                <p className="truncate text-sm font-medium text-[var(--color-text-primary)]" title={channel.name}>{channel.name}</p>
                                <p className="text-[10px] text-[var(--color-text-tertiary)]">{channel.templateName}</p>
                              </div>
                              <button
                                type="button"
                                role="switch"
                                aria-checked={channel.enabled}
                                aria-label={`${channel.enabled ? '停用' : '启用'}渠道 ${channel.name}`}
                                onClick={() => updateChannelMutation.mutate({ id: channel.id, fields: { enabled: !channel.enabled } })}
                                disabled={updateChannelMutation.isPending}
                                className={`relative flex h-5 w-9 shrink-0 items-center rounded-full transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60 ${channel.enabled ? 'bg-[var(--color-nav-bg)]' : 'bg-[var(--color-border)]'}`}
                                data-notifications-channel-toggle
                              >
                                <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${channel.enabled ? 'translate-x-4' : 'translate-x-0.5'}`} />
                              </button>
                            </div>
                            <p className="truncate text-[11px] text-[var(--color-text-secondary)]" title={channel.display || channel.urlMasked}>
                              {channel.display || channel.urlMasked}
                            </p>
                            <p className={`text-[10px] leading-4 ${channel.lastStatus === 'failed' ? 'text-[var(--color-danger)]' : 'text-[var(--color-text-tertiary)]'}`}>
                              {channel.lastStatus === 'sent'
                                ? `投递成功 ${formatDateTime(channel.lastSentAt || channel.updatedAt)}`
                                : channel.lastStatus === 'failed'
                                  ? <span className="line-clamp-2" title={`投递失败：${channel.lastError}`}>投递失败：{channel.lastError}</span>
                                  : '尚未投递'}
                            </p>
                            <div className="mt-auto flex items-center gap-1 border-t border-[var(--color-border)] pt-2">
                              {channel.enabled && (
                                <button
                                  type="button"
                                  onClick={() => {
                                    setTestingChannelId(channel.id)
                                    testChannelMutation.mutate(channel.id)
                                  }}
                                  disabled={testChannelMutation.isPending && testingChannelId === channel.id}
                                  title={`向 ${channel.name} 直发测试消息`}
                                  aria-label={`向 ${channel.name} 直发测试消息`}
                                  className="flex h-7 w-7 items-center justify-center rounded-md text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
                                  data-notifications-channel-test
                                >
                                  {testChannelMutation.isPending && testingChannelId === channel.id
                                    ? <Loader2 size={13} className="animate-spin" />
                                    : <Zap size={13} />}
                                </button>
                              )}
                              <button
                                type="button"
                                onClick={() => {
                                  setEditingChannel(channel)
                                  setChannelName(channel.name)
                                  setChannelNote(channel.note || '')
                                  setTemplateId(channel.template)
                                  const prefill: Record<string, string> = {}
                                  for (const f of channel.fields) {
                                    if (!f.secret && f.value) prefill[f.key] = f.value
                                  }
                                  setTemplateParams(prefill)
                                  setChannelFormStage('form')
                                }}
                                title={`编辑渠道 ${channel.name}`}
                                aria-label={`编辑渠道 ${channel.name}`}
                                className="flex h-7 w-7 items-center justify-center rounded-md text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                                data-notifications-channel-edit
                              >
                                <Pencil size={13} />
                              </button>
                              <span className="flex-1" aria-hidden />
                              <button
                                type="button"
                                onClick={() => setDeletingChannel(channel)}
                                title={`删除渠道 ${channel.name}`}
                                aria-label={`删除渠道 ${channel.name}`}
                                className="flex h-7 w-7 items-center justify-center rounded-md text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                                data-notifications-channel-delete
                              >
                                <Trash2 size={13} />
                              </button>
                            </div>
                          </div>
                        )
                      })}
                    </div>
                  )}
                </div>
              </section>

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
                  <Loader2 size={14} className="animate-spin" /> 统计加载中…
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
                      },
                    ].map(card => (
                      <div key={card.label} className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-base)] p-3" data-notifications-stats-card={card.label}>
                        <p className="text-xs text-[var(--color-text-tertiary)]">{card.label}</p>
                        <p className="mt-1 text-lg font-semibold tabular-nums leading-7 text-[var(--color-text-primary)]">{card.value}</p>
                      </div>
                    ))}
                  </div>

                  {/* —— 入站趋势 —— */}
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
                            className={`rounded-md px-2 py-1 text-[11px] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
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
                      <ResponsiveContainer width="100%" height="100%">
                        <BarChart data={stats.data.trend} margin={{ top: 4, right: 4, bottom: 0, left: -28 }}>
                          <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" vertical={false} />
                          <XAxis
                            dataKey="date"
                            tickFormatter={(v: string) => v.slice(5)}
                            tick={{ fontSize: 10, fill: 'var(--color-text-tertiary)' }}
                            interval="preserveStartEnd"
                            axisLine={{ stroke: 'var(--color-border)' }}
                            tickLine={false}
                          />
                          <YAxis
                            allowDecimals={false}
                            tick={{ fontSize: 10, fill: 'var(--color-text-tertiary)' }}
                            axisLine={false}
                            tickLine={false}
                          />
                          <Tooltip
                            cursor={{ fill: 'var(--color-bg-hover)' }}
                            contentStyle={{ fontSize: 11, borderRadius: 8, border: '1px solid var(--color-border)', background: 'var(--color-bg-elevated)' }}
                            formatter={value => [`${value ?? 0} 条`, '入站']}
                            labelFormatter={label => String(label)}
                          />
                          <Bar dataKey="count" fill="var(--color-nav-bg)" radius={[3, 3, 0, 0]} maxBarSize={18} />
                        </BarChart>
                      </ResponsiveContainer>
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

      <ConfirmDialog
        open={deletingChannel !== null}
        onClose={() => setDeletingChannel(null)}
        onConfirm={() => {
          if (deletingChannel) deleteChannelMutation.mutate(deletingChannel.id)
        }}
        loading={deleteChannelMutation.isPending}
        title="删除这个转发渠道？"
        description={`「${deletingChannel?.name ?? ''}」将被删除，之后的新消息不再转发到该渠道（历史投递记录一并移除）。`}
        confirmText="删除"
        variant="danger"
      />
      <ConfirmDialog
        open={revokingKey !== null}
        onClose={() => setRevokingKey(null)}
        onConfirm={() => {
          if (revokingKey) revokeKeyMutation.mutate(revokingKey.id)
          setRevokingKey(null)
        }}
        loading={revokeKeyMutation.isPending}
        title="吊销这枚投递密钥？"
        description={`「${revokingKey?.name ?? ''}」对应的外部系统将立即无法投递消息，且不可恢复。`}
        confirmText="吊销"
        variant="warning"
      />
      <ConfirmDialog
        open={deleting !== null}
        onClose={() => setDeleting(null)}
        onConfirm={() => deleting && deleteMutation.mutate(deleting.id)}
        loading={deleteMutation.isPending}
        title="删除这条消息？"
        description="消息与附件将从平台永久删除，所有管理员的视图都会移除。"
        confirmText="删除"
        variant="danger"
      />
    </>
  )
}
