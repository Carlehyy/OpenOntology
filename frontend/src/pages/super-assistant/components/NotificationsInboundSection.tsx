import { forwardRef, useEffect, useImperativeHandle, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Copy, Inbox, KeyRound, Loader2, Zap } from 'lucide-react'
import { toast } from 'sonner'

import { absoluteFileUrl } from '@/api/fileAssets'
import { notificationsApi, type NotificationIngestKey } from '@/api/notifications'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { formatDateTime } from '@/utils/datetime'
import { writeTextToClipboard } from '@/utils/clipboard'

import { errorText } from './assistantPanelUtils'
import { inputClass } from './notificationsConfigShared'

/**
 * 渠道配置 · 消息入口分区（外部 → 平台）：投递接入（端点 + 调用示例 + 密钥签发/吊销/链路测试）。
 * 从 NotificationsDialog 原样拆出（行为不变）：签发表单默认收起、已吊销密钥折叠、
 * 密钥明文仅签发时展示一次，弹窗关闭即清除（“仅此一次”承诺不因重开而失效）。
 */
export interface InboundSectionHandle {
  /** Esc 分层回退：有展开的签发表单则收起并返回 true，交由外层决定是否继续冒泡 */
  collapseTransient: () => boolean
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

interface NotificationsInboundSectionProps {
  /** 弹窗开关：关闭时清理密钥明文与表单临时态 */
  open: boolean
  /** 链路测试成功会真实入站一条消息：刷新列表与徽章 */
  invalidateLists: () => void
}

const NotificationsInboundSection = forwardRef<InboundSectionHandle, NotificationsInboundSectionProps>(
  function NotificationsInboundSection({ open, invalidateLists }, ref) {
    const queryClient = useQueryClient()
    const [keyName, setKeyName] = useState('')
    const [keyScope, setKeyScope] = useState('')
    const [mintedKey, setMintedKey] = useState<NotificationIngestKey | null>(null)
    const [revokingKey, setRevokingKey] = useState<NotificationIngestKey | null>(null)
    const [testingKeyId, setTestingKeyId] = useState<string | null>(null)
    // 链路测试二次确认（真实投递到所有启用渠道，误触代价高）
    const [confirmingKeyTest, setConfirmingKeyTest] = useState<NotificationIngestKey | null>(null)
    // 签发表单收起态：默认收起，卡头按钮展开（降低入口卡的常态噪音）
    const [mintFormOpen, setMintFormOpen] = useState(false)

    const ingestKeys = useQuery({
      queryKey: ['notifications', 'ingest-keys'],
      queryFn: notificationsApi.ingestKeys.list,
      enabled: open,
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

    // 弹窗关闭即清除密钥明文与表单临时态（“仅此一次”承诺不因重开而失效）
    useEffect(() => {
      if (!open) {
        setMintedKey(null)
        setMintFormOpen(false)
        setConfirmingKeyTest(null)
        setKeyName('')
        setKeyScope('')
      }
    }, [open])

    useImperativeHandle(ref, () => ({
      collapseTransient: () => {
        if (!mintFormOpen) return false
        setMintFormOpen(false)
        return true
      },
    }), [mintFormOpen])

    const activeIngestKeys = (ingestKeys.data ?? []).filter(key => key.enabled)
    const revokedIngestKeys = (ingestKeys.data ?? []).filter(key => !key.enabled)

    return (
      <section aria-label="消息入口" data-notifications-group="inbound" className="space-y-3">
        <div className="flex items-center gap-2.5 px-0.5">
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-brand-soft text-brand-ink" aria-hidden="true">
            <Inbox size={15} />
          </span>
          <div className="min-w-0">
            <h3 className="text-sm font-semibold text-[var(--color-text-primary)]">消息入口</h3>
            <p className="mt-0.5 text-xs leading-5 text-[var(--color-text-tertiary)]">外部系统把消息投递进平台：签发密钥交给对方，消息自动进入站内并转发。</p>
          </div>
        </div>
        <section aria-label="投递接入" data-notifications-keys-panel className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-base)]">
          <header className="flex items-start justify-between gap-3 border-b border-[var(--color-border)] px-4 py-3">
            <div className="min-w-0">
              <h4 className="text-sm font-semibold text-[var(--color-text-primary)]">投递接入 · X-API-Key</h4>
              <p className="mt-0.5 text-xs leading-5 text-[var(--color-text-tertiary)]">
                密钥明文仅签发时展示一次；调用示例可直接复制给外部系统。
              </p>
            </div>
            <div className="mt-0.5 flex shrink-0 items-center gap-2">
              <span className="rounded-full bg-[var(--color-bg-hover)] px-2 py-0.5 text-xs tabular-nums text-[var(--color-text-tertiary)]">
                {activeIngestKeys.length} 枚有效
              </span>
              <Button
                size="sm"
                variant="outline"
                onClick={() => setMintFormOpen(value => !value)}
                aria-expanded={mintFormOpen}
                className="rounded-lg"
                data-notifications-key-mint-toggle
              >
                <KeyRound size={12} /> {mintFormOpen ? '收起' : '签发密钥'}
              </Button>
            </div>
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
                  className="flex shrink-0 items-center gap-1 rounded-md border border-[var(--color-border)] bg-[var(--color-bg-base)] px-2 py-1 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  data-notifications-copy-curl
                >
                  <Copy size={12} /> 复制调用示例
                </button>
              </div>
              <pre className="scrollbar-none mt-2 overflow-x-auto rounded-md bg-[var(--color-code-bg)] px-2.5 py-2 font-mono text-[11px] leading-5 text-[var(--color-code-fg)]">{ingestCurlSample()}</pre>
            </div>

            {mintedKey && (
              <div role="alert" className="rounded-lg border border-[var(--color-warning)] bg-[var(--color-warning-bg)] p-3" data-notifications-key-plaintext>
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

            {/* 签发表单默认收起：入口卡常态只看端点与密钥清单，需要时再展开 */}
            {mintFormOpen && (
              <div className="flex flex-wrap items-end gap-2 rounded-lg border border-dashed border-[var(--color-border)] p-3">
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
                <Button
                  size="sm"
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
                  loading={mintKeyMutation.isPending}
                  className="rounded-lg"
                  data-notifications-key-mint
                >
                  <KeyRound size={12} /> 签发密钥
                </Button>
              </div>
            )}

            {ingestKeys.isLoading ? (
              <div className="flex items-center justify-center gap-2 py-6 text-xs text-[var(--color-text-tertiary)]">
                <Loader2 size={12} className="animate-spin" /> 加载中…
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
            ) : activeIngestKeys.length === 0 ? (
              <p className="rounded-lg border border-dashed border-[var(--color-border)] px-3 py-5 text-center text-xs leading-5 text-[var(--color-text-tertiary)]">
                还没有有效密钥。点右上「签发密钥」，把密钥与调用示例交给外部系统即可开始投递。
              </p>
            ) : (
              <ul className="space-y-1">
                {activeIngestKeys.map(key => (
                  <li
                    key={key.id}
                    data-notifications-key-item={key.id}
                    className="flex items-center gap-2 rounded-lg px-2 py-2 transition-colors hover:bg-[var(--color-bg-hover)]"
                  >
                    <KeyRound size={15} className="shrink-0 text-[var(--color-text-secondary)]" />
                    <div className="min-w-0 flex-1">
                      {/* 名称与密钥前缀分行：此前同行拼接（如 11111ob_notif_…）连成一片难扫读 */}
                      <p className="truncate text-sm font-medium text-[var(--color-text-primary)]">{key.name}</p>
                      <p className="truncate text-xs text-[var(--color-text-tertiary)]">
                        <span className="font-mono text-[11px]">{key.keyPrefix}_…</span>
                        {key.allowedSourceSystem ? ` · 限定来源：${key.allowedSourceSystem}` : ''} · 最近使用：{key.lastUsedAt ? formatDateTime(key.lastUsedAt) : '未使用'}
                      </p>
                    </div>
                    {/* 链路测试会真实投递并扇出到所有启用渠道：幽灵样式 + 二次确认防误触 */}
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <button
                          type="button"
                          onClick={() => setConfirmingKeyTest(key)}
                          disabled={keyTestMutation.isPending && testingKeyId === key.id}
                          aria-label={`以密钥 ${key.name} 测试入口链路`}
                          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
                          data-notifications-key-test
                        >
                          {keyTestMutation.isPending && testingKeyId === key.id
                            ? <Loader2 size={12} className="animate-spin" />
                            : <Zap size={15} />}
                        </button>
                      </TooltipTrigger>
                      <TooltipContent align="end">以密钥 {key.name} 测试入口链路</TooltipContent>
                    </Tooltip>
                    <button
                      type="button"
                      onClick={() => setRevokingKey(key)}
                      disabled={revokeKeyMutation.isPending}
                      className="flex shrink-0 items-center gap-1 rounded-md px-2 py-1.5 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
                      data-notifications-key-revoke
                    >
                      吊销
                    </button>
                  </li>
                ))}
              </ul>
            )}
            {/* 已吊销密钥默认折叠：留痕可查，但不占入口卡常态空间 */}
            {revokedIngestKeys.length > 0 && (
              <details className="rounded-lg border border-[var(--color-border)] px-3 py-2" data-notifications-revoked-keys>
                <summary className="cursor-pointer list-none text-xs text-[var(--color-text-tertiary)] transition-colors hover:text-[var(--color-text-secondary)]">
                  已吊销密钥（{revokedIngestKeys.length}）
                </summary>
                <ul className="mt-2 space-y-1">
                  {revokedIngestKeys.map(key => (
                    <li
                      key={key.id}
                      data-notifications-key-item={key.id}
                      className="flex items-center gap-2 rounded-lg px-2 py-2 opacity-75"
                    >
                      <KeyRound size={15} className="shrink-0 text-[var(--color-text-tertiary)]" />
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm font-medium text-[var(--color-text-primary)]">{key.name}</p>
                        <p className="truncate text-xs text-[var(--color-text-tertiary)]">
                          <span className="font-mono text-[11px]">{key.keyPrefix}_…</span> · {key.revokedAt ? `已吊销：${formatDateTime(key.revokedAt)}` : '已吊销（时间未知）'}
                        </p>
                      </div>
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        </section>

        <ConfirmDialog
          open={confirmingKeyTest !== null}
          onClose={() => setConfirmingKeyTest(null)}
          onConfirm={() => {
            if (confirmingKeyTest) {
              setTestingKeyId(confirmingKeyTest.id)
              keyTestMutation.mutate(confirmingKeyTest.id)
            }
            setConfirmingKeyTest(null)
          }}
          loading={keyTestMutation.isPending}
          title="发起入口链路测试？"
          description={`将用「${confirmingKeyTest?.name ?? ''}」真实投递一条测试消息，并立即转发到所有启用渠道（外部收件方会收到）。`}
          confirmText="开始测试"
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
      </section>
    )
  },
)

export default NotificationsInboundSection
