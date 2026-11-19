import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ExternalLink, KeyRound, Loader2, Network, PlugZap, RefreshCw, Unplug,
} from 'lucide-react'
import { toast } from 'sonner'

import {
  superAssistantApi,
  type SuperMcpServer,
  type WeKnoraDirectory,
  type WeKnoraEndpoint,
} from '@/api/superAssistant'
import { Alert } from '@/components/ui/Alert'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import { SettingSwitch } from './SettingSwitch'
import { errorText } from './assistantPanelUtils'
import {
  weknoraComplianceDetail,
  weknoraEndpointCompliance,
  weknoraEndpointConnectBlocked,
  weknoraEndpointStatusView,
  weknoraManagedServerFor,
} from './weknoraMcpLogic'

/** 端点状态徽章的语义浅底（success 走 Tailwind 语义类，其余与 Alert 同源令牌） */
const STATUS_TONE_CLASS: Record<string, string> = {
  success: 'bg-success-bg text-success',
  warning: 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]',
  danger: 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]',
  muted: 'bg-[var(--color-bg-hover)] text-[var(--color-text-tertiary)]',
}

/** Token 输入弹窗：标准 ui/dialog 壳（DESIGN.md §4.5 头部模板），
 *  不经 DialogShell 以避免与 AssistantConfiguration 形成模块环。 */
function WeKnoraTokenDialog({ endpoint, acting, onConnect, onClose }: {
  endpoint: WeKnoraEndpoint
  acting: boolean
  onConnect: (endpoint: WeKnoraEndpoint, body: { token: string }) => Promise<boolean>
  onClose: () => void
}) {
  const [token, setToken] = useState('')
  const [error, setError] = useState('')

  const submit = async () => {
    const trimmed = token.trim()
    if (!trimmed) { setError('请输入 WeKnora 端点的访问 Token'); return }
    // 凭据保存成功即关闭（验证结果由 connect 内按 last_test_status 如实 toast）
    const ok = await onConnect(endpoint, { token: trimmed })
    if (ok) onClose()
  }

  return (
    <Dialog open onOpenChange={value => { if (!value) onClose() }}>
      <DialogContent className="flex max-h-[90dvh] w-[min(92vw,36rem)] flex-col overflow-hidden p-0">
        <DialogHeader
          icon={<KeyRound size={18} />}
          className="mb-0 shrink-0 border-b border-[var(--color-border)] px-5 py-4 pr-14"
        >
          <div>
            <DialogTitle className="text-base font-semibold leading-6">接入 WeKnora 端点：{endpoint.name}</DialogTitle>
            <DialogDescription className="mt-1 text-sm leading-6">该端点已设置访问 Token，输入现有 Token 完成接入。</DialogDescription>
          </div>
        </DialogHeader>
        <div className="flex-1 space-y-4 overflow-y-auto p-5">
          <label className="block text-xs text-[var(--color-text-secondary)]">访问 Token <span className="text-[var(--color-danger)]">*</span>
            <input
              type="password"
              value={token}
              autoFocus
              autoComplete="off"
              aria-label={`WeKnora 端点 ${endpoint.name} 的访问 Token`}
              onChange={event => { setToken(event.target.value); setError('') }}
              onKeyDown={event => { if (event.key === 'Enter') void submit() }}
              placeholder="粘贴创建端点时保存的 Token"
              className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 font-mono text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
            <span className="mt-1 block text-[10px] leading-4 text-[var(--color-text-tertiary)]">
              Token 仅在端点创建时展示一次，无法回读；丢失请关闭本弹窗后改用「轮换 Token」重新生成。
            </span>
          </label>
          {error && <p role="alert" className="text-xs text-[var(--color-danger)]">{error}</p>}
        </div>
        <footer className="flex justify-center gap-2 border-t border-[var(--color-border)] px-5 py-4">
          <button
            onClick={onClose}
            className="min-h-10 rounded-lg px-4 text-xs text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)]"
          >
            取消
          </button>
          <button
            onClick={() => void submit()}
            disabled={acting}
            className="inline-flex min-h-10 items-center gap-2 rounded-lg bg-brand px-4 text-xs font-medium text-white hover:bg-brand-deep disabled:opacity-50"
          >
            {acting && <Loader2 size={13} className="animate-spin" />} 接入
          </button>
        </footer>
      </DialogContent>
    </Dialog>
  )
}

/**
 * WeKnora MCP 端点管理（配置面板 MCP tab 顶部，仅管理员）：
 * 挂载即 GET 目录；未 initialized 自动 POST initialize（幂等，缺模型失败不
 * 置闩、可手动重试）；已 initialized 且空目录只提供显式创建按钮，不自动恢复。
 * 接入默认仅只读端点（ask 等工具不合规，展示明细并引导原生页编辑）；
 * 已接入端点的启停/确认复用面板既有 updateMcpServer 链路，断开仅删平台侧
 * 托管行（deleteMcpServer），不触碰 WeKnora 侧端点与知识库配置。
 */
export default function WeKnoraMcpSection({ servers, refreshServers, onOpenWeKnora, onUpdateServerSetting, updatingServerSetting }: {
  servers: SuperMcpServer[]
  refreshServers: () => Promise<void>
  /** 复用 SuperAssistantPage.openWeknora 的一键免登录原生页入口 */
  onOpenWeKnora: () => void
  /** 复用面板既有的启停/自动执行更新链路（与普通 MCP 行同一实现） */
  onUpdateServerSetting: (
    server: SuperMcpServer,
    setting: 'enabled' | 'require_confirmation',
    value: boolean,
  ) => void
  updatingServerSetting: string | null
}) {
  const [directory, setDirectory] = useState<WeKnoraDirectory | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [initializing, setInitializing] = useState(false)
  const [initError, setInitError] = useState<string | null>(null)
  const [actingId, setActingId] = useState<string | null>(null)
  const [tokenDialogFor, setTokenDialogFor] = useState<WeKnoraEndpoint | null>(null)
  const [rotateFor, setRotateFor] = useState<WeKnoraEndpoint | null>(null)
  const [disconnectFor, setDisconnectFor] = useState<WeKnoraEndpoint | null>(null)

  const loadDirectory = useCallback(async () => {
    const next = await superAssistantApi.weknoraMcpEndpoints()
    setDirectory(next)
    return next
  }, [])

  const initialize = useCallback(async (recreate = false) => {
    setInitializing(true)
    setInitError(null)
    try {
      const next = await superAssistantApi.initializeWeKnoraMcp({ recreate })
      setDirectory(next)
      if (!next.initialized) {
        // 初始化未生效（如全局缺模型）：后端不置闩，目录 message 已如实带回，
        // 这里给出可重试的持久错误条而不是静默成功
        setInitError(next.message || '初始化未完成，请检查 WeKnora 配置后重试')
      } else {
        // 已同步 ≠ 已就绪：端点可能存在但尚未接入，不宣称「已就绪」
        toast.success('WeKnora MCP 目录已同步', {
          description: next.endpoints.length > 0 ? `${next.endpoints.length} 个端点` : '暂无端点',
        })
      }
      await refreshServers().catch(() => undefined)
    } catch (error) {
      setInitError(errorText(error, 'WeKnora MCP 初始化失败'))
    } finally {
      setInitializing(false)
    }
  }, [refreshServers])

  const boot = useCallback(async () => {
    setLoading(true)
    setLoadError(null)
    try {
      const next = await loadDirectory()
      // 生命周期契约：GET 目录只刷新状态、不创建任何外部资源；只要后端未
      // 置 initialized 就补一次幂等 initialize（已有端点时仅持久化已见目录
      // 标记，不会重复创建）。StrictMode 双挂载由 bootedRef + 后端幂等兜底。
      if (!next.initialized) await initialize()
    } catch (error) {
      setLoadError(errorText(error, 'WeKnora MCP 目录加载失败'))
    } finally {
      setLoading(false)
    }
  }, [loadDirectory, initialize])

  // 仅首挂载执行一次：servers/refreshServers 变化不重跑（bootedRef 挡住
  // callback 身份变化引发的重复 effect），避免接入后刷新列表造成请求循环
  const bootedRef = useRef(false)
  useEffect(() => {
    if (bootedRef.current) return
    bootedRef.current = true
    void boot()
  }, [boot])

  // 动作成功后的目录/列表刷新按尽力而为处理：刷新失败不得把已成功的
  // 接入/校验/断开误报为失败（下次打开面板会重新拉目录兜底）
  const refreshAll = useCallback(async () => {
    await Promise.allSettled([loadDirectory(), refreshServers()])
  }, [loadDirectory, refreshServers])

  const connect = useCallback(async (
    endpoint: WeKnoraEndpoint,
    body: { token?: string; rotate_token?: boolean } = {},
  ): Promise<boolean> => {
    setActingId(endpoint.id)
    try {
      const server = await superAssistantApi.connectWeKnoraMcpEndpoint(endpoint.id, body)
      await refreshAll()
      // HTTP 200 只代表请求受理：凭据/连接是否可用以 last_test_status 如实呈现
      if (server.last_test_status === 'error') {
        toast.error(`端点「${endpoint.name}」凭据已保存，验证失败`, {
          description: server.last_test_message || '请检查 Token 或端点配置后重新校验',
        })
      } else if (server.last_test_status === 'success') {
        toast.success(`WeKnora 端点「${endpoint.name}」已接入并通过验证`, {
          description: server.tool_manifest.length > 0
            ? `${server.tool_manifest.length} 个工具已挂载`
            : '工具清单为空，请校验连接',
        })
      } else {
        toast.info(`端点「${endpoint.name}」已接入，待校验`)
      }
      return true
    } catch (error) {
      toast.error('WeKnora 端点接入失败', { description: errorText(error) })
      return false
    } finally {
      setActingId(null)
    }
  }, [refreshAll])

  const verify = useCallback(async (endpoint: WeKnoraEndpoint) => {
    setActingId(endpoint.id)
    try {
      const server = await superAssistantApi.verifyWeKnoraMcpEndpoint(endpoint.id)
      await refreshAll()
      // HTTP 200 不代表校验成功：以返回的 last_test_status 如实呈现
      if (server.last_test_status === 'error') {
        toast.error('WeKnora MCP 校验失败', {
          description: server.last_test_message || '请检查 WeKnora 端点配置与 Token',
        })
      } else if (server.last_test_status === 'success') {
        toast.success('WeKnora MCP 校验成功', {
          description: server.tool_manifest.length > 0
            ? `${server.tool_manifest.length} 个工具可用`
            : undefined,
        })
      } else {
        toast.info('WeKnora MCP 校验已返回', { description: server.last_test_message || undefined })
      }
    } catch (error) {
      toast.error('WeKnora MCP 校验失败', { description: errorText(error) })
    } finally {
      setActingId(null)
    }
  }, [refreshAll])

  const disconnect = useCallback(async (endpoint: WeKnoraEndpoint) => {
    const server = weknoraManagedServerFor(endpoint, servers)
    if (!server) { setDisconnectFor(null); return }
    setActingId(endpoint.id)
    try {
      // 复用 deleteMcpServer：仅移除平台侧托管行，WeKnora 侧端点/Token/知识库不动
      await superAssistantApi.deleteMcpServer(server.id)
      await refreshAll()
      toast.success(`已断开 WeKnora 端点「${endpoint.name}」`, {
        description: 'WeKnora 侧端点与知识库配置保持不变，可随时重新接入',
      })
    } catch (error) {
      toast.error('断开失败', { description: errorText(error) })
    } finally {
      setActingId(null)
      setDisconnectFor(null)
    }
  }, [servers, refreshAll])

  const endpoints = directory?.endpoints ?? []

  return (
    <section aria-label="WeKnora MCP 端点" data-testid="weknora-mcp-section" className="grid gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="flex items-baseline gap-1.5 text-[11px] font-semibold text-[var(--color-text-primary)]">
          WeKnora 知识库 MCP
          <span className="text-[10px] font-normal tabular-nums text-[var(--color-text-tertiary)]">{endpoints.length} 个端点</span>
        </h3>
        <button
          type="button"
          onClick={() => void boot()}
          disabled={loading || initializing || actingId !== null}
          aria-label="刷新 WeKnora 端点目录"
          className="inline-flex min-h-8 items-center gap-1 rounded-md px-2 text-[11px] text-brand-ink transition-colors hover:bg-brand-soft disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <RefreshCw size={12} /> 刷新
        </button>
        <button
          type="button"
          onClick={() => onOpenWeKnora()}
          data-ctx-item="weknora-mcp-open-native"
          className="inline-flex min-h-8 items-center gap-1 rounded-md px-2 text-[11px] text-brand-ink transition-colors hover:bg-brand-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <ExternalLink size={12} /> 前往 WeKnora
        </button>
      </div>

      {directory?.message && (
        <Alert variant="info" data-testid="weknora-directory-message">{directory.message}</Alert>
      )}

      {loadError && (
        <Alert variant="danger" role="alert">
          <p>{loadError}</p>
          <button
            type="button"
            onClick={() => void boot()}
            className="mt-1 inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs font-medium underline underline-offset-2"
          >
            <RefreshCw size={11} /> 重试
          </button>
        </Alert>
      )}

      {initError && (
        <Alert variant="danger" role="alert" data-testid="weknora-init-error">
          <p>{initError}</p>
          <button
            type="button"
            onClick={() => void initialize()}
            className="mt-1 inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs font-medium underline underline-offset-2"
          >
            <RefreshCw size={11} /> 重试初始化
          </button>
        </Alert>
      )}

      {loading && (
        <div className="flex items-center justify-center gap-2 rounded-xl border border-[var(--color-border)] p-6 text-xs text-[var(--color-text-tertiary)]">
          <Loader2 size={14} className="animate-spin text-brand-ink" /> 正在加载 WeKnora MCP 目录…
        </div>
      )}

      {!loading && initializing && (
        <div className="flex items-center justify-center gap-2 rounded-xl border border-[var(--color-border)] p-6 text-xs text-[var(--color-text-tertiary)]" data-testid="weknora-initializing">
          <Loader2 size={14} className="animate-spin text-brand-ink" /> 正在初始化 WeKnora MCP 端点目录…
        </div>
      )}

      {!loading && !initializing && directory?.initialized && endpoints.every(endpoint => endpoint.missing) && (
        <div className="rounded-xl border border-dashed border-[var(--color-border)] p-6 text-center" data-testid="weknora-empty-directory">
          <Network size={22} className="mx-auto mb-2 text-[var(--color-text-tertiary)]" />
          <p className="text-xs leading-5 text-[var(--color-text-tertiary)]">
            WeKnora 侧暂无 MCP 端点。可在原生页创建端点，或由平台创建默认端点。
          </p>
          <div className="mt-3 flex justify-center gap-2">
            <button
              type="button"
              onClick={() => void initialize(true)}
              className="inline-flex min-h-9 items-center gap-1.5 rounded-lg border border-dashed border-brand bg-brand-soft px-3 text-xs font-medium text-brand-ink transition-colors hover:border-brand-deep hover:bg-brand-mist focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <PlugZap size={13} /> 创建默认端点
            </button>
            <button
              type="button"
              onClick={() => onOpenWeKnora()}
              className="inline-flex min-h-9 items-center gap-1 rounded-lg px-3 text-xs text-brand-ink transition-colors hover:bg-brand-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <ExternalLink size={12} /> 去原生页创建
            </button>
          </div>
        </div>
      )}

      {endpoints.map(endpoint => {
        const compliance = weknoraEndpointCompliance(endpoint)
        const server = weknoraManagedServerFor(endpoint, servers)
        const status = weknoraEndpointStatusView({
          ...endpoint,
          connection_status: server && ['ready', 'disabled'].includes(endpoint.connection_status)
            ? (server.enabled ? 'ready' : 'disabled')
            : endpoint.connection_status,
        })
        const connected = server !== null
        const blocked = connected ? null : weknoraEndpointConnectBlocked(endpoint, compliance)
        const acting = actingId === endpoint.id
        return (
          <article
            key={endpoint.id}
            data-testid={`weknora-endpoint-${endpoint.id}`}
            className="rounded-xl border border-[var(--color-border)] p-4 transition-colors hover:border-brand-line"
          >
            <div className="flex items-start gap-2">
              <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-brand-soft text-brand-ink"><Network size={16} /></div>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <p className="truncate text-xs font-semibold text-[var(--color-text-primary)]">{endpoint.name}</p>
                  <span className={`shrink-0 rounded px-1.5 py-0.5 text-[9px] ${STATUS_TONE_CLASS[status.tone]}`}>{status.label}</span>
                </div>
                <p className="mt-1 truncate text-[10px] text-[var(--color-text-tertiary)]">
                  知识库：{endpoint.knowledge_bases.length > 0
                    ? endpoint.knowledge_bases.map(kb => kb.name).join('、')
                    : '未限定'}
                </p>
              </div>
            </div>

            <div className="mt-2 flex flex-wrap gap-1">
              <span className="rounded bg-[var(--color-bg-base)] px-1.5 py-0.5 text-[9px] text-[var(--color-text-tertiary)]">{endpoint.tools.length} 工具</span>
              {compliance.compliant
                ? <span className="rounded bg-success-bg px-1.5 py-0.5 text-[9px] text-success">只读</span>
                : <span className="rounded bg-[var(--color-warning-bg)] px-1.5 py-0.5 text-[9px] text-[var(--color-warning)]">不合规</span>}
              {endpoint.missing && <span className="rounded bg-[var(--color-danger-bg)] px-1.5 py-0.5 text-[9px] text-[var(--color-danger)]">上游缺失</span>}
            </div>

            {!compliance.compliant && (
              <p className="mt-2 text-[10px] leading-4 text-[var(--color-warning)]">
                平台仅接入只读端点：{weknoraComplianceDetail(compliance)}，请在 WeKnora 原生页调整后刷新。
              </p>
            )}
            {endpoint.message && (
              <p className="mt-2 line-clamp-2 text-[10px] leading-4 text-[var(--color-text-tertiary)]">{endpoint.message}</p>
            )}

            {blocked && !connected && (
              <p className="mt-2 text-[10px] leading-4 text-[var(--color-text-tertiary)]">{blocked}</p>
            )}

            {!connected && !blocked && (
              <div className="mt-2 flex flex-wrap items-center justify-end gap-1">
                <button
                  type="button"
                  onClick={() => setTokenDialogFor(endpoint)}
                  disabled={actingId !== null}
                  className="inline-flex min-h-9 items-center gap-1 rounded-md bg-brand px-2.5 text-[11px] font-medium text-white transition-colors hover:bg-brand-deep disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <KeyRound size={12} /> 输入 Token 接入
                </button>
                <button
                  type="button"
                  onClick={() => setRotateFor(endpoint)}
                  disabled={actingId !== null}
                  className="inline-flex min-h-9 items-center gap-1 rounded-md px-2 text-[11px] text-brand-ink transition-colors hover:bg-brand-soft disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  轮换 Token
                </button>
              </div>
            )}

            {!connected && blocked && (
              <div className="mt-2 flex items-center justify-end">
                <button
                  type="button"
                  onClick={() => onOpenWeKnora()}
                  className="inline-flex min-h-9 items-center gap-1 rounded-md px-2 text-[11px] text-brand-ink transition-colors hover:bg-brand-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <ExternalLink size={12} /> 去原生页编辑
                </button>
              </div>
            )}

            {connected && server && (
              <>
                {endpoint.enabled && !endpoint.missing && compliance.compliant && (
                  endpoint.connection_status === 'authorization_required'
                  || endpoint.connection_status === 'needs_verification'
                ) && (
                  <div className="mt-2 flex flex-wrap items-center justify-end gap-1">
                    <span className="mr-auto text-[10px] leading-4 text-[var(--color-warning)]">可重新校验配置，或提供当前 Token 重新授权</span>
                    <button
                      type="button"
                      onClick={() => setTokenDialogFor(endpoint)}
                      disabled={acting}
                      className="inline-flex min-h-9 items-center gap-1 rounded-md px-2 text-[11px] text-brand-ink transition-colors hover:bg-brand-soft disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      <KeyRound size={12} /> 输入 Token
                    </button>
                    <button
                      type="button"
                      onClick={() => setRotateFor(endpoint)}
                      disabled={acting}
                      className="inline-flex min-h-9 items-center gap-1 rounded-md px-2 text-[11px] text-brand-ink transition-colors hover:bg-brand-soft disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      轮换 Token
                    </button>
                  </div>
                )}
                <div className="mt-2 flex items-center justify-between gap-2">
                  <div className="flex min-w-0 items-center gap-3">
                    <SettingSwitch
                      label="启用"
                      ariaLabel={`${server.enabled ? '停用' : '启用'} WeKnora 端点 ${endpoint.name}`}
                      checked={server.enabled}
                      busy={updatingServerSetting === `${server.id}:enabled`}
                      onToggle={() => onUpdateServerSetting(server, 'enabled', !server.enabled)}
                    />
                    <SettingSwitch
                      label="自动执行"
                      ariaLabel={`${server.require_confirmation ? '开启' : '关闭'} ${endpoint.name} 自动执行`}
                      checked={!server.require_confirmation}
                      busy={updatingServerSetting === `${server.id}:require_confirmation`}
                      onToggle={() => onUpdateServerSetting(server, 'require_confirmation', !server.require_confirmation)}
                    />
                  </div>
                  <div className="flex shrink-0 items-center gap-1">
                    <button
                      type="button"
                      onClick={() => void verify(endpoint)}
                      disabled={acting}
                      className="inline-flex min-h-9 items-center gap-1 rounded-md px-2 text-[11px] text-brand-ink transition-colors hover:bg-brand-soft disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      {acting ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />} 校验
                    </button>
                    <button
                      type="button"
                      onClick={() => setDisconnectFor(endpoint)}
                      aria-label={`断开 WeKnora 端点 ${endpoint.name}`}
                      className="flex h-9 w-9 items-center justify-center rounded-md text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      <Unplug size={12} />
                    </button>
                  </div>
                </div>
              </>
            )}
          </article>
        )
      })}

      {tokenDialogFor && (
        <WeKnoraTokenDialog
          endpoint={tokenDialogFor}
          acting={actingId === tokenDialogFor.id}
          onConnect={connect}
          onClose={() => setTokenDialogFor(null)}
        />
      )}

      <ConfirmDialog
        open={rotateFor !== null}
        title="轮换 Token 并接入"
        description={rotateFor
          ? `将在 WeKnora 侧为「${rotateFor.name}」生成新 Token 并接入平台。原有 Token 立即失效，其他正在使用旧 Token 的客户端将无法继续访问该端点。`
          : ''}
        confirmText="轮换并接入"
        variant="warning"
        loading={rotateFor !== null && actingId === rotateFor.id}
        onConfirm={() => {
          if (!rotateFor) return
          void connect(rotateFor, { rotate_token: true }).then(ok => { if (ok) setRotateFor(null) })
        }}
        onClose={() => setRotateFor(null)}
      />

      <ConfirmDialog
        open={disconnectFor !== null}
        title="断开 WeKnora 端点"
        description={disconnectFor
          ? `断开后超级助手不再调用「${disconnectFor.name}」的工具。仅移除平台侧接入，不会删除 WeKnora 侧的端点、Token 或知识库配置。`
          : ''}
        confirmText="断开"
        loading={disconnectFor !== null && actingId === disconnectFor.id}
        onConfirm={() => { if (disconnectFor) void disconnect(disconnectFor) }}
        onClose={() => setDisconnectFor(null)}
      />
    </section>
  )
}
