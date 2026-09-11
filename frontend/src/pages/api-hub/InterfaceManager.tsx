import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  Braces, Check, ChevronRight, CirclePlus, Copy, Download, FileCode2, FileUp, Folder, Play,
  Plus, Send, Trash2, X, Database, GripVertical, KeyRound, Share2, ShieldCheck,
  LoaderCircle,
} from 'lucide-react'
import { apiError, apiHub, emptyHubInterface, validateHttpUrl, type HubInterface, type KV, type RunResult } from '@/api/apiHub'
import { authApi, type PrivacyVar, type UserEnvVar } from '@/api/auth'
import { Button } from '@/components/ui/Button'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ConfirmDialog } from './ConfirmDialog'
import { writeTextToClipboard } from '@/utils/clipboard'
import { ProxyKeysModal, SystemDataModal } from './InterfaceDataModals'
import { HttpPublicationModal } from './HttpPublicationModal'
import { buildProxyCallExample } from './proxyCallExample'

interface Props {
  interfaces: HubInterface[]
  reload: () => Promise<HubInterface[]>
  onError: (message: string) => void
}

const methods = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS']
const methodTone: Record<string, string> = {
  GET: 'text-[var(--color-info)] bg-[var(--color-info-bg)]', POST: 'text-brand-ink bg-brand-soft',
  PUT: 'text-[var(--color-warning)] bg-[var(--color-warning-bg)]', PATCH: 'text-brand-ink bg-brand-soft',
  DELETE: 'text-[var(--color-danger)] bg-[var(--color-danger-bg)]', HEAD: 'text-muted-foreground bg-muted',
  OPTIONS: 'text-[var(--color-info)] bg-[var(--color-info-bg)]',
}

type PendingNavigation =
  | { type: 'select'; item: HubInterface }
  | { type: 'create' }

type DropTarget = { group: string; index: number }

export default function InterfaceManager({ interfaces, reload, onError }: Props) {
  const [selectedId, setSelectedId] = useState<number | null>(interfaces[0]?.id ?? null)
  const [draft, setDraft] = useState<HubInterface>(() => interfaces[0] ? structuredClone(interfaces[0]) : emptyHubInterface())
  const [baseline, setBaseline] = useState<HubInterface>(() => interfaces[0] ? structuredClone(interfaces[0]) : emptyHubInterface())
  const [editorTab, setEditorTab] = useState<'params' | 'headers' | 'body' | 'description' | 'privacy'>('params')
  const [saving, setSaving] = useState(false)
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<RunResult | null>(null)
  const [selectedFiles, setSelectedFiles] = useState<File[][]>([])
  const [resultFingerprint, setResultFingerprint] = useState('')
  const [pendingNavigation, setPendingNavigation] = useState<PendingNavigation | null>(null)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [callExampleDraft, setCallExampleDraft] = useState<HubInterface | null>(null)
  const [callExampleCopyState, setCallExampleCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const [draggingId, setDraggingId] = useState<number | null>(null)
  const [dropTarget, setDropTarget] = useState<DropTarget | null>(null)
  const [publicationTarget, setPublicationTarget] = useState<HubInterface | null>(null)
  const [publicationCopying, setPublicationCopying] = useState(false)
  const [publicationCopied, setPublicationCopied] = useState(false)
  const [proxyKeys, setProxyKeys] = useState(false)
  const [systemData, setSystemData] = useState(false)
  const [extraGroups, setExtraGroups] = useState<string[]>([])
  const [newGroupOpen, setNewGroupOpen] = useState(false)
  const [newGroupName, setNewGroupName] = useState('')
  const [newGroupError, setNewGroupError] = useState('')
  const [privacyVars, setPrivacyVars] = useState<PrivacyVar[]>([])
  const [envVars, setEnvVars] = useState<UserEnvVar[]>([])
  const containerRef = useRef<HTMLDivElement>(null)
  const [sizes, setSizes] = useState<[number, number]>([28, 72])

  const startResize = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault()
    const rect = containerRef.current?.getBoundingClientRect()
    if (!rect) return
    const startX = event.clientX
    const start = sizes
    const previousCursor = document.body.style.cursor
    const previousSelect = document.body.style.userSelect
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    const onMove = (moveEvent: PointerEvent) => {
      const delta = ((moveEvent.clientX - startX) / rect.width) * 100
      const left = Math.min(42, Math.max(20, start[0] + delta))
      setSizes([left, 100 - left])
    }
    const onUp = () => {
      document.body.style.cursor = previousCursor
      document.body.style.userSelect = previousSelect
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
  }, [sizes])

  const grouped = useMemo(() => {
    const groups = new Map<string, HubInterface[]>()
    interfaces.forEach(item => {
      const key = item.group_name || ''
      groups.set(key, [...(groups.get(key) || []), item])
    })
    return [...groups.entries()].sort(([a], [b]) => a === '' ? 1 : b === '' ? -1 : a.localeCompare(b, 'zh-CN'))
  }, [interfaces])
  const groupNames = useMemo(() => {
    const names = new Set(extraGroups)
    interfaces.forEach(item => {
      const name = item.group_name.trim()
      if (name) names.add(name)
    })
    const current = draft.group_name.trim()
    if (current) names.add(current)
    return [...names].sort((a, b) => a.localeCompare(b, 'zh-CN'))
  }, [draft.group_name, extraGroups, interfaces])
  const isDirty = draftFingerprint(draft) !== draftFingerprint(baseline)
  const urlError = draft.url.trim() ? validateHttpUrl(draft.url) : ''
  const resultStale = Boolean(
    result && resultFingerprint !== requestFingerprint(draft, selectedFiles)
  )
  const callExample = useMemo(
    () => callExampleDraft ? buildCallExample(callExampleDraft) : '',
    [callExampleDraft],
  )

  const selectNow = (item: HubInterface) => {
    setSelectedId(item.id)
    setDraft(structuredClone(item))
    setBaseline(structuredClone(item))
    setResult(null)
    setResultFingerprint('')
    setSelectedFiles([])
  }
  const createNow = () => {
    setSelectedId(null)
    setDraft(emptyHubInterface())
    setBaseline(emptyHubInterface())
    setResult(null)
    setResultFingerprint('')
    setSelectedFiles([])
  }
  const select = (item: HubInterface) => {
    if (item.id === selectedId) return
    if (isDirty) { setPendingNavigation({ type: 'select', item }); return }
    selectNow(item)
  }
  const create = () => {
    if (isDirty) { setPendingNavigation({ type: 'create' }); return }
    createNow()
  }
  const discardAndNavigate = () => {
    if (!pendingNavigation) return
    if (pendingNavigation.type === 'select') selectNow(pendingNavigation.item)
    else createNow()
    setPendingNavigation(null)
  }
  const patchDraft = <K extends keyof HubInterface>(key: K, value: HubInterface[K]) => setDraft(current => ({ ...current, [key]: value }))
  const closeNewGroup = () => {
    setNewGroupOpen(false)
    setNewGroupError('')
  }
  const openNewGroup = () => {
    setNewGroupName('')
    setNewGroupError('')
    setNewGroupOpen(true)
  }
  const changeGroup = (value: string) => {
    if (value === '__default__') { patchDraft('group_name', ''); return }
    if (value === '__new__') { openNewGroup(); return }
    patchDraft('group_name', value)
  }
  const addNewGroup = () => {
    const name = newGroupName.trim()
    if (!name) { setNewGroupError('分类名称不能为空'); return }
    if (name === '__new__') { setNewGroupError('该名称为保留字，请换一个'); return }
    if (name === '默认分组') { setNewGroupError('「默认分组」为保留名称，请使用其他名称'); return }
    setExtraGroups(current => current.includes(name) ? current : [...current, name])
    patchDraft('group_name', name)
    closeNewGroup()
  }

  useEffect(() => {
    if (!isDirty) return undefined
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [isDirty])

  useEffect(() => setPublicationCopied(false), [selectedId])

  // 个人变量列表：隐私变量插入 {{privacy:KEY}}、环境变量插入 {{env:KEY}} 占位符用。
  // 隐私变量只取 has_value 的项——没有值的变量在调用端无法解析，列出反而误导；
  // 环境变量保存即有值，全部列出。按 AGENTS.md §5 副作用验收：列表加载失败时
  // 静默降级为空列表，不打断编辑，错误由 onError 兜底提示一次。
  useEffect(() => {
    let cancelled = false
    authApi.listPrivacyVars()
      .then(items => { if (!cancelled) setPrivacyVars(Array.isArray(items) ? items.filter(item => item.has_value) : []) })
      .catch(() => { /* 静默：列表为空时按钮仍可点击但无项可选 */ })
    authApi.listEnvVars()
      .then(items => { if (!cancelled) setEnvVars(Array.isArray(items) ? items : []) })
      .catch(() => { /* 静默降级为空列表，同上 */ })
    return () => { cancelled = true }
  }, [])

  const save = async (): Promise<HubInterface | null> => {
    if (!draft.name.trim()) { onError('请填写接口名称'); return null }
    const validationError = validateHttpUrl(draft.url)
    if (validationError) { onError(validationError); return null }
    setSaving(true)
    try {
      const payload = { ...draft, method: draft.method.toUpperCase() }
      const saved = draft.id
        ? await apiHub.updateInterface(draft.id, payload)
        : await apiHub.createInterface(payload)
      setSelectedId(saved.id)
      setDraft(structuredClone(saved))
      setBaseline(structuredClone(saved))
      await reload()
      return saved
    } catch (error) {
      onError(apiError(error)); return null
    } finally { setSaving(false) }
  }

  const reloadPublication = async () => {
    const items = await reload()
    const refreshed = items.find(item => item.id === selectedId)
    if (refreshed) {
      const publication = {
        http_enabled: refreshed.http_enabled,
        proxy_slug: refreshed.proxy_slug,
        proxy_query_keys: refreshed.proxy_query_keys,
        proxy_header_keys: refreshed.proxy_header_keys,
        proxy_body_enabled: refreshed.proxy_body_enabled,
        proxy_body_keys: refreshed.proxy_body_keys,
      }
      setDraft(current => ({ ...current, ...publication }))
      setBaseline(current => ({ ...current, ...publication }))
    }
    return items
  }

  const run = async () => {
    if (!draft.name.trim()) { onError('请填写接口名称'); return }
    const validationError = validateHttpUrl(draft.url)
    if (validationError) { onError(validationError); return }
    setRunning(true)
    try {
      const payload = { ...draft, method: draft.method.toUpperCase() }
      const fingerprint = requestFingerprint(payload, selectedFiles)
      setResult(await apiHub.runDraftRaw(payload, selectedFiles))
      setResultFingerprint(fingerprint)
    } catch (error) { onError(apiError(error)) }
    finally { setRunning(false) }
  }

  const remove = async () => {
    if (!draft.id) return
    setSaving(true)
    try {
      await apiHub.deleteInterface(draft.id)
      const items = await reload()
      const next = items[0]
      setSelectedId(next?.id ?? null)
      setDraft(next ? structuredClone(next) : emptyHubInterface())
      setBaseline(next ? structuredClone(next) : emptyHubInterface())
      setResult(null)
      setSelectedFiles([])
      setDeleteOpen(false)
    } catch (error) { onError(apiError(error)) }
    finally { setSaving(false) }
  }

  const showCallExample = async () => {
    const validationError = validateHttpUrl(draft.url)
    if (validationError) { onError(validationError); return }
    buildCallExample(draft)
    setCallExampleDraft(structuredClone(draft))
    setCallExampleCopyState('idle')

  }

  const copyCallExample = async () => {
    try {
      await writeTextToClipboard(callExample)
      setCallExampleCopyState('copied')
    } catch {
      setCallExampleCopyState('failed')
    }
  }

  const moveInterface = async (targetGroup: string, rawTargetIndex: number) => {
    if (!draggingId) return
    const moving = interfaces.find(item => item.id === draggingId)
    if (!moving) return
    const targetItems = interfaces.filter(item => (item.group_name || '') === targetGroup)
    let targetIndex = rawTargetIndex
    if ((moving.group_name || '') === targetGroup) {
      const sourceIndex = targetItems.findIndex(item => item.id === draggingId)
      if (sourceIndex >= 0 && sourceIndex < targetIndex) targetIndex -= 1
    }
    setDropTarget(null)
    try {
      await apiHub.moveInterface(draggingId, { group_name: targetGroup, target_index: targetIndex })
      const items = await reload()
      if (selectedId === draggingId) {
        const moved = items.find(item => item.id === draggingId)
        if (moved) {
          setDraft(structuredClone(moved))
          setBaseline(structuredClone(moved))
        }
      }
    } catch (error) { onError(apiError(error)) }
    finally { setDraggingId(null) }
  }

  const startInterfaceDrag = (event: React.DragEvent<HTMLDivElement>, item: HubInterface) => {
    if (isDirty) {
      event.preventDefault()
      onError('请先保存当前接口的修改，再调整接口顺序')
      return
    }
    if (!item.id) { event.preventDefault(); return }
    setDraggingId(item.id)
    event.dataTransfer.effectAllowed = 'move'
    event.dataTransfer.setData('text/plain', String(item.id))
  }

  const copyPublishedExample = async () => {
    if (!draft.id) return
    setPublicationCopying(true)
    try {
      const [saved, info] = await Promise.all([
        apiHub.getInterface(draft.id),
        apiHub.proxyInfo(),
      ])
      if (!saved.http_enabled) {
        onError('该接口尚未发布 HTTP 接口')
        return
      }
      const example = buildProxyCallExample({
        item: saved,
        origin: window.location.origin,
        proxyPath: info.path,
        keyHeader: info.key_header,
      })
      await writeTextToClipboard(example)
      setPublicationCopied(true)
    } catch (error) {
      onError(apiError(error) || '复制失败，请检查浏览器剪贴板权限')
    } finally {
      setPublicationCopying(false)
    }
  }

  return (
    <div ref={containerRef} className="scrollbar-none grid h-full min-h-0 overflow-x-auto overflow-y-hidden p-1" style={{ gridTemplateColumns: `minmax(250px, ${sizes[0]}fr) 4px minmax(680px, ${sizes[1]}fr)` }}>
      <aside className="flex min-h-0 min-w-0 flex-col overflow-hidden rounded-lg border border-[var(--color-border)] bg-card shadow-sm">
        <div className="flex min-h-16 shrink-0 items-center border-b border-[var(--color-border)] px-3">
          <div className="flex w-full items-center justify-between"><div><h2 className="text-sm font-semibold">接口清单</h2><p className="text-[10px] text-[var(--color-text-tertiary)]">{interfaces.length} 个已纳管接口</p></div><Button size="sm" onClick={create}><CirclePlus size={13} />新建接口</Button></div>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-2">
          {!interfaces.length ? <EmptyList onCreate={create} /> : grouped.map(([group, items]) => (
            <div key={group || '__default'} className="mb-3">
              <div
                onDragOver={event => {
                  if (!draggingId) return
                  event.preventDefault()
                  event.dataTransfer.dropEffect = 'move'
                  setDropTarget({ group, index: 0 })
                }}
                onDrop={event => { event.preventDefault(); void moveInterface(group, 0) }}
                className={`flex items-center gap-1.5 rounded-md px-2 py-1.5 text-[11px] font-semibold uppercase tracking-wide transition-colors ${dropTarget?.group === group && dropTarget.index === 0 ? 'bg-brand-soft text-brand-ink ring-1 ring-inset ring-brand-line' : 'text-[var(--color-text-tertiary)]'}`}
              >
                <Folder size={12} />{group || '默认分组'}<span className="ml-auto font-normal">{items.length}</span>
              </div>
              <div className="space-y-0.5">
                {items.map((item, index) => (
                  <div
                    key={item.id}
                    draggable={!saving}
                    onDragStart={event => startInterfaceDrag(event, item)}
                    onDragEnd={() => { setDraggingId(null); setDropTarget(null) }}
                    onDragOver={event => {
                      if (!draggingId || draggingId === item.id) return
                      event.preventDefault()
                      event.dataTransfer.dropEffect = 'move'
                      const rect = event.currentTarget.getBoundingClientRect()
                      setDropTarget({ group, index: event.clientY < rect.top + rect.height / 2 ? index : index + 1 })
                    }}
                    onDrop={event => {
                      event.preventDefault()
                      event.stopPropagation()
                      const rect = event.currentTarget.getBoundingClientRect()
                      void moveInterface(group, event.clientY < rect.top + rect.height / 2 ? index : index + 1)
                    }}
                    className={`group relative flex min-h-10 w-full items-center rounded-md pr-1 transition-all ${draggingId === item.id ? 'opacity-45' : ''} ${selectedId === item.id ? 'bg-[var(--color-nav-light)]' : 'hover:bg-[var(--color-bg-hover)]'}`}
                  >
                    {dropTarget?.group === group && dropTarget.index === index && <span className="pointer-events-none absolute inset-x-1 top-0 h-0.5 rounded-full bg-brand" />}
                    {dropTarget?.group === group && dropTarget.index === index + 1 && <span className="pointer-events-none absolute inset-x-1 bottom-0 h-0.5 rounded-full bg-brand" />}
                    <span className="flex h-8 w-5 shrink-0 cursor-grab items-center justify-center text-[var(--color-text-tertiary)] active:cursor-grabbing" title="拖拽调整顺序或移动分组"><GripVertical size={12} /></span>
                    <button type="button" onClick={() => select(item)} className="flex min-w-0 flex-1 items-center gap-2 px-2.5 py-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring">
                      <span className={`w-12 shrink-0 rounded px-1.5 py-0.5 text-center text-[10px] font-bold ${methodTone[item.method] || methodTone.HEAD}`}>{item.method}</span>
                      <span className={`min-w-0 flex-1 truncate text-xs ${selectedId === item.id ? 'font-semibold text-[var(--color-nav-bg)]' : 'text-[var(--color-text-primary)]'}`}>{item.name}</span>
                      {item.http_enabled && <PublicationBadge title="已发布 HTTP 接口" />}
                      <ChevronRight size={12} className="shrink-0 text-[var(--color-text-tertiary)] opacity-0 group-hover:opacity-100" />
                    </button>
                    <button
                      type="button"
                      onClick={() => setPublicationTarget(item)}
                      aria-label={`${item.name}：${item.http_enabled ? '查看 HTTP 发布配置' : '发布 HTTP 接口'}`}
                      title={item.http_enabled ? '查看 HTTP 调用方式' : '发布为带鉴权的 HTTP 接口'}
                      className={`flex h-7 shrink-0 items-center gap-1 rounded px-2 text-[9px] font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${item.http_enabled ? 'bg-brand-mist text-brand-ink hover:bg-brand-mist' : 'border border-dashed border-border bg-card text-muted-foreground hover:border-brand-line hover:text-brand-ink'}`}
                    >
                      <Share2 size={11} />{item.http_enabled ? 'HTTP 设置' : 'HTTP 发布'}
                    </button>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
        <div className="grid shrink-0 grid-cols-2 gap-2 border-t border-[var(--color-border)] bg-card px-3 py-[1.125rem]">
          <Button variant="outline" size="sm" onClick={() => setProxyKeys(true)}><KeyRound size={13} />HTTP 调用方</Button>
          <Button variant="outline" size="sm" onClick={() => setSystemData(true)}><Database size={13} />系统数据</Button>
        </div>
      </aside>

      <div onPointerDown={startResize} role="separator" aria-orientation="vertical" aria-label="调整接口清单宽度" className="group flex cursor-col-resize items-center justify-center"><span className="flex h-12 w-3 items-center justify-center rounded-full border border-transparent text-[var(--color-text-tertiary)] transition-colors group-hover:border-brand-line group-hover:bg-brand-soft group-hover:text-brand-ink"><GripVertical size={12} /></span></div>

      <section className="flex min-h-0 min-w-0 flex-col overflow-hidden rounded-lg border border-[var(--color-border)] bg-card shadow-sm">
        <div className="flex min-h-16 shrink-0 flex-wrap items-center gap-2 border-b border-[var(--color-border)] px-4 py-3">
          <div className="flex min-w-[430px] flex-[1_1_430px] items-center gap-2">
            <input value={draft.name} onChange={event => patchDraft('name', event.target.value)} className="h-8 min-w-[180px] max-w-md flex-1 rounded-md border border-[var(--color-border)] bg-card px-3 text-sm font-semibold outline-none transition-colors placeholder:text-[var(--color-text-tertiary)] hover:border-[var(--color-border-hover)] focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring" placeholder="接口名称" />
            <Select value={draft.group_name || '__default__'} onValueChange={changeGroup}>
              <SelectTrigger className="h-8 w-40 shrink-0 rounded-md bg-card text-xs" title="选择或新增分类" aria-label="选择或新增分类">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__default__">默认分组</SelectItem>
                {groupNames.map(group => <SelectItem key={group} value={group}>{group}</SelectItem>)}
                <SelectItem value="__new__">＋ 新增分类…</SelectItem>
              </SelectContent>
            </Select>
            <Button size="sm" loading={saving} onClick={save}>{!saving && <Check size={14} />}{draft.id ? '保存配置' : '保存接口'}</Button>
            {isDirty && <span className="shrink-0 rounded bg-[var(--color-warning-bg)] px-2 py-1 text-[10px] font-medium text-[var(--color-warning)]">未保存</span>}
          </div>
          <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
            {draft.id && <Button variant="ghost" size="icon-sm" title="复制为新接口" onClick={() => { setSelectedId(null); setBaseline(emptyHubInterface()); setDraft({ ...structuredClone(draft), id: null, name: `${draft.name} 副本`, mcp_enabled: false, open_enabled: false, http_enabled: false, proxy_slug: '', proxy_query_keys: [], proxy_header_keys: [], proxy_body_enabled: false, proxy_body_keys: [] }); setResult(null); setResultFingerprint(''); setSelectedFiles([]) }}><Copy size={14} /></Button>}
            {draft.id && <Button variant="ghost" size="icon-sm" title="删除接口" className="text-[var(--color-danger)]" onClick={() => setDeleteOpen(true)}><Trash2 size={14} /></Button>}
            {draft.id && <Button variant="outline" size="sm" onClick={() => setPublicationTarget(structuredClone(baseline))}><Share2 size={14} />HTTP 发布</Button>}
            {draft.id && draft.http_enabled && <Button variant="outline" size="sm" loading={publicationCopying} onClick={copyPublishedExample} aria-label={'复制“' + draft.name + '”的 HTTP 调用示例'}>{!publicationCopying && (publicationCopied ? <Check size={14} /> : <Copy size={14} />)}{publicationCopied ? '已复制' : '复制 HTTP 示例'}<span className="sr-only" aria-live="polite">{publicationCopied ? 'HTTP 调用示例复制成功' : ''}</span></Button>}
            {/* 桥接接口由平台进程内分发，外部 cURL 无法触达，不提供调试示例 */}
            {!draft.url.trim().startsWith('mcp-bridge://') && <Button variant="outline" size="sm" onClick={() => void showCallExample()}><FileCode2 size={14} />上游调试 cURL</Button>}
          </div>
        </div>

        <div className="shrink-0 p-4 pb-3">
          <div className={`flex overflow-hidden rounded-md border border-border bg-card focus-within:border-[var(--color-nav-bg)] ${urlError ? 'border-[color-mix(in_srgb,var(--color-danger)_40%,transparent)]' : ''}`}>
            <Select value={draft.method} onValueChange={value => patchDraft('method', value)}>
              <SelectTrigger aria-label="请求方法" className="h-10 w-28 shrink-0 rounded-none border-0 border-r border-border bg-transparent px-3 text-xs font-bold shadow-none focus-visible:border-ring">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {methods.map(method => <SelectItem key={method} value={method}>{method}</SelectItem>)}
              </SelectContent>
            </Select>
            <input value={draft.url} aria-invalid={Boolean(urlError)} aria-describedby={urlError ? 'api-hub-url-error' : undefined} onChange={event => patchDraft('url', event.target.value)} className="h-10 min-w-0 flex-1 bg-transparent px-3 font-mono text-xs outline-none" placeholder="https://example.com/api/resource" />
            <button onClick={run} disabled={running} className={`relative m-1 flex min-w-[84px] items-center justify-center gap-1.5 overflow-hidden rounded bg-brand px-4 text-xs font-semibold text-[var(--color-text-inverse)] shadow-sm transition-all duration-200 hover:-translate-y-px hover:bg-brand-deep active:translate-y-0 active:scale-[0.98] disabled:cursor-wait disabled:opacity-90 ${running ? 'ring-4 ring-brand-mist' : ''}`}>
              {running ? <><LoaderCircle size={14} className="animate-spin" />调用中…</> : <><Play size={13} />调用</>}
              {running && <span className="absolute inset-0 animate-pulse bg-card/10" />}
            </button>
          </div>
          {urlError && <p id="api-hub-url-error" role="alert" className="mt-2 text-[11px] text-[var(--color-danger)]">{urlError}</p>}
        </div>

        <div className="flex shrink-0 items-center border-b border-[var(--color-border)] px-4">
          <div className="flex gap-5">
            {(['params', 'headers', 'body', 'description', 'privacy'] as const).map(key => (
              <button key={key} onClick={() => setEditorTab(key)} className={`relative py-2.5 text-xs font-medium ${editorTab === key ? 'text-[var(--color-nav-bg)]' : 'text-[var(--color-text-secondary)]'}`}>
                {{ params: '查询参数', headers: '请求头', body: '请求体', description: '用途说明', privacy: '个人变量' }[key]}
                {editorTab === key && <span className="absolute inset-x-0 bottom-0 h-0.5 bg-[var(--color-nav-bg)]" />}
              </button>
            ))}
          </div>
        </div>

        <div className="min-h-[150px] shrink-0 overflow-y-auto border-b border-[var(--color-border)] p-4">
          {editorTab === 'params' && <KVEditor value={draft.query_params} onChange={value => patchDraft('query_params', value)} keyPlaceholder="参数名" valuePlaceholder="参数值" />}
          {editorTab === 'headers' && <KVEditor value={draft.headers} onChange={value => patchDraft('headers', value)} keyPlaceholder="Header" valuePlaceholder="值" />}
          {editorTab === 'body' && <BodyEditor draft={draft} patchDraft={patchDraft} selectedFiles={selectedFiles} setSelectedFiles={setSelectedFiles} />}
          {editorTab === 'description' && <textarea value={draft.description} onChange={event => patchDraft('description', event.target.value)} className="h-28 w-full resize-none rounded-md border border-border bg-card p-3 text-xs outline-none focus:border-[var(--color-nav-bg)]" placeholder="说明接口用途、可传入的业务参数和返回结果；数据管家编排与 AI 助手将据此理解此接口的用途。" />}
          {editorTab === 'privacy' && <PersonalVarPanel privacyVars={privacyVars} envVars={envVars} />}
        </div>

        <ResponsePanel result={result} stale={resultStale} loading={running} />
      </section>

      <ConfirmDialog open={deleteOpen} onClose={() => setDeleteOpen(false)} onConfirm={remove} loading={saving} variant="danger" title={`删除“${draft.name}”？`} description="接口配置及其全部调用历史都会被删除，此操作不可撤销。" confirmText="删除接口" />
      <ConfirmDialog open={Boolean(pendingNavigation)} onClose={() => setPendingNavigation(null)} onConfirm={discardAndNavigate} variant="danger" title="放弃未保存修改？" description="当前接口的未保存修改将丢失，且无法恢复。" confirmText="放弃并继续" />
      <Dialog open={newGroupOpen} onOpenChange={next => { if (!next) closeNewGroup() }}>
        <DialogContent className="w-[min(92vw,26rem)]">
          <DialogHeader>
            <div className="min-w-0">
              <DialogTitle>新增分类</DialogTitle>
              <DialogDescription>输入新的分类名称，添加后当前接口会立即选中该分类。</DialogDescription>
            </div>
          </DialogHeader>
          <div className="space-y-3">
          <div className="rounded-lg border border-brand-line bg-brand-soft px-3 py-2.5 text-xs leading-5 text-muted-foreground">分类会先保留在本次编辑会话中，保存当前接口后正式生效。</div>
          <label htmlFor="api-hub-new-group" className="block text-xs font-semibold text-foreground">分类名称</label>
          <input id="api-hub-new-group" autoFocus autoComplete="off" value={newGroupName} onChange={event => { setNewGroupName(event.target.value); if (newGroupError) setNewGroupError('') }} onKeyDown={event => { if (event.key === 'Enter') addNewGroup() }} className={`h-10 w-full rounded-lg border bg-card px-3 text-sm outline-none transition-colors focus-visible:ring-2 ${newGroupError ? 'border-[color-mix(in_srgb,var(--color-danger)_40%,transparent)] focus-visible:border-destructive focus-visible:ring-ring' : 'border-[var(--color-border)] focus-visible:border-ring focus-visible:ring-ring'}`} placeholder="例如：用户中心 / 订单服务" />
          {newGroupError && <div role="alert" className="rounded-md bg-[var(--color-danger-bg)] px-3 py-2 text-xs text-[var(--color-danger)]">{newGroupError}</div>}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={closeNewGroup}>取消</Button>
            <Button onClick={addNewGroup}><CirclePlus size={14} />添加并选中</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog
        open={Boolean(callExampleDraft)}
        onOpenChange={next => { if (!next) setCallExampleDraft(null) }}
      >
        <DialogContent className="w-[min(92vw,37.5rem)]">
          <DialogHeader>
            <div className="min-w-0">
              <DialogTitle>上游调试 cURL</DialogTitle>
            </div>
          </DialogHeader>
        <div className="space-y-3.5">
          <div className="rounded-r-lg border-l-[3px] border-brand bg-brand-soft/80 px-3.5 py-2.5 text-xs leading-5 text-muted-foreground">
            此命令直连真实上游地址，仅用于管理员调试；对外系统请使用“HTTP 发布”生成的调用包。CMD / PowerShell / bash 通用。
          </div>
          <pre aria-label="cURL 命令" className="max-h-80 overflow-auto whitespace-pre-wrap break-all rounded-[10px] border border-border bg-muted px-4 py-3.5 font-mono text-[12.5px] leading-[1.7] text-foreground">{callExample}</pre>
          <span className="sr-only" role="status" aria-live="polite">
            {callExampleCopyState === 'copied' ? 'cURL 命令已复制' : callExampleCopyState === 'failed' ? '复制失败，请重试' : ''}
          </span>
        </div>
          <DialogFooter className="justify-center">
            <Button variant="outline" className="min-w-24" onClick={() => setCallExampleDraft(null)}>关闭</Button>
            <Button
              className={`min-w-24 shadow-sm ${callExampleCopyState === 'failed' ? 'bg-[var(--color-danger)] hover:bg-[var(--color-danger-hover)]' : ''}`}
              onClick={() => void copyCallExample()}
            >
              {callExampleCopyState === 'copied' ? <Check size={14} /> : <Copy size={14} />}
              {callExampleCopyState === 'copied' ? '已复制' : callExampleCopyState === 'failed' ? '重试复制' : '复制'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <HttpPublicationModal open={Boolean(publicationTarget)} onClose={() => setPublicationTarget(null)} item={publicationTarget} reload={reloadPublication} onError={onError} />
      <ProxyKeysModal open={proxyKeys} onClose={() => setProxyKeys(false)} interfaces={interfaces} onError={onError} />
      <SystemDataModal open={systemData} onClose={() => setSystemData(false)} interfaces={interfaces} reload={reload} onError={onError} />
    </div>
  )
}

function EmptyList({ onCreate }: { onCreate: () => void }) {
  return <div className="flex flex-col items-center px-5 py-14 text-center"><Braces size={28} className="mb-3 text-[var(--color-text-tertiary)]" /><p className="text-xs text-[var(--color-text-secondary)]">还没有接口</p><button onClick={onCreate} className="mt-2 text-xs font-medium text-[var(--color-nav-bg)]">新建一个接口</button></div>
}

function PublicationBadge({ title }: { title: string }) {
  return <span title={title} className="shrink-0 rounded bg-[var(--color-info-bg)] px-1.5 py-0.5 text-[9px] font-bold tracking-wide text-[var(--color-info)]">HTTP</span>
}

function PersonalVarPanel({ privacyVars, envVars }: { privacyVars: PrivacyVar[]; envVars: UserEnvVar[] }) {
  const [copiedRef, setCopiedRef] = useState<string | null>(null)
  const [copyError, setCopyError] = useState('')

  const copyFormat = async (ref: string) => {
    try {
      await writeTextToClipboard(ref)
      setCopiedRef(ref)
      setCopyError('')
      setTimeout(() => setCopiedRef(null), 2000)
    } catch {
      // 非 HTTPS / 非聚焦页面下剪贴板可能不可靠，如实提示并提供手动兜底
      setCopiedRef(ref)
      setCopyError(ref)
    }
  }

  const section = (
    title: string,
    prefix: 'privacy' | 'env',
    items: { key: string }[],
    icon: ReactNode,
    emptyText: string,
  ) => (
    <section className="space-y-1.5">
      <div className="text-[11px] font-semibold text-[var(--color-text-secondary)]">{title}</div>
      {items.length ? (
        items.map(item => {
          const ref = `{{${prefix}:${item.key}}}`
          return (
            <div key={ref} className="flex items-center gap-2 rounded-md border border-border bg-card px-3 py-2">
              <span className="shrink-0 text-[var(--color-nav-bg)]">{icon}</span>
              <span className="font-mono text-xs font-medium text-[var(--color-text-primary)]">{item.key}</span>
              <code className="ml-1 flex-1 truncate font-mono text-[11px] text-[var(--color-text-tertiary)]">{ref}</code>
              <button
                type="button"
                onClick={() => void copyFormat(ref)}
                className="flex shrink-0 items-center gap-1 rounded px-2 py-1 text-[11px] font-medium text-[var(--color-nav-bg)] transition-colors hover:bg-[var(--color-nav-light)]"
              >
                {copiedRef === ref ? <Check size={13} /> : <Copy size={13} />}
                {copiedRef === ref ? '已尝试复制' : '复制使用格式'}
              </button>
            </div>
          )
        })
      ) : (
        <div className="rounded-md border border-dashed border-[var(--color-border)] py-5 text-center text-xs text-[var(--color-text-tertiary)]">
          {emptyText}
        </div>
      )}
    </section>
  )

  return (
    <div className="space-y-3">
      <div className="rounded-md border border-border bg-card px-3 py-2.5 text-[11px] leading-5 text-[var(--color-text-secondary)]">
        在「请求头」「请求体」「URL」的值中粘贴 <code className="rounded bg-[var(--color-bg-hover)] px-1 font-mono">{'{{privacy:变量名}}'}</code> 或 <code className="rounded bg-[var(--color-bg-hover)] px-1 font-mono">{'{{env:变量名}}'}</code> 格式，调用时平台会自动用你本人的变量明文替换。明文不写入接口配置，也不进入调用历史；「HTTP 发布」与 n8n 流水线链路没有用户身份，不会解析这些占位符。
      </div>
      {section('隐私变量', 'privacy', privacyVars, <ShieldCheck size={14} />, '当前没有可用的隐私变量。请在「个人资料 → 隐私变量」中创建并通过上报脚本上报值。')}
      {section('环境变量', 'env', envVars, <Braces size={14} />, '当前没有环境变量。请在「个人资料 → 环境变量」中添加。')}
      {copyError && (
        <div className="rounded-md border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-3 py-2 text-[11px] leading-5 text-[var(--color-warning)]">
          剪贴板写入失败，请手动复制：<code className="font-mono">{copyError}</code>
        </div>
      )}
    </div>
  )
}

function KVEditor({ value, onChange, keyPlaceholder, valuePlaceholder }: { value: KV[]; onChange: (value: KV[]) => void; keyPlaceholder: string; valuePlaceholder: string }) {
  const rows = value.length ? value : [{ key: '', value: '' }]
  const update = (index: number, key: keyof KV, text: string) => onChange(rows.map((row, i) => i === index ? { ...row, [key]: text } : row))
  return <div className="space-y-2">{rows.map((row, index) => <div key={index} className="flex items-center gap-2"><input value={row.key} onChange={event => update(index, 'key', event.target.value)} className="h-8 w-2/5 rounded-md border border-border bg-card px-2.5 font-mono text-xs outline-none" placeholder={keyPlaceholder} /><input value={row.value} onChange={event => update(index, 'value', event.target.value)} className="h-8 min-w-0 flex-1 rounded-md border border-border bg-card px-2.5 font-mono text-xs outline-none" placeholder={valuePlaceholder} /><button onClick={() => onChange(rows.filter((_, i) => i !== index))} className="text-[var(--color-text-tertiary)] hover:text-[var(--color-danger)]"><X size={14} /></button></div>)}<button onClick={() => onChange([...rows, { key: '', value: '' }])} className="flex items-center gap-1 text-xs text-[var(--color-nav-bg)]"><Plus size={13} />添加一行</button></div>
}

function BodyEditor({
  draft,
  patchDraft,
  selectedFiles,
  setSelectedFiles,
}: {
  draft: HubInterface
  patchDraft: <K extends keyof HubInterface>(key: K, value: HubInterface[K]) => void
  selectedFiles: File[][]
  setSelectedFiles: React.Dispatch<React.SetStateAction<File[][]>>
}) {
  const updateFileField = (index: number, patch: Partial<HubInterface['file_fields'][number]>) => {
    patchDraft('file_fields', draft.file_fields.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item))
  }
  const removeFileField = (index: number) => {
    patchDraft('file_fields', draft.file_fields.filter((_, itemIndex) => itemIndex !== index))
    setSelectedFiles(current => current.filter((_, itemIndex) => itemIndex !== index))
  }
  const chooseFiles = (index: number, files: FileList | null) => {
    const chosenFiles = files ? Array.from(files) : []
    setSelectedFiles(current => {
      const next = [...current]
      next[index] = chosenFiles
      return next
    })
  }

  return (
    <div>
      <div className="mb-3 flex flex-wrap gap-1">
        {(['none', 'json', 'form', 'multipart', 'raw'] as const).map(type => (
          <button key={type} type="button" onClick={() => patchDraft('body_type', type)} className={`rounded px-2.5 py-1 text-[11px] ${draft.body_type === type ? 'bg-[var(--color-nav-light)] font-semibold text-[var(--color-nav-bg)]' : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)]'}`}>
            {type.toUpperCase()}
          </button>
        ))}
      </div>
      {draft.body_type === 'none' ? (
        <div className="rounded-md border border-dashed border-[var(--color-border)] py-9 text-center text-xs text-[var(--color-text-tertiary)]">当前请求不发送 Body</div>
      ) : draft.body_type === 'multipart' ? (
        <div className="grid gap-3 lg:grid-cols-[minmax(240px,0.9fr)_minmax(360px,1.4fr)]">
          <label className="block">
            <span className="mb-1.5 block text-[11px] font-semibold text-foreground">文本字段</span>
            <textarea value={draft.body_content} onChange={event => patchDraft('body_content', event.target.value)} className="h-32 w-full resize-none rounded-md border border-border bg-card p-3 font-mono text-xs outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring" placeholder={'description=说明\ncategory=document'} />
            <span className="mt-1 block text-[10px] leading-4 text-[var(--color-text-tertiary)]">每行一个 key=value，调用时与文件一起组成 multipart/form-data。</span>
          </label>
          <div>
            <div className="mb-1.5 flex items-center justify-between gap-3">
              <span className="text-[11px] font-semibold text-foreground">文件字段</span>
              <button type="button" onClick={() => { patchDraft('file_fields', [...draft.file_fields, { key: draft.file_fields.length ? `file${draft.file_fields.length + 1}` : 'file', accept: '', multiple: false }]); setSelectedFiles(current => [...current, []]) }} className="flex items-center gap-1 text-[11px] font-medium text-brand-ink hover:text-brand-ink"><Plus size={12} />添加文件字段</button>
            </div>
            <div className="space-y-2">
              {draft.file_fields.length ? draft.file_fields.map((field, index) => {
                const files = selectedFiles[index] || []
                return (
                  <div key={index} className="rounded-lg border border-border bg-muted p-2.5">
                    <div className="flex flex-wrap items-center gap-2">
                      <input aria-label={`第 ${index + 1} 个文件字段名`} value={field.key} onChange={event => updateFileField(index, { key: event.target.value })} className="h-8 min-w-[110px] flex-1 rounded-md border border-border bg-card px-2.5 font-mono text-xs outline-none focus-visible:border-ring" placeholder="字段名，例如 file" />
                      <input aria-label={`${field.key || `第 ${index + 1} 个字段`}允许的文件类型`} value={field.accept} onChange={event => updateFileField(index, { accept: event.target.value })} className="h-8 min-w-[150px] flex-[1.3] rounded-md border border-border bg-card px-2.5 font-mono text-[11px] outline-none focus-visible:border-ring" placeholder=".pdf,image/*（可选）" />
                      <label className="flex h-8 cursor-pointer items-center gap-1.5 rounded-md border border-brand-line bg-card px-2.5 text-[11px] font-medium text-brand-ink hover:bg-brand-soft focus-within:ring-2 focus-within:ring-ring">
                        <FileUp size={13} />选择文件
                        <input type="file" accept={field.accept || undefined} multiple={field.multiple} className="sr-only" onChange={event => { chooseFiles(index, event.target.files); event.currentTarget.value = '' }} />
                      </label>
                      <button type="button" onClick={() => removeFileField(index)} aria-label={`删除文件字段 ${field.key || index + 1}`} className="text-[var(--color-text-tertiary)] hover:text-[var(--color-danger)]"><X size={14} /></button>
                    </div>
                    <div className="mt-2 flex min-h-6 flex-wrap items-center gap-2">
                      <label className="flex cursor-pointer items-center gap-1.5 text-[10px] text-muted-foreground"><input type="checkbox" checked={field.multiple} onChange={event => { updateFileField(index, { multiple: event.target.checked }); if (!event.target.checked) setSelectedFiles(current => current.map((items, itemIndex) => itemIndex === index ? items.slice(0, 1) : items)) }} className="accent-[var(--color-nav-bg)]" />允许多文件</label>
                      {files.length ? files.map(file => <span key={`${file.name}-${file.lastModified}`} title={file.name} className="max-w-[210px] truncate rounded bg-brand-soft px-2 py-1 text-[10px] text-brand-ink">{file.name} · {formatFileSize(file.size)}</span>) : <span className="text-[10px] text-[var(--color-text-tertiary)]">本次调用尚未选择文件</span>}
                      {files.length > 0 && <button type="button" onClick={() => chooseFiles(index, null)} className="ml-auto text-[10px] font-medium text-muted-foreground hover:text-[var(--color-danger)]">清空</button>}
                    </div>
                  </div>
                )
              }) : <button type="button" onClick={() => { patchDraft('file_fields', [{ key: 'file', accept: '', multiple: false }]); setSelectedFiles([[]]) }} className="flex h-32 w-full flex-col items-center justify-center rounded-lg border border-dashed border-border bg-muted text-muted-foreground transition-colors hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink"><FileUp size={20} /><span className="mt-2 text-xs font-medium">添加文件上传字段</span><span className="mt-1 text-[10px]">文件只用于本次调用，不会保存到接口配置</span></button>}
            </div>
          </div>
        </div>
      ) : (
        <textarea value={draft.body_content} onChange={event => patchDraft('body_content', event.target.value)} className="h-28 w-full resize-none rounded-md border border-border bg-card p-3 font-mono text-xs outline-none focus:border-[var(--color-nav-bg)]" placeholder={draft.body_type === 'json' ? '{\n  "key": "value"\n}' : draft.body_type === 'form' ? 'key=value\nother=value' : '原始请求内容'} />
      )}
    </div>
  )
}

function ResponsePanel({ result, stale, loading }: { result: RunResult | null; stale: boolean; loading: boolean }) {
  const response = useMemo(() => formatResponseBody(result?.response_body ?? ''), [result?.response_body])
  const copyText = response.text || result?.error || ''
  const [copyFeedback, setCopyFeedback] = useState<{ text: string; status: 'copied' | 'failed' } | null>(null)
  const copyResetRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const copyStatus = copyFeedback?.text === copyText ? copyFeedback.status : null

  useEffect(() => () => {
    if (copyResetRef.current) clearTimeout(copyResetRef.current)
  }, [])

  const copyResponse = async () => {
    if (!copyText) return
    if (copyResetRef.current) clearTimeout(copyResetRef.current)
    try {
      await writeTextToClipboard(copyText)
      setCopyFeedback({ text: copyText, status: 'copied' })
    } catch {
      setCopyFeedback({ text: copyText, status: 'failed' })
    }
    copyResetRef.current = setTimeout(() => setCopyFeedback(null), 1800)
  }

  const downloadResponse = () => {
    if (!result?.download) return
    const url = URL.createObjectURL(result.download.blob)
    const link = document.createElement('a')
    link.href = url
    link.download = result.download.filename
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 0)
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex h-10 shrink-0 items-center gap-3 border-b border-[var(--color-border)] px-4">
        <span className="text-xs font-semibold text-[var(--color-text-primary)]">响应</span>
        {loading ? (
          <span className="flex items-center gap-1.5 text-[11px] font-medium text-brand-ink"><LoaderCircle size={12} className="animate-spin" />请求处理中</span>
        ) : result && (
          <>
            <span className={`rounded px-2 py-0.5 text-[11px] font-semibold ${stale ? 'bg-muted text-muted-foreground' : result.status_code && result.status_code < 400 ? 'bg-[var(--color-success-bg)] text-[var(--color-success)]' : 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]'}`}>{result.status_code ?? 'ERR'}</span>
            <span className="text-[11px] text-[var(--color-text-tertiary)]">{result.elapsed_ms ?? '—'} ms</span>
            {response.isJson && <span className="rounded bg-brand-soft px-1.5 py-0.5 font-mono text-[10px] font-semibold text-brand-ink">JSON</span>}
            {stale && <span className="text-[11px] font-medium text-[var(--color-warning)]">请求已修改，此结果已过期</span>}
            {result.relogin && <span className="text-[11px] text-[var(--color-warning)]">已自动重登</span>}
          </>
        )}
        {result && !loading && (
          <div className="ml-auto flex items-center gap-2">
            {result.download && <button type="button" onClick={downloadResponse} title={`下载 ${result.download.filename}`} className="flex h-7 shrink-0 items-center gap-1.5 rounded-md border border-brand-line bg-brand-soft px-2.5 text-[11px] font-semibold text-brand-ink transition-colors hover:bg-brand-mist"><Download size={12} />下载文件</button>}
            <button
              type="button"
              onClick={() => void copyResponse()}
              disabled={!copyText}
              title="复制响应内容"
              className={`flex h-7 shrink-0 items-center gap-1.5 rounded-md border px-2.5 text-[11px] font-medium transition-all duration-200 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40 ${copyStatus === 'copied' ? 'border-brand-line bg-brand-soft text-brand-ink' : copyStatus === 'failed' ? 'border-[color-mix(in_srgb,var(--color-danger)_30%,transparent)] bg-[var(--color-danger-bg)] text-[var(--color-danger)]' : 'border-[var(--color-border)] bg-card text-[var(--color-text-secondary)] hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink'}`}
            >
              {copyStatus === 'copied' ? <Check size={12} /> : <Copy size={12} />}
              {copyStatus === 'copied' ? '已复制' : copyStatus === 'failed' ? '复制失败' : '复制'}
            </button>
          </div>
        )}
      </div>

      {loading ? (
        <div className="flex flex-1 flex-col items-center justify-center px-8 text-center"><div className="relative mb-5 flex h-12 w-12 items-center justify-center rounded-full bg-brand-soft"><span className="absolute inset-0 animate-ping rounded-full bg-brand-mist opacity-70" /><LoaderCircle size={22} className="relative animate-spin text-brand-ink" /></div><div className="text-sm font-semibold text-foreground">正在调用接口</div><div className="mt-1.5 text-xs text-muted-foreground">正在连接目标服务并等待响应…</div><div className="mt-5 w-full max-w-sm space-y-2"><span className="block h-2 animate-pulse rounded bg-[var(--color-bg-active)]" /><span className="block h-2 w-4/5 animate-pulse rounded bg-muted" /><span className="block h-2 w-3/5 animate-pulse rounded bg-muted" /></div></div>
      ) : !result ? (
        <div className="flex flex-1 items-center justify-center text-xs text-[var(--color-text-tertiary)]"><Send size={18} className="mr-2 opacity-50" />点击“调用”查看响应</div>
      ) : (
        <div className={`min-h-0 flex-1 animate-fade-in overflow-auto bg-muted ${stale ? 'opacity-60' : ''}`}>
          {result.error && <div className="m-4 mb-3 rounded-md bg-[var(--color-danger-bg)] px-3 py-2 text-xs text-[var(--color-danger)]">{result.error}</div>}
          {response.isJson ? <JsonResponse value={response.text} /> : <pre className="whitespace-pre-wrap break-words p-4 font-mono text-xs leading-6 text-[var(--color-text-primary)]">{response.text || '(空响应体)'}</pre>}
        </div>
      )}
    </div>
  )
}

function JsonResponse({ value }: { value: string }) {
  return (
    <div className="min-w-max py-3 font-mono text-xs leading-6">
      {value.split('\n').map((line, index) => (
        <div key={index} className="grid grid-cols-[3rem_minmax(0,1fr)] hover:bg-brand-soft">
          <span className="select-none border-r border-border pr-3 text-right text-[10px] text-[var(--color-text-tertiary)]">{index + 1}</span>
          <code className="whitespace-pre px-4">{highlightJsonLine(line)}</code>
        </div>
      ))}
    </div>
  )
}

function highlightJsonLine(line: string) {
  const tokenPattern = /("(?:\\u[\da-fA-F]{4}|\\[^u]|[^\\"])*"\s*:)|("(?:\\u[\da-fA-F]{4}|\\[^u]|[^\\"])*")|(\btrue\b|\bfalse\b)|(\bnull\b)|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g
  const parts = []
  let lastIndex = 0

  for (const match of line.matchAll(tokenPattern)) {
    const index = match.index
    if (index > lastIndex) parts.push(line.slice(lastIndex, index))
    const token = match[0]
    const tone = match[1]
      ? 'text-brand-ink'
      : match[2]
        ? 'text-[var(--color-info)]'
        : match[3]
          ? 'font-semibold text-brand-ink'
          : match[4]
            ? 'italic text-muted-foreground'
            : 'text-[var(--color-warning)]'
    parts.push(<span key={`${index}-${token}`} className={tone}>{token}</span>)
    lastIndex = index + token.length
  }
  if (lastIndex < line.length) parts.push(line.slice(lastIndex))
  return parts
}

function formatResponseBody(body: string) {
  if (!body) return { text: '', isJson: false }
  try {
    return { text: JSON.stringify(JSON.parse(body), null, 2), isJson: true }
  } catch {
    return { text: body, isJson: false }
  }
}

function requestFingerprint(item: HubInterface, selectedFiles: File[][] = []) {
  return JSON.stringify({
    method: item.method,
    url: item.url,
    query_params: item.query_params,
    headers: item.headers,
    body_type: item.body_type,
    body_content: item.body_content,
    file_fields: item.file_fields,
    selected_files: selectedFiles.map(files => files.map(file => ({
      name: file.name,
      size: file.size,
      type: file.type,
      lastModified: file.lastModified,
    }))),
  })
}

function draftFingerprint(item: HubInterface) {
  return JSON.stringify({
    name: item.name,
    description: item.description,
    group_name: item.group_name,
    ...JSON.parse(requestFingerprint(item)),
    mcp_enabled: item.mcp_enabled,
    open_enabled: item.open_enabled,
    http_enabled: item.http_enabled,
    proxy_slug: item.proxy_slug,
    proxy_query_keys: item.proxy_query_keys,
    proxy_header_keys: item.proxy_header_keys,
    proxy_body_enabled: item.proxy_body_enabled,
    proxy_body_keys: item.proxy_body_keys,
  })
}

function shellQuote(value: string) {
  return `'${value.replaceAll("'", "'\\''")}'`
}

function formatFileSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function buildCallExample(draft: HubInterface) {
  const url = new URL(draft.url)
  draft.query_params.filter(item => item.key.trim()).forEach(item => url.searchParams.append(item.key.trim(), item.value))
  const method = methods.includes(draft.method.toUpperCase()) ? draft.method.toUpperCase() : 'GET'
  const headers = draft.headers.filter(item => item.key.trim()).map(item => ({ key: item.key.trim(), value: item.value }))
    .filter(item => draft.body_type !== 'multipart' || item.key.toLowerCase() !== 'content-type')
  const hasContentType = headers.some(item => item.key.toLowerCase() === 'content-type')
  let body = ''
  if (draft.body_type === 'json' && draft.body_content.trim()) {
    if (!hasContentType) headers.push({ key: 'Content-Type', value: 'application/json; charset=utf-8' })
    body = draft.body_content
  } else if (draft.body_type === 'form' && draft.body_content.trim()) {
    if (!hasContentType) headers.push({ key: 'Content-Type', value: 'application/x-www-form-urlencoded' })
    const form = new URLSearchParams()
    draft.body_content.split('\n').map(line => line.trim()).filter(line => line && !line.startsWith('#')).forEach(line => {
      const separator = line.indexOf('=')
      form.append(separator < 0 ? line : line.slice(0, separator), separator < 0 ? '' : line.slice(separator + 1))
    })
    body = form.toString()
  } else if (draft.body_type === 'raw' && draft.body_content) {
    body = draft.body_content
  }

  const pieces = [`curl -X ${method} ${shellQuote(url.toString())}`]
  headers.forEach(item => pieces.push(`  -H ${shellQuote(`${item.key}: ${item.value}`)}`))
  if (draft.body_type === 'multipart') {
    draft.body_content.split('\n').map(line => line.trim()).filter(line => line && !line.startsWith('#') && line.includes('=')).forEach(line => pieces.push(`  -F ${shellQuote(line)}`))
    draft.file_fields.filter(field => field.key.trim()).forEach(field => pieces.push(`  -F ${shellQuote(`${field.key.trim()}=@/path/to/file`)}`))
  }
  if (body) pieces.push(`  --data-raw ${shellQuote(body)}`)
  return pieces.join(' \\\n')
}
