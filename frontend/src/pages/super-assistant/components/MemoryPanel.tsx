import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Brain, Check, ExternalLink, Loader2, Plus, RefreshCw, Search, Trash2 } from 'lucide-react'

import {
  superAssistantApi,
  type MemoryRecallItem,
  type MemorySettings,
  type MemorySource,
  type MemoryStatus,
} from '@/api/superAssistant'
import type { ModelConfig } from '@/types/ontology'
import { toast } from 'sonner'
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Button, buttonVariants } from '@/components/ui/Button'
import { useAuthStore } from '@/stores/authStore'
import {
  memoryStatusPresentation,
  memoryStatusSnapshot,
} from '@/components/assistant-widget/logic'
import { MemoryStatusNotice } from '@/components/assistant-widget/MemoryStatusNotice'
import { useDebouncedValue } from '@/utils/useDebouncedValue'
import { formatDateTime } from '@/utils/datetime'
import { errorText } from './assistantPanelUtils'
import { SettingSwitch } from './SettingSwitch'
import {
  FORGET_SOURCE_DESCRIPTION,
  canForgetMemorySource,
  canRetryMemorySource,
  hasMoreSources,
  memoryCapableModels,
  memoryWebUrl,
  memorySourceDraftValid,
  memorySourceRoleLabel,
  memorySourceStatusPresentation,
  type MemorySourceTone,
} from './memoryPanelLogic'

/** 状态轮询：仅面板打开时进行，每 5s 一拍、最多 12 拍（有界）；
 *  仍处理中可手动刷新（手动刷新会重置轮询预算）；关闭/卸载面板即清理 */
const STATUS_POLL_INTERVAL_MS = 5000
const MAX_STATUS_POLLS = 12
const SOURCE_PAGE_SIZE = 20
/** Radix Select 不接受空串 value：未指定模型用哨兵值映射 */
const NO_MODEL_VALUE = '__none__'

const SOURCE_TONE_CLASS: Record<MemorySourceTone, string> = {
  ok: 'bg-success-bg text-success',
  busy: 'bg-[var(--color-bg-hover)] text-[var(--color-text-secondary)]',
  warn: 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]',
  muted: 'bg-[var(--color-bg-base)] text-[var(--color-text-tertiary)]',
}

function SourceStatusTag({ status }: { status: string }) {
  const presentation = memorySourceStatusPresentation(status)
  return (
    <span
      data-testid={`memory-source-status-${status}`}
      className={`rounded px-1.5 py-0.5 text-[10px] ${SOURCE_TONE_CLASS[presentation.tone]}`}
    >
      {presentation.label}
    </span>
  )
}

/**
 * Hindsight 长期记忆面板（配置面板「记忆」tab，替换旧 MemoryTab）：
 * - 设置：启用开关 + 记忆模型（复用会话模型列表中 openai/compatible 的启用 LLM，
 *   保存由后端做能力校验，前端不冒充验证）；
 * - 状态：SSE 之外的兜底轮询（有界），pending/failed/最近同步时间可见；
 * - 来源：对话自动沉淀 + 手动记住/追加更正/按来源忘记（异步，绝不提示已忘记）/重试；
 * - 召回：Hindsight 检索命中展示。
 * 记忆用户级共享：删除会话不影响已沉淀记忆；旧 auto-accept/palace/distill 主 UI 已退役。
 */
export default function MemoryPanel({ active, models }: { active: boolean; models: ModelConfig[] }) {
  const isAdmin = useAuthStore(state => state.user?.role === 'admin')
  const [settings, setSettings] = useState<MemorySettings | null>(null)
  const [draftEnabled, setDraftEnabled] = useState(false)
  const [draftModelId, setDraftModelId] = useState<string | null>(null)
  const [settingsLoaded, setSettingsLoaded] = useState(false)
  const [savingSettings, setSavingSettings] = useState(false)

  const [status, setStatus] = useState<MemoryStatus | null>(null)
  const statusPollRoundRef = useRef(0)
  const [statusRefreshing, setStatusRefreshing] = useState(false)

  const [sources, setSources] = useState<MemorySource[]>([])
  const [totalSources, setTotalSources] = useState(0)
  const [sourcesLoading, setSourcesLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [sourcesError, setSourcesError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const debouncedQuery = useDebouncedValue(query, 400)
  const [busyAction, setBusyAction] = useState<string | null>(null)

  const [rememberOpen, setRememberOpen] = useState(false)
  const [correctionTarget, setCorrectionTarget] = useState<MemorySource | null>(null)
  const [draftContent, setDraftContent] = useState('')
  const [submittingSource, setSubmittingSource] = useState(false)
  const [forgetTarget, setForgetTarget] = useState<MemorySource | null>(null)

  const [recallQuery, setRecallQuery] = useState('')
  const [recallItems, setRecallItems] = useState<MemoryRecallItem[] | null>(null)
  /** 召回失败提示：响应 state 非 ready 时保留 message，不把空 items 当成功空结果 */
  const [recallError, setRecallError] = useState<string | null>(null)
  const [recallLoading, setRecallLoading] = useState(false)

  const capableModels = useMemo(() => memoryCapableModels(models), [models])
  const settingsDirty = settings !== null
    && (settings.enabled !== draftEnabled || settings.model_config_id !== draftModelId)

  useEffect(() => {
    let alive = true
    void (async () => {
      try {
        const [loadedSettings, loadedStatus] = await Promise.all([
          superAssistantApi.memorySettings(),
          superAssistantApi.memoryStatus(),
        ])
        if (!alive) return
        // 宽容解析（与会话纪要字段同规）：响应体形态不符时保持未加载态，
        // 不让记忆面板的异常负载击穿整个工作台渲染
        if (loadedSettings && typeof loadedSettings.enabled === 'boolean') {
          setSettings(loadedSettings)
          setDraftEnabled(loadedSettings.enabled)
          setDraftModelId(loadedSettings.model_config_id ?? null)
        }
        if (loadedStatus && typeof loadedStatus.state === 'string') setStatus(loadedStatus)
      } catch (error) {
        toast.error('记忆设置加载失败', { description: errorText(error) })
      } finally {
        if (alive) setSettingsLoaded(true)
      }
    })()
    return () => { alive = false }
  }, [])

  const refreshStatus = useCallback(async (manual = false) => {
    if (manual) setStatusRefreshing(true)
    try {
      const next = await superAssistantApi.memoryStatus()
      if (next && typeof next.state === 'string') setStatus(next)
      if (manual) statusPollRoundRef.current = 0
    } catch { /* 轮询失败静默：下一拍或手动刷新自愈 */ }
    finally {
      if (manual) setStatusRefreshing(false)
    }
  }, [])

  useEffect(() => {
    if (!active) return
    statusPollRoundRef.current = 0
    const timer = window.setInterval(() => {
      if (statusPollRoundRef.current >= MAX_STATUS_POLLS) return
      statusPollRoundRef.current += 1
      void refreshStatus()
    }, STATUS_POLL_INTERVAL_MS)
    return () => {
      window.clearInterval(timer)
      statusPollRoundRef.current = 0
    }
  }, [active, refreshStatus])

  const loadSources = useCallback(async (offset: number, searchQuery: string) => {
    if (offset === 0) setSourcesLoading(true)
    else setLoadingMore(true)
    try {
      const page = await superAssistantApi.memorySources({
        limit: SOURCE_PAGE_SIZE,
        offset,
        ...(searchQuery ? { query: searchQuery } : {}),
      })
      // 宽容解析：items/total 缺失时按空页处理，异常负载不击穿渲染
      const items = Array.isArray(page?.items) ? page.items : []
      setSources(current => (offset === 0 ? items : [...current, ...items]))
      setTotalSources(typeof page?.total === 'number' ? page.total : items.length)
      setSourcesError(null)
    } catch (error) {
      if (offset === 0) setSourcesError(errorText(error, '记忆来源加载失败'))
    } finally {
      setSourcesLoading(false)
      setLoadingMore(false)
    }
  }, [])

  useEffect(() => {
    setRecallItems(null)
    void loadSources(0, debouncedQuery.trim())
  }, [loadSources, debouncedQuery])

  const upsertSource = (next: MemorySource) => {
    setSources(current => {
      const index = current.findIndex(item => item.id === next.id)
      if (index === -1) return [next, ...current]
      const copy = [...current]
      copy[index] = next
      return copy
    })
  }

  const saveSettings = async () => {
    if (!settingsDirty || savingSettings) return
    setSavingSettings(true)
    try {
      const saved = await superAssistantApi.updateMemorySettings({
        enabled: draftEnabled,
        model_config_id: draftModelId,
      })
      setSettings(saved)
      toast.success('记忆设置已保存', { description: '模型能力由后端在保存时校验' })
      void refreshStatus(true)
    } catch (error) {
      toast.error('记忆设置保存失败', { description: errorText(error) })
    } finally {
      setSavingSettings(false)
    }
  }

  const openRemember = () => {
    setCorrectionTarget(null)
    setDraftContent('')
    setRememberOpen(true)
  }

  const openCorrection = (source: MemorySource) => {
    setCorrectionTarget(source)
    setDraftContent('')
    setRememberOpen(true)
  }

  const submitSource = async () => {
    if (!memorySourceDraftValid(draftContent) || submittingSource) return
    setSubmittingSource(true)
    try {
      const created = correctionTarget
        ? await superAssistantApi.createMemorySource({ content: draftContent.trim(), correction_of: correctionTarget.id })
        : await superAssistantApi.createMemorySource({ content: draftContent.trim() })
      upsertSource(created)
      toast.success(correctionTarget ? '更正已提交' : '已加入记忆来源', {
        description: correctionTarget
          ? '更正内容会作为新来源沉淀，并关联原来源'
          : '内容将随后台沉淀为长期记忆',
      })
      setRememberOpen(false)
      setCorrectionTarget(null)
      setDraftContent('')
    } catch (error) {
      toast.error(correctionTarget ? '更正提交失败' : '记住失败', { description: errorText(error) })
    } finally {
      setSubmittingSource(false)
    }
  }

  const forgetSource = async () => {
    const target = forgetTarget
    if (!target || busyAction) return
    setBusyAction(`${target.id}:forget`)
    try {
      const next = await superAssistantApi.deleteMemorySource(target.id)
      upsertSource(next)
      // 遗忘异步完成：这里只报告请求已受理，绝不宣称「已忘记」
      toast.success('已提交忘记请求', {
        description: '该来源产生的相关记忆将在后台一并清理，完成后状态变为「已遗忘」',
      })
    } catch (error) {
      toast.error('忘记请求失败', { description: errorText(error) })
    } finally {
      setBusyAction(null)
      setForgetTarget(null)
    }
  }

  const retrySource = async (source: MemorySource) => {
    if (busyAction) return
    setBusyAction(`${source.id}:retry`)
    try {
      const next = await superAssistantApi.retryMemorySource(source.id)
      upsertSource(next)
      toast.success('已重新提交沉淀')
    } catch (error) {
      toast.error('重试失败', { description: errorText(error) })
    } finally {
      setBusyAction(null)
    }
  }

  const runRecall = async () => {
    const trimmed = recallQuery.trim()
    if (!trimmed || recallLoading) return
    setRecallLoading(true)
    setRecallError(null)
    try {
      const result = await superAssistantApi.searchMemory(trimmed)
      setRecallItems(Array.isArray(result?.items) ? result.items : [])
      // 响应携带召回链路状态：非 ready 时保留失败信息，空 items 不是成功空结果
      if (result && typeof result.state === 'string' && result.state !== 'ready') {
        setRecallError(typeof result.message === 'string' && result.message
          ? result.message
          : `记忆召回暂不可用（${result.state}）`)
      }
    } catch (error) {
      toast.error('记忆召回失败', { description: errorText(error) })
    } finally {
      setRecallLoading(false)
    }
  }

  const statusPresentation = status ? memoryStatusPresentation(status.state) : null
  const consoleUrl = isAdmin ? memoryWebUrl(settings?.web_url) : null

  /** 一键免密进入控制台：先同步开空白标签保住用户手势（await 后再
      window.open 会被弹窗拦截），再领一次性票据导航到平台中继；中继在
      服务端登录控制面并把会话 cookie 透传给浏览器。失败时关闭空白页并
      如实提示——原生地址直开（控制台自带密钥登录）仍是可用兜底。 */
  const openConsole = useCallback(async () => {
    const popup = window.open('', '_blank')
    try {
      const { redirect_url } = await superAssistantApi.createMemoryConsoleEntry()
      const entryUrl = new URL(redirect_url, window.location.origin).toString()
      if (!popup) {
        // 弹窗被拦截：当前标签跳转，免密体验保持完整
        window.location.href = entryUrl
        return
      }
      popup.opener = null
      popup.location.href = entryUrl
    } catch (error) {
      popup?.close()
      toast.error('记忆控制台免密进入失败，可右键新标签打开后手工输入密钥', {
        description: errorText(error),
      })
    }
  }, [])
  const handleConsoleClick = useCallback((event: React.MouseEvent) => {
    // 中键/修饰键/右键保留 <a> 原生行为，落到控制台密钥登录页作手动兜底
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
    event.preventDefault()
    void openConsole()
  }, [openConsole])

  return (
    <div className="grid gap-3" data-testid="memory-panel">
      {/* 设置：启用 + 记忆模型（保存后由后端能力校验） */}
      <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-4" data-testid="memory-settings">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="text-xs font-medium text-[var(--color-text-primary)]">长期记忆（Hindsight）</p>
            <p className="mt-0.5 text-[10px] leading-4 text-[var(--color-text-tertiary)]">
              当前用户全部超级助手对话共享；记忆独立于会话，删除会话不会清除已沉淀内容
            </p>
          </div>
          {!settingsLoaded ? (
            <Loader2 size={16} className="shrink-0 animate-spin text-[var(--color-text-tertiary)]" />
          ) : (
            <SettingSwitch
              ariaLabel={draftEnabled ? '停用长期记忆' : '启用长期记忆'}
              checked={draftEnabled}
              busy={false}
              onToggle={() => setDraftEnabled(value => !value)}
            />
          )}
        </div>
        <div className="mt-3 flex items-center gap-2">
          <Select
            value={draftModelId ?? NO_MODEL_VALUE}
            onValueChange={value => setDraftModelId(value === NO_MODEL_VALUE ? null : value)}
          >
            <SelectTrigger aria-label="记忆模型" className="min-h-9 min-w-0 flex-1 px-2 text-xs">
              <SelectValue placeholder={capableModels.length ? '选择记忆模型' : '暂无可用模型'} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={NO_MODEL_VALUE}>未指定</SelectItem>
              {capableModels.map(model => (
                <SelectItem key={model.id} value={model.id}>
                  <span className="max-w-56 truncate">{model.name}</span>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <button
            type="button"
            onClick={() => void saveSettings()}
            disabled={!settingsDirty || savingSettings}
            data-testid="memory-settings-save"
            className="flex min-h-9 shrink-0 items-center gap-1 rounded-lg bg-brand px-3 text-xs font-medium text-white hover:bg-brand-deep disabled:opacity-50"
          >
            {savingSettings ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
            保存
          </button>
        </div>
        {settingsLoaded && capableModels.length === 0 && (
          <p className="mt-2 text-[10px] leading-4 text-[var(--color-text-tertiary)]">
            暂无 openai / compatible 提供方的启用 LLM，请先在「模型配置」准备模型
          </p>
        )}
        {isAdmin && (
          <div className="mt-3 border-t border-[var(--color-border)] pt-3">
            {consoleUrl ? (
              <a
                href={consoleUrl}
                target="_blank"
                rel="noopener noreferrer"
                className={buttonVariants({ variant: 'outline', size: 'sm' })}
                data-testid="memory-web-entry"
                onClick={handleConsoleClick}
              >
                <ExternalLink size={12} aria-hidden="true" />
                打开 Hindsight
              </a>
            ) : (
              <Button
                variant="outline"
                size="sm"
                disabled
                data-testid="memory-web-entry"
              >
                <ExternalLink size={12} aria-hidden="true" />
                打开 Hindsight
              </Button>
            )}
            <p className="mt-1.5 text-[10px] leading-4 text-[var(--color-text-tertiary)]">
              {consoleUrl
                ? '在新标签页免密打开原生管理控制台，仅管理员可见；中键/右键可直开并手工登录'
                : settingsLoaded
                  ? '尚未配置 Hindsight Web 地址，请在部署配置中设置 HINDSIGHT_WEB_URL'
                  : '正在加载 Hindsight Web 地址…'}
            </p>
          </div>
        )}
      </section>

      {/* 状态：SSE 之外的兜底轮询（有界），手动刷新始终可用 */}
      <section className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-4" data-testid="memory-status-card">
        <div className="flex items-center justify-between gap-2">
          {status ? (
            <MemoryStatusNotice status={memoryStatusSnapshot(status)} />
          ) : (
            <span className="text-xs text-[var(--color-text-tertiary)]">记忆状态加载中…</span>
          )}
          <button
            type="button"
            onClick={() => void refreshStatus(true)}
            disabled={statusRefreshing}
            aria-label="刷新记忆状态"
            title="刷新记忆状态"
            data-testid="memory-status-refresh"
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-secondary)] disabled:opacity-50"
          >
            {statusRefreshing ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
          </button>
        </div>
        {status && (
          <p className="mt-2 text-[10px] leading-4 text-[var(--color-text-tertiary)]">
            待处理 {status.pending_count} · 失败 {status.failed_count} · 最近同步 {formatDateTime(status.last_synced_at)}
          </p>
        )}
        {status && status.state === 'unconfigured' && (
          <p className="mt-1 text-[10px] leading-4 text-[var(--color-text-tertiary)]">
            {status.message || '选择记忆模型并保存后，对话将开始自动沉淀长期记忆'}
          </p>
        )}
        {status && statusPresentation?.tone === 'warn' && status.message && (
          <p className="mt-1 text-[10px] leading-4 text-[var(--color-warning)]" title={status.message}>
            {status.message}
          </p>
        )}
      </section>

      {/* 来源：自动沉淀 + 手动记住 */}
      <section className="grid gap-2">
        <div className="flex items-center justify-between gap-2">
          <h4 className="text-[11px] font-semibold text-[var(--color-text-primary)]">
            记忆来源
            <span className="ml-1 text-[10px] font-normal tabular-nums text-[var(--color-text-tertiary)]">共 {totalSources} 条</span>
          </h4>
          <button
            type="button"
            onClick={openRemember}
            aria-label="手动记住"
            data-testid="memory-remember-open"
            className="flex min-h-8 items-center gap-1 rounded-lg bg-brand px-2.5 text-xs font-medium text-white hover:bg-brand-deep"
          >
            <Plus size={12} /> 记住
          </button>
        </div>
        <input
          value={query}
          onChange={event => setQuery(event.target.value)}
          placeholder="筛选来源内容…"
          aria-label="筛选记忆来源"
          className="min-h-9 w-full rounded-lg border border-[var(--color-border)] bg-transparent px-3 text-xs outline-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        {sourcesLoading && (
          <div className="p-8 text-center text-xs text-[var(--color-text-tertiary)]"><Loader2 size={16} className="mx-auto animate-spin" /></div>
        )}
        {sourcesError && (
          <div className="rounded-xl border border-dashed border-[var(--color-border)] p-6 text-center text-xs text-[var(--color-text-tertiary)]">
            {sourcesError}
            <button type="button" onClick={() => void loadSources(0, debouncedQuery.trim())} className="ml-2 text-brand-ink hover:underline">重试</button>
          </div>
        )}
        {!sourcesLoading && !sourcesError && sources.length === 0 && (
          <div className="rounded-xl border border-dashed border-[var(--color-border)] p-8 text-center text-xs text-[var(--color-text-tertiary)]">
            <Brain size={20} className="mx-auto mb-2" />
            {debouncedQuery.trim() ? '没有匹配的来源' : '暂无记忆来源，对话完成后的已提交消息与助手完整回复会自动沉淀'}
          </div>
        )}
        {sources.map(source => (
          <article key={source.id} data-testid="memory-source-item" className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-3">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="rounded bg-brand-soft px-1.5 py-0.5 text-[10px] text-brand-ink">{memorySourceRoleLabel(source)}</span>
              <SourceStatusTag status={source.status} />
              {source.correction_of && (
                <span className="rounded bg-[var(--color-bg-base)] px-1.5 py-0.5 text-[10px] text-[var(--color-text-tertiary)]" title={`更正于来源 ${source.correction_of}`}>
                  更正
                </span>
              )}
              <span className="ml-auto text-[10px] text-[var(--color-text-tertiary)]" title={`发生时间 ${formatDateTime(source.occurred_at)}`}>
                {formatDateTime(source.occurred_at ?? source.created_at)}
              </span>
            </div>
            <p className="mt-2 whitespace-pre-wrap text-xs leading-5 text-[var(--color-text-primary)]">{source.content}</p>
            {source.error && (
              <p className="mt-1 text-[10px] leading-4 text-[var(--color-danger)]" title={source.error}>{source.error}</p>
            )}
            <div className="mt-2 flex flex-wrap justify-end gap-1">
              {canRetryMemorySource(source) && (
                <button
                  type="button"
                  onClick={() => void retrySource(source)}
                  disabled={busyAction !== null}
                  data-testid="memory-source-retry"
                  className="flex min-h-7 items-center gap-1 rounded-md border border-[var(--color-border)] px-2 text-[11px] text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] disabled:opacity-50"
                >
                  {busyAction === `${source.id}:retry` ? <Loader2 size={11} className="animate-spin" /> : <RefreshCw size={11} />}
                  重试
                </button>
              )}
              <button
                type="button"
                onClick={() => openCorrection(source)}
                data-testid="memory-source-correct"
                className="flex min-h-7 items-center gap-1 rounded-md border border-[var(--color-border)] px-2 text-[11px] text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)]"
              >
                更正
              </button>
              {canForgetMemorySource(source) && (
                <button
                  type="button"
                  onClick={() => setForgetTarget(source)}
                  aria-label="按来源忘记"
                  data-testid="memory-source-forget"
                  className="flex min-h-7 items-center gap-1 rounded-md px-2 text-[11px] text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)]"
                >
                  {busyAction === `${source.id}:forget` ? <Loader2 size={11} className="animate-spin" /> : <Trash2 size={11} />}
                  忘记
                </button>
              )}
            </div>
          </article>
        ))}
        {!sourcesLoading && hasMoreSources(sources.length, totalSources) && (
          <button
            type="button"
            onClick={() => void loadSources(sources.length, debouncedQuery.trim())}
            disabled={loadingMore}
            data-testid="memory-load-more"
            className="min-h-9 rounded-lg border border-[var(--color-border)] text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] disabled:opacity-50"
          >
            {loadingMore ? '加载中…' : `加载更多（还有 ${totalSources - sources.length} 条）`}
          </button>
        )}
      </section>

      {/* 召回：Hindsight 检索 */}
      <section className="grid gap-2">
        <h4 className="text-[11px] font-semibold text-[var(--color-text-primary)]">记忆召回</h4>
        <div className="flex gap-2">
          <input
            value={recallQuery}
            onChange={event => setRecallQuery(event.target.value)}
            onKeyDown={event => { if (event.key === 'Enter') void runRecall() }}
            placeholder="输入问题，查看会召回哪些记忆…"
            aria-label="记忆召回查询"
            className="min-h-9 min-w-0 flex-1 rounded-lg border border-[var(--color-border)] bg-transparent px-3 text-xs outline-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          <button
            type="button"
            onClick={() => void runRecall()}
            disabled={!recallQuery.trim() || recallLoading}
            aria-label="召回记忆"
            data-testid="memory-recall-run"
            className="flex min-h-9 shrink-0 items-center gap-1 rounded-lg border border-[var(--color-border)] px-3 text-xs text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-bg-hover)] disabled:opacity-50"
          >
            {recallLoading ? <Loader2 size={12} className="animate-spin" /> : <Search size={12} />}
            召回
          </button>
        </div>
        {recallItems !== null && recallItems.length === 0 && (
          recallError ? (
            <p role="status" className="rounded-lg border border-[var(--color-warning)] bg-[var(--color-warning-bg)] p-3 text-[11px] leading-5 text-[var(--color-warning)]">
              {recallError}
            </p>
          ) : (
            <p className="rounded-lg border border-dashed border-[var(--color-border)] p-4 text-center text-[11px] text-[var(--color-text-tertiary)]">
              没有命中的记忆
            </p>
          )
        )}
        {recallItems !== null && recallItems.length > 0 && recallError && (
          <p role="status" className="text-[10px] leading-4 text-[var(--color-warning)]" title={recallError}>{recallError}</p>
        )}
        {recallItems?.map(item => (
          <article key={item.id} data-testid="memory-recall-item" className="rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-3">
            <p className="whitespace-pre-wrap text-xs leading-5 text-[var(--color-text-primary)]">{item.text}</p>
            <p className="mt-1.5 text-[10px] text-[var(--color-text-tertiary)]">
              {item.type} · 来源 {item.source_ids.length} 条
              {item.occurred_start ? ` · ${formatDateTime(item.occurred_start)}` : ''}
            </p>
          </article>
        ))}
      </section>

      {/* 手动记住 / 追加更正共用弹窗 */}
      <Dialog open={rememberOpen} onOpenChange={value => { if (!value) { setRememberOpen(false); setCorrectionTarget(null) } }}>
        <DialogContent className="w-[min(92vw,28rem)]">
          <DialogHeader className="mb-0">
            <DialogTitle className="text-sm">{correctionTarget ? '追加更正' : '记住'}</DialogTitle>
            {correctionTarget && (
              <DialogDescription className="mt-0.5 text-[10px] leading-4">
                更正会作为新来源沉淀并关联原来源；原来源产生的记忆在被清理前继续生效
              </DialogDescription>
            )}
          </DialogHeader>
          <textarea
            value={draftContent}
            onChange={event => setDraftContent(event.target.value)}
            rows={4}
            aria-label={correctionTarget ? '更正内容' : '要记住的内容'}
            placeholder={correctionTarget ? '写出更正后的内容…' : '要记住的内容，例如：部署评审安排在每周四下午'}
            className="mt-3 w-full rounded-lg border border-[var(--color-border)] bg-transparent p-3 text-xs leading-5 outline-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          <button
            type="button"
            onClick={() => void submitSource()}
            disabled={!memorySourceDraftValid(draftContent) || submittingSource}
            data-testid="memory-submit-source"
            className="mt-4 flex min-h-9 w-full items-center justify-center gap-1.5 rounded-lg bg-brand text-xs font-medium text-white hover:bg-brand-deep disabled:opacity-50"
          >
            {submittingSource ? <Loader2 size={13} className="animate-spin" /> : <Check size={13} />}
            {correctionTarget ? '提交更正' : '保存'}
          </button>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={forgetTarget !== null}
        title="按来源忘记"
        description={FORGET_SOURCE_DESCRIPTION}
        confirmText="忘记"
        variant="danger"
        loading={busyAction?.endsWith(':forget') ?? false}
        onConfirm={() => { if (forgetTarget) void forgetSource() }}
        onClose={() => setForgetTarget(null)}
      />
    </div>
  )
}
