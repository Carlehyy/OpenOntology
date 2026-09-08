import { useEffect, useRef, useState } from 'react'
import { Bot, GitPullRequest, Loader2, PlugZap, Plus, Save, ShieldCheck, Trash2 } from 'lucide-react'

import {
  superAssistantApi,
  type MulticaConfig,
  type MulticaTestResult,
  type RemoteAgent,
} from '@/api/superAssistant'
import { toast } from 'sonner'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select'
import { errorText } from './assistantPanelUtils'
import { DialogShell } from './AssistantConfiguration'

type IntegrationTab = 'multica' | 'remote-agents' | 'github'

const INTEGRATION_TABS: Array<{ key: IntegrationTab; label: string; soon?: boolean }> = [
  { key: 'multica', label: 'Multica' },
  { key: 'remote-agents', label: '远程助手' },
  { key: 'github', label: 'GitHub', soon: true },
]

const EMPTY_FORM = {
  id: '' as string,
  key: 'remote.',
  label: '',
  description: '',
  endpoint: '',
  token: '',
  enabled: true,
  timeoutSeconds: 120,
}

/** 已保存工作区的兜底选项：优先显示回填名称，历史行无名称时回落 ID */
function savedWorkspaceOption(config: MulticaConfig) {
  return { id: config.workspace_id, name: config.workspace_name || config.workspace_id, slug: '' }
}

/** 远程助手面板：声明式注册目录（每用户多条）。列表 + 单条内联编辑表单；
 *  保存后立即进入 delegate_to_assistant 工具目录，无需重启。 */
function RemoteAgentsPanel({ onError, onChanged }: {
  onError: (message: string) => void
  onChanged: () => void
}) {
  const [agents, setAgents] = useState<RemoteAgent[]>([])
  const [loaded, setLoaded] = useState(false)
  const [form, setForm] = useState<typeof EMPTY_FORM | null>(null)
  const [saving, setSaving] = useState(false)
  const [testingId, setTestingId] = useState('')
  const [testMessage, setTestMessage] = useState('')

  const reload = () => {
    superAssistantApi.listRemoteAgents()
      .then(data => { setAgents(data); setLoaded(true) })
      .catch(err => onError(errorText(err, '远程助手加载失败')))
  }

  useEffect(reload, [])

  const startCreate = () => {
    setTestMessage('')
    setForm({ ...EMPTY_FORM })
  }

  const startEdit = (agent: RemoteAgent) => {
    setTestMessage('')
    setForm({
      id: agent.id,
      key: agent.key,
      label: agent.label,
      description: agent.description,
      endpoint: agent.endpoint,
      token: '',
      enabled: agent.enabled,
      timeoutSeconds: agent.timeout_seconds,
    })
  }

  const save = async () => {
    if (!form || saving) return
    if (!form.key.trim() || !form.label.trim() || !form.endpoint.trim()) {
      onError('请填写 key、名称与服务地址')
      return
    }
    setSaving(true)
    try {
      const payload = {
        key: form.key.trim(),
        label: form.label.trim(),
        description: form.description.trim(),
        endpoint: form.endpoint.trim(),
        enabled: form.enabled,
        timeout_seconds: form.timeoutSeconds,
        ...(form.token.trim() ? { token: form.token.trim() } : {}),
      }
      const saved = form.id
        ? await superAssistantApi.updateRemoteAgent(form.id, payload)
        : await superAssistantApi.createRemoteAgent(payload)
      setForm(null)
      reload()
      onChanged()
      toast.success('远程助手已保存', {
        description: `${saved.label}（${saved.key}）${saved.enabled ? '已进入委派目录' : '已停用'}`,
      })
    } catch (err) {
      onError(errorText(err, '保存失败'))
    } finally {
      setSaving(false)
    }
  }

  const remove = async (agent: RemoteAgent) => {
    try {
      await superAssistantApi.deleteRemoteAgent(agent.id)
      reload()
      onChanged()
      toast.success(`已删除远程助手 ${agent.label}`)
    } catch (err) {
      onError(errorText(err, '删除失败'))
    }
  }

  const test = async (agent: RemoteAgent) => {
    if (testingId) return
    setTestingId(agent.id)
    setTestMessage('')
    try {
      const result = await superAssistantApi.testRemoteAgent(agent.id)
      setTestMessage(`${agent.label}：${result.ok ? '连接成功' : '连接失败'} — ${result.message}`)
    } catch (err) {
      setTestMessage(`${agent.label}：测试请求失败 — ${errorText(err, '未知错误')}`)
    } finally {
      setTestingId('')
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-5 [scrollbar-gutter:stable] sm:p-6">
        <section className="rounded-xl border border-[var(--color-border)] p-4">
          <div className="flex items-start gap-2">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-sky-50 text-sky-700"><Bot size={16} /></div>
            <div className="min-w-0 flex-1">
              <p className="text-xs font-semibold text-[var(--color-text-primary)]">远程助手</p>
              <p className="mt-1 text-[11px] leading-5 text-[var(--color-text-tertiary)]">
                声明式注册外部 agent：填写 key/名称/端点即进入超级助手的委派目录，
                以 <code className="font-mono">delegate_to_assistant</code> 调用并按会话续聊。契约与安全策略见端点字段说明。
              </p>
            </div>
            <button
              type="button"
              onClick={startCreate}
              className="inline-flex min-h-9 shrink-0 items-center gap-1 rounded-lg border border-[var(--color-border)] bg-white px-2.5 text-xs text-brand-ink transition-colors hover:bg-brand-soft"
            >
              <Plus size={13} /> 新增
            </button>
          </div>

          <div className="mt-3 space-y-2" data-testid="remote-agent-list">
            {loaded && agents.length === 0 && (
              <p className="rounded-lg bg-[var(--color-bg-base)] px-3 py-2 text-[11px] text-[var(--color-text-tertiary)]">
                还没有注册远程助手。点「新增」填写端点即可让超级助手委派它。
              </p>
            )}
            {agents.map(agent => (
              <div key={agent.id} className="flex items-center gap-2 rounded-lg border border-[var(--color-border)] px-3 py-2">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <p className="truncate text-xs font-medium text-[var(--color-text-primary)]">{agent.label}</p>
                    <code className="shrink-0 rounded bg-brand-soft px-1 py-0.5 font-mono text-[10px] text-brand-ink">{agent.key}</code>
                    {!agent.enabled && <span className="shrink-0 rounded bg-slate-100 px-1 py-0.5 text-[9px] text-slate-400">已停用</span>}
                    {agent.token_set && <span className="shrink-0 text-[10px] text-[var(--color-text-tertiary)]">已配凭据</span>}
                  </div>
                  <p className="mt-0.5 truncate font-mono text-[10px] text-[var(--color-text-tertiary)]">{agent.endpoint}</p>
                </div>
                <button type="button" onClick={() => test(agent)} disabled={testingId === agent.id}
                  className="inline-flex min-h-9 items-center gap-1 rounded-lg px-2 text-xs text-brand-ink transition-colors hover:bg-brand-soft disabled:opacity-50">
                  {testingId === agent.id ? <Loader2 size={12} className="animate-spin" /> : <ShieldCheck size={12} />} 测试
                </button>
                <button type="button" onClick={() => startEdit(agent)}
                  className="min-h-9 rounded-lg px-2 text-xs text-brand-ink transition-colors hover:bg-brand-soft">编辑</button>
                <button type="button" onClick={() => void remove(agent)} aria-label={`删除 ${agent.label}`}
                  className="inline-flex min-h-9 items-center rounded-lg px-2 text-xs text-red-600 transition-colors hover:bg-red-50">
                  <Trash2 size={12} />
                </button>
              </div>
            ))}
          </div>

          {form && (
            <div className="mt-3 space-y-3 rounded-lg bg-[var(--color-bg-base)] p-3" data-testid="remote-agent-form">
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="block text-xs text-[var(--color-text-secondary)]">key <span className="text-red-500">*</span>
                  <input
                    data-testid="remote-agent-key"
                    value={form.key}
                    disabled={!!form.id}
                    onChange={event => setForm({ ...form, key: event.target.value })}
                    placeholder="remote.my-agent"
                    className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 font-mono text-sm outline-none focus:border-brand-deep focus:ring-2 focus:ring-ring/10 disabled:opacity-60"
                  />
                  <span className="mt-1 block text-[10px] leading-4 text-[var(--color-text-tertiary)]">
                    以 remote. 开头（小写字母/数字/-/_），创建后不可改。
                  </span>
                </label>
                <label className="block text-xs text-[var(--color-text-secondary)]">名称 <span className="text-red-500">*</span>
                  <input
                    data-testid="remote-agent-label"
                    value={form.label}
                    onChange={event => setForm({ ...form, label: event.target.value })}
                    placeholder="例如：客服知识库助手"
                    className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 text-sm outline-none focus:border-brand-deep focus:ring-2 focus:ring-ring/10"
                  />
                </label>
              </div>
              <label className="block text-xs text-[var(--color-text-secondary)]">能力描述（给超级助手路由用）
                <input
                  data-testid="remote-agent-description"
                  value={form.description}
                  onChange={event => setForm({ ...form, description: event.target.value })}
                  placeholder="它擅长什么、适合什么任务（子助手看不到对话历史，描述要自包含）"
                  className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 text-sm outline-none focus:border-brand-deep focus:ring-2 focus:ring-ring/10"
                />
              </label>
              <label className="block text-xs text-[var(--color-text-secondary)]">回合端点 <span className="text-red-500">*</span>
                <input
                  data-testid="remote-agent-endpoint"
                  value={form.endpoint}
                  onChange={event => setForm({ ...form, endpoint: event.target.value })}
                  placeholder="https://agent.example.com/turn"
                  className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 font-mono text-sm outline-none focus:border-brand-deep focus:ring-2 focus:ring-ring/10"
                />
                <span className="mt-1 block text-[10px] leading-4 text-[var(--color-text-tertiary)]">
                  POST JSON：{"{ message, session_ref }"} → {"{ status, content, session_ref }"}（首回合由远端签发
                  session_ref 续聊复用）。生产环境按 SSRF 策略拒绝内网地址。
                </span>
              </label>
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="block text-xs text-[var(--color-text-secondary)]">Bearer Token
                  <input
                    data-testid="remote-agent-token"
                    type="password"
                    value={form.token}
                    onChange={event => setForm({ ...form, token: event.target.value })}
                    placeholder={form.id && agents.find(a => a.id === form.id)?.token_set ? '已保存（留空保留）' : '可留空'}
                    className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 font-mono text-sm outline-none focus:border-brand-deep focus:ring-2 focus:ring-ring/10"
                  />
                  <span className="mt-1 block text-[10px] leading-4 text-[var(--color-text-tertiary)]">加密存储、永不回显。</span>
                </label>
                <div className="grid grid-cols-2 gap-3">
                  <label className="block text-xs text-[var(--color-text-secondary)]">超时（秒）
                    <input
                      type="number" min={10} max={600}
                      value={form.timeoutSeconds}
                      onChange={event => setForm({ ...form, timeoutSeconds: Number(event.target.value) || 120 })}
                      className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 text-sm outline-none focus:border-brand-deep focus:ring-2 focus:ring-ring/10"
                    />
                  </label>
                  <label className="flex items-end pb-1 text-xs text-[var(--color-text-secondary)]">
                    <input type="checkbox" checked={form.enabled} onChange={event => setForm({ ...form, enabled: event.target.checked })} className="mr-2 h-4 w-4 accent-brand" />
                    启用
                  </label>
                </div>
              </div>
            </div>
          )}
          {testMessage && (
            <p role="status" data-testid="remote-agent-test-result" className="mt-3 rounded-lg px-3 py-2 text-[11px] leading-5 bg-brand-soft text-brand-ink">
              {testMessage}
            </p>
          )}
        </section>
      </div>
      <footer className="flex shrink-0 justify-center gap-3 px-5 pb-4">
        <button onClick={() => setForm(null)} className="min-h-10 min-w-24 rounded-lg px-4 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">取消</button>
        <button
          onClick={() => void save()}
          data-testid="remote-agent-save-button"
          disabled={saving || !form}
          className="inline-flex min-h-10 min-w-24 items-center justify-center gap-2 rounded-lg bg-brand px-4 text-xs font-medium text-white transition-colors hover:bg-brand-deep focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
        >
          {saving ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />} 保存
        </button>
      </footer>
    </div>
  )
}

/** 外部集成面板：左侧集成类型 tabs，multica 为首个落地集成，远程助手为
 *  声明式注册目录，GitHub 为结构化占位（后续新集成在此追加 tab）。
 *  DialogShell 为常开弹层，调用方以条件挂载控制显隐（同 McpDialog 用法）。
 */
export default function IntegrationsDialog({ onClose, onSaved }: {
  onClose: () => void
  onSaved?: () => void | Promise<void>
}) {
  const [tab, setTab] = useState<IntegrationTab>('multica')
  const [config, setConfig] = useState<MulticaConfig | null>(null)
  const [baseUrl, setBaseUrl] = useState('')
  const [token, setToken] = useState('')
  const [workspaceId, setWorkspaceId] = useState('')
  const [enabled, setEnabled] = useState(false)
  const [workspaces, setWorkspaces] = useState<MulticaTestResult['workspaces']>([])
  const [testResult, setTestResult] = useState<MulticaTestResult | null>(null)
  const [testing, setTesting] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const baseUrlInputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    setError('')
    setTestResult(null)
    superAssistantApi.multicaConfig()
      .then(data => {
        setConfig(data)
        setBaseUrl(data.base_url)
        setWorkspaceId(data.workspace_id)
        setEnabled(data.enabled)
        setToken('')
        // 已保存配置的兜底选项：优先显示回填的工作区名，历史行无名称时
        // 回落显示 ID（下次测试连接/保存即补齐名称）
        setWorkspaces(data.configured && data.workspace_id
          ? [savedWorkspaceOption(data)]
          : [])
        // 已配置且已存凭据时打开即实时拉取全量工作区（工作区列表不持久化，
        // 仅靠上面的单条兜底会掩盖其余可选工作区）；拉取失败静默回落兜底。
        // 实时列表缺失已保存项时补一条，保证当前选中仍以名称展示。
        if (data.configured && data.token_set && data.base_url) {
          superAssistantApi.multicaWorkspaces()
            .then(result => {
              setWorkspaces(data.workspace_id && !result.workspaces.some(item => item.id === data.workspace_id)
                ? [...result.workspaces, savedWorkspaceOption(data)]
                : result.workspaces)
            })
            .catch(() => {})
        }
      })
      .catch(err => setError(errorText(err, '配置加载失败')))
  }, [])

  const testConnection = async () => {
    if (testing) return
    setTesting(true)
    setError('')
    try {
      const result = await superAssistantApi.testMultica({
        base_url: baseUrl.trim() || null,
        token: token.trim() || null,
      })
      setTestResult(result)
      if (result.ok) setWorkspaces(result.workspaces)
    } catch (err) {
      setError(errorText(err, '连接测试失败'))
    } finally {
      setTesting(false)
    }
  }

  const save = async () => {
    if (saving) return
    if (!baseUrl.trim() || !workspaceId) {
      setError('请填写服务地址并选择工作区（可先执行连接测试获取工作区列表）')
      return
    }
    if (enabled && !token.trim() && !config?.token_set) {
      setError('启用集成前请先填写 API Token')
      return
    }
    setSaving(true)
    setError('')
    try {
      const selectedWorkspace = workspaces.find(workspace => workspace.id === workspaceId)
      const saved = await superAssistantApi.updateMulticaConfig({
        base_url: baseUrl.trim(),
        token: token.trim() || null,
        workspace_id: workspaceId,
        workspace_name: selectedWorkspace?.name || config?.workspace_name || null,
        enabled,
      })
      setConfig(saved)
      await onSaved?.()
      toast.success('Multica 配置已保存', {
        description: saved.enabled ? '现在可以在输入框使用 /multica: 命令' : '集成已停用',
      })
      onClose()
    } catch (err) {
      setError(errorText(err, '保存失败'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <DialogShell
      size="large"
      title="外部集成"
      description="把外部平台接入超级助手；配置生效后可在输入框使用对应命令。"
      onClose={onClose}
      /* 固定宽高：切换 tab 不改变弹窗大小，垂直超出经内容区滚轮滚动 */
      contentClassName="h-[min(82dvh,44rem)]"
      /* 焦点落服务地址输入框，避免左栏 tab/底部按钮被默认聚焦呈现选中态 */
      onOpenAutoFocus={event => {
        event.preventDefault()
        baseUrlInputRef.current?.focus()
      }}
    >
      <div className="flex min-h-0 flex-1">
        <nav aria-label="集成类型" className="flex w-40 shrink-0 flex-col gap-1 border-r border-[var(--color-border)] p-2">
          {INTEGRATION_TABS.map(item => (
            <button
              key={item.key}
              type="button"
              role="tab"
              aria-selected={tab === item.key}
              data-integrations-tab={item.key}
              onClick={() => setTab(item.key)}
              className={`flex min-h-10 items-center gap-1.5 rounded-lg px-2.5 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${tab === item.key
                ? 'bg-brand-soft font-medium text-brand-ink'
                : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)]'}`}
            >
              {item.key === 'multica' ? <PlugZap size={14} className="shrink-0" /> : item.key === 'remote-agents' ? <Bot size={14} className="shrink-0" /> : <GitPullRequest size={14} className="shrink-0" />}
              <span className="min-w-0 truncate">{item.label}</span>
              {item.soon && <span className="ml-auto shrink-0 rounded bg-slate-100 px-1 py-0.5 text-[9px] text-slate-400">规划中</span>}
            </button>
          ))}
        </nav>
        {tab === 'multica' ? (
          <div className="flex min-h-0 flex-1 flex-col">
            <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-5 [scrollbar-gutter:stable] sm:p-6">
              <section data-testid="multica-config-card" className="rounded-xl border border-[var(--color-border)] p-4">
          <div className="flex items-start gap-2">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-sky-50 text-sky-700"><PlugZap size={16} /></div>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-1.5">
                <p className="text-xs font-semibold text-[var(--color-text-primary)]">Multica</p>
                <span className={`h-2 w-2 rounded-full ${config?.last_test_status === 'success' ? 'bg-success' : config?.last_test_status === 'error' ? 'bg-red-500' : 'bg-slate-300'}`} />
                {config?.enabled && <span className="rounded bg-brand-soft px-1.5 py-0.5 text-[9px] text-brand-ink">已启用</span>}
              </div>
              <p className="mt-1 text-[11px] leading-5 text-[var(--color-text-tertiary)]">
                多智能体协作工作台：查看智能体、下发任务、查看任务清单。配置后输入 <code className="font-mono">/multica:</code> 使用命令。
              </p>
            </div>
          </div>

          <div className="mt-3 space-y-3">
            <label className="block text-xs text-[var(--color-text-secondary)]">服务地址 <span className="text-red-500">*</span>
              <input
                ref={baseUrlInputRef}
                data-testid="multica-base-url"
                value={baseUrl}
                onChange={event => setBaseUrl(event.target.value)}
                placeholder="http://127.0.0.1:8080"
                className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 text-sm outline-none focus:border-brand-deep focus:ring-2 focus:ring-ring/10"
              />
              <span className="mt-1 block text-[10px] leading-4 text-[var(--color-text-tertiary)]">
                自托管实例的 API 地址（默认端口 8080）；生产环境按 SSRF 策略拒绝内网地址。
              </span>
            </label>
            <label className="block text-xs text-[var(--color-text-secondary)]">API Token
              <input
                data-testid="multica-token"
                type="password"
                value={token}
                onChange={event => setToken(event.target.value)}
                placeholder={config?.token_set ? '已保存（留空保留）' : 'mul_…（在 Multica 网页 Settings → API Token 创建）'}
                className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 font-mono text-sm outline-none focus:border-brand-deep focus:ring-2 focus:ring-ring/10"
              />
              <span className="mt-1 block text-[10px] leading-4 text-[var(--color-text-tertiary)]">
                加密存储、永不回显；只用 PAT，不保存登录验证码。
              </span>
            </label>
            <label className="block text-xs text-[var(--color-text-secondary)]">工作区 <span className="text-red-500">*</span>
              <Select value={workspaceId} onValueChange={setWorkspaceId}>
                <SelectTrigger aria-label="Multica 工作区" className="mt-1.5 min-h-11 text-sm">
                  <SelectValue placeholder="连接测试后选择" />
                </SelectTrigger>
                <SelectContent>
                  {workspaces.map(workspace => (
                    <SelectItem key={workspace.id} value={workspace.id}>
                      {workspace.name || workspace.slug || workspace.id}
                    </SelectItem>
                  ))}
                  {!workspaces.some(workspace => workspace.id === workspaceId) && workspaceId && (
                    <SelectItem value={workspaceId}>{workspaceId}</SelectItem>
                  )}
                </SelectContent>
              </Select>
            </label>
            {/* 操作控件同行靠左，不做两端分置 */}
            <div className="flex items-center gap-2">
              <button
                type="button"
                data-testid="multica-test-button"
                onClick={() => void testConnection()}
                disabled={testing || !baseUrl.trim()}
                className="inline-flex min-h-10 items-center gap-1.5 rounded-lg border border-[var(--color-border)] bg-white px-3 text-xs text-brand-ink transition-colors hover:bg-brand-soft disabled:opacity-50"
              >
                {testing ? <Loader2 size={13} className="animate-spin" /> : <ShieldCheck size={13} />} 测试连接
              </button>
              <label className="flex min-h-10 items-center gap-2 rounded-lg border border-[var(--color-border)] px-3 text-xs text-[var(--color-text-secondary)]">
                <input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)} className="h-4 w-4 accent-brand" />
                启用集成
              </label>
            </div>
            {testResult && (
              <p role="status" data-testid="multica-test-result" className={`rounded-lg px-3 py-2 text-[11px] leading-5 ${testResult.ok ? 'bg-brand-soft text-brand-ink' : 'bg-red-50 text-red-700'}`}>
                {testResult.message}
              </p>
            )}
            {config?.enabled && config.commands.length > 0 && (
              <div className="rounded-lg bg-[var(--color-bg-base)] p-3">
                <p className="text-[10px] font-medium text-[var(--color-text-secondary)]">可用命令</p>
                <ul className="mt-1 space-y-1">
                  {config.commands.map(command => (
                    <li key={command.command} className="flex items-baseline gap-2 text-[11px] leading-5">
                      <code className="font-mono text-brand-ink">{command.usage.split(' ', 1)[0]}</code>
                      <span className="text-[var(--color-text-secondary)]">{command.title}{command.write ? ' · 执行前需确认' : ''}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
            </section>
              {error && <p role="alert" className="mt-4 text-xs text-red-600">{error}</p>}
            </div>
            {/* 操作按钮归属各集成面板：每个集成独立保存，互不干扰。
                紧贴内容区不留隔离边距，弹窗高度固定后按钮位置稳定 */}
            <footer className="flex shrink-0 justify-center gap-3 px-5 pb-4">
              <button onClick={onClose} className="min-h-10 min-w-24 rounded-lg px-4 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">取消</button>
              <button
                onClick={() => void save()}
                data-testid="multica-save-button"
                disabled={saving || !baseUrl.trim()}
                className="inline-flex min-h-10 min-w-24 items-center justify-center gap-2 rounded-lg bg-brand px-4 text-xs font-medium text-white transition-colors hover:bg-brand-deep focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
              >
                {saving ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />} 保存
              </button>
            </footer>
          </div>
        ) : tab === 'remote-agents' ? (
          <RemoteAgentsPanel
            onError={message => setError(message)}
            onChanged={() => void onSaved?.()}
          />
        ) : (
          <div data-testid="integrations-github-placeholder" className="flex min-h-0 flex-1 flex-col items-center justify-center gap-2 p-8 text-center">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-slate-50 text-slate-300"><GitPullRequest size={20} /></div>
            <p className="text-xs font-medium text-[var(--color-text-secondary)]">GitHub 集成规划中</p>
            <p className="max-w-64 text-[11px] leading-5 text-[var(--color-text-tertiary)]">
              计划支持 Issue 查询与创建、仓库事件接入；上线后在此配置，输入框将同步提供对应命令。
            </p>
          </div>
        )}
      </div>
      {tab === 'remote-agents' && error && (
        <p role="alert" className="px-5 pb-2 text-xs text-red-600">{error}</p>
      )}
    </DialogShell>
  )
}
