import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react'
import type { UseQueryResult } from '@tanstack/react-query'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronDown, Loader2, Mail, Pencil, Plug, Plus, Send, Trash2, Zap } from 'lucide-react'
import { toast } from 'sonner'

import { notificationsApi, type NotificationChannel } from '@/api/notifications'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { formatDateTime } from '@/utils/datetime'

import { errorText } from './assistantPanelUtils'
import { SettingSwitch } from './SettingSwitch'
import { channelFailureHint } from './notificationsFormat'
import { TEMPLATE_ICONS, inputClass } from './notificationsConfigShared'

/**
 * 渠道配置 · 消息出口分区（平台 → 外部）：转发渠道为主面板，发件邮箱（SMTP）为
 * 邮件渠道的平台级依赖、降次折叠展示。渠道卡四层信息（身份/摘要/投递状态/操作），
 * 启停开关统一 SettingSwitch 规格并按行隔离 busy。
 */
export interface OutboundSectionHandle {
  /** Esc 分层回退：有展开的渠道表单/类型网格则收起并返回 true */
  collapseTransient: () => boolean
}

interface NotificationsOutboundSectionProps {
  /** 弹窗开关：关闭时本组件随父级 return null 卸载，表单 state 即毁 */
  open: boolean
  /** 本分区是否可见（常挂载 + CSS 隐藏模式下控制 query 拉取时机与基线一致） */
  visible: boolean
  /** 渠道列表 query 由父组件持有（发送分区与转发面板共用同一 observer） */
  channels: UseQueryResult<NotificationChannel[]>
}

const NotificationsOutboundSection = forwardRef<OutboundSectionHandle, NotificationsOutboundSectionProps>(
  function NotificationsOutboundSection({ open, visible, channels }, ref) {
    const queryClient = useQueryClient()
    const [channelName, setChannelName] = useState('')
    const [channelNote, setChannelNote] = useState('')
    const [templateId, setTemplateId] = useState('dingtalk')
    const [templateParams, setTemplateParams] = useState<Record<string, string>>({})
    const [editingChannel, setEditingChannel] = useState<NotificationChannel | null>(null)
    // 渠道表单阶段：idle=列表；pick=模板网格选择；form=字段表单（编辑直达）
    const [channelFormStage, setChannelFormStage] = useState<'idle' | 'pick' | 'form'>('idle')
    const [deletingChannel, setDeletingChannel] = useState<NotificationChannel | null>(null)
    const [testingChannelId, setTestingChannelId] = useState<string | null>(null)
    // 启停按行隔离 busy：只禁用/脉冲被操作的那张卡，不再一刀切禁全部
    const [togglingChannelId, setTogglingChannelId] = useState<string | null>(null)
    // 投递失败详情展开（替代只能 hover 的 title 悬浮——触屏/键盘拿不到）
    const [failedDetailId, setFailedDetailId] = useState<string | null>(null)
    // 平台发件设置（SMTP）
    const [smtpHost, setSmtpHost] = useState('')
    const [smtpPort, setSmtpPort] = useState('465')
    const [smtpUsername, setSmtpUsername] = useState('')
    const [smtpPassword, setSmtpPassword] = useState('')
    const [smtpSender, setSmtpSender] = useState('')
    const [smtpTestTo, setSmtpTestTo] = useState('')
    // 邮件渠道依赖提示「去配置」要展开并滚到 SMTP 卡
    const smtpCardRef = useRef<HTMLDetailsElement>(null)

    const channelTemplates = useQuery({
      queryKey: ['notifications', 'channel-templates'],
      queryFn: notificationsApi.channelTemplates,
      enabled: open && visible,
      staleTime: Infinity,
      // 分区隐藏期间 observer 仍在（常挂载），但关闭弹窗退订后默认 gcTime 5min 会回收缓存——
      // 模板是静态定义，钉住缓存与基线（常驻 observer 永不重拉）行为对齐
      gcTime: Infinity,
    })

    const smtp = useQuery({
      queryKey: ['notifications', 'smtp'],
      queryFn: notificationsApi.smtp.get,
      enabled: open && visible,
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

    const resetChannelForm = () => {
      setChannelName('')
      setChannelNote('')
      setTemplateParams({})
      setEditingChannel(null)
      setChannelFormStage('idle')
    }

    const currentTemplate = (channelTemplates.data ?? []).find(t => t.id === templateId)

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
      onMutate: ({ id }) => setTogglingChannelId(current => current ?? id),
      onSuccess: (_data, variables) => {
        toast.success(variables.fields.enabled ? '渠道已启用' : '渠道已停用')
        void queryClient.invalidateQueries({ queryKey: ['notifications', 'channels'] })
      },
      onError: error => toast.error(errorText(error)),
      // 条件清理：连点两行时先完成的那次 settle 不清掉仍在飞行中的另一行 busy
      onSettled: (_data, _error, variables) => setTogglingChannelId(current => current === variables.id ? null : current),
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

    // 「关闭弹窗清空表单（含 SMTP 密码）」由卸载兜底：父级 return null 时本组件销毁，
    // 全部 state 与 smtpLoadedRef 随实例重建——挂载期 open 恒 true，无需 !open effect。
    // 分区切换（弹窗仍开）不卸载（父级常挂载保活），草稿按基线行为保留。

    useImperativeHandle(ref, () => ({
      collapseTransient: () => {
        if (channelFormStage === 'idle') return false
        resetChannelForm()
        return true
      },
    }), [channelFormStage])

    const enabledChannels = (channels.data ?? []).filter(channel => channel.enabled)

    /** 邮件渠道依赖引导：展开发件邮箱卡并滚入视野 */
    const openSmtpCard = () => {
      smtpCardRef.current?.setAttribute('open', '')
      smtpCardRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    }

    return (
      <section aria-label="消息出口" data-notifications-group="outbound" className="space-y-3">
        <div className="flex items-center gap-2.5 px-0.5">
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-brand-soft text-brand-ink" aria-hidden="true">
            <Send size={15} />
          </span>
          <div className="min-w-0 flex-1">
            <h3 className="text-base font-semibold text-[var(--color-text-primary)]">
              <span className="mr-1.5 font-mono text-xs font-normal text-[var(--color-text-tertiary)]">02</span>消息出口
            </h3>
            <p className="mt-0.5 text-xs leading-5 text-[var(--color-text-tertiary)]">消息到达即转发到启用渠道；邮件类经由平台发件邮箱发出。</p>
          </div>
          <span className="shrink-0 self-center rounded-full bg-[var(--color-bg-hover)] px-2 py-0.5 text-xs tabular-nums text-[var(--color-text-tertiary)]">
            {enabledChannels.length} 个渠道启用
          </span>
        </div>

        {/* —— 转发渠道（出口主面板）：用户九成时间在看渠道状态，置于 SMTP 之前 —— */}
        <section aria-label="转发渠道" data-notifications-channels-panel className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-base)]">
          <header className="flex items-start justify-between gap-3 border-b border-[var(--color-border)] px-4 py-3">
            <div className="min-w-0">
              <h4 className="text-sm font-semibold text-[var(--color-text-primary)]">转发渠道</h4>
              <p className="mt-0.5 text-xs leading-5 text-[var(--color-text-tertiary)]">
                消息到达即为启用渠道生成转发；渠道旁可直发测试消息验证连通。
              </p>
            </div>
            {channelFormStage === 'idle' && (
              <Button
                size="sm"
                onClick={() => { setTemplateId('dingtalk'); setTemplateParams({}); setChannelFormStage('pick') }}
                className="rounded-lg"
                data-notifications-channel-create-open
              >
                <Plus size={12} /> 新建渠道
              </Button>
            )}
          </header>
          <div className="p-4">
            {channelFormStage === 'pick' && (
              /* —— 模板选择网格：选类型起手，每类一句人话（限高内滚防页面高度突变） —— */
              <div className="mb-4 max-h-[55svh] overflow-y-auto" data-notifications-template-grid>
                <div className="mb-2 flex items-center justify-between">
                  <p className="text-xs font-medium text-[var(--color-text-secondary)]">选择渠道类型</p>
                  <button
                    type="button"
                    onClick={resetChannelForm}
                    className="rounded-md px-2 py-1 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    取消
                  </button>
                </div>
                {channelTemplates.isLoading && (
                  <div className="flex items-center justify-center gap-2 rounded-lg border border-dashed border-[var(--color-border)] py-6 text-xs text-[var(--color-text-tertiary)]">
                    <Loader2 size={12} className="animate-spin" /> 渠道类型加载中…
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
                    /* 空数据分支与 error 分支对齐：给重试入口，而不是把恢复动作推给用户手动刷新 */
                    <div className="flex flex-col items-center gap-2 rounded-lg border border-dashed border-[var(--color-border)] px-3 py-5 text-center">
                      <p className="text-xs leading-5 text-[var(--color-text-tertiary)]">渠道类型定义加载为空。</p>
                      <button
                        type="button"
                        onClick={() => void channelTemplates.refetch()}
                        className="rounded-md border border-[var(--color-border)] px-2.5 py-1 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        data-notifications-templates-retry
                      >
                        重试
                      </button>
                    </div>
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
                          <Icon size={15} />
                        </span>
                        <span className="min-w-0">
                          <span className="block truncate text-sm font-medium text-[var(--color-text-primary)]">{t.name}</span>
                          <span className="mt-0.5 block line-clamp-2 text-xs leading-4 text-[var(--color-text-tertiary)]">{t.description}</span>
                        </span>
                      </button>
                    )
                  })}
                </div>
              </div>
            )}

            {channelFormStage === 'form' && !currentTemplate && (
              <div className="mb-4 flex items-center justify-center gap-2 rounded-lg border border-dashed border-[var(--color-border)] py-6 text-xs text-[var(--color-text-tertiary)]">
                <Loader2 size={12} className="animate-spin" /> 正在加载渠道类型…
              </div>
            )}

            {channelFormStage === 'form' && currentTemplate && (
              /* —— 字段表单：新建/编辑共用；敏感字段留空=保持不变。
                  限高内滚 + 操作行吸底：窄视口下保存按钮不再被顶出屏外 —— */
              <div className="mb-4 flex max-h-[60svh] flex-col overflow-y-auto rounded-lg border border-[var(--color-border)]" data-notifications-channel-form>
                <div className="p-3">
                  <div className="flex items-center gap-2">
                    <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-brand-soft text-brand-ink">
                      {(() => { const Icon = TEMPLATE_ICONS[currentTemplate.id] ?? Plug; return <Icon size={15} /> })()}
                    </span>
                    <p className="min-w-0 flex-1 text-sm font-medium text-[var(--color-text-primary)]">
                      {editingChannel ? `编辑 · ${editingChannel.name}` : `新建 ${currentTemplate.name}`}
                    </p>
                    {!editingChannel && (
                      <button
                        type="button"
                        onClick={() => setChannelFormStage('pick')}
                        className="shrink-0 rounded-md px-2 py-1 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
                        {f.hint && <span className="text-xs leading-4 text-[var(--color-text-tertiary)]">{f.hint}</span>}
                      </label>
                    ))}
                  </div>
                  {templateId === 'email' && smtp.data && !smtp.data.configured && (
                    <p className="mt-2 rounded-md bg-[var(--color-warning-bg)] px-2.5 py-1.5 text-xs leading-5 text-[var(--color-warning)]" data-notifications-email-needs-smtp>
                      邮件渠道需要先配置「发件邮箱」。
                    </p>
                  )}
                </div>
                <div className="sticky bottom-0 flex justify-end gap-2 border-t border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 py-2.5">
                  <button type="button" onClick={resetChannelForm} className="rounded-lg border border-[var(--color-border)] px-3 py-2 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                    取消
                  </button>
                  <Button
                    size="sm"
                    onClick={submitChannelForm}
                    loading={createChannelMutation.isPending || saveChannelMutation.isPending}
                    className="rounded-lg"
                    data-notifications-channel-create
                  >
                    <Plug size={12} /> {editingChannel ? '保存修改' : '新建渠道'}
                  </Button>
                </div>
              </div>
            )}

            {channels.isLoading ? (
              <div className="flex items-center justify-center gap-2 py-6 text-xs text-[var(--color-text-tertiary)]">
                <Loader2 size={12} className="animate-spin" /> 加载中…
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
                  const statusDot = channel.lastStatus === 'sent'
                    ? 'bg-[var(--color-success)]'
                    : channel.lastStatus === 'failed'
                      ? 'bg-[var(--color-danger)]'
                      : 'bg-[var(--color-border-hover)]'
                  return (
                    <div
                      key={channel.id}
                      data-notifications-channel-item={channel.id}
                      className="flex flex-col gap-1.5 rounded-lg border border-[var(--color-border)] p-3 transition-colors hover:border-[var(--color-border-hover)]"
                    >
                      {/* 层1 身份：状态点（成功绿/失败红/未投递灰）+ 名称 + 启停开关 */}
                      <div className="flex items-center gap-2">
                        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${statusDot}`} aria-hidden="true"
                          title={channel.lastStatus === 'sent' ? '最近投递成功' : channel.lastStatus === 'failed' ? '最近投递失败' : '尚未投递'} />
                        <p className="min-w-0 flex-1 truncate text-sm font-medium text-[var(--color-text-primary)]" title={channel.name}>{channel.name}</p>
                        <SettingSwitch
                          ariaLabel={`${channel.enabled ? '停用' : '启用'}渠道 ${channel.name}`}
                          checked={channel.enabled}
                          busy={updateChannelMutation.isPending && togglingChannelId === channel.id}
                          onToggle={() => updateChannelMutation.mutate({ id: channel.id, fields: { enabled: !channel.enabled } })}
                          data-notifications-channel-toggle
                          className="shrink-0"
                        />
                      </div>
                      {/* 层2 摘要：类型 + 收件/地址；邮件类显性化对发件邮箱的依赖 */}
                      <p className="truncate text-xs text-[var(--color-text-tertiary)]" title={channel.display || channel.urlMasked}>
                        {channel.templateName} · {channel.display || channel.urlMasked}
                      </p>
                      {channel.template === 'email' && (
                        smtp.data?.configured ? (
                          <p className="flex items-center gap-1 text-xs text-[var(--color-text-tertiary)]" data-notifications-channel-smtp-dep="ok">
                            <Mail size={12} /> 经由「发件邮箱」<span className="font-medium text-[var(--color-success)]">已配置</span>
                          </p>
                        ) : (
                          <button
                            type="button"
                            onClick={openSmtpCard}
                            className="flex items-center gap-1 self-start rounded-md text-xs text-[var(--color-warning)] transition-colors hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                            data-notifications-channel-smtp-dep="missing"
                          >
                            <Mail size={12} /> 需先配置「发件邮箱」→ 去配置
                          </button>
                        )
                      )}
                      {/* 层3 最近一次投递/测试直发结果；失败行可点开完整错误（不再只靠 title 悬浮）。
                          统计区按正式投递单聚合，口径差异在展开详情里明示 */}
                      {channel.lastStatus === 'failed' ? (
                        <button
                          type="button"
                          onClick={() => setFailedDetailId(current => current === channel.id ? null : channel.id)}
                          className="flex items-start gap-1 text-left text-xs leading-4 text-[var(--color-danger)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          data-notifications-channel-status
                          aria-expanded={failedDetailId === channel.id}
                        >
                          <span className="line-clamp-2">投递失败 {formatDateTime(channel.updatedAt)} · {channelFailureHint(channel.lastError || '')}</span>
                        </button>
                      ) : (
                        <p className="text-xs leading-4 text-[var(--color-text-tertiary)]" data-notifications-channel-status>
                          {channel.lastStatus === 'sent'
                            ? `投递成功 ${formatDateTime(channel.lastSentAt || channel.updatedAt)}`
                            : '尚未投递'}
                        </p>
                      )}
                      {channel.lastStatus === 'failed' && failedDetailId === channel.id && (
                        <div className="max-h-40 overflow-y-auto rounded-md bg-[var(--color-danger-bg)] px-2 py-1.5 text-xs leading-5" data-notifications-channel-failure-detail>
                          <p className="break-all text-[var(--color-danger)]">最近一次尝试失败（{formatDateTime(channel.updatedAt)}）：{channel.lastError}</p>
                          <p className="mt-0.5 text-[var(--color-text-tertiary)]">此处含测试直发；统计区仅计正式投递单。可点「测试」重发验证。</p>
                        </div>
                      )}
                      {/* 层4 操作：测试（仅启用）/ 编辑 / 删除 */}
                      <div className="mt-auto flex items-center gap-1 border-t border-[var(--color-border)] pt-2">
                        {channel.enabled && (
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <button
                                type="button"
                                onClick={() => {
                                  setTestingChannelId(channel.id)
                                  testChannelMutation.mutate(channel.id)
                                }}
                                disabled={testChannelMutation.isPending && testingChannelId === channel.id}
                                aria-label={`向 ${channel.name} 直发测试消息`}
                                className="flex h-7 w-7 items-center justify-center rounded-md text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
                                data-notifications-channel-test
                              >
                                {testChannelMutation.isPending && testingChannelId === channel.id
                                  ? <Loader2 size={12} className="animate-spin" />
                                  : <Zap size={15} />}
                              </button>
                            </TooltipTrigger>
                            <TooltipContent align="end">向 {channel.name} 直发测试消息</TooltipContent>
                          </Tooltip>
                        )}
                        <Tooltip>
                          <TooltipTrigger asChild>
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
                              aria-label={`编辑渠道 ${channel.name}`}
                              className="flex h-7 w-7 items-center justify-center rounded-md text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                              data-notifications-channel-edit
                            >
                              <Pencil size={15} />
                            </button>
                          </TooltipTrigger>
                          <TooltipContent align="end">编辑渠道 {channel.name}</TooltipContent>
                        </Tooltip>
                        <span className="flex-1" aria-hidden />
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <button
                              type="button"
                              onClick={() => setDeletingChannel(channel)}
                              aria-label={`删除渠道 ${channel.name}`}
                              className="flex h-7 w-7 items-center justify-center rounded-md text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                              data-notifications-channel-delete
                            >
                              <Trash2 size={15} />
                            </button>
                          </TooltipTrigger>
                          <TooltipContent align="end">删除渠道 {channel.name}</TooltipContent>
                        </Tooltip>
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        </section>

        {/* —— 发件邮箱：邮件渠道的平台级依赖，降次折叠；依赖状态在上方渠道卡上显性化 —— */}
        <details ref={smtpCardRef} className="group rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-base)]" data-notifications-smtp-card>
          <summary className="flex cursor-pointer list-none items-center gap-2.5 border-b border-transparent px-4 py-3 transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring group-open:border-[var(--color-border)]">
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-brand-soft text-brand-ink">
              <Mail size={15} />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block text-sm font-semibold text-[var(--color-text-primary)]">发件邮箱</span>
              <span className="block text-xs leading-5 text-[var(--color-text-tertiary)]">邮件渠道的统一发件凭据；配置一次，密码加密存储。</span>
            </span>
            <span className={`shrink-0 rounded-full px-2 py-0.5 text-xs ${smtp.isError ? 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]' : smtp.data?.configured ? 'bg-[var(--color-success-bg)] text-[var(--color-success)]' : 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]'}`}>
              {smtp.isError ? '加载失败' : smtp.data?.configured ? '已配置' : '未配置'}
            </span>
            <ChevronDown size={12} className="shrink-0 text-[var(--color-text-tertiary)] transition-transform group-open:rotate-180" aria-hidden />
          </summary>
          {smtp.isError ? (
            /* 与密钥/模板/渠道三块对齐：加载失败给错误态 + 重试，而不是渲染空白可编辑表单 */
            <div role="alert" className="flex flex-col items-center gap-2 p-6 text-center">
              <p className="text-xs leading-5 text-[var(--color-text-tertiary)]">发件邮箱配置加载失败：{errorText(smtp.error)}</p>
              <button
                type="button"
                onClick={() => void smtp.refetch()}
                className="rounded-md border border-[var(--color-border)] px-2.5 py-1 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                data-notifications-smtp-retry
              >
                重试
              </button>
            </div>
          ) : (
          <div className="border-t-0 p-4">
            <div className="flex flex-wrap items-end gap-2">
              <label className="flex min-w-40 flex-col gap-1 text-xs text-[var(--color-text-secondary)]">
                SMTP 主机
                <input value={smtpHost} onChange={e => setSmtpHost(e.target.value)} maxLength={200} placeholder="如 smtp.qq.com" className={inputClass} data-notifications-smtp-host />
              </label>
              <label className="flex w-24 flex-col gap-1 text-xs text-[var(--color-text-secondary)]">
                端口
                <input value={smtpPort} onChange={e => setSmtpPort(e.target.value.replace(/[^0-9]/g, ''))} inputMode="numeric" className={inputClass} data-notifications-smtp-port />
              </label>
              <label className="flex min-w-44 flex-1 flex-col gap-1 text-xs text-[var(--color-text-secondary)]">
                账号
                <input value={smtpUsername} onChange={e => setSmtpUsername(e.target.value)} maxLength={200} placeholder="如 noreply@example.com" className={inputClass} data-notifications-smtp-username />
              </label>
              <label className="flex min-w-36 flex-1 flex-col gap-1 text-xs text-[var(--color-text-secondary)]">
                密码/授权码
                <input type="password" autoComplete="new-password" value={smtpPassword} onChange={e => setSmtpPassword(e.target.value)} maxLength={500} placeholder={smtp.data?.hasPassword ? '留空保持不变' : 'SMTP 密码或授权码'} className={inputClass} data-notifications-smtp-password />
              </label>
              <label className="flex w-40 flex-col gap-1 text-xs text-[var(--color-text-secondary)]">
                发件人名称（可选）
                <input value={smtpSender} onChange={e => setSmtpSender(e.target.value)} maxLength={200} placeholder="平台通知" className={inputClass} data-notifications-smtp-sender />
              </label>
              <Button
                size="sm"
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
                loading={smtpPutMutation.isPending}
                className="ml-auto rounded-lg"
                data-notifications-smtp-save
              >
                <Mail size={12} /> 保存
              </Button>
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
                {smtpTestMutation.isPending ? <Loader2 size={12} className="animate-spin" /> : <Send size={12} />}
                发送测试邮件
              </button>
            </div>
          </div>
          )}
        </details>

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
      </section>
    )
  },
)

export default NotificationsOutboundSection
