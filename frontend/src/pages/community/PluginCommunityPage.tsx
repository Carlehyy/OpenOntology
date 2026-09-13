import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useReducedMotion } from 'motion/react'
import {
  CheckCircle2,
  CircleAlert,
  Clock3,
  Code2,
  FileUp,
  Hammer,
  Loader2,
  Pencil,
  PlugZap,
  Plus,
  Search,
  Trash2,
  Wrench,
  X,
} from 'lucide-react'

import { communityApi, mcpDevApi } from '@/api/community'
import type { McpTool, SuperMcpServer } from '@/api/superAssistant'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import McpServerDialog from '@/components/mcp/McpServerDialog'
import { AnimatedNumber } from '@/components/motion-ui/animated-number'
import { Checkbox } from '@/components/motion-ui/checkbox'
import {
  CenterMorphModal,
  CenterMorphModalContent,
} from '@/components/motion-ui/center-morph-modal'
import { IconButton } from '@/components/motion-ui/icon-button'
import {
  MultiSelect,
  MultiSelectContent,
  MultiSelectEmpty,
  MultiSelectInput,
  MultiSelectItem,
  MultiSelectList,
  MultiSelectTrigger,
  MultiSelectValue,
} from '@/components/motion-ui/multi-select'
import { Tooltip } from '@/components/motion-ui/tooltip'
import { Modal } from '@/components/ui/Modal'
import { toast } from 'sonner'


type StatusKey = 'success' | 'error' | 'untested'

const statusKeyOf = (server: SuperMcpServer): StatusKey => {
  if (server.last_test_status === 'success') return 'success'
  if (server.last_test_status === 'error') return 'error'
  return 'untested'
}

const errorText = (error: any, fallback = '操作失败') =>
  error?.detail || error?.message || fallback

const serverTitle = (server: SuperMcpServer) =>
  server.display_name || server.name

const transportLabel = (server: SuperMcpServer) => {
  if (server.transport === 'developed') return '自研'
  if (server.transport === 'streamable_http') return 'Streamable HTTP'
  return server.transport.toUpperCase()
}

const endpointText = (server: SuperMcpServer) => {
  if (server.transport === 'developed') return '平台进程内执行'
  if (server.transport === 'stdio') return [server.command, ...server.args].filter(Boolean).join(' ')
  return server.url
}

const developed = (server: SuperMcpServer) => server.transport === 'developed'

const exportable = (server: SuperMcpServer) => server.tool_manifest.length > 0

const exportHint = (server: SuperMcpServer) =>
  server.transport === 'streamable_http'
    ? '生成的接口为单发 JSON-RPC（tools/call）POST 直连该 MCP Server，仅适用于无状态服务；要求握手/会话头的有状态服务可能无法直接调用。'
    : '该传输无法直连导出：生成的接口由平台桥接调用（服务端以原生传输执行 MCP 调用后回传结果），凭据保留在服务端。'

const schemaPlaceholder = (schema: any): unknown => {
  if (!schema || typeof schema !== 'object') return ''
  if ('default' in schema) return schema.default
  switch (schema.type) {
    case 'object': return {}
    case 'array': return []
    case 'integer':
    case 'number': return 0
    case 'boolean': return false
    default: return ''
  }
}

const argumentsTemplate = (inputSchema: Record<string, unknown> | undefined) => {
  const properties = (inputSchema as { properties?: Record<string, unknown> } | undefined)?.properties || {}
  return Object.fromEntries(
    Object.entries(properties).map(([key, schema]) => [key, schemaPlaceholder(schema)]),
  )
}

const jsonRpcExample = (tool: McpTool) => JSON.stringify({
  jsonrpc: '2.0',
  id: 1,
  method: 'tools/call',
  params: { name: tool.name, arguments: argumentsTemplate(tool.input_schema) },
}, null, 2)


function TestStatus({ server }: { server: SuperMcpServer }) {
  if (server.last_test_status === 'success') {
    return <span className="inline-flex items-center gap-1.5 rounded-full border border-[color:var(--color-success)]/25 bg-[var(--color-success-bg)] px-2.5 py-1 text-[11px] font-medium text-[var(--color-success)]"><CheckCircle2 size={12} /> 已通过</span>
  }
  if (server.last_test_status === 'error') {
    return <span className="inline-flex items-center gap-1.5 rounded-full border border-destructive/25 bg-[var(--color-danger-bg)] px-2.5 py-1 text-[11px] font-medium text-destructive"><CircleAlert size={12} /> 异常</span>
  }
  return <span className="inline-flex items-center gap-1.5 rounded-full border border-border bg-muted px-2.5 py-1 text-[11px] font-medium text-muted-foreground"><Clock3 size={12} /> 未测试</span>
}

function StatCard({ icon, label, value, tone }: {
  icon: React.ReactNode
  label: string
  value: number
  tone?: 'default' | 'success'
}) {
  return (
    <div className="flex items-center gap-3 rounded-xl border border-border bg-card px-4 py-3 shadow-sm/50">
      <span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg ${tone === 'success' ? 'bg-[var(--color-success-bg)] text-[var(--color-success)]' : 'bg-brand-soft text-brand-ink'}`}>
        {icon}
      </span>
      <div className="min-w-0">
        <p className="text-[11px] text-muted-foreground">{label}</p>
        <p className="text-base font-semibold tabular-nums text-foreground">
          <AnimatedNumber value={value} duration={0.9} />
        </p>
      </div>
    </div>
  )
}

function ToolManifestDialog({ server, onClose }: { server: SuperMcpServer; onClose: () => void }) {
  const tools = server.tool_manifest || []
  return (
    <Modal
      open
      onClose={onClose}
      title={`${serverTitle(server)} · 工具清单`}
      description={`最近一次测试发现 ${tools.length} 个工具；标识 ${server.name}`}
      size="2xl"
      contentClassName="p-5"
    >
      <div className="rounded-xl border border-border bg-muted p-4 text-xs leading-5 text-muted-foreground">
        <p className="font-medium text-foreground">调用方式</p>
        <p className="mt-1.5">协议：MCP JSON-RPC 2.0，方法 <code className="rounded bg-card px-1 font-mono text-[11px] text-brand-ink ring-1 ring-border">tools/call</code>，参数为 <code className="rounded bg-card px-1 font-mono text-[11px] text-brand-ink ring-1 ring-border">{'{ name, arguments }'}</code>。</p>
        {server.transport === 'stdio' ? (
          <p className="mt-1.5">地址：本地进程 <span className="break-all font-mono text-[11px] text-[var(--color-text-tertiary)]">{endpointText(server) || '未配置 command'}</span>（stdio 由平台进程托管，不提供直连地址）。</p>
        ) : developed(server) ? (
          <p className="mt-1.5">执行：平台进程内执行（Python 内核，无直连地址）；在超级助手会话中按 <code className="rounded bg-card px-1 font-mono text-[11px] text-brand-ink ring-1 ring-border">mcp__{server.name}__工具名</code> 调用。</p>
        ) : (
          <p className="mt-1.5">地址：<span className="break-all font-mono text-[11px] text-[var(--color-text-tertiary)]">{server.url}</span>（{transportLabel(server)}，需携带已配置的请求头）。</p>
        )}
      </div>
      {tools.length === 0 ? (
        <div className="mt-3 rounded-2xl border border-dashed border-border p-10 text-center">
          <Code2 size={26} className="mx-auto text-muted-foreground/60" />
          <p className="mt-3 text-sm font-medium text-muted-foreground">暂无可用工具</p>
          <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">请先关闭窗口并执行连接测试。</p>
        </div>
      ) : (
        <div className="mt-3 space-y-3">
          {tools.map((tool: McpTool, _index: number) => (
            <article
              key={tool.name}
              className="rounded-xl border border-border bg-muted p-4"
            >
              <div className="flex items-start gap-3">
                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-card text-brand-ink ring-1 ring-border"><Code2 size={15} /></span>
                <div className="min-w-0 flex-1">
                  <h3 className="break-all font-mono text-xs font-semibold text-foreground">{tool.name}</h3>
                  <p className="mt-1 text-xs leading-5 text-muted-foreground">{tool.description || '暂无工具描述'}</p>
                  <details className="mt-2 text-xs">
                    <summary className="cursor-pointer font-medium text-brand-ink">输入参数（JSON Schema）</summary>
                    <pre className="mt-1.5 max-h-48 overflow-auto rounded-lg bg-card p-3 font-mono text-[11px] leading-4 text-muted-foreground ring-1 ring-border">{JSON.stringify(tool.input_schema ?? {}, null, 2)}</pre>
                  </details>
                  <details className="mt-1.5 text-xs">
                    <summary className="cursor-pointer font-medium text-brand-ink">请求示例（tools/call）</summary>
                    <pre className="mt-1.5 max-h-56 overflow-auto rounded-lg bg-card p-3 font-mono text-[11px] leading-4 text-muted-foreground ring-1 ring-border">{jsonRpcExample(tool)}</pre>
                  </details>
                </div>
              </div>
            </article>
          ))}
        </div>
      )}
    </Modal>
  )
}

function ExportToolsDialog({ server, onClose, onDone }: { server: SuperMcpServer; onClose: () => void; onDone: () => void }) {
  const tools = server.tool_manifest || []
  const [selected, setSelected] = useState<Set<string>>(() => new Set(tools.map(tool => tool.name)))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const toggle = (name: string) => {
    setSelected(current => {
      const next = new Set(current)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  const submit = async () => {
    if (!selected.size || busy) return
    setBusy(true)
    setError('')
    try {
      const result = await communityApi.exportMcpTools(server.id, [...selected])
      const parts = [`已生成 ${result.created.length} 个 HTTP 接口`]
      if (result.skipped.length) parts.push(`${result.skipped.length} 个同名已跳过`)
      const notifyExport = result.created.length ? toast.success : toast.warning
      notifyExport(
        '工具已导出至接口代理',
        { description: `${parts.join('，')}；请前往「接口代理 · 接口管理」的「MCP 插件」分组查看。` },
      )
      onDone()
      onClose()
    } catch (exportError) {
      setError(errorText(exportError, '导出失败，请稍后重试。'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <CenterMorphModal open onOpenChange={next => { if (!next && !busy) onClose() }}>
      <CenterMorphModalContent
        ariaLabel={`转接口 · ${serverTitle(server)}`}
        closeButtonLabel="关闭转接口弹窗"
        dismissible={!busy}
        className="flex max-h-[82dvh] w-full !max-w-[min(94vw,32rem)] !rounded-[14px] flex-col overflow-hidden bg-card text-foreground"
      >
        <header className="shrink-0 border-b border-border px-5 py-4 pr-14">
          <h2 className="text-base font-semibold text-foreground">转接口 · {serverTitle(server)}</h2>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">勾选要生成 HTTP 接口的工具；生成后可在「接口代理 · 接口管理」的「MCP 插件」分组中查看。</p>
        </header>
        <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-5">
          <p className="rounded-lg border border-[color:var(--color-warning)]/30 bg-[var(--color-warning-bg)] px-3 py-2 text-[11px] leading-5 text-[var(--color-warning)]">{exportHint(server)}</p>
          {tools.map((tool, _index) => (
            <div
              key={tool.name}
              className="flex items-start gap-3 rounded-xl border border-border bg-card p-3 text-xs transition-colors hover:border-brand-mist"
            >
              <Checkbox
                checked={selected.has(tool.name)}
                onCheckedChange={() => toggle(tool.name)}
                aria-label={`选择工具 ${tool.name}`}
                className="mt-0.5"
              />
              <span className="min-w-0">
                <span className="block break-all font-mono font-semibold text-foreground">{tool.name}</span>
                <span className="mt-0.5 block leading-5 text-muted-foreground">{tool.description || '暂无工具描述'}</span>
              </span>
            </div>
          ))}
          {error && <p role="alert" className="rounded-xl border border-destructive/30 bg-[var(--color-danger-bg)] px-4 py-3 text-xs leading-5 text-destructive">{error}</p>}
        </div>
        <footer className="flex shrink-0 items-center justify-between gap-3 border-t border-border bg-muted px-5 py-4">
          <div className="flex gap-3 text-xs text-muted-foreground">
            <button type="button" onClick={() => setSelected(new Set(tools.map(tool => tool.name)))} className="transition-colors hover:text-foreground">全选</button>
            <button type="button" onClick={() => setSelected(new Set())} className="transition-colors hover:text-foreground">全不选</button>
          </div>
          <div className="flex gap-3">
            <button type="button" onClick={onClose} disabled={busy} className="min-h-10 min-w-24 rounded-xl border border-border bg-card px-4 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">取消</button>
            <button type="button" onClick={() => void submit()} disabled={busy || !selected.size} className="inline-flex min-h-10 min-w-24 items-center justify-center gap-2 rounded-xl bg-brand px-4 text-xs font-medium text-[var(--color-text-inverse)] transition-colors hover:bg-brand-deep focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:cursor-not-allowed disabled:opacity-45">
              {busy && <Loader2 size={13} className="animate-spin motion-reduce:animate-none" />} 生成接口
            </button>
          </div>
        </footer>
      </CenterMorphModalContent>
    </CenterMorphModal>
  )
}

/** 新建开发项目弹窗：标识 + 名称 + 描述，创建后直接进入开发页 */
function McpDevCreateDialog({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate()
  const [name, setName] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [description, setDescription] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const nameValid = /^[a-zA-Z0-9][a-zA-Z0-9_-]*$/.test(name.trim())

  const submit = async () => {
    if (busy || !nameValid) return
    setBusy(true)
    setError('')
    try {
      const project = await mcpDevApi.createProject({
        name: name.trim(),
        display_name: displayName.trim(),
        description: description.trim(),
      })
      toast.success('开发项目已创建', { description: '正在进入开发页，用 @mcp_tool 声明你的工具函数。' })
      onClose()
      navigate(`/community/plugins/develop/${project.id}`)
    } catch (createError) {
      setError(errorText(createError, '创建失败，请稍后重试。'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open
      onClose={() => { if (!busy) onClose() }}
      title="开发 MCP"
      description="用 Python 编写若干工具函数（封装你的接口），发布后作为 MCP 进入本清单，供超级助手调用。"
      contentClassName="p-5"
      footer={(
        <div className="flex justify-end gap-3">
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            className="min-h-10 min-w-24 rounded-xl border border-border bg-card px-4 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted"
          >
            取消
          </button>
          <button
            type="button"
            onClick={() => void submit()}
            disabled={busy || !nameValid}
            className="inline-flex min-h-10 min-w-24 items-center justify-center gap-2 rounded-xl bg-brand px-4 text-xs font-medium text-[var(--color-text-inverse)] transition-colors hover:bg-brand-deep disabled:cursor-not-allowed disabled:opacity-45"
          >
            {busy && <Loader2 size={13} className="animate-spin" />} 创建并开发
          </button>
        </div>
      )}
    >
      <div className="space-y-4 text-xs">
        <label className="block">
          <span className="mb-1 block font-medium text-foreground">标识（发布后即 MCP 标识，不可改）</span>
          <input
            value={name}
            onChange={event => setName(event.target.value)}
            disabled={busy}
            maxLength={100}
            placeholder="例如 my_weather_tools"
            aria-label="开发项目标识"
            className="w-full rounded-lg border border-border bg-card px-3 py-2 font-mono text-sm text-foreground focus-visible:ring-2 focus-visible:ring-ring"
          />
          {!nameValid && name.trim() !== '' && (
            <span className="mt-1 block text-[11px] text-destructive">仅限字母/数字/下划线/连字符，且以字母或数字开头</span>
          )}
        </label>
        <label className="block">
          <span className="mb-1 block font-medium text-foreground">显示名称</span>
          <input
            value={displayName}
            onChange={event => setDisplayName(event.target.value)}
            disabled={busy}
            maxLength={200}
            placeholder="例如 天气工具集"
            aria-label="开发项目显示名称"
            className="w-full rounded-lg border border-border bg-card px-3 py-2 text-sm text-foreground focus-visible:ring-2 focus-visible:ring-ring"
          />
        </label>
        <label className="block">
          <span className="mb-1 block font-medium text-foreground">描述</span>
          <textarea
            value={description}
            onChange={event => setDescription(event.target.value)}
            disabled={busy}
            maxLength={500}
            rows={2}
            aria-label="开发项目描述"
            className="w-full resize-none rounded-lg border border-border bg-card px-3 py-2 text-sm text-foreground focus-visible:ring-2 focus-visible:ring-ring"
          />
        </label>
        {error && <p role="alert" className="rounded-xl border border-destructive/30 bg-[var(--color-danger-bg)] px-4 py-3 text-xs leading-5 text-destructive">{error}</p>}
      </div>
    </Modal>
  )
}

/** 零数据空态：三步引导替代一句话提示，对齐世界模型页的 beUI 空态模式 */
function EmptyGuide({ onAdd }: { onAdd: () => void }) {
  const steps = [
    { icon: <Plus size={16} />, title: '登记 MCP', desc: '粘贴客户端 JSON 或手动配置 stdio / SSE / Streamable HTTP' },
    { icon: <Wrench size={16} />, title: '连接测试', desc: '验证连通性并发现工具，清单与参数结构自动可见' },
    { icon: <FileUp size={16} />, title: '转接口或对话调用', desc: '一键生成接口代理 HTTP 接口，或供超级助手直接调用' },
  ]
  return (
    <div className="flex min-h-64 flex-1 flex-col items-center justify-center gap-6 rounded-2xl border border-border bg-card px-6 py-8 text-center">
      <div>
        <p className="text-sm font-semibold text-muted-foreground">从第一个 MCP Server 开始</p>
        <p className="mt-1 text-xs text-muted-foreground">登记插件配置，测试通过后即可查看工具清单并转为 HTTP 接口</p>
      </div>
      <ol className="grid w-full max-w-3xl grid-cols-1 gap-3 sm:grid-cols-3" aria-label="插件社区使用流程指引">
        {steps.map((step, index) => (
          <li key={step.title} className="relative flex flex-col items-center gap-2 rounded-xl border border-border bg-muted px-4 pb-4 pt-5 text-center">
            <span className="absolute left-3 top-3 flex h-5 w-5 items-center justify-center rounded-full bg-brand/10 text-[11px] font-semibold text-brand-ink">
              {index + 1}
            </span>
            <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-brand-soft text-brand-ink">{step.icon}</span>
            <p className="text-sm font-medium text-foreground">{step.title}</p>
            <p className="text-[11px] leading-5 text-muted-foreground">{step.desc}</p>
          </li>
        ))}
      </ol>
      <button
        type="button"
        onClick={onAdd}
        className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-brand px-5 text-sm font-medium text-[var(--color-text-inverse)] shadow-sm transition-all duration-200 hover:-translate-y-0.5 hover:bg-brand-deep active:translate-y-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
      >
        <Plus size={15} /> 添加第一个 MCP
      </button>
    </div>
  )
}

export default function PluginCommunityPage() {
  const reduce = useReducedMotion() ?? false
  const navigate = useNavigate()
    const [servers, setServers] = useState<SuperMcpServer[]>([])
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState<StatusKey[]>([])
  const [editing, setEditing] = useState<SuperMcpServer | 'new' | null>(null)
  const [testingId, setTestingId] = useState<string | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<SuperMcpServer | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [manifestTarget, setManifestTarget] = useState<SuperMcpServer | null>(null)
  const [exportTarget, setExportTarget] = useState<SuperMcpServer | null>(null)
  const [devCreateOpen, setDevCreateOpen] = useState(false)

  const load = useCallback(async () => {
    try {
      const items = await communityApi.mcpServers()
      setServers(Array.isArray(items) ? items.filter(item => !item.builtin_key) : [])
    } catch (error) {
      setServers([])
      toast.error('MCP 清单加载失败', { description: errorText(error, '请检查服务连接后重试。') })
    } finally {
      setLoading(false)
    }
  }, [toast])

  useEffect(() => { void load() }, [load])

  const filteredServers = useMemo(() => {
    const keyword = search.trim().toLowerCase()
    return servers.filter(server => {
      const matchesKeyword = !keyword || [
        server.name,
        server.display_name,
        server.description,
        endpointText(server),
        transportLabel(server),
        ...server.tool_manifest.flatMap(tool => [tool.name, tool.description]),
      ].some(value => String(value || '').toLowerCase().includes(keyword))
      // 多选状态筛选：未勾选视为不过滤（全部）
      const matchesStatus = statusFilter.length === 0 || statusFilter.includes(statusKeyOf(server))
      return matchesKeyword && matchesStatus
    })
  }, [search, servers, statusFilter])

  const stats = useMemo(() => ({
    total: servers.length,
    healthy: servers.filter(server => server.last_test_status === 'success').length,
    tools: servers.reduce((sum, server) => sum + server.tool_manifest.length, 0),
  }), [servers])

  const testServer = async (server: SuperMcpServer) => {
    setTestingId(server.id)
    try {
      const result = await communityApi.testMcpServer(server.id)
      await load()
      const notifyTest = result.ok ? toast.success : toast.error
      notifyTest(result.ok ? 'MCP 连接成功' : 'MCP 连接失败', { description: result.message })
    } catch (error) {
      toast.error('MCP 测试失败', { description: errorText(error, '请稍后重试。') })
    } finally {
      setTestingId(null)
    }
  }

  const removeServer = async () => {
    if (!deleteTarget || deleting) return
    setDeleting(true)
    try {
      await communityApi.deleteMcpServer(deleteTarget.id)
      setServers(current => current.filter(item => item.id !== deleteTarget.id))
      toast.success('MCP Server 已删除', { description: `「${serverTitle(deleteTarget)}」已从清单移除。` })
      setDeleteTarget(null)
    } catch (error) {
      toast.error('删除失败', { description: errorText(error, '请稍后重试。') })
    } finally {
      setDeleting(false)
    }
  }

  // 操作列四个操作完全同构：beUI IconButton + Tooltip（default h-8 w-8、14px 图标），
  // 悬停色仅按语义区分（转接口品牌色、删除危险色），结构零混搭
  const renderActions = (server: SuperMcpServer) => (
    <div className="flex items-center justify-center gap-1">
      <Tooltip content={developed(server) ? '重跑发布校验（逐工具按样例参数真实执行）' : '测试连接并刷新工具清单'}>
        <IconButton
          label={`测试 MCP ${serverTitle(server)}`}
          reduce={reduce}
          onClick={() => void testServer(server)}
          disabled={testingId === server.id}
        >
          {testingId === server.id
            ? <Loader2 size={14} className="animate-spin motion-reduce:animate-none" />
            : <Wrench size={14} />}
        </IconButton>
      </Tooltip>
      <Tooltip
        content={developed(server)
          ? '自研 MCP 暂不支持转接口（规划中）'
          : exportable(server)
            ? '将勾选工具生成为接口代理的 HTTP 接口'
            : '尚未发现工具，请先执行连接测试'}
      >
        <IconButton
          label={`转接口 ${serverTitle(server)}`}
          reduce={reduce}
          onClick={() => { if (!developed(server)) setExportTarget(server) }}
          disabled={!exportable(server) || developed(server)}
          className="hover:bg-brand-soft hover:text-brand-ink"
        >
          <FileUp size={14} />
        </IconButton>
      </Tooltip>
      {developed(server) ? (
        <Tooltip content="进入开发页编辑工具脚本（发布绑定版不受影响）">
          <IconButton
            label={`开发 ${serverTitle(server)}`}
            reduce={reduce}
            onClick={() => navigate(`/community/plugins/develop/${server.dev_project_id ?? ''}`)}
          >
            <Hammer size={14} />
          </IconButton>
        </Tooltip>
      ) : (
        <Tooltip content="编辑名称、描述与连接配置">
          <IconButton label={`编辑 MCP ${serverTitle(server)}`} reduce={reduce} onClick={() => setEditing(server)}>
            <Pencil size={14} />
          </IconButton>
        </Tooltip>
      )}
      <Tooltip content="删除该 MCP Server 及其工具清单">
        <IconButton
          label={`删除 MCP ${serverTitle(server)}`}
          reduce={reduce}
          onClick={() => setDeleteTarget(server)}
          className="hover:bg-[var(--color-danger-bg)] hover:text-destructive focus-visible:ring-ring"
        >
          <Trash2 size={14} />
        </IconButton>
      </Tooltip>
    </div>
  )

  return (
    <div className="flex min-h-full flex-col gap-4 md:h-full md:min-h-0">
      <h1 className="sr-only">插件社区</h1>

      <section data-testid="mcp-server-stats" aria-label="MCP 统计" className="grid shrink-0 grid-cols-1 gap-3 sm:grid-cols-3">
        <StatCard icon={<PlugZap size={16} />} label="MCP Server" value={stats.total} />
        <StatCard icon={<CheckCircle2 size={16} />} label="测试通过" value={stats.healthy} tone="success" />
        <StatCard icon={<Code2 size={16} />} label="已发现工具" value={stats.tools} />
      </section>

      <section aria-label="MCP 筛选与操作" className="flex shrink-0 flex-wrap items-center gap-3 rounded-2xl border border-border bg-card px-4 py-3 xl:flex-nowrap">
        <div className="relative w-full sm:w-64 xl:w-72 xl:flex-none">
          <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-[var(--color-text-tertiary)]" />
          <input
            value={search}
            onChange={event => setSearch(event.target.value)}
            placeholder="搜索名称、标识、地址或工具..."
            aria-label="搜索 MCP"
            className="w-full rounded-xl border border-border bg-card py-2 pl-8 pr-8 text-sm text-foreground outline-none transition placeholder:text-[var(--color-text-tertiary)] focus-visible:ring-2 focus-visible:ring-ring"
          />
          {search && (
            <button type="button" onClick={() => setSearch('')} aria-label="清除 MCP 搜索" className="absolute right-2 top-1/2 -translate-y-1/2 text-[var(--color-text-tertiary)] transition-colors hover:text-foreground">
              <X size={12} />
            </button>
          )}
        </div>
          <MultiSelect value={statusFilter} onValueChange={values => setStatusFilter(values as StatusKey[])}>
            <MultiSelectTrigger className="min-h-[38px] w-56 rounded-xl bg-background">
              <MultiSelectValue placeholder="全部状态" />
              <MultiSelectInput aria-label="筛选 MCP 状态" placeholder="筛选状态…" />
            </MultiSelectTrigger>
            <MultiSelectContent>
              <MultiSelectList ariaLabel="MCP 状态">
                <MultiSelectItem value="success">测试通过</MultiSelectItem>
                <MultiSelectItem value="error">连接异常</MultiSelectItem>
                <MultiSelectItem value="untested">未测试</MultiSelectItem>
              </MultiSelectList>
              <MultiSelectEmpty />
            </MultiSelectContent>
          </MultiSelect>
        {(search || statusFilter.length > 0) && (
          <button type="button" onClick={() => { setSearch(''); setStatusFilter([]) }} className="shrink-0 px-2 py-1 text-xs text-muted-foreground transition-colors hover:text-foreground">
            清除筛选
          </button>
        )}
        <p className="ml-auto shrink-0 text-xs tabular-nums text-[var(--color-text-tertiary)]">显示 {filteredServers.length} / {servers.length} 项</p>
        <button
          type="button"
          onClick={() => setDevCreateOpen(true)}
          title="用 Python 编写工具函数并封装为本平台 MCP"
          className="inline-flex h-[38px] shrink-0 items-center gap-1.5 rounded-xl border border-brand-line bg-brand-soft px-3.5 text-sm font-medium text-brand-ink transition-all duration-200 hover:-translate-y-px hover:bg-brand-mist active:translate-y-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        >
          <Hammer size={15} /> 开发 MCP
        </button>
        <button
          type="button"
          onClick={() => setEditing('new')}
          className="inline-flex h-[38px] shrink-0 items-center gap-1.5 rounded-xl bg-brand px-3.5 text-sm font-medium text-[var(--color-text-inverse)] transition-all duration-200 hover:-translate-y-px hover:bg-brand-deep active:translate-y-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        >
          <Plus size={15} /> 添加 MCP
        </button>
      </section>

      {loading ? (
        <div className="flex min-h-64 flex-1 items-center justify-center rounded-2xl border border-border bg-card">
          <div className="text-center"><Loader2 size={24} className="mx-auto animate-spin text-brand-ink motion-reduce:animate-none" /><p className="mt-3 text-sm text-muted-foreground">正在加载 MCP 清单...</p></div>
        </div>
      ) : filteredServers.length === 0 ? (
        servers.length ? (
          <div className="flex min-h-64 flex-1 flex-col items-center justify-center rounded-2xl border border-border bg-card px-6 text-center">
            <div>
              <PlugZap size={28} className="text-muted-foreground" />
            </div>
            <p className="mt-3 text-sm font-medium text-muted-foreground">没有匹配的 MCP Server</p>
            <p className="mt-1 text-xs text-muted-foreground">请调整搜索词或状态筛选后重试。</p>
          </div>
        ) : (
          <EmptyGuide onAdd={() => setEditing('new')} />
        )
      ) : (
        <>
          <section aria-label="MCP Server 清单" className="hidden min-h-64 min-w-0 flex-1 flex-col overflow-hidden rounded-2xl border border-border bg-card shadow-sm/50 md:flex">
            <div className="min-h-0 flex-1 overflow-auto">
              <table className="w-full min-w-[960px] table-fixed text-sm">
                <thead className="sticky top-0 z-10 border-b border-border bg-muted/95 backdrop-blur">
                  <tr>
                    <th className="w-[30%] px-4 py-2.5 text-left text-xs font-medium text-muted-foreground">MCP Server</th>
                    <th className="w-[14%] px-4 py-2.5 text-center text-xs font-medium text-muted-foreground">传输方式</th>
                    <th className="w-[13%] px-4 py-2.5 text-center text-xs font-medium text-muted-foreground">工具</th>
                    <th className="w-[14%] px-4 py-2.5 text-center text-xs font-medium text-muted-foreground">连接状态</th>
                    <th className="w-[29%] px-2 py-2.5 text-center text-xs font-medium text-muted-foreground">操作</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {filteredServers.map((server, _index) => (
                    <tr
                      key={server.id}
                      className="transition-colors hover:bg-muted/60"
                    >
                      <td className="px-4 py-3 align-middle">
                        <div className="min-w-0">
                          <p className="truncate font-medium text-foreground" title={serverTitle(server)}>{serverTitle(server)}</p>
                          <p className="mt-0.5 truncate text-[11px] leading-4 text-[var(--color-text-tertiary)]" title={`${server.name} · ${endpointText(server)}`}>
                            <span className="font-mono">{server.name}</span> · <span className="font-mono">{endpointText(server) || '尚未配置地址'}</span>
                          </p>
                          {server.description && <p className="mt-1 line-clamp-1 text-xs leading-4 text-muted-foreground" title={server.description}>{server.description}</p>}
                        </div>
                      </td>
                      <td className="px-4 py-3 text-center align-middle"><span className="inline-flex rounded-lg border border-border bg-muted px-2 py-1 text-[11px] font-medium text-muted-foreground">{transportLabel(server)}</span></td>
                      <td className="px-4 py-3 text-center align-middle"><button type="button" onClick={() => setManifestTarget(server)} aria-label={`查看 ${serverTitle(server)} 的工具清单`} className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium text-brand-ink transition-colors hover:bg-brand-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"><Code2 size={13} /> 共 {server.tool_manifest.length} 个</button></td>
                      <td className="px-4 py-3 text-center align-middle"><TestStatus server={server} /></td>
                      <td className="px-2 py-2 align-middle">{renderActions(server)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <div className="grid gap-3 md:hidden">
            {filteredServers.map((server, _index) => (
              <article
                key={server.id}
                className="rounded-2xl border border-border bg-card p-4 shadow-sm/50"
              >
                <div className="flex items-start gap-3">
                  <div className="min-w-0 flex-1"><h2 className="truncate text-sm font-semibold text-foreground">{serverTitle(server)}</h2><p className="mt-1 truncate font-mono text-[10px] text-[var(--color-text-tertiary)]">{server.name} · {endpointText(server)}</p></div>
                  <TestStatus server={server} />
                </div>
                {server.description && <p className="mt-2 line-clamp-2 text-xs leading-5 text-muted-foreground">{server.description}</p>}
                <div className="mt-3 flex flex-wrap gap-1.5"><span className="rounded-lg bg-muted px-2 py-1 text-[10px] text-muted-foreground">{transportLabel(server)}</span><button type="button" onClick={() => setManifestTarget(server)} className="inline-flex min-h-8 items-center gap-1 rounded-lg bg-brand-soft px-2 text-[10px] font-medium text-brand-ink"><Code2 size={11} /> 共 {server.tool_manifest.length} 个工具</button></div>
                {server.last_test_message && <p className="mt-3 line-clamp-2 text-xs leading-5 text-muted-foreground">{server.last_test_message}</p>}
                <div className="mt-3 border-t border-border pt-2">{renderActions(server)}</div>
              </article>
            ))}
          </div>
        </>
      )}

      {editing && <McpServerDialog server={editing === 'new' ? undefined : editing} client={communityApi} onClose={() => setEditing(null)} onSaved={load} />}
      {devCreateOpen && <McpDevCreateDialog onClose={() => setDevCreateOpen(false)} />}
      {manifestTarget && <ToolManifestDialog server={servers.find(item => item.id === manifestTarget.id) || manifestTarget} onClose={() => setManifestTarget(null)} />}
      {exportTarget && (
        <ExportToolsDialog
          server={servers.find(item => item.id === exportTarget.id) || exportTarget}
          onClose={() => setExportTarget(null)}
          onDone={load}
        />
      )}
      <ConfirmDialog open={!!deleteTarget} title="删除 MCP Server" description={`确认删除 MCP Server「${deleteTarget ? serverTitle(deleteTarget) : ''}」？${deleteTarget && developed(deleteTarget) ? '其开发项目、全部历史版本与样例参数将一并删除，' : '相关连接配置和工具清单将一并移除，'}此操作无法撤销。`} confirmText={deleting ? '删除中...' : '确认删除'} variant="danger" onConfirm={() => void removeServer()} onClose={() => !deleting && setDeleteTarget(null)} />
    </div>
  )
}
