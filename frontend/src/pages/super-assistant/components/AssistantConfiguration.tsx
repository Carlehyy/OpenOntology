import { useCallback, useEffect, useRef, useState } from 'react'
import {
  FileCode2, FileText, Folder, Loader2, Pencil, PlugZap, Plus,
  Save, Sparkles, Trash2, Upload, Wrench, X,
} from 'lucide-react'

import {
  superAssistantApi,
  type AssistantTool,
  type McpTransport,
  type SkillFile,
  type SuperMcpServer,
  type SuperSkill,
} from '@/api/superAssistant'
import { toast } from 'sonner'
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select'
import {
  parseMcpClientConfig,
  type ParsedMcpClientServer,
} from '@/lib/mcpClientConfig'
import { ApprovalTab, EvolutionPendingBadge, MemoryTab } from './AssistantEvolution'
import { errorText } from './assistantPanelUtils'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { groupAssistantTools } from './toolLogic'

export { errorText }


// 薄封装 ui/dialog：统一配置域弹层的头部与尺寸语言；
// 遮罩、Esc 关闭与焦点管理交给 Radix，不再自绘 fixed 弹层和焦点陷阱。
// （外部集成弹层等同语言复用，故导出）
export function DialogShell({ title, description, size = 'default', onClose, children, contentClassName, onOpenAutoFocus, icon }: {
  title: string
  description?: string
  size?: 'default' | 'large' | 'wide'
  onClose: () => void
  children: React.ReactNode
  /** 追加到弹层根节点的类（如外部集成的固定高度） */
  contentClassName?: string
  /** 覆盖 Radix 默认的首个可聚焦元素聚焦（如聚焦首个输入框而非左栏 tab） */
  onOpenAutoFocus?: (event: Event) => void
  /** 标准头部图标盒（DESIGN.md §4.5：h-10 w-10 rounded-xl 品牌底 + 18px lucide） */
  icon?: React.ReactNode
}) {
  const sizeClass = {
    default: 'max-h-[90dvh] w-[min(92vw,36rem)]',
    large: 'max-h-[85dvh] w-[min(94vw,48rem)]',
    wide: 'max-h-[90dvh] w-[min(96vw,64rem)]',
  }[size]

  return (
    <Dialog open onOpenChange={value => { if (!value) onClose() }}>
      <DialogContent
        onOpenAutoFocus={onOpenAutoFocus}
        className={`flex flex-col overflow-hidden p-0 ${sizeClass} ${contentClassName ?? ''}`}
      >
        <DialogHeader
          icon={icon}
          className="mb-0 shrink-0 border-b border-[var(--color-border)] px-5 py-4 pr-12"
        >
          <div>
            <DialogTitle className="text-base font-semibold leading-6">{title}</DialogTitle>
            {description && <DialogDescription className="mt-1 text-sm leading-6">{description}</DialogDescription>}
          </div>
        </DialogHeader>
        {children}
      </DialogContent>
    </Dialog>
  )
}
function SkillCreateDialog({ onClose, onSaved }: { onClose: () => void; onSaved: () => Promise<void> }) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [content, setContent] = useState('')
  const [alwaysActive, setAlwaysActive] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const save = async () => {
    if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(name) || name.length > 64 || !description.trim() || !content.trim()) {
      setError('请填写合法的技能名称、技能描述和具体内容')
      return
    }
    setBusy(true); setError('')
    try {
      await superAssistantApi.createSkill({
        name,
        description: description.trim(),
        content,
        enabled: true,
        always_active: alwaysActive,
      })
      await onSaved()
      toast.success('Skill 已创建', { description: '已生成标准目录和 SKILL.md' })
      onClose()
    } catch (error) { setError(errorText(error, '创建失败')) } finally { setBusy(false) }
  }

  return (
    <DialogShell title="新建 Skill" description="系统会生成标准 SKILL.md，并在独立目录中保存该技能。" onClose={onClose} icon={<Sparkles size={18} />}>
      <div className="flex-1 space-y-4 overflow-y-auto p-5">
        <label className="block text-xs text-[var(--color-text-secondary)]">技能名称 <span className="text-red-500">*</span>
          <input value={name} onChange={event => setName(event.target.value.toLowerCase().replace(/[_\s]+/g, '-'))} placeholder="research-helper"
            className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 font-mono text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring" />
          <span className="mt-1 block text-[10px] leading-4 text-[var(--color-text-tertiary)]">用于技能包目录和调用标识，仅支持小写字母、数字和连字符。</span>
        </label>
        <label className="block text-xs text-[var(--color-text-secondary)]">技能描述 <span className="text-red-500">*</span>
          <textarea value={description} onChange={event => setDescription(event.target.value)} rows={2}
            placeholder="说明这个技能做什么，以及什么情况下应使用它"
            className="mt-1.5 w-full resize-y rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)] p-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring" />
        </label>
        <label className="block text-xs text-[var(--color-text-secondary)]">具体内容 <span className="text-red-500">*</span>
          <textarea value={content} onChange={event => setContent(event.target.value)} rows={10} placeholder="# 工作流程&#10;&#10;1. …"
            className="mt-1.5 w-full resize-y rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)] p-3 font-mono text-xs leading-5 outline-none focus-visible:ring-2 focus-visible:ring-ring" />
        </label>
        <label className="flex min-h-11 items-center justify-between rounded-lg border border-[var(--color-border)] px-3 text-xs text-[var(--color-text-secondary)]">
          常驻系统提示
          <input type="checkbox" checked={alwaysActive} onChange={event => setAlwaysActive(event.target.checked)} className="h-4 w-4 accent-brand" />
        </label>
        {error && <p role="alert" className="text-xs text-red-600">{error}</p>}
      </div>
      <footer className="flex justify-center gap-2 border-t border-[var(--color-border)] px-5 py-4">
        <button onClick={onClose} className="min-h-10 rounded-lg px-4 text-xs text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)]">取消</button>
        <button onClick={save} disabled={busy} className="inline-flex min-h-10 items-center gap-2 rounded-lg bg-brand px-4 text-xs font-medium text-white hover:bg-brand-deep disabled:opacity-50">
          {busy && <Loader2 size={13} className="animate-spin" />} 创建
        </button>
      </footer>
    </DialogShell>
  )
}

function SkillEditor({ skill, onClose, onSaved }: { skill: SuperSkill; onClose: () => void; onSaved: () => Promise<void> }) {
  const [files, setFiles] = useState<SkillFile[]>(skill.manifest)
  const [revision, setRevision] = useState(skill.revision)
  const [selectedPath, setSelectedPath] = useState('SKILL.md')
  const [content, setContent] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [newPath, setNewPath] = useState('')
  const [error, setError] = useState('')
  const [confirmingRemove, setConfirmingRemove] = useState(false)
  const [removing, setRemoving] = useState(false)

  const loadFile = useCallback(async (path: string) => {
    setLoading(true); setError('')
    try {
      const result = await superAssistantApi.skillFile(skill.id, path)
      setContent(result.content); setSelectedPath(path)
    } catch (error) { setError(errorText(error, '读取文件失败')) } finally { setLoading(false) }
  }, [skill.id])

  useEffect(() => { void loadFile('SKILL.md') }, [loadFile])

  const save = async () => {
    setSaving(true); setError('')
    try {
      const result: any = await superAssistantApi.putSkillFile(skill.id, selectedPath, content)
      setFiles(result.manifest || files)
      setRevision(result.revision || revision)
      await onSaved()
      toast.success('文件已保存', { description: `${selectedPath} · revision ${result.revision}` })
    } catch (error) { setError(errorText(error, '保存失败')) } finally { setSaving(false) }
  }

  const startNewFile = () => {
    const path = newPath.trim().replace(/^\/+/, '')
    if (!path || path.includes('..')) { setError('请输入 Skill 目录内的合法相对路径'); return }
    setSelectedPath(path); setContent(''); setNewPath(''); setLoading(false); setError('')
  }

  // SKILL.md 保护由删除按钮的渲染条件（selectedPath !== 'SKILL.md'）保证
  const removeFile = async () => {
    setRemoving(true)
    try {
      await superAssistantApi.deleteSkillFile(skill.id, selectedPath)
      const next = await superAssistantApi.skillFiles(skill.id)
      setFiles(next); await loadFile('SKILL.md'); await onSaved()
      toast.success('文件已删除')
    } catch (error) { setError(errorText(error, '删除失败')) } finally {
      setRemoving(false); setConfirmingRemove(false)
    }
  }

  return (
    <>
    <DialogShell size="wide" title={`编辑 Skill：${skill.name}`} description={`revision ${revision} · ${files.length} 个文件`} onClose={onClose}>
      <div className="grid min-h-0 flex-1 grid-cols-1 md:grid-cols-[260px_minmax(0,1fr)]">
        <aside className="flex min-h-40 flex-col border-b border-[var(--color-border)] bg-[var(--color-bg-base)] md:border-b-0 md:border-r">
          <div className="border-b border-[var(--color-border)] p-3">
            <label className="text-[11px] text-[var(--color-text-secondary)]">新建相对路径</label>
            <div className="mt-1 flex gap-1.5">
              <input value={newPath} onChange={event => setNewPath(event.target.value)} onKeyDown={event => event.key === 'Enter' && startNewFile()}
                placeholder="references/guide.md" className="min-w-0 flex-1 rounded-md border border-[var(--color-border)] bg-white px-2 text-xs outline-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" />
              <button onClick={startNewFile} aria-label="新建文件" className="flex h-10 w-10 items-center justify-center rounded-md border border-[var(--color-border)] bg-white hover:bg-brand-soft"><Plus size={14} /></button>
            </div>
          </div>
          <div className="max-h-52 flex-1 overflow-y-auto p-2 md:max-h-none">
            {files.map(file => (
              <button key={file.path} onClick={() => file.editable && void loadFile(file.path)} disabled={!file.editable}
                className={`mb-1 flex min-h-9 w-full items-center gap-2 rounded-md px-2 text-left text-xs ${selectedPath === file.path ? 'bg-brand-mist text-brand-ink' : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)]'} disabled:cursor-not-allowed disabled:opacity-45`}>
                {file.path.includes('/') ? <FileCode2 size={13} /> : <FileText size={13} />}
                <span className="min-w-0 flex-1 truncate">{file.path}</span>
                <span className="text-[9px] text-[var(--color-text-tertiary)]">{Math.ceil(file.size / 1024)}K</span>
              </button>
            ))}
          </div>
        </aside>
        <div className="flex min-h-0 flex-col">
          <div className="flex min-h-12 items-center justify-between border-b border-[var(--color-border)] px-4">
            <span className="truncate font-mono text-xs text-[var(--color-text-secondary)]">{selectedPath}</span>
            <div className="flex gap-1">
              {selectedPath !== 'SKILL.md' && (
                <button onClick={() => setConfirmingRemove(true)} aria-label="删除当前文件" className="flex h-10 w-10 items-center justify-center rounded-lg text-[var(--color-text-tertiary)] hover:bg-red-50 hover:text-red-600"><Trash2 size={14} /></button>
              )}
              <button onClick={save} disabled={saving || loading} className="inline-flex min-h-10 items-center gap-2 rounded-lg bg-brand px-3 text-xs font-medium text-white hover:bg-brand-deep disabled:opacity-50">
                {saving ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />} 保存
              </button>
            </div>
          </div>
          {loading ? (
            <div className="flex flex-1 items-center justify-center"><Loader2 size={20} className="animate-spin text-brand-ink" /></div>
          ) : (
            <textarea aria-label={`编辑 ${selectedPath}`} value={content} onChange={event => setContent(event.target.value)} spellCheck={false}
              className="min-h-[360px] flex-1 resize-none bg-[var(--color-bg-elevated)] p-4 font-mono text-xs leading-6 text-[var(--color-text-primary)] outline-none focus-visible:ring-2 focus:ring-inset focus-visible:ring-ring" />
          )}
          {error && <p role="alert" className="border-t border-red-100 bg-red-50 px-4 py-2 text-xs text-red-700">{error}</p>}
        </div>
      </div>
    </DialogShell>
    <ConfirmDialog
      open={confirmingRemove}
      title="删除文件"
      description={`确定删除 ${selectedPath}？`}
      confirmText="删除"
      variant="danger"
      loading={removing}
      onConfirm={() => void removeFile()}
      onClose={() => setConfirmingRemove(false)}
    />
    </>
  )
}


function McpDialog({ server, onClose, onSaved }: {
  server?: SuperMcpServer
  onClose: () => void
  onSaved: () => Promise<void>
}) {
  const [name, setName] = useState(server?.name || '')
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
  const clientConfigRef = useRef<HTMLTextAreaElement>(null)
  const [enabled, setEnabled] = useState(server?.enabled ?? true)
  const [confirmation, setConfirmation] = useState(server?.require_confirmation ?? true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

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
    setName(config.name)
    setTransport(config.transport)
    setUrl(config.url)
    setCommand(config.command)
    setArgs(JSON.stringify(config.args, null, 2))
    setHeaders(Object.keys(config.headers).length ? JSON.stringify(config.headers, null, 2) : '')
    setEnv(Object.keys(config.env).length ? JSON.stringify(config.env, null, 2) : '')
    setClientConfigMessage([
      `已填入「${config.name}」，请检查后再保存。`,
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
    } catch (error) {
      setClientConfigOptions([])
      setClientConfigMessage('')
      setError(errorText(error, '无法解析 MCP 配置'))
    }
  }

  const save = async () => {
    setBusy(true); setError('')
    try {
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
        await superAssistantApi.updateMcpServer(server.id, {
          ...connection, enabled, require_confirmation: confirmation,
          ...(parsedHeaders ? { headers: parsedHeaders } : changedTransport && transport === 'stdio' ? { headers: {} } : {}),
          ...(parsedEnv ? { env: parsedEnv } : changedTransport && transport !== 'stdio' ? { env: {} } : {}),
        })
      } else {
        await superAssistantApi.createMcpServer({
          name, ...connection, headers: parsedHeaders || {}, env: parsedEnv || {},
          enabled, require_confirmation: confirmation,
        })
      }
      await onSaved()
      toast.success(server ? 'MCP 配置已更新' : 'MCP Server 已添加', { description: '请执行连接测试以发现工具清单' })
      onClose()
    } catch (error) { setError(errorText(error, '保存失败')) } finally { setBusy(false) }
  }

  return (
    <DialogShell size="large" title={server ? `编辑 MCP：${server.name}` : '添加 MCP Server'} description="支持粘贴客户端 JSON，也可直接配置 stdio、SSE 或 Streamable HTTP。" onClose={onClose}>
      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto overscroll-contain p-5 [scrollbar-gutter:stable] sm:p-6">
        {!server && <details className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)]" open>
          <summary className="cursor-pointer px-3 py-2.5 text-xs font-medium text-[var(--color-text-secondary)]">粘贴 MCP 客户端 JSON</summary>
          <div className="space-y-2 border-t border-[var(--color-border)] p-3">
            <textarea ref={clientConfigRef} value={clientConfig} onChange={event => {
              setClientConfig(event.target.value)
              setClientConfigOptions([])
              setClientConfigMessage('')
              setError('')
            }} rows={8}
              aria-label="MCP 客户端 JSON"
              placeholder={'{\n  "mcpServers": {\n    "api-hub": {\n      "command": "npx",\n      "args": ["-y", "mcp-remote", "https://example.com/mcp"]\n    }\n  }\n}'}
              className="w-full resize-none overflow-hidden rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-3 font-mono text-xs leading-5 outline-none focus-visible:ring-2 focus-visible:ring-ring" />
            <button type="button" onClick={applyClientConfig} disabled={!clientConfig.trim()}
              className="min-h-9 rounded-md border border-[var(--color-border)] bg-white px-3 text-xs text-brand-ink hover:bg-brand-soft disabled:opacity-50">解析并填入下方表单</button>
            <p className="text-[10px] leading-5 text-[var(--color-text-tertiary)]">
              兼容 Claude、Cursor、Cline、Windsurf、Gemini、JetBrains、Continue、VS Code 与 Zed 常见 JSON / JSONC 格式。
            </p>
            {clientConfigOptions.length > 1 && (
              <label className="block text-xs text-[var(--color-text-secondary)]">
                选择要填入的 MCP Server
                <Select
                  value={String(selectedClientConfig)}
                  onValueChange={value => {
                    const nextIndex = Number(value)
                    setSelectedClientConfig(nextIndex)
                    fillFromClientConfig(clientConfigOptions[nextIndex])
                  }}
                >
                  <SelectTrigger aria-label="选择要填入的 MCP Server" className="mt-1.5 min-h-10 text-xs">
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
            {clientConfigMessage && <p role="status" className="rounded-lg bg-brand-soft px-3 py-2 text-[10px] leading-5 text-brand-ink">{clientConfigMessage}</p>}
          </div>
        </details>}
        <label className="block text-xs text-[var(--color-text-secondary)]">名称 <span className="text-red-500">*</span>
          <input value={name} disabled={!!server} onChange={event => setName(event.target.value)} placeholder="knowledge_search"
            className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 font-mono text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60" />
        </label>
        <label className="block text-xs text-[var(--color-text-secondary)]">传输方式 <span className="text-red-500">*</span>
          <Select value={transport} onValueChange={value => setTransport(value as McpTransport)}>
            <SelectTrigger aria-label="传输方式" className="mt-1.5 min-h-11 text-sm">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="streamable_http">Streamable HTTP（推荐）</SelectItem>
              <SelectItem value="sse">SSE（旧版兼容）</SelectItem>
              <SelectItem value="stdio">stdio（启动本地进程）</SelectItem>
            </SelectContent>
          </Select>
        </label>
        {transport === 'stdio' ? <>
          <label className="block text-xs text-[var(--color-text-secondary)]">command <span className="text-red-500">*</span>
            <input value={command} onChange={event => setCommand(event.target.value)} placeholder="npx"
              className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 font-mono text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring" />
          </label>
          <label className="block text-xs text-[var(--color-text-secondary)]">args JSON
            <textarea value={args} onChange={event => setArgs(event.target.value)} rows={4} placeholder={'["-y", "@example/mcp-server"]'}
              className="mt-1.5 w-full resize-y rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)] p-3 font-mono text-xs leading-5 outline-none focus-visible:ring-2 focus-visible:ring-ring" />
          </label>
          <label className="block text-xs text-[var(--color-text-secondary)]">env JSON
            <textarea value={env} onChange={event => setEnv(event.target.value)} rows={4} placeholder={server ? `留空保持现有环境变量（${server.env_names.join(', ') || '无'}）` : '{\n  "API_KEY": "…"\n}'}
              className="mt-1.5 w-full resize-y rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)] p-3 font-mono text-xs leading-5 outline-none focus-visible:ring-2 focus-visible:ring-ring" />
          </label>
          <p className="rounded-lg bg-amber-50 p-3 text-[11px] leading-5 text-amber-800">stdio 会在后端容器内启动进程，部署方必须显式启用并允许该 command。env 会加密存储且不回显。</p>
        </> : <>
          <label className="block text-xs text-[var(--color-text-secondary)]">MCP URL <span className="text-red-500">*</span>
            <input type="url" value={url} onChange={event => setUrl(event.target.value)} placeholder="https://mcp.example.com/mcp"
              className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring" />
            <span className="mt-1 block text-[10px] leading-4 text-[var(--color-text-tertiary)]">公网地址可直接连接；生产环境会拒绝环回、内网和链路本地地址。</span>
          </label>
          <label className="block text-xs text-[var(--color-text-secondary)]">请求头 JSON
            <textarea value={headers} onChange={event => setHeaders(event.target.value)} rows={4} placeholder={server ? `留空保持现有请求头（${server.header_names.join(', ') || '无'}）` : '{\n  "Authorization": "Bearer …"\n}'}
              className="mt-1.5 w-full resize-y rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)] p-3 font-mono text-xs leading-5 outline-none focus-visible:ring-2 focus-visible:ring-ring" />
          </label>
        </>}
        <label className="flex min-h-11 items-center justify-between rounded-lg border border-[var(--color-border)] px-3 text-xs text-[var(--color-text-secondary)]">
          启用此 Server
          <input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)} className="h-4 w-4 accent-brand" />
        </label>
        <label className="flex min-h-11 items-center justify-between rounded-lg border border-[var(--color-border)] px-3 text-xs text-[var(--color-text-secondary)]">
          每次工具调用前要求确认
          <input type="checkbox" checked={confirmation} onChange={event => setConfirmation(event.target.checked)} className="h-4 w-4 accent-brand" />
        </label>
        {!confirmation && <p className="rounded-lg bg-amber-50 p-3 text-xs leading-5 text-amber-800">关闭确认会允许模型直接执行该 Server 的所有工具，请仅对完全可信、只读的服务使用。</p>}
        {error && <p role="alert" className="text-xs text-red-600">{error}</p>}
      </div>
      <footer className="flex shrink-0 justify-center gap-3 border-t border-[var(--color-border)] px-5 py-4">
        <button onClick={onClose} className="min-h-10 min-w-24 rounded-lg px-4 text-xs text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)]">取消</button>
        <button onClick={save} disabled={busy || !name || (transport === 'stdio' ? !command : !url)} className="inline-flex min-h-10 min-w-24 items-center justify-center gap-2 rounded-lg bg-brand px-4 text-xs font-medium text-white hover:bg-brand-deep disabled:opacity-50">
          {busy && <Loader2 size={13} className="animate-spin" />} 保存
        </button>
      </footer>
    </DialogShell>
  )
}


function SettingSwitch({ label, ariaLabel, checked, busy, onToggle }: {
  label: string
  ariaLabel: string
  checked: boolean
  busy: boolean
  onToggle: () => void
}) {
  return (
    <div className="inline-flex items-center gap-1.5">
      <span className="text-[10px] text-[var(--color-text-secondary)]">{label}</span>
      <button type="button" role="switch" aria-label={ariaLabel} aria-checked={checked} aria-busy={busy} disabled={busy} onClick={onToggle}
        className="relative inline-flex h-11 w-11 shrink-0 touch-manipulation items-center justify-center rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 disabled:cursor-wait disabled:opacity-60">
        <span aria-hidden="true" className={`relative inline-flex h-4 w-7 items-center rounded-full transition-colors motion-reduce:transition-none ${busy ? 'animate-pulse motion-reduce:animate-none' : ''} ${checked ? 'bg-brand' : 'bg-slate-300'}`}>
          <span className={`inline-block h-3 w-3 rounded-full bg-white shadow-sm transition-transform motion-reduce:transition-none ${checked ? 'translate-x-3.5' : 'translate-x-0.5'}`} />
        </span>
      </button>
    </div>
  )
}


// 工具分类的稳定展示文案：read_only 同轮可并行、confirmation_required
// 执行前需用户审批（与对话中的确认卡对应）
const TOOL_CATEGORY_LABELS: Record<AssistantTool['category'], string> = {
  read_only: '只读 · 可并行',
  confirmation_required: '写操作 · 执行前审批',
  standard: '常规',
}

function ToolParameters({ parameters }: { parameters: Record<string, unknown> }) {
  const properties = (parameters?.properties || {}) as Record<string, { type?: string; description?: string }>
  const required = new Set((parameters?.required as string[] | undefined) || [])
  const entries = Object.entries(properties)
  if (!entries.length) {
    return <p className="mt-1.5 text-[10px] leading-4 text-[var(--color-text-tertiary)]">无参数</p>
  }
  return (
    <dl className="mt-1.5 space-y-1">
      {entries.map(([name, spec]) => (
        <div key={name} className="flex gap-2 text-[10px] leading-4">
          <dt className="shrink-0 font-mono text-[var(--color-text-secondary)]">
            {name}{required.has(name) && <span className="text-red-500">*</span>}
          </dt>
          <dd className="min-w-0 text-[var(--color-text-tertiary)]">
            {spec.type || 'any'}{spec.description ? ` — ${spec.description}` : ''}
          </dd>
        </div>
      ))}
    </dl>
  )
}

function ToolsTab({ tools, refreshTools }: {
  tools: AssistantTool[]
  refreshTools: () => Promise<void>
}) {
  const [updatingName, setUpdatingName] = useState<string | null>(null)

  const toggleTool = async (tool: AssistantTool) => {
    // busy/防重入按工具名粒度：单个开关保存中不连坐禁用其它卡片
    if (updatingName === tool.name) return
    setUpdatingName(tool.name)
    try {
      await superAssistantApi.updateAssistantTool(tool.name, !tool.enabled)
      await refreshTools()
    } catch (error) {
      toast.error('工具设置更新失败', { description: errorText(error) })
    } finally {
      setUpdatingName(null)
    }
  }

  const disabledCount = tools.filter(tool => !tool.enabled).length
  const groups = groupAssistantTools(tools)
  return (
    <div className="grid gap-4" data-testid="tools-tab">
      <p className="text-[10px] leading-4 text-[var(--color-text-tertiary)]">
        内置工具 {tools.length} 个{disabledCount > 0 ? ` · 已禁用 ${disabledCount} 个` : ''}。禁用后工具不进入对话目录；MCP 工具请在 MCP 标签页按 Server 管理。
      </p>
      {groups.length === 0 && (
        <div className="rounded-xl border border-dashed border-[var(--color-border)] p-10 text-center text-xs text-[var(--color-text-tertiary)]">
          <Wrench size={22} className="mx-auto mb-2" />暂无工具目录
        </div>
      )}
      {groups.map(group => (
        <section key={group.key} data-testid={`tool-group-${group.key}`}>
          <h4 className="mb-2 flex items-baseline gap-1.5 text-[11px] font-semibold text-[var(--color-text-primary)]">
            {group.label}
            <span className="text-[10px] font-normal tabular-nums text-[var(--color-text-tertiary)]">
              {group.tools.length} 个{group.disabledCount > 0 ? ` · 已禁用 ${group.disabledCount}` : ''}
            </span>
          </h4>
          <div className="grid gap-3">
            {group.tools.map(tool => (
              <article key={tool.name} data-testid={`tool-card-${tool.name}`} className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-4 transition-colors hover:border-brand-line">
                <div className="flex items-start gap-2">
                  <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-brand-soft text-brand-ink"><Wrench size={16} /></div>
                  <div className="min-w-0 flex-1">
                    <p className="truncate font-mono text-xs font-semibold text-[var(--color-text-primary)]">{tool.name}</p>
                    <p className="mt-0.5 text-[10px] text-[var(--color-text-tertiary)]">{TOOL_CATEGORY_LABELS[tool.category]}</p>
                  </div>
                  <SettingSwitch
                    label="启用"
                    ariaLabel={`${tool.enabled ? '禁用' : '启用'}工具 ${tool.name}`}
                    checked={tool.enabled}
                    busy={updatingName === tool.name}
                    onToggle={() => void toggleTool(tool)}
                  />
                </div>
                <p className="mt-2 line-clamp-2 text-[11px] leading-5 text-[var(--color-text-secondary)]">{tool.description || '暂无描述'}</p>
                <div className="mt-2 flex flex-wrap gap-1">
                  {!tool.available && tool.unavailable_reason && (
                    <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[9px] text-amber-800">{tool.unavailable_reason}</span>
                  )}
                  {!tool.enabled && <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[9px] text-slate-500">已禁用</span>}
                </div>
                <details className="mt-2">
                  <summary className="cursor-pointer text-[11px] text-brand-ink transition-colors hover:text-brand-deep">查看参数</summary>
                  <ToolParameters parameters={tool.parameters} />
                </details>
              </article>
            ))}
          </div>
        </section>
      ))}
    </div>
  )
}


/** 配置面板宽度边界：默认 26rem，拖拽/键盘在 [288, min(600, 视口-720)] 内调整，
    为聊天卡头部（品牌区已让出）与标题保底最小可用宽度 */
export const DEFAULT_CONFIG_PANEL_WIDTH = 416
const MIN_PANEL_WIDTH = 288
const maxPanelWidth = () => Math.max(MIN_PANEL_WIDTH, Math.min(600, window.innerWidth - 720))
const clampPanelWidth = (width: number) => Math.min(Math.max(width, MIN_PANEL_WIDTH), maxPanelWidth())

export default function ConfigurationPanel({ open, onClose, width, onWidthResize, onDraggingChange, skills, servers, tools, refreshSkills, refreshServers, refreshTools, conversationId }: {
  open: boolean
  onClose: () => void
  /** 面板宽度（px）：受控于页面级状态（聊天列据此让位），拖拽/键盘经 onWidthResize 回写 */
  width: number
  onWidthResize: (width: number) => void
  /** 拖拽中同步给页面：聊天列让位的 padding 过渡需在拖拽期间禁用，保证跟手 */
  onDraggingChange: (dragging: boolean) => void
  skills: SuperSkill[]
  servers: SuperMcpServer[]
  tools: AssistantTool[]
  refreshSkills: () => Promise<void>
  refreshServers: () => Promise<void>
  refreshTools: () => Promise<void>
  conversationId: string | null
}) {
  const [tab, setTab] = useState<'skills' | 'mcp' | 'tools' | 'approval' | 'memory'>('skills')
  const [dragging, setDragging] = useState(false)
  const [creatingSkill, setCreatingSkill] = useState(false)
  const [editingSkill, setEditingSkill] = useState<SuperSkill | null>(null)
  const [editingMcp, setEditingMcp] = useState<SuperMcpServer | 'new' | null>(null)
  const [testingId, setTestingId] = useState<string | null>(null)
  const [updatingSkillId, setUpdatingSkillId] = useState<string | null>(null)
  const [updatingServerSetting, setUpdatingServerSetting] = useState<string | null>(null)
  const [removingSkill, setRemovingSkill] = useState<SuperSkill | null>(null)
  const [removingServer, setRemovingServer] = useState<SuperMcpServer | null>(null)
  const [removeBusy, setRemoveBusy] = useState(false)
  const uploadRef = useRef<HTMLInputElement>(null)
  const configurableServers = servers.filter(server => server.builtin_key !== 'minio')

  const importZip = async (file?: File) => {
    if (!file) return
    try {
      await superAssistantApi.importSkill(file)
      await refreshSkills()
      toast.success('Skill 文件夹已导入', { description: file.name })
    } catch (error) { toast.error('导入失败', { description: errorText(error) }) }
    if (uploadRef.current) uploadRef.current.value = ''
  }

  const toggleSkill = async (skill: SuperSkill) => {
    // busy/防重入按 skill.id 粒度：单个开关保存中不连坐禁用其它卡片
    if (updatingSkillId === skill.id) return
    setUpdatingSkillId(skill.id)
    try {
      await superAssistantApi.updateSkill(skill.id, { enabled: !skill.enabled })
      await refreshSkills()
    } catch (error) {
      toast.error('Skill 设置更新失败', { description: errorText(error) })
    } finally {
      setUpdatingSkillId(null)
    }
  }

  const toggleSkillAlwaysActive = async (skill: SuperSkill) => {
    if (updatingSkillId === skill.id) return
    setUpdatingSkillId(skill.id)
    try {
      await superAssistantApi.updateSkill(skill.id, { always_active: !skill.always_active })
      await refreshSkills()
    } catch (error) {
      toast.error('Skill 设置更新失败', { description: errorText(error) })
    } finally {
      setUpdatingSkillId(null)
    }
  }

  const removeSkill = async (skill: SuperSkill) => {
    setRemoveBusy(true)
    try { await superAssistantApi.deleteSkill(skill.id); await refreshSkills(); toast.success('Skill 已删除') }
    catch (error) { toast.error('删除失败', { description: errorText(error) }) }
    finally { setRemoveBusy(false); setRemovingSkill(null) }
  }

  const testServer = async (server: SuperMcpServer) => {
    setTestingId(server.id)
    try {
      const result = await superAssistantApi.testMcpServer(server.id)
      await refreshServers()
      const notifyTest = result.ok ? toast.success : toast.error
      notifyTest(result.ok ? 'MCP 连接成功' : 'MCP 连接失败', { description: result.message })
    } catch (error) { toast.error('MCP 测试失败', { description: errorText(error) }) }
    finally { setTestingId(null) }
  }

  const updateServerSetting = async (
    server: SuperMcpServer,
    setting: 'enabled' | 'require_confirmation',
    value: boolean,
  ) => {
    // busy/防重入按 server.id:setting 粒度：单个开关保存中不连坐禁用其它卡片
    if (updatingServerSetting === `${server.id}:${setting}`) return
    setUpdatingServerSetting(`${server.id}:${setting}`)
    try {
      const patch = setting === 'enabled' ? { enabled: value } : { require_confirmation: value }
      await superAssistantApi.updateMcpServer(server.id, patch)
      await refreshServers()
    } catch (error) {
      toast.error('MCP 设置更新失败', { description: errorText(error) })
    } finally {
      setUpdatingServerSetting(null)
    }
  }

  const removeServer = async (server: SuperMcpServer) => {
    setRemoveBusy(true)
    try { await superAssistantApi.deleteMcpServer(server.id); await refreshServers(); toast.success('MCP Server 已删除') }
    catch (error) { toast.error('删除失败', { description: errorText(error) }) }
    finally { setRemoveBusy(false); setRemovingServer(null) }
  }

  return (
    <>
      <aside
        aria-hidden={!open}
        inert={!open}
        className={`absolute inset-y-0 right-0 z-30 overflow-hidden transition-[width] duration-300 ease-[cubic-bezier(0.22,1,0.36,1)] motion-reduce:transition-none ${open
          ? 'pointer-events-auto w-[min(26rem,100%)] lg:w-[var(--config-w,26rem)]'
          : 'pointer-events-none w-0'} ${dragging ? 'transition-none' : ''}`}
      >
        {/* 拖拽手柄：贴面板左缘的窄条，悬停显指示线；面板收起时随 aside inert 一并失效 */}
        <div
          role="separator"
          aria-orientation="vertical"
          aria-label="拖拽调整配置面板宽度"
          tabIndex={0}
          onPointerDown={event => {
            event.preventDefault()
            event.currentTarget.setPointerCapture(event.pointerId)
            setDragging(true)
            onDraggingChange(true)
          }}
          onPointerMove={event => {
            if (dragging) onWidthResize(clampPanelWidth(window.innerWidth - event.clientX))
          }}
          onPointerUp={event => {
            event.currentTarget.releasePointerCapture(event.pointerId)
            setDragging(false)
            onDraggingChange(false)
          }}
          onPointerCancel={() => {
            setDragging(false)
            onDraggingChange(false)
          }}
          onKeyDown={event => {
            // 面板锚定右侧：← 加宽、→ 收窄
            if (event.key === 'ArrowLeft') onWidthResize(clampPanelWidth(width + 16))
            if (event.key === 'ArrowRight') onWidthResize(clampPanelWidth(width - 16))
          }}
          className="group absolute inset-y-0 left-0 z-10 hidden w-1.5 cursor-col-resize items-center justify-center focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset lg:flex"
        >
          <span
            aria-hidden
            className={`h-8 w-px rounded-full transition-colors duration-150 ${dragging
              ? 'bg-brand'
              : 'bg-[var(--color-border)] group-hover:bg-[var(--color-border-hover)]'}`}
          />
        </div>
        <section
          aria-label="助手配置"
          className="absolute inset-y-0 right-0 flex w-[min(var(--config-w,26rem),100vw)] min-h-0 flex-col overflow-hidden border-l border-[var(--color-border)] bg-card"
        >
          <header className="flex shrink-0 items-start justify-between border-b border-[var(--color-border)] px-4 py-3.5">
            <div className="min-w-0">
              <h2 className="text-sm font-semibold text-[var(--color-text-primary)]">助手配置</h2>
              <p className="mt-1 text-[10px] leading-4 text-[var(--color-text-tertiary)]">管理当前助手可用的 Skill 与 MCP Server</p>
            </div>
            <button type="button" onClick={onClose} aria-label="关闭助手配置"
              className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
              <X size={16} />
            </button>
          </header>
          <div className="relative mx-4 mt-3 grid grid-cols-5 gap-1 rounded-lg border border-slate-200 bg-slate-50/70 p-0.5">
            <div
              className="absolute bottom-0.5 top-0.5 w-[calc(20%_-_4px)] rounded-md bg-brand shadow-sm transition-all duration-300 ease-out"
              style={{ left: `calc(${(['skills', 'mcp', 'tools', 'approval', 'memory'] as const).indexOf(tab) * 20}% + 2px)` }}
            />
            <button type="button" onClick={() => setTab('skills')}
              className={`relative z-10 min-h-9 rounded-md text-xs font-medium transition-colors duration-200 ${tab === 'skills' ? 'text-white' : 'text-slate-500 hover:text-slate-700'}`}>
              Skill <span className={`ml-1 text-[10px] tabular-nums ${tab === 'skills' ? 'text-white' : 'text-slate-400'}`}>{skills.length}</span>
            </button>
            <button type="button" onClick={() => setTab('mcp')}
              className={`relative z-10 min-h-9 rounded-md text-xs font-medium transition-colors duration-200 ${tab === 'mcp' ? 'text-white' : 'text-slate-500 hover:text-slate-700'}`}>
              MCP <span className={`ml-1 text-[10px] tabular-nums ${tab === 'mcp' ? 'text-white' : 'text-slate-400'}`}>{configurableServers.length}</span>
            </button>
            <button type="button" onClick={() => setTab('tools')}
              className={`relative z-10 min-h-9 rounded-md text-xs font-medium transition-colors duration-200 ${tab === 'tools' ? 'text-white' : 'text-slate-500 hover:text-slate-700'}`}>
              工具 <span className={`ml-1 text-[10px] tabular-nums ${tab === 'tools' ? 'text-white' : 'text-slate-400'}`}>{tools.length}</span>
            </button>
            <button type="button" onClick={() => setTab('approval')}
              className={`relative z-10 flex min-h-9 items-center justify-center gap-1 rounded-md text-xs font-medium transition-colors duration-200 ${tab === 'approval' ? 'text-white' : 'text-slate-500 hover:text-slate-700'}`}>
              待审批 <EvolutionPendingBadge />
            </button>
            <button type="button" onClick={() => setTab('memory')}
              className={`relative z-10 min-h-9 rounded-md text-xs font-medium transition-colors duration-200 ${tab === 'memory' ? 'text-white' : 'text-slate-500 hover:text-slate-700'}`}>
              记忆
            </button>
          </div>
          {/* scrollbar-none：内容超长时仍可滚轮/拖拽滚动，仅不显示滚动条 */}
          <div className="scrollbar-none min-h-0 flex-1 overflow-y-auto p-4">
          {tab === 'approval' ? (
            <ApprovalTab conversationId={conversationId} />
          ) : tab === 'memory' ? (
            <MemoryTab />
          ) : tab === 'tools' ? (
            <ToolsTab tools={tools} refreshTools={refreshTools} />
          ) : tab === 'skills' ? (
            <>
              <div className="grid gap-3">
                {skills.length === 0 && <div className="rounded-xl border border-dashed border-[var(--color-border)] p-10 text-center text-xs text-[var(--color-text-tertiary)]"><Folder size={22} className="mx-auto mb-2" />暂无 Skill</div>}
                {skills.map(skill => (
                  <article key={skill.id} className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-4 transition-colors hover:border-brand-line">
                    <div className="flex items-start gap-2">
                      <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-brand-soft text-brand-ink"><Folder size={16} /></div>
                      <div className="min-w-0 flex-1">
                        <p className="truncate font-mono text-xs font-semibold text-[var(--color-text-primary)]">{skill.name}</p>
                        <p className="mt-0.5 truncate text-[10px] text-[var(--color-text-tertiary)]">
                          r{skill.revision} · {skill.manifest.length} 个文件 · {skill.use_count > 0 ? `使用 ${skill.use_count} 次` : '未使用'}
                        </p>
                      </div>
                    </div>
                    <div className="mt-2 flex flex-wrap gap-1">
                      {!skill.enabled && <span className="inline-flex rounded bg-slate-100 px-1.5 py-0.5 text-[9px] text-slate-500">已停用</span>}
                      {skill.always_active && <span className="inline-flex rounded bg-brand-soft px-1.5 py-0.5 text-[9px] text-brand-ink">常驻</span>}
                    </div>
                    <p className="mt-2 line-clamp-2 text-[11px] leading-5 text-[var(--color-text-secondary)]">{skill.description || '暂无描述'}</p>
                    <div className="mt-2 flex items-center justify-between gap-2">
                      <div className="flex min-w-0 items-center gap-3">
                        <SettingSwitch
                          label="启用"
                          ariaLabel={`${skill.enabled ? '停用' : '启用'} Skill ${skill.name}`}
                          checked={skill.enabled}
                          busy={updatingSkillId === skill.id}
                          onToggle={() => void toggleSkill(skill)}
                        />
                        <SettingSwitch
                          label="常驻"
                          ariaLabel={`${skill.always_active ? '取消' : '设为'}常驻 Skill ${skill.name}`}
                          checked={skill.always_active}
                          busy={updatingSkillId === skill.id}
                          onToggle={() => void toggleSkillAlwaysActive(skill)}
                        />
                      </div>
                      <div className="flex shrink-0 items-center gap-1">
                        <button type="button" onClick={() => setEditingSkill(skill)} className="inline-flex min-h-9 items-center gap-1 rounded-md px-2 text-[11px] text-[var(--color-text-secondary)] transition-colors hover:bg-brand-soft hover:text-brand-ink"><Pencil size={12} /> 文件</button>
                        <button type="button" onClick={() => setRemovingSkill(skill)} aria-label={`删除 ${skill.name}`} className="flex h-9 w-9 items-center justify-center rounded-md text-[var(--color-text-tertiary)] transition-colors hover:bg-red-50 hover:text-red-600"><Trash2 size={12} /></button>
                      </div>
                    </div>
                  </article>
                ))}
              </div>
            </>
          ) : (
            <>
              <div className="grid gap-3">
                {configurableServers.length === 0 && <div className="rounded-xl border border-dashed border-[var(--color-border)] p-10 text-center text-xs text-[var(--color-text-tertiary)]"><PlugZap size={22} className="mx-auto mb-2" />暂无 MCP Server</div>}
                {configurableServers.map(server => (
                  <article key={server.id} className="rounded-xl border border-[var(--color-border)] p-4 transition-colors hover:border-brand-line">
                    <div className="flex items-start gap-2">
                      <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-sky-50 text-sky-700"><PlugZap size={16} /></div>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5"><p className="truncate text-xs font-semibold text-[var(--color-text-primary)]">{server.name}</p><span className={`h-2 w-2 rounded-full ${server.last_test_status === 'success' ? 'bg-success' : server.last_test_status === 'error' ? 'bg-red-500' : 'bg-slate-300'}`} /></div>
                        <p className="mt-1 truncate text-[10px] text-[var(--color-text-tertiary)]">{server.transport === 'stdio' ? `${server.command} ${server.args.join(' ')}` : server.url}</p>
                      </div>
                    </div>
                    <div className="mt-2 flex flex-wrap gap-1">
                      <span className="rounded bg-[var(--color-bg-base)] px-1.5 py-0.5 text-[9px] text-[var(--color-text-tertiary)]">{server.tool_manifest.length} tools</span>
                      <span className="rounded bg-[var(--color-bg-base)] px-1.5 py-0.5 text-[9px] text-[var(--color-text-tertiary)]">{server.transport === 'streamable_http' ? 'Streamable HTTP' : server.transport.toUpperCase()}</span>
                      <span className="rounded bg-[var(--color-bg-base)] px-1.5 py-0.5 text-[9px] text-[var(--color-text-tertiary)]">{server.require_confirmation ? '执行前确认' : '自动执行'}</span>
                      {!server.enabled && <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[9px] text-slate-500">已停用</span>}
                    </div>
                    {server.last_test_message && <p className="mt-2 line-clamp-2 text-[10px] leading-4 text-[var(--color-text-tertiary)]">{server.last_test_message}</p>}
                    <div className="mt-2 flex items-center justify-between gap-2">
                      <div className="flex min-w-0 items-center gap-3">
                        <SettingSwitch label="启用" ariaLabel={`${server.enabled ? '停用' : '启用'} MCP ${server.name}`} checked={server.enabled}
                          busy={updatingServerSetting === `${server.id}:enabled`}
                          onToggle={() => void updateServerSetting(server, 'enabled', !server.enabled)} />
                        <SettingSwitch label="自动执行" ariaLabel={`${server.require_confirmation ? '开启' : '关闭'} ${server.name} 自动执行`} checked={!server.require_confirmation}
                          busy={updatingServerSetting === `${server.id}:require_confirmation`}
                          onToggle={() => void updateServerSetting(server, 'require_confirmation', !server.require_confirmation)} />
                      </div>
                      <div className="flex shrink-0 items-center gap-1">
                        <button type="button" onClick={() => void testServer(server)} disabled={testingId === server.id} className="inline-flex min-h-9 items-center gap-1 rounded-md px-2 text-[11px] text-brand-ink transition-colors hover:bg-brand-soft disabled:opacity-50">{testingId === server.id ? <Loader2 size={12} className="animate-spin" /> : <Wrench size={12} />} 测试</button>
                        {!server.builtin_key && <button type="button" onClick={() => setEditingMcp(server)} aria-label={`编辑 MCP ${server.name}`} className="flex h-9 w-9 items-center justify-center rounded-md text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-bg-hover)]"><Pencil size={12} /></button>}
                        <button type="button" onClick={() => setRemovingServer(server)} aria-label={`删除 MCP ${server.name}`} className="flex h-9 w-9 items-center justify-center rounded-md text-[var(--color-text-tertiary)] transition-colors hover:bg-red-50 hover:text-red-600"><Trash2 size={12} /></button>
                      </div>
                    </div>
                  </article>
                ))}
              </div>
            </>
          )}
          </div>
          {/* 仅 skills/mcp tab 有底部动作；approval/memory tab 不渲染空 footer，
              避免面板底部出现一条无内容的分割线 */}
          {(tab === 'skills' || tab === 'mcp') && (
            <footer className="shrink-0 border-t border-[var(--color-border)] bg-white p-3">
              {tab === 'skills' ? (
                <div className="grid grid-cols-2 gap-2">
                  <button type="button" onClick={() => setCreatingSkill(true)}
                    className="inline-flex min-h-11 items-center justify-center gap-1.5 rounded-lg border border-dashed border-brand bg-brand-soft/70 px-3 text-xs font-medium text-brand-ink transition-all hover:border-brand-deep hover:bg-brand-mist active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                    <Plus size={14} /> 新建 Skill
                  </button>
                  <button type="button" onClick={() => uploadRef.current?.click()}
                    className="inline-flex min-h-11 items-center justify-center gap-1.5 rounded-lg border border-dashed border-brand bg-brand-soft/70 px-3 text-xs font-medium text-brand-ink transition-all hover:border-brand-deep hover:bg-brand-mist active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                    <Upload size={14} /> 导入 ZIP
                  </button>
                </div>
              ) : (
                <button type="button" onClick={() => setEditingMcp('new')}
                  className="inline-flex min-h-11 w-full items-center justify-center gap-1.5 rounded-lg border border-dashed border-brand bg-brand-soft/70 px-3 text-xs font-medium text-brand-ink transition-all hover:border-brand-deep hover:bg-brand-mist active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                  <Plus size={14} /> 添加 MCP
                </button>
              )}
              <input ref={uploadRef} type="file" accept=".zip,application/zip" className="hidden" onChange={event => void importZip(event.target.files?.[0])} />
            </footer>
          )}
        </section>
      </aside>
      {creatingSkill && <SkillCreateDialog onClose={() => setCreatingSkill(false)} onSaved={refreshSkills} />}
      {editingSkill && <SkillEditor skill={editingSkill} onClose={() => setEditingSkill(null)} onSaved={refreshSkills} />}
      {editingMcp && <McpDialog server={editingMcp === 'new' ? undefined : editingMcp} onClose={() => setEditingMcp(null)} onSaved={refreshServers} />}
      <ConfirmDialog
        open={removingSkill !== null}
        title="删除 Skill"
        description={removingSkill ? `确定删除 Skill「${removingSkill.name}」及其整个文件夹？` : ''}
        confirmText="删除"
        variant="danger"
        loading={removeBusy}
        onConfirm={() => { if (removingSkill) void removeSkill(removingSkill) }}
        onClose={() => setRemovingSkill(null)}
      />
      <ConfirmDialog
        open={removingServer !== null}
        title="删除 MCP Server"
        description={removingServer ? `确定删除 MCP Server「${removingServer.name}」？` : ''}
        confirmText="删除"
        variant="danger"
        loading={removeBusy}
        onConfirm={() => { if (removingServer) void removeServer(removingServer) }}
        onClose={() => setRemovingServer(null)}
      />
    </>
  )
}
