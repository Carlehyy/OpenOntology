import { useDeferredValue, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, Ban, Check, ChevronLeft, ChevronRight, Copy, KeyRound, Loader2,
  Plus, Plug, RotateCcw, Search, ShieldCheck, Terminal, X,
} from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { LoadingState } from '@/components/ui/LoadingState'
import { ConfirmModal } from '@/components/ui/Modal'
import { eventsApi, type IngestKey, type IngestKeyListResp } from '@/api/events'
import { writeTextToClipboard } from '@/utils/clipboard'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

const KEY_PAGE_SIZE = 5

function fmt(iso: string | null): string {
  if (!iso) return '从未'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '时间未知'
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function CopyBtn({ text, small }: { text: string; small?: boolean }) {
  const [done, setDone] = useState(false)
  return (
    <button
      type="button"
      onClick={() => {
        void writeTextToClipboard(text).then(() => {
          setDone(true)
          window.setTimeout(() => setDone(false), 1500)
        }).catch(() => setDone(false))
      }}
      className={`inline-flex items-center gap-1 rounded-md text-[var(--color-success)] transition-colors hover:text-[var(--color-success)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${small ? 'text-xs' : 'text-sm'}`}
    >
      {done ? <Check size={13} /> : <Copy size={13} />}{done ? '已复制' : '复制'}
    </button>
  )
}

export default function IngestKeysDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const [scope, setScope] = useState('')
  const [newKey, setNewKey] = useState<IngestKey | null>(null)
  const [error, setError] = useState('')
  const [revokeTarget, setRevokeTarget] = useState<IngestKey | null>(null)
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState<'all' | 'active' | 'revoked'>('all')
  const [sourceSystem, setSourceSystem] = useState('')
  const [page, setPage] = useState(1)
  const deferredSearch = useDeferredValue(search.trim())
  const deferredSourceSystem = useDeferredValue(sourceSystem.trim())

  const endpoint = `${window.location.origin}/api/v2/ingest/events`
  const keyQuery = useQuery<IngestKeyListResp>({
    queryKey: ['ingest-keys', { q: deferredSearch, status, sourceSystem: deferredSourceSystem, page }],
    queryFn: () => eventsApi.listKeys({
      q: deferredSearch || undefined,
      status,
      sourceSystem: deferredSourceSystem || undefined,
      page,
      pageSize: KEY_PAGE_SIZE,
    }),
    enabled: open,
    placeholderData: previous => previous,
  })

  const keys = keyQuery.data?.items || []
  const total = keyQuery.data?.total || 0
  const totalPages = Math.max(1, Math.ceil(total / KEY_PAGE_SIZE))
  const hasFilters = Boolean(search.trim() || sourceSystem.trim() || status !== 'all')

  useEffect(() => setPage(1), [deferredSearch, deferredSourceSystem, status])
  useEffect(() => {
    if (page > totalPages) setPage(totalPages)
  }, [page, totalPages])

  const createMutation = useMutation({
    mutationFn: () => eventsApi.createKey(name.trim(), scope.trim() || undefined),
    onSuccess: key => {
      setNewKey(key)
      setName('')
      setScope('')
      setError('')
      setPage(1)
      queryClient.invalidateQueries({ queryKey: ['ingest-keys'] })
    },
    onError: (cause: any) => setError(cause?.detail || cause?.message || '创建失败（需要管理员权限）'),
  })

  const revokeMutation = useMutation({
    mutationFn: (id: string) => eventsApi.revokeKey(id),
    onSuccess: () => {
      setError('')
      setRevokeTarget(null)
      queryClient.invalidateQueries({ queryKey: ['ingest-keys'] })
    },
    onError: (cause: any) => setError(cause?.detail || cause?.message || '密钥吊销失败'),
  })

  const clearFilters = () => {
    setSearch('')
    setStatus('all')
    setSourceSystem('')
    setPage(1)
  }

  if (!open) return null

  const curl = `curl -X POST "${endpoint}" \\
  -H "X-API-Key: <你的密钥>" \\
  -H "Content-Type: application/json" \\
  -d '{
    "title": "产线停机",
    "event_type": "设备异常",
    "severity": "high",
    "source_system": "MES",
    "source_ref": "WO-2026-0007",
    "occurred_at": "2026-07-07T09:12:00Z",
    "payload": { "line": "A3", "code": "E502" }
  }'`

  return createPortal(
    <div className="fixed inset-0 z-[80] flex justify-end">
      <div className="absolute inset-0 bg-[var(--color-bg-overlay)]" onClick={onClose} />
      <aside className="anim-drawer-in relative flex w-full max-w-2xl flex-col border-l border-border bg-card shadow-xl">
        <header className="flex shrink-0 items-center justify-between border-b border-border px-6 py-4">
          <div className="flex items-center gap-2">
            <Plug size={18} className="text-[var(--color-success)]" />
            <h2 className="text-lg font-semibold text-foreground">API 接入 · 第三方上传</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="flex h-9 w-9 items-center justify-center rounded-lg text-[var(--color-text-tertiary)] transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            aria-label="关闭接入管理"
          >
            <X size={20} />
          </button>
        </header>

        <div className="flex-1 space-y-6 overflow-y-auto px-6 py-5">
          <section>
            <h3 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              <Terminal size={13} /> 上传端点
            </h3>
            <div className="mb-2 flex items-center gap-2 rounded-lg border border-border bg-muted px-3 py-2">
              <code className="flex-1 break-all text-xs text-foreground">POST {endpoint}</code>
              <CopyBtn text={endpoint} small />
            </div>
            <div className="relative rounded-lg border border-border bg-muted p-3">
              <div className="absolute right-2 top-2"><CopyBtn text={curl.replace(/\\\n\s*/g, ' ')} small /></div>
              <pre className="overflow-x-auto whitespace-pre font-mono text-xs text-muted-foreground">{curl}</pre>
            </div>
            <p className="mt-2 text-xs leading-relaxed text-[var(--color-text-tertiary)]">
              批量上传时，请求体传 <code>{'{ "events": [ ... ] }'}</code>。字段 <code>source_system</code> 与 <code>source_ref</code>
              构成幂等键，重复投递同一 <code>source_ref</code> 不会重复登记。
            </p>
          </section>

          <section className="border-t border-border pt-5">
            <h3 className="mb-3 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              <KeyRound size={13} /> 生成密钥（管理员）
            </h3>
            {newKey?.plaintextKey && (
              <div className="mb-3 rounded-lg border border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-[var(--color-success-bg)] p-3">
                <div className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-[var(--color-success)]">
                  <ShieldCheck size={13} /> 密钥“{newKey.name}”已生成，请立即复制保存——仅此一次展示
                </div>
                <div className="flex items-center gap-2 rounded-md bg-card px-2 py-1.5">
                  <code className="flex-1 break-all text-xs text-foreground">{newKey.plaintextKey}</code>
                  <CopyBtn text={newKey.plaintextKey} small />
                </div>
              </div>
            )}
            {error && (
              <div className="mb-3 flex items-center gap-2 rounded-lg bg-[var(--color-danger-bg)] px-3 py-2 text-xs text-[var(--color-danger)]">
                <AlertTriangle size={13} /> {error}
              </div>
            )}
            <div className="flex items-end gap-2">
              <Input label="密钥名（= 第三方来源标识）" value={name} onChange={event => setName(event.target.value)}
                placeholder="如：MES产线网关" className="flex-1" />
              <Input label="限定来源系统（可选）" value={scope} onChange={event => setScope(event.target.value)}
                placeholder="如：MES" className="flex-1" />
              <Button onClick={() => createMutation.mutate()} loading={createMutation.isPending} disabled={!name.trim()}>
                <Plus size={14} /> 生成
              </Button>
            </div>
          </section>

          <section className="border-t border-border pt-5">
            <div className="mb-3 flex items-center justify-between gap-3">
              <div>
                <h3 className="text-sm font-semibold text-foreground">已有密钥</h3>
                <p className="mt-0.5 text-xs text-[var(--color-text-tertiary)]">按名称、前缀、状态或来源系统查询</p>
              </div>
              <div className="flex items-center gap-2 text-xs tabular-nums text-[var(--color-text-tertiary)]">
                {keyQuery.isFetching && !keyQuery.isLoading && <Loader2 size={13} className="animate-spin text-[var(--color-success)]" />}
                共 {total} 把
              </div>
            </div>

            <div className="mb-3 grid grid-cols-1 gap-2 sm:grid-cols-[minmax(0,1.5fr)_120px_minmax(0,1fr)_36px]">
              <label className="relative">
                <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-[var(--color-text-tertiary)]" />
                <input
                  value={search}
                  onChange={event => setSearch(event.target.value)}
                  placeholder="搜索名称、前缀或来源"
                  className="h-9 w-full rounded-lg border border-border bg-card pl-8 pr-3 text-xs text-foreground outline-none transition focus-visible:ring-2 focus-visible:ring-ring"
                />
              </label>
              <Select
                value={status || '__all__'}
                onValueChange={value => setStatus((value === '__all__' ? '' : value) as typeof status)}
              >
                <SelectTrigger aria-label="密钥状态" className="h-9 w-fit min-w-32 rounded-lg bg-card px-2.5 text-xs">
                  <SelectValue placeholder="全部状态" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="__all__">全部状态</SelectItem>
                  <SelectItem value="active">有效</SelectItem>
                  <SelectItem value="revoked">已吊销</SelectItem>
                </SelectContent>
              </Select>
              <input
                value={sourceSystem}
                onChange={event => setSourceSystem(event.target.value)}
                placeholder="来源系统，如 MES"
                className="h-9 rounded-lg border border-border bg-card px-3 text-xs text-foreground outline-none transition focus-visible:ring-2 focus-visible:ring-ring"
              />
              <button
                type="button"
                onClick={clearFilters}
                disabled={!hasFilters}
                className="flex h-9 w-9 items-center justify-center rounded-lg border border-border bg-card text-[var(--color-text-tertiary)] transition-colors hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
                title="清除筛选"
                aria-label="清除密钥筛选"
              >
                <RotateCcw size={14} />
              </button>
            </div>

            {keyQuery.isLoading ? (
              <LoadingState />
            ) : keyQuery.isError ? (
              <div className="rounded-xl border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-4 py-10 text-center text-sm text-[var(--color-danger)]">
                密钥列表加载失败，请确认当前账号具备管理员权限
              </div>
            ) : keys.length ? (
              <div className="space-y-2">
                {keys.map(key => {
                  const revoked = !key.enabled || Boolean(key.revokedAt)
                  return (
                    <article key={key.id} className={`rounded-xl border px-3 py-3 transition-colors ${revoked ? 'border-border bg-muted' : 'border-border bg-card hover:border-[color-mix(in_srgb,var(--color-success)_35%,transparent)]'}`}>
                      <div className="flex items-center gap-3">
                        <span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg ${revoked ? 'bg-muted text-[var(--color-text-tertiary)]' : 'bg-[var(--color-success-bg)] text-[var(--color-success)]'}`}>
                          <KeyRound size={15} />
                        </span>
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-2">
                            <span className="truncate text-sm font-medium text-foreground">{key.name}</span>
                            {key.allowedSourceSystem && (
                              <span className="shrink-0 rounded-md bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                                限 {key.allowedSourceSystem}
                              </span>
                            )}
                            <span className={`shrink-0 text-[10px] font-medium ${revoked ? 'text-[var(--color-danger)]' : 'text-[var(--color-success)]'}`}>
                              {revoked ? '已吊销' : '有效'}
                            </span>
                          </div>
                          <div className="mt-0.5 text-xs text-[var(--color-text-tertiary)]">
                            创建 {fmt(key.createdAt)} · 最近使用 {fmt(key.lastUsedAt)}
                          </div>
                        </div>
                        {!revoked && (
                          <button
                            type="button"
                            onClick={() => setRevokeTarget(key)}
                            disabled={revokeMutation.isPending}
                            className="inline-flex shrink-0 items-center gap-1 rounded-lg px-2 py-1.5 text-xs text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)] disabled:opacity-40"
                          >
                            <Ban size={13} /> 吊销
                          </button>
                        )}
                      </div>
                      {!revoked && (
                        <div className="mt-2 font-mono text-[11px] text-[var(--color-text-tertiary)]">
                          {key.keyPrefix}…（明文仅生成时展示一次，遗失请吊销后重新生成）
                        </div>
                      )}
                    </article>
                  )
                })}
              </div>
            ) : (
              <div className="rounded-xl border border-dashed border-border px-4 py-10 text-center">
                <KeyRound size={22} className="mx-auto text-[var(--color-text-tertiary)]" />
                <p className="mt-2 text-sm font-medium text-muted-foreground">{hasFilters ? '没有匹配的密钥' : '还没有密钥'}</p>
                <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">{hasFilters ? '请调整筛选条件后重试' : '可在上方生成一把供第三方系统使用'}</p>
                {hasFilters && (
                  <button type="button" onClick={clearFilters} className="mt-3 text-xs font-medium text-[var(--color-success)] hover:underline">
                    清除筛选
                  </button>
                )}
              </div>
            )}

            {!keyQuery.isLoading && !keyQuery.isError && total > 0 && (
              <div className="mt-3 flex items-center justify-between border-t border-border pt-3">
                <span className="text-xs tabular-nums text-[var(--color-text-tertiary)]">
                  显示 {(page - 1) * KEY_PAGE_SIZE + 1}–{Math.min(page * KEY_PAGE_SIZE, total)} / {total}
                </span>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => setPage(current => Math.max(1, current - 1))}
                    disabled={page <= 1 || keyQuery.isFetching}
                    className="flex h-8 w-8 items-center justify-center rounded-lg border border-border bg-card text-muted-foreground transition-colors hover:bg-muted disabled:opacity-40"
                    aria-label="上一页密钥"
                  >
                    <ChevronLeft size={15} />
                  </button>
                  <span className="min-w-16 text-center text-xs tabular-nums text-muted-foreground">{page} / {totalPages}</span>
                  <button
                    type="button"
                    onClick={() => setPage(current => Math.min(totalPages, current + 1))}
                    disabled={page >= totalPages || keyQuery.isFetching}
                    className="flex h-8 w-8 items-center justify-center rounded-lg border border-border bg-card text-muted-foreground transition-colors hover:bg-muted disabled:opacity-40"
                    aria-label="下一页密钥"
                  >
                    <ChevronRight size={15} />
                  </button>
                </div>
              </div>
            )}
          </section>
        </div>
      </aside>
      <ConfirmModal
        open={Boolean(revokeTarget)}
        onClose={() => setRevokeTarget(null)}
        onConfirm={() => { if (revokeTarget) revokeMutation.mutate(revokeTarget.id) }}
        title="吊销密钥"
        description={revokeTarget ? `确认吊销密钥“${revokeTarget.name}”？吊销后使用该密钥的第三方系统将立即无法上报事件，此操作不可恢复。` : undefined}
        confirmText="确认吊销"
        variant="danger"
        loading={revokeMutation.isPending}
      />
    </div>,
    document.body,
  )
}
