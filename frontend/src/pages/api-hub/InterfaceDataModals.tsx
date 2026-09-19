import { formatDateTime } from '@/utils/datetime'
import { useEffect, useRef, useState } from 'react'
import {
  CheckCircle2, Copy, Download, FileJson, KeyRound, Pencil,
  Plus, ShieldCheck, Trash2, Upload,
} from 'lucide-react'
import {
  apiError, apiHub, type HubInterface, type ProxyInfo,
  type ProxyKey, type ProxyKeyPayload,
} from '@/api/apiHub'
import { Button } from '@/components/ui/Button'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { writeTextToClipboard } from '@/utils/clipboard'
import { summarizeBackup, validateProxyKeySchedule } from './interfaceUxHelpers'

interface SharedProps {
  interfaces: HubInterface[]
  reload: () => Promise<HubInterface[]>
  onError: (message: string) => void
}

export function ProxyKeysModal({ open, onClose, interfaces, onError }: Omit<SharedProps, 'reload'> & { open: boolean; onClose: () => void }) {
  const [info, setInfo] = useState<ProxyInfo | null>(null)
  const [keys, setKeys] = useState<ProxyKey[]>([])
  const [editing, setEditing] = useState<ProxyKey | null | undefined>(undefined)
  const [revealed, setRevealed] = useState('')
  const [deleteKey, setDeleteKey] = useState<ProxyKey | null>(null)
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(false)
  const [loadFailed, setLoadFailed] = useState(false)

  const load = async () => {
    setLoading(true)
    setLoadFailed(false)
    try {
      const [nextInfo, nextKeys] = await Promise.all([apiHub.proxyInfo(), apiHub.listProxyKeys()])
      setInfo(nextInfo); setKeys(nextKeys)
    } catch {
      setLoadFailed(true)
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => {
    if (!open) return
    setEditing(undefined); setRevealed('')
    void load()
  }, [open])

  const saveKey = async (payload: ProxyKeyPayload) => {
    setBusy(true)
    try {
      if (editing?.id) {
        await apiHub.updateProxyKey(editing.id, payload)
        setEditing(undefined)
      } else {
        const created = await apiHub.createProxyKey(payload)
        setRevealed(created.secret || '')
        setEditing(undefined)
      }
      await load()
    } catch (error) { onError(apiError(error)) }
    finally { setBusy(false) }
  }
  const toggle = async (key: ProxyKey) => {
    setBusy(true)
    try {
      await apiHub.updateProxyKey(key.id, { ...keyPayload(key), enabled: !key.enabled })
      await load()
    } catch (error) { onError(apiError(error)) }
    finally { setBusy(false) }
  }
  const remove = async () => {
    if (!deleteKey) return
    setBusy(true)
    try { await apiHub.deleteProxyKey(deleteKey.id); setDeleteKey(null); await load() }
    catch (error) { onError(apiError(error)) }
    finally { setBusy(false) }
  }

  return (
    <>
      <Dialog open={open} onOpenChange={next => { if (!next) onClose() }}>
        <DialogContent className="flex max-h-[85vh] w-[min(92vw,48rem)] flex-col overflow-hidden">
          <DialogHeader className="shrink-0">
            <div className="min-w-0">
              <DialogTitle>调用密钥</DialogTitle>
              <DialogDescription>管理对外已发布 HTTP 接口的调用凭证；第三方凭密钥调用已发布接口，可在此查看、停用或撤销。</DialogDescription>
            </div>
          </DialogHeader>
          <div className="min-h-0 flex-1 overflow-y-auto pr-1">
            {revealed ? <SecretView secret={revealed} info={info} onDone={() => setRevealed('')} />
              : editing !== undefined ? <ProxyKeyForm keyValue={editing} interfaces={interfaces} busy={busy} onCancel={() => setEditing(undefined)} onSave={saveKey} />
                : loading ? <KeysSkeleton />
                  : loadFailed ? (
                    <div role="alert" className="rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_30%,transparent)] bg-[var(--color-danger-bg)] px-4 py-6 text-center">
                      <p className="text-xs font-medium text-[var(--color-danger)]">加载调用密钥失败，请检查网络后重试。</p>
                      <Button size="sm" variant="outline" className="mt-3" onClick={() => void load()}>重试</Button>
                    </div>
                  ) : (
                    <div className="space-y-4">
                      <div className="flex items-center justify-between rounded-lg border border-border bg-card px-4 py-3">
                        <div className="flex items-center gap-3">
                          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-brand-soft text-brand-ink"><ShieldCheck size={17} /></div>
                          <div>
                            <div className="text-sm font-semibold">{keys.length} 把调用密钥</div>
                            <div className="text-[10px] text-[var(--color-text-tertiary)]">{info?.published.length || 0} 个已发布 HTTP 接口</div>
                          </div>
                        </div>
                        <Button size="sm" onClick={() => setEditing(null)}><Plus size={14} />创建密钥</Button>
                      </div>
                      {!keys.length ? (
                        <div className="rounded-lg border border-dashed border-[var(--color-border)] py-16 text-center text-xs text-[var(--color-text-tertiary)]">
                          <KeyRound size={26} className="mx-auto mb-3 opacity-50" />还没有调用密钥
                        </div>
                      ) : (
                        <div className="max-h-[52vh] space-y-2 overflow-y-auto">
                          {keys.map(key => {
                            const tone = key.status === 'active'
                              ? 'bg-brand-soft text-brand-ink'
                              : key.status === 'expired'
                                ? 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]'
                                : key.status === 'scheduled'
                                  ? 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]'
                                  : 'bg-muted text-muted-foreground'
                            return (
                              <div key={key.id} className="rounded-lg border border-[var(--color-border)] p-4">
                                <div className="flex items-start justify-between gap-4">
                                  <div>
                                    <div className="flex items-center gap-2">
                                      <span className="text-sm font-semibold">{key.name}</span>
                                      <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${tone}`}>{statusLabel(key.status)}</span>
                                    </div>
                                    <div className="mt-1 font-mono text-[11px] text-[var(--color-text-tertiary)]">{key.masked_key}</div>
                                  </div>
                                  <div className="flex gap-1">
                                    <Button variant="ghost" size="icon-sm" title="编辑" aria-label={`编辑密钥 ${key.name}`} onClick={() => setEditing(key)}><Pencil size={13} /></Button>
                                    <Button variant="ghost" size="sm" disabled={busy} onClick={() => toggle(key)}>{key.enabled ? '停用' : '启用'}</Button>
                                    <Button variant="ghost" size="icon-sm" title="撤销" aria-label={`撤销密钥 ${key.name}`} className="text-[var(--color-danger)]" onClick={() => setDeleteKey(key)}><Trash2 size={13} /></Button>
                                  </div>
                                </div>
                                <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-[10px] text-[var(--color-text-tertiary)]">
                                  <span>{key.scope_all ? '全部已发布接口' : `${key.interface_ids.length} 个指定接口`}</span>
                                  <span>{key.expires_at ? `有效期至 ${formatTime(key.expires_at)}` : '长期有效'}</span>
                                  <span>{key.last_used_at ? `最后调用 ${formatTime(key.last_used_at)}` : '尚未调用'}</span>
                                </div>
                              </div>
                            )
                          })}
                        </div>
                      )}
                    </div>
                  )}
          </div>
          <DialogFooter className="shrink-0 border-t border-border pt-4">
            <Button variant="outline" onClick={onClose}>关闭</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <ConfirmDialog open={Boolean(deleteKey)} onClose={() => setDeleteKey(null)} onConfirm={remove} loading={busy} variant="danger" title={`撤销密钥“${deleteKey?.name || ''}”？`} description="撤销后调用方将立即无法继续使用，且不能恢复。" confirmText="永久撤销" />
    </>
  )
}

function KeysSkeleton() {
  return (
    <div className="space-y-4" aria-label="正在加载调用密钥">
      <div className="flex items-center justify-between rounded-lg border border-border bg-card px-4 py-3">
        <div className="h-9 w-40 animate-pulse rounded bg-muted" />
        <div className="h-8 w-24 animate-pulse rounded bg-muted" />
      </div>
      {[0, 1, 2].map(index => <div key={index} className="h-20 animate-pulse rounded-lg border border-border bg-muted" />)}
    </div>
  )
}

export function SystemDataModal({ open, onClose, interfaces, reload, onError }: SharedProps & { open: boolean; onClose: () => void }) {
  const [name, setName] = useState(defaultBackupName)
  const [mode, setMode] = useState<'full' | 'partial'>('full')
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [includeSensitive, setIncludeSensitive] = useState(false)
  const [message, setMessage] = useState('')
  const [importPreview, setImportPreview] = useState<(ReturnType<typeof summarizeBackup> & { ok: true }) & { fileName: string } | null>(null)
  const [pendingPayload, setPendingPayload] = useState<unknown>(null)
  const [importError, setImportError] = useState('')
  const [importing, setImporting] = useState(false)

  const exportData = async () => {
    const ids = mode === 'partial' ? [...selected] : []
    if (mode === 'partial' && !ids.length) { onError('请至少选择一个接口'); return }
    try {
      const response = await apiHub.exportBackup({ name, mode, ids, include_sensitive: includeSensitive })
      const url = URL.createObjectURL(response.data)
      const link = document.createElement('a')
      link.href = url; link.download = `${name || '接口备份'}.json`; link.click()
      URL.revokeObjectURL(url)
      setMessage(`已导出 ${mode === 'full' ? interfaces.length : ids.length} 个接口`)
    } catch (error) { onError(apiError(error)) }
  }

  // 导入拆成「选择文件 → 本地校验与预览 → 明确开始还原」三步：
  // 先在本地展示文件版本、接口数量与影响说明，确认后才写入。
  const previewImport = async (file?: File) => {
    if (!file) return
    setImportError('')
    setMessage('')
    let parsed: unknown
    try {
      parsed = JSON.parse(await file.text())
    } catch {
      setImportError(`「${file.name}」不是有效 JSON 文件，请选择接口代理备份文件。`)
      return
    }
    const summary = summarizeBackup(parsed)
    if (!summary.ok) {
      setImportError(`「${file.name}」：${summary.error}`)
      return
    }
    setPendingPayload(parsed)
    setImportPreview({ ...summary, fileName: file.name })
  }

  const resetImport = () => {
    setImportPreview(null)
    setPendingPayload(null)
    setImportError('')
  }

  const confirmImport = async () => {
    if (!pendingPayload) return
    setImporting(true)
    try {
      const result = await apiHub.importBackup(pendingPayload)
      await reload()
      setMessage(`还原完成：导入 ${result.imported} 个，跳过 ${result.skipped} 个`)
      resetImport()
    } catch (error) { onError(apiError(error)) }
    finally { setImporting(false) }
  }

  return (
    <Dialog open={open} onOpenChange={next => { if (!next) onClose() }}>
      <DialogContent className="flex max-h-[85vh] w-[min(92vw,48rem)] flex-col overflow-hidden">
        <DialogHeader className="shrink-0">
          <div className="min-w-0">
            <DialogTitle>系统数据</DialogTitle>
            <DialogDescription>备份或还原接口清单；调用历史、W3 凭据和代理密钥不会写入备份。</DialogDescription>
          </div>
        </DialogHeader>
        <div className="min-h-0 flex-1 overflow-y-auto pr-1">
          {message && <div className="mb-4 rounded-md bg-[var(--color-success-bg)] px-3 py-2 text-xs text-[var(--color-success)]">{message}</div>}
          <div className="grid min-h-[430px] grid-cols-1 gap-6 sm:grid-cols-2">
            <section className="space-y-4 rounded-lg border border-[var(--color-border)] p-4">
              <div><h4 className="text-sm font-semibold">数据备份</h4><p className="mt-1 text-[11px] text-[var(--color-text-tertiary)]">将接口配置导出为可迁移 JSON 包。</p></div>
              <input value={name} aria-label="备份文件名称" onChange={event => setName(event.target.value)} className="h-9 w-full rounded-md border border-border bg-card px-3 text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring" />
              <div className="grid grid-cols-2 gap-2"><ModeButton active={mode === 'full'} onClick={() => setMode('full')} title="完整备份" subtitle={`${interfaces.length} 个接口`} /><ModeButton active={mode === 'partial'} onClick={() => setMode('partial')} title="部分备份" subtitle={`${selected.size} 个已选`} /></div>
              {mode === 'partial' && <div className="max-h-52 overflow-y-auto rounded-md border border-[var(--color-border)] p-2">{interfaces.map(item => <label key={item.id} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-xs hover:bg-[var(--color-bg-hover)]"><input type="checkbox" checked={selected.has(item.id!)} onChange={() => setSelected(current => { const next = new Set(current); if (next.has(item.id!)) next.delete(item.id!); else next.add(item.id!); return next })} /><span className="w-10 font-mono text-[10px]">{item.method}</span><span className="truncate">{item.name}</span></label>)}</div>}
              <label className={`flex items-start gap-2 rounded-md border px-3 py-2 text-[11px] ${includeSensitive ? 'border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] text-[var(--color-warning)]' : 'border-[var(--color-border)] text-[var(--color-text-secondary)]'}`}><input type="checkbox" checked={includeSensitive} onChange={event => setIncludeSensitive(event.target.checked)} className="mt-0.5" /><span><strong>包含请求 Header 和 Body 原值</strong><br />默认关闭。开启后备份可能含 API Key、Cookie 或业务敏感数据，请只存放在受控位置。</span></label>
              <Button onClick={exportData}><Download size={14} />导出备份</Button>
            </section>
            <section className="space-y-4 rounded-lg border border-[var(--color-border)] p-4">
              <div><h4 className="text-sm font-semibold">数据还原</h4><p className="mt-1 text-[11px] text-[var(--color-text-tertiary)]">以名称、方法和 URL 去重；导入后的 MCP 与 HTTP 发布状态统一保持关闭，需管理员重新确认。</p></div>
              {importError && (
                <div role="alert" className="rounded-md bg-[var(--color-danger-bg)] px-3 py-2 text-xs leading-5 text-[var(--color-danger)]">{importError}</div>
              )}
              {importPreview ? (
                <div className="space-y-3 rounded-lg border border-[var(--color-border)] bg-muted p-3 text-xs">
                  <div className="flex items-center gap-2 font-semibold text-foreground"><FileJson size={14} className="shrink-0 text-[var(--color-text-tertiary)]" /><span className="min-w-0 truncate">{importPreview.fileName}</span></div>
                  <dl className="space-y-1.5 text-[var(--color-text-secondary)]">
                    <div className="flex gap-2"><dt className="w-20 shrink-0 text-[var(--color-text-tertiary)]">备份名称</dt><dd className="min-w-0 flex-1 truncate">{importPreview.name}</dd></div>
                    <div className="flex gap-2"><dt className="w-20 shrink-0 text-[var(--color-text-tertiary)]">版本</dt><dd>v{importPreview.version}</dd></div>
                    <div className="flex gap-2"><dt className="w-20 shrink-0 text-[var(--color-text-tertiary)]">导出时间</dt><dd>{formatDateTime(importPreview.exportedAt, { seconds: true })}</dd></div>
                    <div className="flex gap-2"><dt className="w-20 shrink-0 text-[var(--color-text-tertiary)]">接口数量</dt><dd>{importPreview.interfaceCount} 个接口{importPreview.groupCount ? `（${importPreview.groupCount} 个分组）` : ''}</dd></div>
                    <div className="flex gap-2"><dt className="w-20 shrink-0 text-[var(--color-text-tertiary)]">敏感值</dt><dd>{importPreview.includesSensitive ? '包含 Header/Body 原值' : '不含敏感值'}</dd></div>
                  </dl>
                  <p className="rounded-md border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-2.5 py-2 text-[11px] leading-4 text-[var(--color-warning)]">
                    已有接口按名称、方法和 URL 去重跳过，不会被覆盖；导入项的发布状态统一保持关闭。请确认预览内容后再开始。
                  </p>
                  <div className="flex justify-end gap-2">
                    <Button variant="outline" size="sm" disabled={importing} onClick={resetImport}>重新选择</Button>
                    <Button size="sm" loading={importing} onClick={() => void confirmImport()}>开始还原</Button>
                  </div>
                </div>
              ) : (
                <label className="flex min-h-64 cursor-pointer flex-col items-center justify-center rounded-lg border border-dashed border-[var(--color-border-hover)] bg-card text-center hover:border-[var(--color-nav-bg)]">
                  <Upload size={28} className="mb-3 text-[var(--color-text-tertiary)]" />
                  <span className="text-xs font-medium">选择接口代理备份文件</span>
                  <span className="mt-1 text-[11px] text-[var(--color-text-tertiary)]">支持 .json，选择后先预览再确认还原</span>
                  <input type="file" accept=".json,application/json" className="hidden" onChange={event => { void previewImport(event.target.files?.[0]); event.currentTarget.value = '' }} />
                </label>
              )}
            </section>
          </div>
        </div>
        <DialogFooter className="shrink-0 border-t border-border pt-4">
          <Button variant="outline" onClick={onClose}>关闭</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function ModeButton({ active, onClick, title, subtitle }: { active: boolean; onClick: () => void; title: string; subtitle: string }) { return <button type="button" onClick={onClick} className={`rounded-md border p-3 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${active ? 'border-[var(--color-nav-bg)] bg-[var(--color-nav-light)]' : 'border-[var(--color-border)]'}`}><div className={`text-xs font-medium ${active ? 'text-[var(--color-nav-bg)]' : ''}`}>{title}</div><div className="mt-1 text-[10px] text-[var(--color-text-tertiary)]">{subtitle}</div></button> }
function defaultBackupName() { const now = new Date(); const pad = (value: number) => String(value).padStart(2, '0'); return `接口备份-${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}-${pad(now.getHours())}-${pad(now.getMinutes())}` }
function statusLabel(status: ProxyKey['status']) { return { active: '有效', disabled: '已停用', scheduled: '待生效', expired: '已过期' }[status] }
function formatTime(value: string) { return formatDateTime(new Date(value), { seconds: true }) }
function keyPayload(key: ProxyKey): ProxyKeyPayload { return { name: key.name, enabled: key.enabled, valid_from: key.valid_from, expires_at: key.expires_at, scope_all: key.scope_all, interface_ids: key.interface_ids } }
function toLocalInput(value?: string | null) { if (!value) return ''; const date = new Date(value); const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000); return local.toISOString().slice(0, 16) }
function toIso(value: string) { return value ? new Date(value).toISOString() : null }

function ProxyKeyForm({ keyValue, interfaces, busy, onCancel, onSave }: { keyValue: ProxyKey | null; interfaces: HubInterface[]; busy: boolean; onCancel: () => void; onSave: (payload: ProxyKeyPayload) => void }) {
  const [name, setName] = useState(keyValue?.name || '')
  const [enabled, setEnabled] = useState(keyValue?.enabled ?? true)
  const [validFrom, setValidFrom] = useState(toLocalInput(keyValue?.valid_from))
  const [expiresAt, setExpiresAt] = useState(toLocalInput(keyValue?.expires_at))
  const [scopeAll, setScopeAll] = useState(keyValue?.scope_all || false)
  const [selected, setSelected] = useState<Set<number>>(new Set(keyValue?.interface_ids || []))
  const [error, setError] = useState('')

  // 时间窗校验与后端 proxy_keys 的 400 同口径，提前到输入时就地提示；
  // 编辑已有密钥时允许过期时间落在过去（后端 allow_expired）。
  const scheduleError = validateProxyKeySchedule(validFrom, expiresAt, { isCreate: !keyValue })

  const setExpiryShortcut = (kind: 'none' | 'day' | 'month') => {
    if (kind === 'none') { setExpiresAt(''); return }
    const date = new Date()
    date.setHours(date.getHours() + (kind === 'day' ? 24 : 24 * 30))
    setExpiresAt(toLocalInput(date.toISOString()))
  }

  const submit = () => {
    if (!name.trim()) { setError('请填写密钥名称'); return }
    if (scheduleError) { setError(scheduleError); return }
    if (!scopeAll && !selected.size) { setError('请选择至少一个可调用接口，或授权全部接口'); return }
    onSave({ name: name.trim(), enabled, valid_from: toIso(validFrom), expires_at: toIso(expiresAt), scope_all: scopeAll, interface_ids: [...selected] })
  }

  return (
    <div className="space-y-4">
      <div className="text-sm font-semibold">{keyValue ? '编辑调用密钥' : '创建调用密钥'}</div>
      <div>
        <label htmlFor="proxy-key-name" className="mb-1 block text-xs font-medium">密钥名称</label>
        <input id="proxy-key-name" value={name} onChange={event => setName(event.target.value)} className="h-10 w-full rounded-md border border-[var(--color-border)] bg-card px-3 text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring" placeholder="例如：订单系统生产环境" />
      </div>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div>
          <label htmlFor="proxy-key-valid-from" className="mb-1 block text-xs font-medium">生效时间</label>
          <input id="proxy-key-valid-from" type="datetime-local" value={validFrom} onChange={event => setValidFrom(event.target.value)} className="h-10 w-full rounded-md border border-[var(--color-border)] bg-card px-3 text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring" />
        </div>
        <div>
          <div className="mb-1 flex items-center justify-between gap-2">
            <label htmlFor="proxy-key-expires-at" className="block text-xs font-medium">过期时间</label>
            <span className="flex gap-1.5">
              {([['none', '无期限'], ['day', '24 小时'], ['month', '30 天']] as const).map(([kind, label]) => (
                <button
                  key={kind}
                  type="button"
                  onClick={() => setExpiryShortcut(kind)}
                  className="rounded px-1.5 py-0.5 text-[10px] font-medium text-[var(--color-nav-bg)] hover:bg-[var(--color-nav-light)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  {label}
                </button>
              ))}
            </span>
          </div>
          <input id="proxy-key-expires-at" type="datetime-local" value={expiresAt} aria-invalid={Boolean(scheduleError)} aria-describedby={scheduleError ? 'proxy-key-schedule-error' : undefined} onChange={event => setExpiresAt(event.target.value)} className="h-10 w-full rounded-md border border-[var(--color-border)] bg-card px-3 text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring" />
        </div>
      </div>
      <p className="-mt-2 text-[10px] text-[var(--color-text-tertiary)]">生效与过期时间按当前浏览器时区填写，保存后统一转换为 UTC。</p>
      {scheduleError && <p id="proxy-key-schedule-error" role="alert" className="-mt-2 text-xs text-[var(--color-danger)]">{scheduleError}</p>}
      <div className="flex gap-6">
        <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)} className="accent-[var(--color-nav-bg)]" />立即启用</label>
        <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={scopeAll} onChange={event => setScopeAll(event.target.checked)} className="accent-[var(--color-nav-bg)]" />允许全部已发布接口</label>
      </div>
      <div>
        <div className="mb-2 flex items-center justify-between gap-2">
          <span className="text-xs font-medium">指定接口权限</span>
          {!scopeAll && selected.size > 0 && <span className="text-[10px] text-[var(--color-text-tertiary)]">已选 {selected.size} 项</span>}
        </div>
        <div className={`max-h-56 overflow-y-auto rounded-md border border-[var(--color-border)] p-2 ${scopeAll ? 'pointer-events-none opacity-40' : ''}`}>
          {interfaces.map(item => (
            <label key={item.id} className="flex items-center gap-2 rounded px-2 py-2 text-xs hover:bg-[var(--color-bg-hover)]">
              <input type="checkbox" checked={selected.has(item.id!)} onChange={() => setSelected(current => { const next = new Set(current); if (next.has(item.id!)) next.delete(item.id!); else next.add(item.id!); return next })} />
              <span className="w-12 font-mono text-[10px] font-semibold">{item.method}</span>
              <span className="min-w-0 flex-1 truncate">{item.name}</span>
              <span className="text-[10px] text-[var(--color-text-tertiary)]">{item.http_enabled ? '已发布' : '未发布'}</span>
            </label>
          ))}
        </div>
      </div>
      {error && <div role="alert" className="rounded-md bg-[var(--color-danger-bg)] px-3 py-2 text-xs text-[var(--color-danger)]">{error}</div>}
      <div className="flex justify-end gap-2">
        <Button variant="outline" onClick={onCancel}>取消</Button>
        <Button loading={busy} onClick={submit}>{keyValue ? '保存修改' : '创建密钥'}</Button>
      </div>
    </div>
  )
}

function SecretView({ secret, info, onDone }: { secret: string; info: ProxyInfo | null; onDone: () => void }) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const copyResetRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // 完整密钥只在创建时返回一次（后端只存哈希）：挂载即全选，方便手动复制兜底
  useEffect(() => {
    inputRef.current?.focus()
    inputRef.current?.select()
    return () => { if (copyResetRef.current) clearTimeout(copyResetRef.current) }
  }, [])

  const copySecret = async () => {
    if (copyResetRef.current) clearTimeout(copyResetRef.current)
    try {
      await writeTextToClipboard(secret)
      setCopyState('copied')
    } catch {
      setCopyState('failed')
      inputRef.current?.focus()
      inputRef.current?.select()
    }
    copyResetRef.current = setTimeout(() => setCopyState('idle'), 2000)
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3 rounded-lg bg-brand-soft p-4 text-brand-ink">
        <CheckCircle2 size={20} />
        <div>
          <div className="text-sm font-semibold">密钥创建成功</div>
          <div className="text-[11px]">完整密钥只显示这一次，请立即保存。</div>
        </div>
      </div>
      <div className="flex gap-2">
        <input ref={inputRef} readOnly value={secret} aria-label="完整调用密钥" className="h-10 min-w-0 flex-1 rounded-md border border-brand-line bg-brand-soft px-3 font-mono text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" />
        <Button variant={copyState === 'failed' ? 'danger' : 'default'} onClick={() => void copySecret()}>
          <Copy size={14} />{copyState === 'copied' ? '已复制' : copyState === 'failed' ? '复制失败' : '复制密钥'}
        </Button>
      </div>
      {copyState === 'failed' && (
        <p role="alert" className="text-[11px] leading-4 text-[var(--color-danger)]">未能写入剪贴板。密钥已为你全选，请按 Ctrl+C / ⌘+C 手动复制。</p>
      )}
      <pre className="overflow-auto whitespace-pre-wrap rounded-md bg-[var(--color-code-bg)] p-4 font-mono text-[11px] leading-5 text-[var(--color-code-fg)]">{`curl '${window.location.origin}${info?.path || '/proxy'}/<公开路径>' \\\n  -H '${info?.key_header || 'X-API-Hub-Key'}: ${secret}'`}</pre>
      <div className="flex justify-end"><Button onClick={onDone}>我已保存</Button></div>
    </div>
  )
}
