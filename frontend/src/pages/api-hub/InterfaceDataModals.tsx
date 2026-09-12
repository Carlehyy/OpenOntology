import { formatDateTime } from '@/utils/datetime'
import { useEffect, useState } from 'react'
import {
  CheckCircle2, Copy, Download, KeyRound, Pencil,
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

  const load = async () => {
    try {
      const [nextInfo, nextKeys] = await Promise.all([apiHub.proxyInfo(), apiHub.listProxyKeys()])
      setInfo(nextInfo); setKeys(nextKeys)
    } catch (error) { onError(apiError(error)) }
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
        <DialogContent className="w-[min(92vw,48rem)]">
          <DialogHeader>
            <div className="min-w-0">
              <DialogTitle>调用方管理</DialogTitle>
              <DialogDescription>查看、停用或撤销平台已经生成的调用凭证；日常分享无需在这里手动创建。</DialogDescription>
            </div>
          </DialogHeader>
          {revealed ? <SecretView secret={revealed} info={info} onDone={() => setRevealed('')} />
          : editing !== undefined ? <ProxyKeyForm keyValue={editing} interfaces={interfaces} busy={busy} onCancel={() => setEditing(undefined)} onSave={saveKey} />
            : <div className="space-y-4"><div className="flex items-center justify-between rounded-lg border border-border bg-card px-4 py-3"><div className="flex items-center gap-3"><div className="flex h-9 w-9 items-center justify-center rounded-lg bg-brand-soft text-brand-ink"><ShieldCheck size={17} /></div><div><div className="text-sm font-semibold">{keys.length} 把调用密钥</div><div className="text-[10px] text-[var(--color-text-tertiary)]">{info?.published.length || 0} 个已发布 HTTP 接口</div></div></div><Button size="sm" onClick={() => setEditing(null)}><Plus size={14} />创建密钥</Button></div>{!keys.length ? <div className="rounded-lg border border-dashed border-[var(--color-border)] py-16 text-center text-xs text-[var(--color-text-tertiary)]"><KeyRound size={26} className="mx-auto mb-3 opacity-50" />还没有调用密钥</div> : <div className="max-h-[52vh] space-y-2 overflow-y-auto">{keys.map(key => { const tone = key.status === 'active' ? 'bg-brand-soft text-brand-ink' : key.status === 'expired' ? 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]' : key.status === 'scheduled' ? 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]' : 'bg-muted text-muted-foreground'; return <div key={key.id} className="rounded-lg border border-[var(--color-border)] p-4"><div className="flex items-start justify-between gap-4"><div><div className="flex items-center gap-2"><span className="text-sm font-semibold">{key.name}</span><span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${tone}`}>{statusLabel(key.status)}</span></div><div className="mt-1 font-mono text-[11px] text-[var(--color-text-tertiary)]">{key.masked_key}</div></div><div className="flex gap-1"><Button variant="ghost" size="icon-sm" title="编辑" onClick={() => setEditing(key)}><Pencil size={13} /></Button><Button variant="ghost" size="sm" disabled={busy} onClick={() => toggle(key)}>{key.enabled ? '停用' : '启用'}</Button><Button variant="ghost" size="icon-sm" title="撤销" className="text-[var(--color-danger)]" onClick={() => setDeleteKey(key)}><Trash2 size={13} /></Button></div></div><div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-[10px] text-[var(--color-text-tertiary)]"><span>{key.scope_all ? '全部已发布接口' : `${key.interface_ids.length} 个指定接口`}</span><span>{key.expires_at ? `有效期至 ${formatTime(key.expires_at)}` : '长期有效'}</span><span>{key.last_used_at ? `最后调用 ${formatTime(key.last_used_at)}` : '尚未调用'}</span></div></div> })}</div>}</div>}
          <DialogFooter>
            <Button variant="outline" onClick={onClose}>关闭</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <ConfirmDialog open={Boolean(deleteKey)} onClose={() => setDeleteKey(null)} onConfirm={remove} loading={busy} variant="danger" title={`撤销密钥“${deleteKey?.name || ''}”？`} description="撤销后调用方将立即无法继续使用，且不能恢复。" confirmText="永久撤销" />
    </>
  )
}

export function SystemDataModal({ open, onClose, interfaces, reload, onError }: SharedProps & { open: boolean; onClose: () => void }) {
  const [name, setName] = useState(defaultBackupName)
  const [mode, setMode] = useState<'full' | 'partial'>('full')
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [includeSensitive, setIncludeSensitive] = useState(false)
  const [message, setMessage] = useState('')

  const exportData = async () => {
    const ids = mode === 'partial' ? [...selected] : []
    if (mode === 'partial' && !ids.length) { onError('请至少选择一个接口'); return }
    try {
      const response = await apiHub.exportBackup({ name, mode, ids, include_sensitive: includeSensitive })
      const url = URL.createObjectURL(response.data)
      const link = document.createElement('a')
      link.href = url; link.download = `${name || 'Backup'}.json`; link.click()
      URL.revokeObjectURL(url)
      setMessage(`已导出 ${mode === 'full' ? interfaces.length : ids.length} 个接口`)
    } catch (error) { onError(apiError(error)) }
  }
  const importData = async (file?: File) => {
    if (!file) return
    try {
      const result = await apiHub.importBackup(JSON.parse(await file.text()))
      await reload()
      setMessage(`还原完成：导入 ${result.imported} 个，跳过 ${result.skipped} 个`)
    } catch (error) { onError(error instanceof SyntaxError ? '备份文件不是有效 JSON' : apiError(error)) }
  }

  return (
    <Dialog open={open} onOpenChange={next => { if (!next) onClose() }}>
      <DialogContent className="w-[min(92vw,48rem)]">
        <DialogHeader>
          <div className="min-w-0">
            <DialogTitle>系统数据</DialogTitle>
            <DialogDescription>备份或还原接口清单；调用历史、W3 凭据和代理密钥不会写入备份。</DialogDescription>
          </div>
        </DialogHeader>
        {message && <div className="mb-4 rounded-md bg-[var(--color-success-bg)] px-3 py-2 text-xs text-[var(--color-success)]">{message}</div>}
      <div className="grid min-h-[430px] grid-cols-2 gap-6">
        <section className="space-y-4 rounded-lg border border-[var(--color-border)] p-4">
          <div><h4 className="text-sm font-semibold">数据备份</h4><p className="mt-1 text-[11px] text-[var(--color-text-tertiary)]">将接口配置导出为可迁移 JSON 包。</p></div>
          <input value={name} onChange={event => setName(event.target.value)} className="h-9 w-full rounded-md border border-border bg-card px-3 text-xs outline-none" />
          <div className="grid grid-cols-2 gap-2"><ModeButton active={mode === 'full'} onClick={() => setMode('full')} title="完整备份" subtitle={`${interfaces.length} 个接口`} /><ModeButton active={mode === 'partial'} onClick={() => setMode('partial')} title="部分备份" subtitle={`${selected.size} 个已选`} /></div>
          {mode === 'partial' && <div className="max-h-52 overflow-y-auto rounded-md border border-[var(--color-border)] p-2">{interfaces.map(item => <label key={item.id} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-xs hover:bg-[var(--color-bg-hover)]"><input type="checkbox" checked={selected.has(item.id!)} onChange={() => setSelected(current => { const next = new Set(current); if (next.has(item.id!)) next.delete(item.id!); else next.add(item.id!); return next })} /><span className="w-10 font-mono text-[10px]">{item.method}</span><span className="truncate">{item.name}</span></label>)}</div>}
          <label className={`flex items-start gap-2 rounded-md border px-3 py-2 text-[11px] ${includeSensitive ? 'border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] text-[var(--color-warning)]' : 'border-[var(--color-border)] text-[var(--color-text-secondary)]'}`}><input type="checkbox" checked={includeSensitive} onChange={event => setIncludeSensitive(event.target.checked)} className="mt-0.5" /><span><strong>包含请求 Header 和 Body 原值</strong><br />默认关闭。开启后备份可能含 API Key、Cookie 或业务敏感数据，请只存放在受控位置。</span></label>
          <Button onClick={exportData}><Download size={14} />导出备份</Button>
        </section>
        <section className="space-y-4 rounded-lg border border-[var(--color-border)] p-4">
          <div><h4 className="text-sm font-semibold">数据还原</h4><p className="mt-1 text-[11px] text-[var(--color-text-tertiary)]">以名称、方法和 URL 去重；导入后的 MCP 与 HTTP 发布状态统一保持关闭，需管理员重新确认。</p></div>
          <label className="flex min-h-64 cursor-pointer flex-col items-center justify-center rounded-lg border border-dashed border-[var(--color-border-hover)] bg-card text-center hover:border-[var(--color-nav-bg)]"><Upload size={28} className="mb-3 text-[var(--color-text-tertiary)]" /><span className="text-xs font-medium">选择 API-Hub 备份文件</span><span className="mt-1 text-[11px] text-[var(--color-text-tertiary)]">支持 .json</span><input type="file" accept=".json,application/json" className="hidden" onChange={event => { void importData(event.target.files?.[0]); event.currentTarget.value = '' }} /></label>
        </section>
      </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>关闭</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function ModeButton({ active, onClick, title, subtitle }: { active: boolean; onClick: () => void; title: string; subtitle: string }) { return <button onClick={onClick} className={`rounded-md border p-3 text-left ${active ? 'border-[var(--color-nav-bg)] bg-[var(--color-nav-light)]' : 'border-[var(--color-border)]'}`}><div className={`text-xs font-medium ${active ? 'text-[var(--color-nav-bg)]' : ''}`}>{title}</div><div className="mt-1 text-[10px] text-[var(--color-text-tertiary)]">{subtitle}</div></button> }
function defaultBackupName() { const now = new Date(); const pad = (value: number) => String(value).padStart(2, '0'); return `Backup-${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}-${pad(now.getHours())}-${pad(now.getMinutes())}` }
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
  const submit = () => {
    if (!name.trim()) { setError('请填写密钥名称'); return }
    if (!scopeAll && !selected.size) { setError('请选择至少一个可调用接口，或授权全部接口'); return }
    onSave({ name: name.trim(), enabled, valid_from: toIso(validFrom), expires_at: toIso(expiresAt), scope_all: scopeAll, interface_ids: [...selected] })
  }
  return <div className="space-y-4"><div className="text-sm font-semibold">{keyValue ? '编辑调用密钥' : '创建调用密钥'}</div><div><label className="mb-1 block text-xs font-medium">密钥名称</label><input value={name} onChange={event => setName(event.target.value)} className="h-10 w-full rounded-md border border-[var(--color-border)] px-3 text-xs" placeholder="例如：订单系统生产环境" /></div><div className="grid grid-cols-2 gap-4"><div><label className="mb-1 block text-xs font-medium">生效时间</label><input type="datetime-local" value={validFrom} onChange={event => setValidFrom(event.target.value)} className="h-10 w-full rounded-md border border-[var(--color-border)] px-3 text-xs" /></div><div><label className="mb-1 block text-xs font-medium">过期时间</label><input type="datetime-local" value={expiresAt} onChange={event => setExpiresAt(event.target.value)} className="h-10 w-full rounded-md border border-[var(--color-border)] px-3 text-xs" /></div></div><div className="flex gap-6"><label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)} className="accent-[var(--color-nav-bg)]" />立即启用</label><label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={scopeAll} onChange={event => setScopeAll(event.target.checked)} className="accent-[var(--color-nav-bg)]" />允许全部已发布接口</label></div><div><div className="mb-2 text-xs font-medium">指定接口权限</div><div className={`max-h-56 overflow-y-auto rounded-md border border-[var(--color-border)] p-2 ${scopeAll ? 'pointer-events-none opacity-40' : ''}`}>{interfaces.map(item => <label key={item.id} className="flex items-center gap-2 rounded px-2 py-2 text-xs hover:bg-[var(--color-bg-hover)]"><input type="checkbox" checked={selected.has(item.id!)} onChange={() => setSelected(current => { const next = new Set(current); if (next.has(item.id!)) next.delete(item.id!); else next.add(item.id!); return next })} /><span className="w-12 font-mono text-[10px] font-semibold">{item.method}</span><span className="min-w-0 flex-1 truncate">{item.name}</span><span className="text-[10px] text-[var(--color-text-tertiary)]">{item.http_enabled ? '已发布' : '未发布'}</span></label>)}</div></div>{error && <div className="rounded-md bg-[var(--color-danger-bg)] px-3 py-2 text-xs text-[var(--color-danger)]">{error}</div>}<div className="flex justify-end gap-2"><Button variant="outline" onClick={onCancel}>取消</Button><Button loading={busy} onClick={submit}>{keyValue ? '保存修改' : '创建密钥'}</Button></div></div>
}

function SecretView({ secret, info, onDone }: { secret: string; info: ProxyInfo | null; onDone: () => void }) {
  return <div className="space-y-4"><div className="flex items-center gap-3 rounded-lg bg-brand-soft p-4 text-brand-ink"><CheckCircle2 size={20} /><div><div className="text-sm font-semibold">密钥创建成功</div><div className="text-[11px]">完整密钥只显示这一次，请立即保存。</div></div></div><div className="flex gap-2"><input readOnly value={secret} className="h-10 min-w-0 flex-1 rounded-md border border-brand-line bg-brand-soft px-3 font-mono text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" /><Button onClick={() => writeTextToClipboard(secret)}><Copy size={14} />复制密钥</Button></div><pre className="overflow-auto whitespace-pre-wrap rounded-md bg-[var(--color-code-bg)] p-4 font-mono text-[11px] leading-5 text-[var(--color-code-fg)]">{`curl '${window.location.origin}${info?.path || '/proxy'}/<公开路径>' \\\n  -H '${info?.key_header || 'X-API-Hub-Key'}: ${secret}'`}</pre><div className="flex justify-end"><Button onClick={onDone}>我已保存</Button></div></div>
}
