import { useEffect, useState } from 'react'
import { GitPullRequest, Loader2, PlugZap, Save, ShieldCheck } from 'lucide-react'

import {
  superAssistantApi,
  type MulticaConfig,
  type MulticaTestResult,
} from '@/api/superAssistant'
import { toast } from 'sonner'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select'
import { errorText } from './assistantPanelUtils'
import { DialogShell } from './AssistantConfiguration'

type IntegrationTab = 'multica' | 'github'

const INTEGRATION_TABS: Array<{ key: IntegrationTab; label: string; soon?: boolean }> = [
  { key: 'multica', label: 'multica' },
  { key: 'github', label: 'GitHub', soon: true },
]

/** 外部集成面板：左侧集成类型 tabs，multica 为首个落地集成，GitHub 为
 *  结构化占位（后续新集成在此追加 tab）。DialogShell 为常开弹层，调用方
 *  以条件挂载控制显隐（同 McpDialog 用法）。
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
          ? [{ id: data.workspace_id, name: data.workspace_name || data.workspace_id, slug: '' }]
          : [])
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
      toast.success('multica 配置已保存', {
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
    >
      <div className="flex min-h-0 flex-1">
        <nav aria-label="集成类型" className="flex w-28 shrink-0 flex-col gap-1 border-r border-[var(--color-border)] p-2 sm:w-32">
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
              {item.key === 'multica' ? <PlugZap size={14} className="shrink-0" /> : <GitPullRequest size={14} className="shrink-0" />}
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
                <p className="text-xs font-semibold text-[var(--color-text-primary)]">multica</p>
                <span className={`h-2 w-2 rounded-full ${config?.last_test_status === 'success' ? 'bg-brand' : config?.last_test_status === 'error' ? 'bg-red-500' : 'bg-slate-300'}`} />
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
                placeholder={config?.token_set ? '已保存（留空保留）' : 'mul_…（在 multica 网页 Settings → API Token 创建）'}
                className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 font-mono text-sm outline-none focus:border-brand-deep focus:ring-2 focus:ring-ring/10"
              />
              <span className="mt-1 block text-[10px] leading-4 text-[var(--color-text-tertiary)]">
                加密存储、永不回显；只用 PAT，不保存登录验证码。
              </span>
            </label>
            <label className="block text-xs text-[var(--color-text-secondary)]">工作区 <span className="text-red-500">*</span>
              <Select value={workspaceId} onValueChange={setWorkspaceId}>
                <SelectTrigger aria-label="multica 工作区" className="mt-1.5 min-h-11 text-sm">
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
            <div className="flex items-center justify-between gap-2">
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
                不加分割线——内容不足一屏时它悬在空白里，左边又被导航截断 */}
            <footer className="flex shrink-0 justify-center gap-3 px-5 pb-4 pt-2">
              <button onClick={onClose} className="min-h-10 min-w-24 rounded-lg px-4 text-xs text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)]">取消</button>
              <button
                onClick={() => void save()}
                data-testid="multica-save-button"
                disabled={saving || !baseUrl.trim()}
                className="inline-flex min-h-10 min-w-24 items-center justify-center gap-2 rounded-lg bg-brand px-4 text-xs font-medium text-white hover:bg-brand-deep disabled:opacity-50"
              >
                {saving ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />} 保存
              </button>
            </footer>
          </div>
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
    </DialogShell>
  )
}
