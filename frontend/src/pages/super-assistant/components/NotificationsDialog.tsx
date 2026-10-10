import { useEffect, useMemo, useRef, useState } from 'react'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  Archive, ArchiveRestore, ArrowLeft, Bell, Copy, FileText, KeyRound, Loader2, MailOpen,
  Paperclip, Radio, Send, Star, Trash2,
} from 'lucide-react'
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
 * 布局采用「浏览态 / 管理态」双模式：
 * - 浏览态：全宽工具栏（tab 筛选 + 全局动作）+ 左列表(22rem)/右详情两栏；
 *   窄屏单栏切换（列表 ⇄ 详情），由选中态驱动。
 * - 管理态（发送消息 / 接入密钥 / 转发渠道）：主体整体切换为全宽面板，
 *   面板顶部带「返回消息」——管理表单与列表不再挤占同一帧。
 */
type DialogMode = 'browse' | 'compose' | 'keys' | 'channels'

const MODE_TITLES: Record<Exclude<DialogMode, 'browse'>, string> = {
  compose: '发送消息',
  keys: '接入密钥',
  channels: '转发渠道',
}

/** 通知正文沿用会话排版映射，仅覆盖 img：内嵌媒体走鉴权拉取与音视频原生控件 */
const notificationMarkdownComponents = {
  ...assistantMarkdownComponents,
  img: ({ src, alt }: { src?: string; alt?: string }) => (
    <NotificationMedia src={ src } alt={ alt } />
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

/** 工具栏动作按钮：浏览态中性、激活管理态高亮；窄屏只留图标 */
const modeButtonClass = (active: boolean) => `flex shrink-0 items-center gap-1 rounded-md px-2 py-1.5 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
  active
    ? 'bg-brand-soft font-medium text-brand-ink'
    : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)]'
}`

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

export default function NotificationsDialog({
  open,
  onClose,
}: {
  open: boolean
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const [mode, setMode] = useState<DialogMode>('browse')
  const [tab, setTab] = useState<NotificationTab>('all')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [deleting, setDeleting] = useState<NotificationMessage | null>(null)
  const [keyName, setKeyName] = useState('')
  const [keyScope, setKeyScope] = useState('')
  const [mintedKey, setMintedKey] = useState<NotificationIngestKey | null>(null)
  const [revokingKey, setRevokingKey] = useState<NotificationIngestKey | null>(null)
  const [channelName, setChannelName] = useState('')
  const [channelUrl, setChannelUrl] = useState('')
  const [channelNote, setChannelNote] = useState('')
  const [deletingChannel, setDeletingChannel] = useState<NotificationChannel | null>(null)
  const [testingChannelId, setTestingChannelId] = useState<string | null>(null)

  // 手动发送表单
  const [composeTitle, setComposeTitle] = useState('')
  const [composeBody, setComposeBody] = useState('')
  const [composePriority, setComposePriority] = useState<NotificationPriority>('normal')

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

  // 弹窗关闭即回到浏览态并清除密钥明文与表单临时态（“仅此一次”承诺不因重开而失效）
  useEffect(() => {
    if (!open) {
      setMode('browse')
      setMintedKey(null)
      setKeyName('')
      setKeyScope('')
      setChannelName('')
      setChannelUrl('')
      setChannelNote('')
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
      setMode('browse')
      setComposeTitle('')
      setComposeBody('')
      setComposePriority('normal')
      setSelectedId(data.id)
      queryClient.setQueryData(['notifications', 'detail', data.id], data)
      invalidateLists()
    },
    onError: error => toast.error(errorText(error)),
  })

  const channels = useQuery({
    queryKey: ['notifications', 'channels'],
    queryFn: notificationsApi.channels.list,
    enabled: open && mode === 'channels',
  })

  const createChannelMutation = useMutation({
    mutationFn: notificationsApi.channels.create,
    onSuccess: () => {
      toast.success('渠道已创建')
      setChannelName('')
      setChannelUrl('')
      setChannelNote('')
      void queryClient.invalidateQueries({ queryKey: ['notifications', 'channels'] })
    },
    onError: error => toast.error(errorText(error)),
  })

  const updateChannelMutation = useMutation({
    mutationFn: ({ id, fields }: { id: string; fields: { enabled?: boolean; name?: string } }) =>
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

  const ingestKeys = useQuery({
    queryKey: ['notifications', 'ingest-keys'],
    queryFn: notificationsApi.ingestKeys.list,
    enabled: open && mode === 'keys',
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

  const handleCompose = () => {
    if (!composeTitle.trim()) {
      toast.error('标题不能为空')
      return
    }
    createMutation.mutate({
      title: composeTitle.trim(),
      body: composeBody,
      priority: composePriority,
    })
  }

  if (!open) return null

  return (
    <>
      <DialogShell
        title="消息通知"
        description="平台与外部系统投递的消息，归档与标记仅影响你自己的视图"
        size="wide"
        icon={<Bell size={18} className="text-white" />}
        onClose={onClose}
        contentClassName="h-[min(85dvh,46rem)]"
      >
        {/* 全宽工具栏：tab 筛选（左）+ 全局动作（右）。窄屏横向滚动、动作只留图标 */}
        <div className="scrollbar-none flex shrink-0 items-center gap-1 overflow-x-auto border-b border-[var(--color-border)] px-2 py-1.5">
          {NOTIFICATION_TABS.map(({ key, label }) => {
            const count = tabCount(key)
            const active = mode === 'browse' && tab === key
            return (
              <button
                key={key}
                type="button"
                onClick={() => { setTab(key); setSelectedId(null); setMode('browse') }}
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
            <span className="mx-0.5 h-4 w-px shrink-0 bg-[var(--color-border)]" aria-hidden />
            <button
              type="button"
              onClick={() => setMode(current => (current === 'channels' ? 'browse' : 'channels'))}
              aria-pressed={mode === 'channels'}
              aria-label="转发渠道"
              title="转发渠道"
              className={modeButtonClass(mode === 'channels')}
              data-notifications-channels-toggle
            >
              <Radio size={13} /> <span className="hidden sm:inline">转发渠道</span>
            </button>
            <button
              type="button"
              onClick={() => setMode(current => (current === 'keys' ? 'browse' : 'keys'))}
              aria-pressed={mode === 'keys'}
              aria-label="接入密钥"
              title="接入密钥"
              className={modeButtonClass(mode === 'keys')}
              data-notifications-keys-toggle
            >
              <KeyRound size={13} /> <span className="hidden sm:inline">接入密钥</span>
            </button>
            <button
              type="button"
              onClick={() => setMode(current => (current === 'compose' ? 'browse' : 'compose'))}
              aria-pressed={mode === 'compose'}
              aria-label="发送消息"
              title="发送消息"
              className={modeButtonClass(mode === 'compose')}
              data-notifications-compose-toggle
            >
              <Send size={13} /> <span className="hidden sm:inline">发送消息</span>
            </button>
          </div>
        </div>

        {mode === 'browse' ? (
          /* —— 浏览态：左列表 / 右详情；窄屏由选中态驱动单栏切换 —— */
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
        ) : (
          /* —— 管理态：主体整体切换为全宽面板（表单与列表不再挤占同一帧）—— */
          <div className="flex min-h-0 flex-1 flex-col" data-notifications-manage>
            <div className="flex shrink-0 items-center gap-3 border-b border-[var(--color-border)] px-3 py-2">
              <button
                type="button"
                onClick={() => setMode('browse')}
                aria-label="返回消息列表"
                data-notifications-back
                className="flex shrink-0 items-center gap-1 rounded-md px-2 py-1 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <ArrowLeft size={13} /> 返回消息
              </button>
              <h3 className="text-sm font-semibold text-[var(--color-text-primary)]">{MODE_TITLES[mode]}</h3>
            </div>

            {mode === 'channels' ? (
              <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-4" data-notifications-channels-panel>
                <p className="text-xs leading-5 text-[var(--color-text-tertiary)]">
                  消息到达即为所有启用渠道生成转发（apprise 协议：<code className="rounded bg-[var(--color-bg-hover)] px-1 py-0.5 font-mono text-[11px]">mailto://</code>、<code className="rounded bg-[var(--color-bg-hover)] px-1 py-0.5 font-mono text-[11px]">dingtalk://</code>、<code className="rounded bg-[var(--color-bg-hover)] px-1 py-0.5 font-mono text-[11px]">json://</code> 等）；渠道地址即凭据，加密存储仅回显掩码。
                </p>
                <div className="flex flex-wrap items-end gap-2">
                  <label className="flex min-w-36 flex-1 flex-col gap-1 text-xs text-[var(--color-text-secondary)]">
                    渠道名称
                    <input
                      value={channelName}
                      onChange={event => setChannelName(event.target.value)}
                      maxLength={200}
                      placeholder="如 钉钉运维群"
                      className={inputClass}
                      data-notifications-channel-name
                    />
                  </label>
                  <label className="flex min-w-52 flex-[2] flex-col gap-1 text-xs text-[var(--color-text-secondary)]">
                    apprise URL
                    <input
                      value={channelUrl}
                      onChange={event => setChannelUrl(event.target.value)}
                      maxLength={2000}
                      placeholder="json://host/path 或 mailto://user:pass@host"
                      className={inputClass}
                      data-notifications-channel-url
                    />
                  </label>
                  <button
                    type="button"
                    onClick={() => {
                      if (!channelName.trim()) {
                        toast.error('渠道名称不能为空')
                        return
                      }
                      if (!channelUrl.trim()) {
                        toast.error('apprise URL 不能为空')
                        return
                      }
                      createChannelMutation.mutate({
                        name: channelName.trim(),
                        appriseUrl: channelUrl.trim(),
                        note: channelNote.trim() || null,
                      })
                    }}
                    disabled={createChannelMutation.isPending}
                    className="flex items-center gap-1.5 rounded-lg px-3 py-2 text-xs font-medium text-white transition-all hover:opacity-95 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
                    style={{ background: 'var(--color-nav-bg)' }}
                    data-notifications-channel-create
                  >
                    {createChannelMutation.isPending ? <Loader2 size={13} className="animate-spin" /> : <Radio size={13} />}
                    新建渠道
                  </button>
                </div>
                <input
                  value={channelNote}
                  onChange={event => setChannelNote(event.target.value)}
                  maxLength={500}
                  placeholder="备注（可选）：渠道用途、接收人范围…"
                  className={`${inputClass} sm:max-w-md`}
                  data-notifications-channel-note
                />
                <div className="min-h-0 flex-1">
                  {channels.isLoading ? (
                    <div className="flex items-center justify-center gap-2 py-8 text-xs text-[var(--color-text-tertiary)]">
                      <Loader2 size={14} className="animate-spin" /> 加载中…
                    </div>
                  ) : channels.isError ? (
                    <div role="alert" className="flex flex-col items-center gap-2 py-8 text-center">
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
                    <p className="px-2 py-6 text-center text-xs leading-5 text-[var(--color-text-tertiary)]">
                      还没有转发渠道。新建后，新消息会自动转发到所有启用渠道。
                    </p>
                  ) : (
                    <ul className="space-y-1">
                      {(channels.data ?? []).map(channel => (
                        <li
                          key={channel.id}
                          data-notifications-channel-item={channel.id}
                          className="flex items-center gap-2 rounded-lg px-2 py-2 transition-colors hover:bg-[var(--color-bg-hover)]"
                        >
                          <Radio size={14} className={`shrink-0 ${channel.enabled ? 'text-[var(--color-text-secondary)]' : 'text-[var(--color-text-tertiary)]'}`} />
                          <div className="min-w-0 flex-1">
                            <p className="truncate text-sm text-[var(--color-text-primary)]">
                              {channel.name}
                              <span className="ml-2 font-mono text-[10px] text-[var(--color-text-tertiary)]">{channel.urlMasked}</span>
                            </p>
                            <p className="truncate text-[10px] text-[var(--color-text-tertiary)]">
                              {channel.note ? `${channel.note} · ` : ''}
                              {channel.lastStatus === 'sent'
                                ? `最近投递成功：${formatDateTime(channel.lastSentAt || channel.updatedAt)}`
                                : channel.lastStatus === 'failed'
                                  ? `最近投递失败：${channel.lastError}`
                                  : '尚未投递'}
                            </p>
                          </div>
                          <button
                            type="button"
                            onClick={() => {
                              setTestingChannelId(channel.id)
                              testChannelMutation.mutate(channel.id)
                            }}
                            disabled={testChannelMutation.isPending && testingChannelId === channel.id}
                            className="flex shrink-0 items-center rounded-md px-2 py-1 text-xs text-brand-ink transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
                            data-notifications-channel-test
                          >
                            {testChannelMutation.isPending && testingChannelId === channel.id
                              ? <Loader2 size={12} className="animate-spin" />
                              : '测试'}
                          </button>
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
                          <button
                            type="button"
                            onClick={() => setDeletingChannel(channel)}
                            title={`删除渠道 ${channel.name}`}
                            aria-label={`删除渠道 ${channel.name}`}
                            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                            data-notifications-channel-delete
                          >
                            <Trash2 size={14} />
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </div>
            ) : mode === 'keys' ? (
              <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-4" data-notifications-keys-panel>
                <p className="text-xs leading-5 text-[var(--color-text-tertiary)]">
                  外部系统以 X-API-Key 调用 <code className="rounded bg-[var(--color-bg-hover)] px-1 py-0.5 font-mono text-[11px]">POST /api/v2/notifications/ingest</code> 投递消息；密钥明文仅签发时展示一次。
                </p>
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
                        className="flex shrink-0 items-center gap-1 rounded-md border border-[var(--color-border)] px-2 py-1.5 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
                <div className="min-h-0 flex-1">
                  {ingestKeys.isLoading ? (
                    <div className="flex items-center justify-center gap-2 py-8 text-xs text-[var(--color-text-tertiary)]">
                      <Loader2 size={14} className="animate-spin" /> 加载中…
                    </div>
                  ) : ingestKeys.isError ? (
                    <div role="alert" className="flex flex-col items-center gap-2 py-8 text-center">
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
                    <p className="px-2 py-6 text-center text-xs leading-5 text-[var(--color-text-tertiary)]">
                      还没有投递密钥。签发后把密钥与接口地址交给外部系统即可开始投递。
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
                            <button
                              type="button"
                              onClick={() => setRevokingKey(key)}
                              disabled={revokeKeyMutation.isPending}
                              className="flex shrink-0 items-center gap-1 rounded-md px-2 py-1 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
                              data-notifications-key-revoke
                            >
                              吊销
                            </button>
                          )}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </div>
            ) : (
              <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-4">
                <p className="text-xs leading-5 text-[var(--color-text-tertiary)]">
                  正式消息：进入站内列表，并转发到所有已启用的外部渠道。
                </p>
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
                <label className="flex min-h-0 flex-1 flex-col gap-1 text-xs text-[var(--color-text-secondary)]">
                  正文（Markdown）
                  <textarea
                    value={composeBody}
                    onChange={event => setComposeBody(event.target.value)}
                    placeholder="支持 Markdown：图片、表格、代码块……"
                    className={`${inputClass} min-h-48 flex-1 resize-none font-mono text-xs leading-6`}
                    data-notifications-compose-body
                  />
                </label>
                <div className="flex justify-end gap-2">
                  <button
                    type="button"
                    onClick={() => setMode('browse')}
                    className="rounded-lg border border-[var(--color-border)] px-3 py-1.5 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    取消
                  </button>
                  <button
                    type="button"
                    onClick={handleCompose}
                    disabled={createMutation.isPending}
                    className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium text-white transition-all hover:opacity-95 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
                    style={{ background: 'var(--color-nav-bg)' }}
                    data-notifications-compose-send
                  >
                    {createMutation.isPending ? <Loader2 size={13} className="animate-spin" /> : <Send size={13} />}
                    发送
                  </button>
                </div>
              </div>
            )}
          </div>
        )}
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
