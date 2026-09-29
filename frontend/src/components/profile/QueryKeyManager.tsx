import { useEffect, useState } from 'react'
import { KeyRound, Loader2, Plus, ShieldOff } from 'lucide-react'
import { toast } from 'sonner'

import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { authApi, type QueryKeyCategory, type QueryKeyItem, type QueryKeyValidity } from '@/api/auth'
import { formatDateTime } from '@/utils/datetime'
/**
 * 变量查询密钥管理（PAT 式）：环境变量 / 隐私变量两个分区各挂一份。
 *
 * - 密钥跟用户不跟变量、按类别隔离、多把并存：外部流水线（n8n）持某类别
 *   有效密钥即可 GET 对应公开端点，读取该用户该类别全部变量明文。
 * - 明文仅创建时一次性展示（平台只落 sha256 哈希）：展示弹窗由父层
 *   ProfileModal 托管（onCreated 上抛），切 tab 卸载本组件也不丢明文；
 *   复制交互按 AGENTS.md §5 副作用验收：提示如实（"已尝试复制"）+
 *   输入框自动全选的手动 Cmd+C / Ctrl+C 兜底。
 */

const VALIDITY_OPTIONS: Array<{ value: QueryKeyValidity; label: string }> = [
  { value: '1d', label: '1 天' },
  { value: '7d', label: '1 周' },
  { value: '30d', label: '1 个月' },
  { value: '90d', label: '3 个月' },
  { value: '365d', label: '1 年' },
  { value: 'permanent', label: '永久' },
]

const CATEGORY_META: Record<QueryKeyCategory, { label: string; publicPath: string }> = {
  env: { label: '环境变量', publicPath: '/api/public/env-vars' },
  privacy: { label: '隐私变量', publicPath: '/api/public/privacy-vars' },
}

const inputClass =
  'w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 py-2 text-sm text-[var(--color-text-primary)] outline-none transition-colors placeholder:text-[var(--color-text-tertiary)] focus:border-[var(--color-primary)] disabled:cursor-not-allowed disabled:bg-[var(--color-muted)] disabled:text-[var(--color-text-secondary)]'

function errorMessage(error: any, fallback: string) {
  const detail = error?.detail ?? error?.message
  if (typeof detail === 'string' && detail) return detail
  if (detail && typeof detail === 'object' && typeof detail.message === 'string') return detail.message
  return fallback
}

function formatTime(iso: string | null | undefined): string {
  // 全站日期展示唯一入口（utils/datetime）：库里的时间无 Z 后缀、语义是
  // UTC，直接 new Date 会被按本地时区解析（上海慢 8 小时）。
  return formatDateTime(iso, { seconds: true, fallback: '—' })
}

function keyStatus(item: QueryKeyItem): 'active' | 'expired' | 'revoked' {
  if (item.revoked_at) return 'revoked'
  if (item.expires_at && new Date(item.expires_at).getTime() <= Date.now()) return 'expired'
  return 'active'
}

const STATUS_META: Record<'active' | 'expired' | 'revoked', { label: string; variant: 'success' | 'warning' | 'secondary' }> = {
  active: { label: '有效', variant: 'success' },
  expired: { label: '已过期', variant: 'warning' },
  revoked: { label: '已吊销', variant: 'secondary' },
}

export default function QueryKeyManager({
  category,
  onCreated,
}: {
  category: QueryKeyCategory
  /** 生成成功后把明文上抛给父层的一次性展示弹窗（本组件卸载不丢明文）。 */
  onCreated: (key: string) => void
}) {
  const meta = CATEGORY_META[category]

  const [keys, setKeys] = useState<QueryKeyItem[]>([])
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [newName, setNewName] = useState('')
  const [validity, setValidity] = useState<QueryKeyValidity>('365d')
  const [revokeTarget, setRevokeTarget] = useState<QueryKeyItem | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    authApi.listQueryKeys(category)
      .then(items => { if (!cancelled) setKeys(Array.isArray(items) ? items : []) })
      .catch(error => {
        if (!cancelled) toast.error(errorMessage(error, '查询密钥加载失败'))
      })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [category])

  const createKey = async () => {
    const name = newName.trim()
    if (name.length > 64) { toast.error('密钥名称不能超过 64 个字符'); return }
    setBusy(true)
    try {
      const created = await authApi.createQueryKey(category, name, validity)
      setKeys(current => [created, ...current])
      setNewName('')
      toast.success('已生成。密钥明文仅此一次展示，请立即复制保存')
      onCreated(created.key)
    } catch (error) {
      toast.error(errorMessage(error, '生成查询密钥失败'))
    } finally {
      setBusy(false)
    }
  }

  const revokeKey = async (item: QueryKeyItem) => {
    setBusy(true)
    try {
      await authApi.revokeQueryKey(item.id)
      setKeys(current => current.map(k => k.id === item.id ? { ...k, revoked_at: new Date().toISOString() } : k))
      toast.success('已吊销，使用该密钥的调用将立即失效')
    } catch (error) {
      toast.error(errorMessage(error, '吊销查询密钥失败'))
    } finally {
      setBusy(false)
      setRevokeTarget(null)
    }
  }

  return (
    <section aria-label={`${meta.label}查询密钥`} className="mt-5 border-t border-[var(--color-border)] pt-4">
      <h4 className="flex items-center gap-1.5 text-sm font-medium text-[var(--color-text-primary)]">
        <KeyRound size={14} />查询密钥（只读 API）
      </h4>
      <p className="mt-0.5 text-xs text-[var(--color-text-tertiary)]">
        外部流水线（如 n8n）可请求 <code className="font-mono">{meta.publicPath}</code> 并在请求头携带
        {' '}<code className="font-mono">Authorization: Bearer 密钥</code> 读取你的全部{meta.label}。密钥跟账号不跟单个变量，
        可同时持有多把，各自独立吊销；泄露时立即吊销即可止损。
      </p>

      <div className="mt-3 flex flex-wrap items-end gap-2">
        <label className="min-w-40 flex-1">
          <span className="mb-1.5 block text-xs font-medium text-[var(--color-text-secondary)]">密钥名称（备注用）</span>
          <input
            value={newName}
            onChange={event => setNewName(event.target.value)}
            onKeyDown={event => { if (event.key === 'Enter') void createKey() }}
            placeholder="如 n8n 流水线"
            aria-label={`新${meta.label}查询密钥名称`}
            className={inputClass}
          />
        </label>
        <div className="w-32">
          <span className="mb-1.5 block text-xs font-medium text-[var(--color-text-secondary)]">有效期</span>
          <Select value={validity} onValueChange={value => setValidity(value as QueryKeyValidity)}>
            <SelectTrigger aria-label={`新${meta.label}查询密钥有效期`} className="h-[38px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {VALIDITY_OPTIONS.map(option => (
                <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <Button size="sm" loading={busy} disabled={loading} onClick={() => void createKey()}>
          <Plus size={13} />生成密钥
        </Button>
      </div>

      <div className="mt-3 space-y-2">
        {loading ? (
          <div className="flex items-center gap-2 px-1 py-3 text-xs text-[var(--color-text-tertiary)]">
            <Loader2 size={14} className="animate-spin" />正在加载查询密钥...
          </div>
        ) : keys.length === 0 ? (
          <p className="px-1 py-3 text-xs text-[var(--color-text-tertiary)]">尚无查询密钥，在上方生成</p>
        ) : (
          <ul className="space-y-1">
            {keys.map(item => {
              const status = keyStatus(item)
              const statusMeta = STATUS_META[status]
              return (
                <li key={item.id} className="rounded-lg border border-[var(--color-border)] px-3 py-2">
                  <div className="flex items-center justify-between gap-2">
                    <div className="min-w-0">
                      <p className="truncate text-sm text-[var(--color-text-primary)]">
                        {item.name || <span className="text-[var(--color-text-tertiary)]">未命名</span>}
                        {' '}<span className="font-mono text-xs text-[var(--color-text-tertiary)]">{item.key_prefix}…</span>
                      </p>
                      <p className="text-xs text-[var(--color-text-tertiary)]">
                        创建 {formatTime(item.created_at)} · 有效期至 {item.expires_at ? formatTime(item.expires_at) : '永久'} · 最近使用 {formatTime(item.last_used_at)}
                      </p>
                    </div>
                    <div className="flex shrink-0 items-center gap-1">
                      <Badge variant={statusMeta.variant}>{statusMeta.label}</Badge>
                      {status === 'active' && (
                        <Button
                          variant="ghost"
                          size="icon-sm"
                          aria-label={`吊销查询密钥 ${item.name || item.key_prefix}`}
                          disabled={busy}
                          onClick={() => setRevokeTarget(item)}
                        >
                          <ShieldOff size={14} />
                        </Button>
                      )}
                    </div>
                  </div>
                </li>
              )
            })}
          </ul>
        )}
      </div>

      <ConfirmDialog
        open={revokeTarget !== null}
        onClose={() => setRevokeTarget(null)}
        onConfirm={() => revokeTarget && void revokeKey(revokeTarget)}
        title="吊销查询密钥"
        variant="danger"
        confirmText="吊销"
        loading={busy}
        description={revokeTarget ? (
          <>
            确定吊销 <span className="font-mono">{revokeTarget.key_prefix}…</span>（{revokeTarget.name || '未命名'}）？
            使用该密钥的外部流水线将立即失去访问能力，且不可恢复；如需继续使用请重新生成新密钥。
          </>
        ) : null}
      />
    </section>
  )
}
