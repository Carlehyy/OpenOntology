import { useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle, Check, CheckCircle2, Code2, Copy, RefreshCw, Share2, ShieldCheck, SlidersHorizontal,
} from 'lucide-react'
import {
  apiError, apiHub, type ForwardingPackage, type HubInterface,
} from '@/api/apiHub'
import { Button } from '@/components/ui/Button'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import { writeTextToClipboard } from '@/utils/clipboard'

interface Props {
  open: boolean
  onClose: () => void
  item: HubInterface | null
  reload: () => Promise<HubInterface[]>
  onError: (message: string) => void
}

interface PublicationDraft {
  slug: string
  queryKeys: string[]
  headerKeys: string[]
  bodyEnabled: boolean
  bodyKeys: string[]
}

export function HttpPublicationModal({ open, onClose, item, reload, onError }: Props) {
  const [current, setCurrent] = useState<HubInterface | null>(item)
  const [callPackage, setCallPackage] = useState<ForwardingPackage | null>(null)
  const [tab, setTab] = useState<'curl' | 'python' | 'javascript'>('curl')
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState('')
  const [proxyPath, setProxyPath] = useState('/proxy')
  const [configuration, setConfiguration] = useState<PublicationDraft | null>(null)

  useEffect(() => {
    if (!open || !item) return
    setCurrent(item)
    setCallPackage(null)
    setTab('curl')
    setCopied('')
    setConfiguration(publicationDraft(item))
    apiHub.proxyInfo().then(info => setProxyPath(info.path)).catch(error => onError(apiError(error)))
  }, [item, onError, open])

  const candidates = useMemo(() => publicationCandidates(current), [current])
  // 与后端 interface_has_personal_refs 同口径：URL/Header 值/Body 含个人变量
  // 占位符时公开链路永不解析，发布必被后端 400 拒绝——这里提前说明原因。
  const hasPersonalRefs = useMemo(() => Boolean(
    current && (
      personalRefPattern.test(current.url)
      || current.headers.some(header => personalRefPattern.test(header.value))
      || personalRefPattern.test(current.body_content)
    ),
  ), [current])

  if (!item || !current || !configuration) return null

  const editableCount = configuration.queryKeys.length
    + configuration.headerKeys.length
    + configuration.bodyKeys.length
    + (configuration.bodyEnabled && !configuration.bodyKeys.length ? 1 : 0)
  const publicUrl = current.http_enabled && current.proxy_slug
    ? `${window.location.origin}${proxyPath}/${current.proxy_slug}`
    : ''

  const savePublication = async (createPackage: boolean) => {
    if (!current.id) return
    if (!configuration.slug.trim()) { onError('请填写转发公开路径'); return }
    setBusy(true)
    try {
      const published = await apiHub.setHttpPublication(current.id, {
        enabled: true,
        slug: configuration.slug,
        query_keys: configuration.queryKeys,
        header_keys: configuration.headerKeys,
        body_enabled: configuration.bodyEnabled,
        body_keys: configuration.bodyKeys,
      })
      setCurrent(published)
      setConfiguration(publicationDraft(published))
      await reload()
      if (createPackage) {
        setCallPackage(await apiHub.createForwardingPackage(published.id!))
        setTab('curl')
      }
    } catch (error) { onError(apiError(error)) }
    finally { setBusy(false) }
  }

  const disable = async () => {
    if (!current.id) return
    setBusy(true)
    try {
      const disabled = await apiHub.setHttpPublication(current.id, {
        enabled: false,
        slug: current.proxy_slug,
        query_keys: current.proxy_query_keys,
        header_keys: current.proxy_header_keys,
        body_enabled: current.proxy_body_enabled,
        body_keys: current.proxy_body_keys,
      })
      setCurrent(disabled)
      setConfiguration(publicationDraft(disabled))
      setCallPackage(null)
      await reload()
    } catch (error) { onError(apiError(error)) }
    finally { setBusy(false) }
  }

  const copy = async (value: string, key: string) => {
    try {
      await writeTextToClipboard(value)
      setCopied(key)
    } catch { onError('复制失败，请手动选择代码复制') }
  }

  const code = callPackage ? forwardingCode(callPackage, tab) : ''

  return (
    <Dialog open={open} onOpenChange={next => { if (!next) onClose() }}>
      <DialogContent className="w-[min(92vw,48rem)]">
        <DialogHeader icon={<Share2 size={18} />}>
          <DialogTitle>{`HTTP 发布 · ${current.name}`}</DialogTitle>
          <DialogDescription>勾选调用方可传入的业务参数后，平台生成可直接复制的调用包；固定值和认证信息继续由平台保管。</DialogDescription>
        </DialogHeader>
        <div className="max-h-[66vh] space-y-5 overflow-y-auto pr-1">
        <section className={`flex items-center justify-between gap-5 rounded-xl border px-4 py-4 ${current.http_enabled ? 'border-brand-line bg-brand-soft' : 'border-brand-line bg-brand-soft'}`}>
          <div className="flex min-w-0 items-start gap-3">
            <div className={`mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg ${current.http_enabled ? 'bg-brand-mist text-brand-ink' : 'bg-brand-mist text-brand-ink'}`}>
              {current.http_enabled ? <CheckCircle2 size={18} /> : <SlidersHorizontal size={18} />}
            </div>
            <div className="min-w-0">
              <div className="text-sm font-semibold text-foreground">{current.http_enabled ? 'HTTP 接口已经可以使用' : '请确认 HTTP 参数'}</div>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">已选择 {editableCount} 项调用方可修改的数据；未选择的参数会继续使用接口中保存的固定值。</p>
            </div>
          </div>
          <span className={`shrink-0 rounded-full px-2.5 py-1 text-[10px] font-semibold ${current.http_enabled ? 'bg-brand-mist text-brand-ink' : 'bg-card text-brand-ink'}`}>{current.http_enabled ? '已转发' : '待发布'}</span>
        </section>

        {!callPackage && (
          <>
            {hasPersonalRefs && (
              <section className="flex items-start gap-3 rounded-xl border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-4 py-3">
                <AlertTriangle size={18} className="mt-0.5 shrink-0 text-[var(--color-warning)]" />
                <div>
                  <div className="text-xs font-semibold text-[var(--color-warning)]">此接口含个人变量占位符，不能发布</div>
                  <p className="mt-1 text-[10px] leading-4 text-[var(--color-warning)]">URL / 请求头 / 请求体中存在 {'{{privacy:}}'} 或 {'{{env:}}'} 占位符；公开代理链路没有用户身份、不会解析占位符，发布后调用必然失败。请先在接口编辑器中移除占位符，再发布。</p>
                </div>
              </section>
            )}
            <section className="rounded-xl border border-[var(--color-border)] bg-card p-4">
              <label className="text-xs font-semibold text-foreground" htmlFor="api-hub-proxy-slug">HTTP 公开路径</label>
              <p className="mt-1 text-[11px] leading-5 text-muted-foreground">保存后调用地址保持稳定；只能使用小写字母、数字、短横线和下划线。</p>
              <div className="mt-3 flex overflow-hidden rounded-lg border border-border bg-card focus-within:ring-2 focus-within:ring-ring">
                <span className="flex min-w-0 items-center truncate border-r border-border px-3 font-mono text-[11px] text-muted-foreground">{window.location.origin}{proxyPath}/</span>
                <input id="api-hub-proxy-slug" value={configuration.slug} onChange={event => setConfiguration(value => value && ({ ...value, slug: event.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, '') }))} className="h-10 min-w-[180px] flex-1 bg-card px-3 font-mono text-xs outline-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" placeholder="interface-path" />
              </div>
            </section>

            <section className="rounded-xl border border-[var(--color-border)] bg-card p-4">
              <div className="flex items-start gap-3">
                <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-brand-soft text-brand-ink"><SlidersHorizontal size={17} /></div>
                <div><div className="text-xs font-semibold text-foreground">允许调用方传入的参数</div><p className="mt-1 text-[11px] leading-5 text-muted-foreground">选中的值可在每次转发时覆盖。认证字段会默认排除，固定参数不会暴露给调用方。</p></div>
              </div>
              <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-3">
                <ParameterGroup title="Query 参数" items={candidates.query} selected={configuration.queryKeys} emptyText="接口未配置 Query 参数" onToggle={key => setConfiguration(value => value && ({ ...value, queryKeys: toggleValue(value.queryKeys, key) }))} />
                <ParameterGroup title="业务请求头" items={candidates.headers} selected={configuration.headerKeys} emptyText="没有可安全开放的请求头" onToggle={key => setConfiguration(value => value && ({ ...value, headerKeys: toggleValue(value.headerKeys, key, true) }))} />
                <ParameterGroup title="请求 Body" items={candidates.body} selected={configuration.bodyKeys} emptyText={candidates.rawBody ? 'Raw Body 只能整段开放' : '没有可开放的 Body 字段'} onToggle={key => setConfiguration(value => {
                  if (!value) return value
                  const bodyKeys = toggleValue(value.bodyKeys, key)
                  return { ...value, bodyKeys, bodyEnabled: bodyKeys.length > 0 }
                })} rawBody={candidates.rawBody} rawEnabled={configuration.bodyEnabled} onToggleRaw={() => setConfiguration(value => value && ({ ...value, bodyEnabled: !value.bodyEnabled, bodyKeys: [] }))} />
              </div>
            </section>

            <section className="flex items-start gap-3 rounded-xl border border-brand-line bg-brand-soft px-4 py-3">
              <ShieldCheck size={18} className="mt-0.5 shrink-0 text-brand-ink" />
              <div><div className="text-xs font-semibold text-brand-ink">转发时不注入登录态</div><p className="mt-1 text-[10px] leading-4 text-brand-ink">如目标需要登录态认证，请改为在接口详情中配置认证 Header 或查询参数并保存。</p></div>
            </section>
          </>
        )}

        {current.http_enabled && !callPackage && (
          <section className="rounded-xl border border-[var(--color-border)] bg-card p-4">
            <div className="flex items-center justify-between gap-4">
              <div><div className="text-xs font-semibold text-foreground">当前调用地址</div><div className="mt-1 text-[11px] text-muted-foreground">调用方无需知道真实接口。</div></div>
              <button type="button" onClick={() => void copy(publicUrl, 'url')} className="rounded-md px-2 py-1 text-xs font-medium text-brand-ink hover:bg-brand-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">{copied === 'url' ? '已复制' : '复制地址'}</button>
            </div>
            <code className="mt-3 block break-all rounded-lg bg-[var(--color-code-bg)] px-3 py-2.5 text-[11px] text-[var(--color-code-fg)]">{publicUrl}</code>
          </section>
        )}

        {callPackage && (
          <section className="space-y-4">
            <div className="rounded-xl border border-brand-line bg-brand-soft p-4">
              <div className="flex items-start gap-3"><CheckCircle2 size={20} className="mt-0.5 shrink-0 text-brand-ink" /><div><div className="text-sm font-semibold text-brand-ink">调用包已生成</div><p className="mt-1 text-xs leading-5 text-brand-ink">这份凭证只允许调用“{current.name}”。把下面代码发给调用方，对方修改业务数据后即可运行。</p></div></div>
            </div>

            <div className="rounded-xl border border-[var(--color-border)] bg-card p-4">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-2"><Code2 size={15} className="text-brand-ink" /><span className="text-xs font-semibold text-foreground">可直接使用的调用代码</span></div>
                <div className="flex rounded-lg bg-muted p-1">
                  {(['curl', 'python', 'javascript'] as const).map(value => (
                    <button key={value} type="button" onClick={() => { setTab(value); setCopied('') }} className={`rounded-md px-3 py-1 text-[10px] font-semibold transition-colors ${tab === value ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}`}>{value === 'curl' ? 'cURL' : value === 'python' ? 'Python' : 'JavaScript'}</button>
                  ))}
                </div>
              </div>
              <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-[var(--color-code-bg)] p-4 font-mono text-[11px] leading-5 text-[var(--color-code-fg)]">{code}</pre>
              <div className="mt-3 flex items-center justify-between gap-4 text-[10px] text-muted-foreground"><span>完整凭证只在这次生成结果中展示；之后可以单独撤销。</span><button type="button" onClick={() => void copy(code, 'inline-code')} className="shrink-0 rounded px-2 py-1 font-semibold text-brand-ink hover:bg-brand-soft">{copied === 'inline-code' ? '已复制' : '复制代码'}</button></div>
            </div>

            {editableLabels(callPackage).length > 0 && (
              <div className="flex flex-wrap items-center gap-2 rounded-xl border border-border bg-muted px-4 py-3">
                <span className="mr-1 text-[10px] font-semibold text-muted-foreground">调用方可修改</span>
                {editableLabels(callPackage).map(label => <span key={label} className="rounded-full border border-border bg-card px-2 py-1 font-mono text-[10px] text-foreground">{label}</span>)}
              </div>
            )}
          </section>
        )}

        </div>
        {callPackage ? (
          <DialogFooter>
            <Button variant="outline" onClick={onClose}>完成</Button>
            <Button onClick={() => void copy(code, 'code')}><Copy size={14} />{copied === 'code' ? '已复制' : '复制当前代码'}</Button>
          </DialogFooter>
        ) : (
          <DialogFooter>
            {current.http_enabled && <Button variant="ghost" onClick={() => void disable()} disabled={busy} className="mr-auto text-muted-foreground">停止 HTTP 发布</Button>}
            <Button variant="outline" onClick={() => setConfiguration(suggestedPublicationDraft(current))} disabled={busy}><RefreshCw size={14} />恢复推荐选择</Button>
            <Button variant="outline" onClick={() => void savePublication(false)} loading={busy}>保存 HTTP 配置</Button>
            <Button onClick={() => void savePublication(true)} loading={busy}>{!busy && <Share2 size={14} />}{current.http_enabled ? '保存并生成调用包' : '发布并生成调用包'}</Button>
          </DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  )
}

function ParameterGroup({
  title,
  items,
  selected,
  emptyText,
  onToggle,
  rawBody = false,
  rawEnabled = false,
  onToggleRaw,
}: {
  title: string
  items: string[]
  selected: string[]
  emptyText: string
  onToggle: (key: string) => void
  rawBody?: boolean
  rawEnabled?: boolean
  onToggleRaw?: () => void
}) {
  return (
    <div className="rounded-lg border border-border bg-muted p-3">
      <div className="mb-2 flex items-center justify-between gap-2"><span className="text-[11px] font-semibold text-foreground">{title}</span><span className="text-[9px] text-[var(--color-text-tertiary)]">{rawBody ? (rawEnabled ? '已开放' : '固定') : `${selected.length}/${items.length}`}</span></div>
      {items.length > 0 ? <div className="space-y-1.5">{items.map(key => {
        const active = selected.some(value => value.toLowerCase() === key.toLowerCase())
        return <button key={key} type="button" role="checkbox" aria-checked={active} onClick={() => onToggle(key)} className={`flex w-full items-center gap-2 rounded-md border px-2.5 py-2 text-left font-mono text-[10px] transition-colors ${active ? 'border-brand-line bg-card text-brand-ink shadow-sm' : 'border-transparent text-muted-foreground hover:border-border hover:bg-card'}`}><span className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${active ? 'border-brand bg-brand text-[var(--color-text-inverse)]' : 'border-border bg-card'}`}>{active && <Check size={10} strokeWidth={3} />}</span><span className="min-w-0 truncate" title={bodyKeyLabel(key)}>{title === '请求 Body' ? bodyKeyLabel(key) : key}</span></button>
      })}</div> : rawBody ? <button type="button" role="checkbox" aria-checked={rawEnabled} onClick={onToggleRaw} className={`flex w-full items-start gap-2 rounded-md border px-2.5 py-2 text-left text-[10px] leading-4 transition-colors ${rawEnabled ? 'border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] text-[var(--color-warning)]' : 'border-border bg-card text-muted-foreground'}`}><span className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded border ${rawEnabled ? 'border-[var(--color-warning)] bg-[var(--color-warning)] text-[var(--color-text-inverse)]' : 'border-border'}`}>{rawEnabled && <Check size={10} strokeWidth={3} />}</span><span>允许调用方完整替换 Raw Body</span></button> : <div className="rounded-md border border-dashed border-border bg-card px-2.5 py-4 text-center text-[10px] text-[var(--color-text-tertiary)]">{emptyText}</div>}
    </div>
  )
}

const sensitiveName = /(authorization|authentication|auth(?:[-_]?(?:code|key|token))?(?:$|[-_])|cookie|credential|token|secret|password|passwd|api[-_]?key|private[-_]?key|session|signature|bearer|jwt)/i
const managedHeaders = new Set(['accept', 'accept-encoding', 'authorization', 'connection', 'content-length', 'content-type', 'cookie', 'host', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer', 'transfer-encoding', 'upgrade', 'user-agent', 'x-api-hub-key'])
// 个人变量占位符（与后端 PERSONAL_REF_RE 同字符集）；不用 g 标志，避免 test 状态残留
const personalRefPattern = /\{\{(privacy|env):[A-Za-z0-9_.-]+\}\}/

function publicationDraft(item: HubInterface): PublicationDraft {
  const hasStoredConfiguration = item.http_enabled || Boolean(item.proxy_slug)
    || item.proxy_query_keys.length > 0 || item.proxy_header_keys.length > 0
    || item.proxy_body_enabled || item.proxy_body_keys.length > 0
  return hasStoredConfiguration ? {
    slug: item.proxy_slug || suggestedSlug(item),
    queryKeys: [...item.proxy_query_keys],
    headerKeys: [...item.proxy_header_keys],
    bodyEnabled: item.proxy_body_enabled,
    bodyKeys: [...item.proxy_body_keys],
  } : suggestedPublicationDraft(item)
}

function suggestedPublicationDraft(item: HubInterface): PublicationDraft {
  const candidates = recommendedPublicationCandidates(item)
  return {
    slug: item.proxy_slug || suggestedSlug(item),
    queryKeys: [...candidates.query],
    headerKeys: [...candidates.headers],
    bodyEnabled: candidates.body.length > 0,
    bodyKeys: [...candidates.body],
  }
}

function publicationCandidates(item: HubInterface | null) {
  if (!item) return { query: [] as string[], headers: [] as string[], body: [] as string[], rawBody: false }
  const recommended = recommendedPublicationCandidates(item)
  return {
    query: uniqueKeys([...recommended.query, ...item.proxy_query_keys], false, false),
    headers: uniqueKeys([...recommended.headers, ...item.proxy_header_keys], true, false),
    body: uniqueKeys([...recommended.body, ...item.proxy_body_keys], false, false),
    rawBody: recommended.rawBody,
  }
}

function recommendedPublicationCandidates(item: HubInterface) {
  const query = uniqueKeys(item.query_params.map(value => value.key), false, true)
  const headers = uniqueKeys(
    item.headers.map(value => value.key).filter(key => !managedHeaders.has(key.trim().toLowerCase())),
    true,
    true,
  )
  let body: string[] = []
  if (item.body_type === 'json') {
    try {
      const value = JSON.parse(item.body_content)
      if (value && typeof value === 'object' && !Array.isArray(value)) body = jsonLeafPaths(value)
    } catch { body = [] }
  } else if (item.body_type === 'form') {
    body = item.body_content.split('\n').map(line => line.trim()).filter(line => line && !line.startsWith('#') && line.includes('=')).map(line => line.split('=', 1)[0].trim())
  } else if (item.body_type === 'multipart') {
    body = [
      ...item.body_content.split('\n').map(line => line.trim()).filter(line => line && !line.startsWith('#') && line.includes('=')).map(line => line.split('=', 1)[0].trim()),
      ...item.file_fields.map(field => field.key),
    ]
  }
  body = uniqueKeys(body, false, true)
  return { query, headers, body, rawBody: item.body_type === 'raw' }
}

function uniqueKeys(values: string[], lower: boolean, safeOnly: boolean) {
  const result: string[] = []
  const seen = new Set<string>()
  values.forEach(value => {
    const key = (value || '').trim()
    const marker = lower ? key.toLowerCase() : key
    if (!key || seen.has(marker) || (safeOnly && sensitiveName.test(key))) return
    seen.add(marker)
    result.push(key)
  })
  return result
}

function jsonLeafPaths(value: Record<string, unknown>, prefix = ''): string[] {
  return Object.entries(value).flatMap(([key, item]) => {
    if (sensitiveName.test(key)) return []
    const path = `${prefix}/${key.replaceAll('~', '~0').replaceAll('/', '~1')}`
    return item && typeof item === 'object' && !Array.isArray(item) && Object.keys(item).length
      ? jsonLeafPaths(item as Record<string, unknown>, path)
      : [path]
  })
}

function suggestedSlug(item: HubInterface) {
  try {
    const url = new URL(item.url)
    const tail = url.pathname.replace(/\/$/, '').split('/').pop() || item.name
    const slug = tail.toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^[-_]+|[-_]+$/g, '')
    return slug.slice(0, 64) || `interface-${item.id || 'new'}`
  } catch {
    return `interface-${item.id || 'new'}`
  }
}

function toggleValue(values: string[], key: string, lower = false) {
  const marker = lower ? key.toLowerCase() : key
  const exists = values.some(value => (lower ? value.toLowerCase() : value) === marker)
  return exists
    ? values.filter(value => (lower ? value.toLowerCase() : value) !== marker)
    : [...values, key]
}

function editableLabels(value: ForwardingPackage) {
  return [...new Set([
    ...value.query_params.map(item => item.key),
    ...value.header_params.map(item => item.key),
    ...value.editable_body_keys.map(bodyKeyLabel),
    ...(value.body_enabled && value.body_type === 'raw' ? ['Raw Body（整段）'] : []),
  ])]
}

function bodyKeyLabel(value: string) {
  const parts = value.split('/').filter(Boolean)
    .map(part => part.replaceAll('~1', '/').replaceAll('~0', '~'))
  return parts.join('.') || value
}

function forwardingCode(value: ForwardingPackage, tab: 'curl' | 'python' | 'javascript') {
  const endpoint = `${window.location.origin}${value.path}`
  const queryEntries = value.query_params.map(item => [item.key, item.value || `<${item.key}>`] as const)
  const headerEntries = value.header_params.map(item => [item.key, item.value || `<${item.key}>`] as const)
  if (tab === 'python') return pythonCode(value, endpoint, queryEntries, headerEntries)
  if (tab === 'javascript') return javascriptCode(value, endpoint, queryEntries, headerEntries)
  return curlCode(value, endpoint, queryEntries, headerEntries)
}

function curlCode(value: ForwardingPackage, endpoint: string, queryEntries: readonly (readonly [string, string])[], headerEntries: readonly (readonly [string, string])[]) {
  const query = queryEntries.length
    ? `?${queryEntries.map(([key, item]) => `${encodeURIComponent(key)}=${item}`).join('&')}`
    : ''
  const body = forwardingBody(value)
  const lines = [`curl -X ${value.method} ${shellQuote(endpoint + query)}`, `  -H ${shellQuote(`${value.key_header}: ${value.secret}`)}`]
  headerEntries.forEach(([key, item]) => lines.push(`  -H ${shellQuote(`${key}: ${item}`)}`))
  if (value.body_enabled && value.body_type === 'multipart') {
    value.multipart_fields.forEach(item => lines.push(`  -F ${shellQuote(`${item.key}=${item.value}`)}`))
    value.file_fields.forEach(field => lines.push(`  -F ${shellQuote(`${field.key}=@/path/to/file`)}`))
  } else if (body) {
    const contentType = forwardingContentType(value.body_type)
    if (contentType) lines.push(`  -H ${shellQuote(`Content-Type: ${contentType}`)}`)
    lines.push(`  ${value.body_type === 'raw' ? '--data-binary' : '--data-raw'} ${shellQuote(body)}`)
  }
  return lines.join(' \\\n')
}

function pythonCode(value: ForwardingPackage, endpoint: string, queryEntries: readonly (readonly [string, string])[], headerEntries: readonly (readonly [string, string])[]) {
  const headers = Object.fromEntries([[value.key_header, value.secret], ...headerEntries])
  const body = forwardingBody(value)
  const contentType = forwardingContentType(value.body_type)
  if (body && contentType) headers['Content-Type'] = contentType
  const parts = [
    'import requests',
    '',
    `url = ${JSON.stringify(endpoint)}`,
    `params = ${JSON.stringify(Object.fromEntries(queryEntries), null, 2)}`,
    `headers = ${JSON.stringify(headers, null, 2)}`,
  ]
  if (value.body_enabled && value.body_type === 'multipart') {
    parts.push(`data = ${JSON.stringify(Object.fromEntries(value.multipart_fields.map(item => [item.key, item.value])), null, 2)}`)
    parts.push('files = [')
    value.file_fields.forEach(field => parts.push(`    (${JSON.stringify(field.key)}, ("YOUR_FILENAME", open("/path/to/file", "rb"), "application/octet-stream")),`))
    parts.push(']')
  } else if (body) parts.push(`body = ${JSON.stringify(body)}`)
  const requestPayload = value.body_enabled && value.body_type === 'multipart' ? ', data=data, files=files' : body ? ', data=body' : ''
  parts.push('', `response = requests.request(${JSON.stringify(value.method)}, url, params=params, headers=headers${requestPayload})`, 'response.raise_for_status()', 'print(response.text)')
  return parts.join('\n')
}

function javascriptCode(value: ForwardingPackage, endpoint: string, queryEntries: readonly (readonly [string, string])[], headerEntries: readonly (readonly [string, string])[]) {
  const headers = Object.fromEntries([[value.key_header, value.secret], ...headerEntries])
  const body = forwardingBody(value)
  const contentType = forwardingContentType(value.body_type)
  if (body && contentType) headers['Content-Type'] = contentType
  const parts = [
    `const url = new URL(${JSON.stringify(endpoint)});`,
    `const params = ${JSON.stringify(Object.fromEntries(queryEntries), null, 2)};`,
    'Object.entries(params).forEach(([key, value]) => url.searchParams.set(key, value));',
    '',
  ]
  if (value.body_enabled && value.body_type === 'multipart') {
    parts.push('const form = new FormData();')
    value.multipart_fields.forEach(item => parts.push(`form.append(${JSON.stringify(item.key)}, ${JSON.stringify(item.value)});`))
    value.file_fields.forEach((field, index) => {
      const selector = `input[type="file"][data-field=${JSON.stringify(field.key)}]`
      const variable = `files_${field.key.replace(/[^a-zA-Z0-9_$]/g, '_') || 'upload'}_${index + 1}`
      parts.push(`const ${variable} = document.querySelector(${JSON.stringify(selector)})?.files;`)
      parts.push(`if (!${variable}?.length) throw new Error(${JSON.stringify(`请选择文件字段 ${field.key}`)});`)
      parts.push(`${field.multiple ? `[...${variable}].forEach(file => form.append(${JSON.stringify(field.key)}, file));` : `form.append(${JSON.stringify(field.key)}, ${variable}[0]);`}`)
    })
    parts.push('')
  }
  parts.push(
    'const response = await fetch(url, {',
    `  method: ${JSON.stringify(value.method)},`,
    `  headers: ${JSON.stringify(headers, null, 2).replaceAll('\n', '\n  ')},`,
  )
  if (value.body_enabled && value.body_type === 'multipart') parts.push('  body: form,')
  else if (body) parts.push(`  body: ${JSON.stringify(body)},`)
  parts.push('});', 'if (!response.ok) throw new Error(`HTTP ${response.status}`);', 'console.log(await response.text());')
  return parts.join('\n')
}

function forwardingBody(value: ForwardingPackage) {
  if (!value.body_enabled || value.body_type === 'multipart') return ''
  if (value.body_type === 'raw') return 'YOUR_REQUEST_BODY'
  return value.body_template
}

function forwardingContentType(bodyType: ForwardingPackage['body_type']) {
  if (bodyType === 'json') return 'application/json'
  if (bodyType === 'form') return 'application/x-www-form-urlencoded'
  return ''
}

function shellQuote(value: string) {
  return `'${value.replaceAll("'", "'\\''")}'`
}
