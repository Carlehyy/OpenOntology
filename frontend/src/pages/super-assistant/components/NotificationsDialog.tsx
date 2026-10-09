import { useEffect, useMemo, useRef, useState } from 'react'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  Archive, ArchiveRestore, Bell, FileText, Loader2, MailOpen, Paperclip,
  Send, Star, Trash2,
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
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { formatDateTime } from '@/utils/datetime'
import { useAuthStore } from '@/stores/authStore'

import { DialogShell } from './AssistantConfiguration'
import { assistantMarkdownComponents } from './AssistantConversation'
import { errorText } from './assistantPanelUtils'
import NotificationMedia from './NotificationMedia'
import { formatFileSize, priorityStyle, sourceTypeLabel } from './notificationsFormat'

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
  const [tab, setTab] = useState<NotificationTab>('all')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [deleting, setDeleting] = useState<NotificationMessage | null>(null)
  const [composeOpen, setComposeOpen] = useState(false)

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
      setComposeOpen(false)
      setComposeTitle('')
      setComposeBody('')
      setComposePriority('normal')
      setSelectedId(data.id)
      queryClient.setQueryData(['notifications', 'detail', data.id], data)
      invalidateLists()
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
        contentClassName="h-[min(82dvh,44rem)]"
      >
        <div className="flex min-h-0 flex-1">
          {/* 左栏：tab 筛选 + 消息列表 */}
          <div className="flex w-72 shrink-0 flex-col border-r border-[var(--color-border)]" data-notifications-list>
            <div className="flex shrink-0 flex-wrap gap-1 border-b border-[var(--color-border)] p-2">
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
                    className={`flex items-center gap-1 rounded-md px-2 py-1 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
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
            </div>
            <div className="flex shrink-0 items-center justify-between gap-1 px-2 py-1.5">
              <button
                type="button"
                onClick={() => readAllMutation.mutate()}
                disabled={unreadCount === 0 || readAllMutation.isPending}
                className="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
                data-notifications-read-all
              >
                <MailOpen size={13} /> 全部已读
              </button>
              <button
                type="button"
                onClick={() => setComposeOpen(value => !value)}
                className="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-brand-ink transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                data-notifications-compose-toggle
              >
                <Send size={13} /> 发送消息
              </button>
            </div>
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
                      onClick={() => { setSelectedId(item.id); setComposeOpen(false) }}
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

          {/* 右栏：发送消息 或 消息详情 */}
          <div className="flex min-w-0 flex-1 flex-col" data-notifications-detail>
            {composeOpen ? (
              <div className="flex flex-1 flex-col gap-3 overflow-y-auto p-4">
                <h3 className="text-sm font-semibold text-[var(--color-text-primary)]">发送消息</h3>
                <p className="text-xs leading-5 text-[var(--color-text-tertiary)]">
                  正式消息：进入站内列表，并转发到所有已启用的外部渠道。
                </p>
                <label className="flex flex-col gap-1 text-xs text-[var(--color-text-secondary)]">
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
                <div className="flex flex-col gap-1 text-xs text-[var(--color-text-secondary)]">
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
                <label className="flex flex-1 flex-col gap-1 text-xs text-[var(--color-text-secondary)]">
                  正文（Markdown）
                  <textarea
                    value={composeBody}
                    onChange={event => setComposeBody(event.target.value)}
                    placeholder="支持 Markdown：图片、表格、代码块……"
                    className={`${inputClass} min-h-40 flex-1 resize-none font-mono text-xs leading-6`}
                    data-notifications-compose-body
                  />
                </label>
                <div className="flex justify-end gap-2">
                  <button
                    type="button"
                    onClick={() => setComposeOpen(false)}
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
            ) : selectedId ? (
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
      </DialogShell>

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
