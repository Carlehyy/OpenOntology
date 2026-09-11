import { useEffect, useId, useRef, useState } from 'react'
import { Loader2, X } from 'lucide-react'

import type { McpManagementClient } from '@/api/community'
import type { McpTransport, SuperMcpServer } from '@/api/superAssistant'
import { toast } from 'sonner'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import {
  parseMcpClientConfig,
  type ParsedMcpClientServer,
} from '@/lib/mcpClientConfig'


const errorText = (error: any, fallback = '操作失败') =>
  error?.detail || error?.message || fallback


export default function McpServerDialog({
  server,
  client,
  onClose,
  onSaved,
}: {
  server?: SuperMcpServer
  client: McpManagementClient
  onClose: () => void
  onSaved: () => Promise<void>
}) {
  const titleId = useId()
  const descriptionId = useId()
  const dialogRef = useRef<HTMLElement>(null)
  const clientConfigRef = useRef<HTMLTextAreaElement>(null)
  const [identifier, setIdentifier] = useState(server?.name || '')
  const [displayName, setDisplayName] = useState(server?.display_name || '')
  const [descriptionText, setDescriptionText] = useState(server?.description || '')
  const [transport, setTransport] = useState<McpTransport>(server?.transport || 'streamable_http')
  const [url, setUrl] = useState(server?.url || '')
  const [command, setCommand] = useState(server?.command || '')
  const [args, setArgs] = useState(JSON.stringify(server?.args || [], null, 2))
  const [headers, setHeaders] = useState('')
  const [env, setEnv] = useState('')
  const [clientConfig, setClientConfig] = useState('')
  const [clientConfigOptions, setClientConfigOptions] = useState<ParsedMcpClientServer[]>([])
  const [selectedClientConfig, setSelectedClientConfig] = useState(0)
  const [clientConfigMessage, setClientConfigMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    const previousFocus = document.activeElement as HTMLElement | null
    const selector = 'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), summary, [tabindex]:not([tabindex="-1"])'
    const focusables = () => Array.from(dialog.querySelectorAll<HTMLElement>(selector))
    focusables()[0]?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
        return
      }
      if (event.key !== 'Tab') return
      const items = focusables()
      if (!items.length) return
      const first = items[0]
      const last = items[items.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      previousFocus?.focus()
    }
  }, [onClose])

  useEffect(() => {
    const textarea = clientConfigRef.current
    if (!textarea) return
    textarea.style.height = 'auto'
    textarea.style.height = `${textarea.scrollHeight}px`
  }, [clientConfig])

  const parseStringMap = (value: string, label: string): Record<string, string> | undefined => {
    if (!value.trim()) return undefined
    const parsed = JSON.parse(value)
    if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object' || Object.values(parsed).some(item => typeof item !== 'string')) {
      throw new Error(`${label} 必须是字符串键值 JSON 对象`)
    }
    return parsed as Record<string, string>
  }

  const fillFromClientConfig = (config: ParsedMcpClientServer) => {
    setIdentifier(config.name)
    setTransport(config.transport)
    setUrl(config.url)
    setCommand(config.command)
    setArgs(JSON.stringify(config.args, null, 2))
    setHeaders(Object.keys(config.headers).length ? JSON.stringify(config.headers, null, 2) : '')
    setEnv(Object.keys(config.env).length ? JSON.stringify(config.env, null, 2) : '')
    setClientConfigMessage([
      `已按「${config.name}」填入标识与连接信息；名称和描述需要手动填写。`,
      ...config.warnings,
    ].join(' '))
  }

  const applyClientConfig = () => {
    setError('')
    try {
      const parsed = parseMcpClientConfig(clientConfig)
      setClientConfigOptions(parsed.servers)
      setSelectedClientConfig(0)
      fillFromClientConfig(parsed.servers[0])
      if (parsed.servers.length > 1) {
        setClientConfigMessage([
          `已从 ${parsed.sourceLabel} 识别 ${parsed.servers.length} 个 Server，当前填入第 1 个；可在下方切换。`,
          ...parsed.servers[0].warnings,
        ].join(' '))
      }
    } catch (parseError) {
      setClientConfigOptions([])
      setClientConfigMessage('')
      setError(errorText(parseError, '无法解析 MCP 配置'))
    }
  }

  const save = async () => {
    setBusy(true)
    setError('')
    try {
      const normalizedName = identifier.trim()
      if (!/^[a-zA-Z0-9][a-zA-Z0-9_-]*$/.test(normalizedName)) {
        throw new Error('标识只能包含字母、数字、下划线和连字符，且必须以字母或数字开头')
      }
      if (!displayName.trim()) throw new Error('名称为必填项')
      if (!descriptionText.trim()) throw new Error('描述为必填项')
      const parsedHeaders = parseStringMap(headers, 'Headers')
      const parsedEnv = parseStringMap(env, 'env')
      const parsedArgs = JSON.parse(args || '[]')
      if (!Array.isArray(parsedArgs) || parsedArgs.some(item => typeof item !== 'string')) {
        throw new Error('args 必须是字符串 JSON 数组')
      }
      if (transport === 'stdio' && !command.trim()) throw new Error('stdio 传输必须填写 command')
      if (transport !== 'stdio' && !url.trim()) throw new Error('HTTP 传输必须填写 URL')
      const changedTransport = !!server && server.transport !== transport
      const connection = transport === 'stdio'
        ? { transport, url: '', command: command.trim(), args: parsedArgs }
        : { transport, url: url.trim(), command: null, args: [] }
      if (server) {
        await client.updateMcpServer(server.id, {
          display_name: displayName.trim(),
          description: descriptionText.trim(),
          ...connection,
          ...(parsedHeaders ? { headers: parsedHeaders } : changedTransport && transport === 'stdio' ? { headers: {} } : {}),
          ...(parsedEnv ? { env: parsedEnv } : changedTransport && transport !== 'stdio' ? { env: {} } : {}),
        })
      } else {
        await client.createMcpServer({
          name: normalizedName,
          display_name: displayName.trim(),
          description: descriptionText.trim(),
          ...connection,
          headers: parsedHeaders || {},
          env: parsedEnv || {},
          enabled: false,
          require_confirmation: true,
        })
      }
      await onSaved()
      toast.success(server ? 'MCP 配置已更新' : 'MCP Server 已添加', { description: '配置已登记；建议立即执行连接测试以刷新工具清单。' })
      onClose()
    } catch (saveError) {
      setError(errorText(saveError, '保存失败'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-[70] flex items-center justify-center bg-[var(--color-bg-overlay)] p-4 backdrop-blur-[2px]" onMouseDown={onClose}>
      <section
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        onMouseDown={event => event.stopPropagation()}
        className="flex max-h-[88dvh] w-full max-w-3xl flex-col overflow-hidden rounded-2xl border border-border bg-card shadow-[0_28px_90px_rgba(15,23,42,0.24)]"
      >
        <header className="flex shrink-0 items-start justify-between border-b border-border px-5 py-4 sm:px-6">
          <div>
            <h2 id={titleId} className="text-base font-semibold text-foreground">{server ? `编辑 MCP：${server.display_name || server.name}` : '添加 MCP Server'}</h2>
            <p id={descriptionId} className="mt-1 text-xs leading-5 text-muted-foreground">支持粘贴客户端 JSON，也可直接配置 stdio、SSE 或 Streamable HTTP。</p>
          </div>
          <button type="button" onClick={onClose} aria-label="关闭添加 MCP 弹窗" className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            <X size={17} />
          </button>
        </header>

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto overscroll-contain p-5 [scrollbar-gutter:stable] sm:p-6">
          {!server && (
            <details className="rounded-xl border border-border bg-muted" open>
              <summary className="cursor-pointer px-4 py-3 text-xs font-medium text-foreground">粘贴 MCP 客户端 JSON</summary>
              <div className="space-y-3 border-t border-border p-4">
                <textarea
                  ref={clientConfigRef}
                  value={clientConfig}
                  onChange={event => {
                    setClientConfig(event.target.value)
                    setClientConfigOptions([])
                    setClientConfigMessage('')
                    setError('')
                  }}
                  rows={8}
                  aria-label="MCP 客户端 JSON"
                  placeholder={'{\n  "mcpServers": {\n    "api-hub": {\n      "command": "npx",\n      "args": ["-y", "mcp-remote", "https://example.com/mcp"]\n    }\n  }\n}'}
                  className="w-full resize-none overflow-hidden rounded-xl border border-border bg-card p-3 font-mono text-xs leading-5 text-foreground outline-none transition focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring"
                />
                <button type="button" onClick={applyClientConfig} disabled={!clientConfig.trim()} className="min-h-10 rounded-lg border border-border bg-card px-3 text-xs font-medium text-brand-ink transition-colors hover:border-brand-mist hover:bg-brand-soft disabled:cursor-not-allowed disabled:opacity-45">
                  解析并填入下方表单
                </button>
                <p className="text-[11px] leading-5 text-muted-foreground">
                  兼容 Claude、Cursor、Cline、Windsurf、Gemini、JetBrains、Continue、VS Code 与 Zed 常见 JSON / JSONC 格式。
                </p>
                {clientConfigOptions.length > 1 && (
                  <label className="block text-xs font-medium text-[var(--color-text-secondary)]">
                    选择要填入的 MCP Server
                    <Select
                      value={String(selectedClientConfig)}
                      onValueChange={value => {
                        const nextIndex = Number(value)
                        setSelectedClientConfig(nextIndex)
                        fillFromClientConfig(clientConfigOptions[nextIndex])
                      }}
                    >
                      <SelectTrigger className="mt-1.5 h-10 w-full rounded-lg bg-card px-3 text-xs">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                      {clientConfigOptions.map((config, index) => (
                        <SelectItem key={`${config.name}-${index}`} value={String(index)}>{config.name}</SelectItem>
                      ))}
                      </SelectContent>
                    </Select>
                  </label>
                )}
                {clientConfigMessage && <p role="status" className="rounded-lg bg-brand-soft px-3 py-2 text-[11px] leading-5 text-brand-ink">{clientConfigMessage}</p>}
              </div>
            </details>
          )}

          <div className="grid gap-4 md:grid-cols-2">
            <label className="block text-xs font-medium text-[var(--color-text-secondary)]">
              名称 <span className="text-destructive">*</span>
              <input value={displayName} onChange={event => setDisplayName(event.target.value)} placeholder="如：DMP 数据服务" className="mt-1.5 min-h-11 w-full rounded-xl border border-border bg-card px-3 text-sm text-foreground outline-none transition focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring" />
              <span className="mt-1.5 block text-[11px] font-normal leading-4 text-muted-foreground">列表与工具清单中展示的可读名称，需手动填写。</span>
            </label>
            <label className="block text-xs font-medium text-[var(--color-text-secondary)]">
              标识 <span className="text-destructive">*</span>
              <input value={identifier} disabled={!!server} onChange={event => setIdentifier(event.target.value)} placeholder="dmp-mcp-server" className="mt-1.5 min-h-11 w-full rounded-xl border border-border bg-card px-3 font-mono text-sm text-foreground outline-none transition focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring disabled:bg-muted disabled:text-muted-foreground" />
              <span className="mt-1.5 block text-[11px] font-normal leading-4 text-muted-foreground">唯一标识，可从客户端 JSON 解析；保存后不可修改。</span>
            </label>
          </div>
          <label className="block text-xs font-medium text-[var(--color-text-secondary)]">
            描述 <span className="text-destructive">*</span>
            <textarea value={descriptionText} onChange={event => setDescriptionText(event.target.value)} rows={2} placeholder="该 MCP Server 的用途说明，如：提供 DMP 平台的数据检索能力" className="mt-1.5 w-full resize-y rounded-xl border border-border bg-card p-3 text-sm leading-5 text-foreground outline-none transition focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring" />
          </label>
          <div className="grid gap-4 md:grid-cols-2">
            <label className="block text-xs font-medium text-[var(--color-text-secondary)]">
              传输方式 <span className="text-destructive">*</span>
              <Select value={transport} onValueChange={value => setTransport(value as McpTransport)}>
                <SelectTrigger className="mt-1.5 h-11 w-full rounded-xl bg-card px-3 text-sm">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="streamable_http">Streamable HTTP（推荐）</SelectItem>
                  <SelectItem value="sse">SSE（旧版兼容）</SelectItem>
                  <SelectItem value="stdio">stdio（启动本地进程）</SelectItem>
                </SelectContent>
              </Select>
            </label>
          </div>

          {transport === 'stdio' ? (
            <>
              <label className="block text-xs font-medium text-[var(--color-text-secondary)]">
                command <span className="text-destructive">*</span>
                <input value={command} onChange={event => setCommand(event.target.value)} placeholder="npx" className="mt-1.5 min-h-11 w-full rounded-xl border border-border bg-card px-3 font-mono text-sm text-foreground outline-none transition focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring" />
              </label>
              <div className="grid gap-4 md:grid-cols-2">
                <label className="block text-xs font-medium text-[var(--color-text-secondary)]">
                  args JSON
                  <textarea value={args} onChange={event => setArgs(event.target.value)} rows={5} placeholder={'["-y", "@example/mcp-server"]'} className="mt-1.5 w-full resize-y rounded-xl border border-border bg-card p-3 font-mono text-xs leading-5 text-foreground outline-none transition focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring" />
                </label>
                <label className="block text-xs font-medium text-[var(--color-text-secondary)]">
                  env JSON
                  <textarea value={env} onChange={event => setEnv(event.target.value)} rows={5} placeholder={server ? `留空保持现有环境变量（${server.env_names.join(', ') || '无'}）` : '{\n  "API_KEY": "…"\n}'} className="mt-1.5 w-full resize-y rounded-xl border border-border bg-card p-3 font-mono text-xs leading-5 text-foreground outline-none transition focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring" />
                </label>
              </div>
              <p className="rounded-xl border border-[color:var(--color-warning)]/30 bg-[var(--color-warning-bg)] px-4 py-3 text-xs leading-5 text-[var(--color-warning)]">stdio 会在后端容器内启动进程，部署方必须显式启用并允许该 command。环境变量会加密存储且不会回显。</p>
            </>
          ) : (
            <>
              <label className="block text-xs font-medium text-[var(--color-text-secondary)]">
                MCP URL <span className="text-destructive">*</span>
                <input type="url" value={url} onChange={event => setUrl(event.target.value)} placeholder="https://mcp.example.com/mcp" className="mt-1.5 min-h-11 w-full rounded-xl border border-border bg-card px-3 text-sm text-foreground outline-none transition focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring" />
                <span className="mt-1.5 block text-[11px] font-normal leading-4 text-muted-foreground">公网地址可直接连接；生产环境会拒绝环回、内网和链路本地地址。</span>
              </label>
              <label className="block text-xs font-medium text-[var(--color-text-secondary)]">
                请求头 JSON
                <textarea value={headers} onChange={event => setHeaders(event.target.value)} rows={4} placeholder={server ? `留空保持现有请求头（${server.header_names.join(', ') || '无'}）` : '{\n  "Authorization": "Bearer …"\n}'} className="mt-1.5 w-full resize-y rounded-xl border border-border bg-card p-3 font-mono text-xs leading-5 text-foreground outline-none transition focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring" />
              </label>
            </>
          )}

          {error && <p role="alert" className="rounded-xl border border-destructive/30 bg-[var(--color-danger-bg)] px-4 py-3 text-xs leading-5 text-destructive">{error}</p>}
        </div>

        <footer className="flex shrink-0 justify-center gap-3 border-t border-border bg-muted px-5 py-4 sm:px-6">
          <button type="button" onClick={onClose} className="min-h-10 min-w-24 rounded-xl border border-border bg-card px-4 text-xs font-medium text-[var(--color-text-secondary)] transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">取消</button>
          <button type="button" onClick={save} disabled={busy || !identifier.trim() || !displayName.trim() || !descriptionText.trim() || (transport === 'stdio' ? !command.trim() : !url.trim())} className="inline-flex min-h-10 min-w-24 items-center justify-center gap-2 rounded-xl bg-brand px-4 text-xs font-medium text-white transition-colors hover:bg-brand-deep focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-45">
            {busy && <Loader2 size={13} className="animate-spin motion-reduce:animate-none" />} 保存
          </button>
        </footer>
      </section>
    </div>
  )
}
