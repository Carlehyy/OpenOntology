/**
 * 智能助手 — 在本体授权边界内检索、推理与行动
 *
 * 页面只负责布局与视图选择；会话/run/队列状态在 useAgentConversationState，
 * 目录数据在 useAgentCatalog。每个会话的 run 独立订阅，切换会话不会中断
 * 原会话的后台执行；刷新/新 tab 从 conversation.activeRun 恢复。
 */
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle, ArrowLeftRight, BellRing, Bot, FileText, History, List, Loader2,
  Network, PlugZap, Scale, Send, Shield, Square, Workflow, X,
} from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { LoadingState } from '@/components/ui/LoadingState'
import { toast } from 'sonner'
import SessionHistoryPopover from '@/components/SessionHistoryPopover'
import { useAuthStore } from '@/stores/authStore'
import { agentApi } from '@/api/agent'
import { DynamicSentinelDrawer } from './DynamicSentinelDrawer'
import { BoundaryDrawer } from './BoundaryDrawer'
import { AgentChatTranscript } from './components/AgentChatTranscript'
import {
  AgentCallChainView,
  SplitHandle,
  downloadJson,
  safeExportFilename,
  useAssistantLayout,
  type ChatMsg,
} from './components/AgentWorkbenchPresentation'
import { OntologyNetworkView } from './components/OntologyNetworkView'
import { OntologyCardCarousel } from './components/OntologyCardCarousel'
import { McpConnectionDialog } from './components/McpConnectionDialog'
import { useAgentCatalog } from './hooks/useAgentCatalog'
import { useAgentConversationState } from './hooks/useAgentConversationState'

const InstanceKnowledgeGraph = lazy(() => import('./InstanceKnowledgeGraph'))
const DecisionSimulationView = lazy(() => import('./DecisionSimulationView'))

const selectArrow = "url(\"data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 24 24' fill='none' stroke='%23999' stroke-width='2'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E\")"

type WorkspaceView = 'ontology' | 'data' | 'decision' | 'trace'

export default function AgentWorkbenchPage() {
  const isAdmin = useAuthStore(state => state.user?.role === 'admin')
  const accountUsername = useAuthStore(state => state.user?.username || '')
  const navigate = useNavigate()
  const { containerRef, sizes, startResize } = useAssistantLayout()
  const catalog = useAgentCatalog()
  const [workspaceView, setWorkspaceView] = useState<WorkspaceView>('ontology')
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [sentinelDrawerOpen, setSentinelDrawerOpen] = useState(false)
  const [showHistory, setShowHistory] = useState(false)
  const [showJump, setShowJump] = useState(false)
  const [mcpDialogOpen, setMcpDialogOpen] = useState(false)
  const [input, setInput] = useState('')
  const [exportingConversationId, setExportingConversationId] = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  const conversation = useAgentConversationState({
    oid: catalog.oid,
    releaseId: catalog.releaseId,
    modelId: catalog.modelId,
    onDecisionIntent: () => setWorkspaceView('decision'),
    onOpenDecision: () => setWorkspaceView('decision'),
    onOpenData: () => setWorkspaceView('data'),
    onRestoreView: view => setWorkspaceView(view),
  })
  const {
    messages, busy, queueCount, backgroundRuns, attentionRuns, graphSignal, decisionRunId,
    decisionPending, conversationId, send, stopRun, retryRun, reconnectRun,
  } = conversation
  const { setGraphSignal } = conversation

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages])

  const myMessages = useMemo(() => messages.filter(message => message.role === 'user'), [messages])
  const jumpToMessage = useCallback((id: string) => {
    setShowJump(false)
    requestAnimationFrame(() => {
      document.getElementById(`agent-msg-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    })
  }, [])

  const exportConversation = useCallback(async (conversationIdToExport: string) => {
    if (!catalog.oid || exportingConversationId) return
    setExportingConversationId(conversationIdToExport)
    try {
      const data = await agentApi.exportConversation(catalog.oid, conversationIdToExport)
      downloadJson(data, safeExportFilename(data.conversation.title, conversationIdToExport))
      const legacyCount = data.summary.contentCompleteness.legacyTruncatedToolResultCount
      const notifyExport = legacyCount > 0 ? toast.warning : toast.success
      notifyExport('会话记录已导出', {
        description: legacyCount > 0
          ? `JSON 已包含全部消息；其中 ${legacyCount} 个旧工具结果在历史存储时已截断，文件内已标注。`
          : `已导出 ${data.summary.messageCount} 条消息和 ${data.summary.toolStepCount} 个工具步骤。`,
      })
    } catch (cause: any) {
      toast.error('会话导出失败', { description: cause?.detail || cause?.message || '请稍后重试。' })
    } finally {
      setExportingConversationId(null)
    }
  }, [catalog.oid, exportingConversationId])

  const openBackgroundRun = useCallback((cid: string) => {
    void conversation.loadConversation(cid)
  }, [conversation])

  const highlightCitations = useCallback((message: ChatMsg, citations: ChatMsg['citations']) => {
    setWorkspaceView('data')
    setGraphSignal({
      sequence: Date.now(),
      steps: message.steps,
      citations,
      intent: 'highlight',
    })
  }, [setGraphSignal])

  const clearHighlight = useCallback(() => {
    setGraphSignal({
      sequence: Date.now(),
      steps: [],
      citations: [],
      intent: 'clear-highlight',
    })
  }, [setGraphSignal])

  const handleResetChat = useCallback(() => {
    conversation.resetChat()
    setShowHistory(false)
    setShowJump(false)
  }, [conversation])

  const handleOpenConversation = useCallback((cid: string) => {
    setShowHistory(false)
    void conversation.loadConversation(cid)
  }, [conversation])

  if (catalog.ontologiesLoading) return <LoadingState message="加载配置..." />

  const panelClass = 'workspace-topology-surface min-h-0 min-w-0 overflow-hidden rounded-lg border border-[var(--color-border)] shadow-sm'
  const graphLoading = workspaceView === 'ontology' && !!catalog.oid
    && catalog.backendId === catalog.oid && catalog.syncStatus === 'loading'
  const graphError = workspaceView === 'ontology' && !!catalog.oid
    && catalog.backendId === catalog.oid && catalog.syncStatus === 'error' && !catalog.graphOntology

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-[var(--color-bg-base)]">
      <div
        ref={containerRef}
        className="scrollbar-none grid flex-1 min-h-0 overflow-x-auto overflow-y-hidden p-1"
        style={{ gridTemplateColumns: `minmax(560px, ${sizes[0]}fr) 4px minmax(420px, ${sizes[1]}fr)` }}
      >
        {/* 左卡：本体结构 / 数据推演图谱 */}
        <section data-testid="agent-ontology-panel" className={`${panelClass} col-start-1 row-start-1 flex flex-col`}>
          <div className="flex h-14 shrink-0 items-center border-b border-[var(--color-border)] bg-card px-4">
            <div className="flex w-full min-w-0 items-center justify-between gap-3">
              <div className="flex min-w-0 flex-1 items-center gap-2">
                <div className="flex h-8 w-8 items-center justify-center rounded-md bg-[var(--color-info-bg)] text-[var(--color-info)]">
                  {workspaceView === 'trace' ? <Workflow size={16} /> : workspaceView === 'decision' ? <Scale size={16} /> : <Network size={16} />}
                </div>
                <div className="min-w-0">
                  <h3 className="truncate text-sm font-semibold text-[var(--color-text-primary)]">
                    {workspaceView === 'ontology' ? '本体拓扑图' : workspaceView === 'data' ? '数据推演图谱' : workspaceView === 'decision' ? '决策推演' : 'Agent调用链'}
                  </h3>
                  <p className={`truncate text-[11px] ${workspaceView === 'ontology' && catalog.syncStatus === 'error' ? 'text-[var(--color-danger)]' : 'text-[var(--color-text-tertiary)]'}`}>
                    {workspaceView === 'ontology' && catalog.syncStatus === 'error'
                      ? (catalog.syncError || '网络图加载失败。')
                      : workspaceView === 'ontology'
                        ? `${catalog.selectedOntology?.name || '未选择本体'} · 只读展示对象类型与关系`
                        : workspaceView === 'data'
                          ? `${catalog.selectedOntology?.name || '未选择本体'} · 实例、路径与拟议变更联动`
                          : workspaceView === 'decision'
                            ? `${catalog.selectedOntology?.name || '未选择本体'} · 隔离快照、多视角与方案比较`
                            : `${catalog.selectedOntology?.name || '未选择本体'} · 当前会话工具调用可审计、可复盘`}
                  </p>
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <button
                  type="button"
                  onClick={() => setMcpDialogOpen(true)}
                  disabled={!catalog.oid || !catalog.caps || !catalog.caps.enabled}
                  aria-label="MCP 接入"
                  data-testid="agent-mcp-button"
                  className="group/tip relative flex h-8 w-8 items-center justify-center rounded-md border border-brand-line bg-brand-soft text-brand-ink transition-colors hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink disabled:cursor-not-allowed disabled:opacity-30"
                >
                  <PlugZap size={14} />
                  <span className="pointer-events-none absolute -bottom-8 left-1/2 -translate-x-1/2 whitespace-nowrap rounded-md border border-border bg-popover px-2 py-0.5 text-[11px] text-popover-foreground shadow-sm opacity-0 transition-opacity group-hover/tip:opacity-100">MCP 接入</span>
                </button>
                <select
                  value={catalog.oid}
                  onChange={event => catalog.selectOntology(event.target.value)}
                  aria-label="选择本体"
                  className="h-8 min-w-[180px] cursor-pointer appearance-none rounded-md border border-[var(--color-border)] bg-[var(--color-bg-base)] bg-no-repeat pl-3 pr-8 text-xs text-[var(--color-text-primary)] outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring"
                  style={{ backgroundImage: selectArrow, backgroundPosition: 'right 10px center' }}
                >
                  {catalog.releasedOntologyList.length === 0 && <option value="">无已发布本体</option>}
                  {catalog.releasedOntologyList.length > 0 && <option value="">请选择已发布本体</option>}
                  {catalog.releasedOntologyList.map((ontology: any) => (
                    <option key={ontology.id} value={ontology.id}>
                      {ontology.name} · {ontology.current_release_version || ontology.version}
                    </option>
                  ))}
                </select>
                <div className="flex items-center rounded-md border border-border bg-muted p-0.5" aria-label="切换工作台视图">
                  {([
                    { id: 'ontology', label: '本体拓扑图', icon: Network },
                    { id: 'data', label: '数据推演图谱', icon: ArrowLeftRight },
                    { id: 'decision', label: '决策推演', icon: Scale },
                    { id: 'trace', label: 'Agent调用链', icon: Workflow },
                  ] as const).map(item => (
                    <button
                      key={item.id}
                      type="button"
                      onClick={() => setWorkspaceView(item.id)}
                      disabled={!catalog.oid}
                      title={item.label}
                      aria-label={`切换到${item.label}`}
                      aria-pressed={workspaceView === item.id}
                      data-testid={item.id === 'data' ? 'workspace-view-toggle' : `workspace-view-${item.id}`}
                      className={`flex h-7 w-8 items-center justify-center rounded transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${workspaceView === item.id
                        ? 'bg-card text-brand-ink shadow-sm' : 'text-[var(--color-text-tertiary)] hover:bg-card hover:text-foreground'}`}
                    >
                      <item.icon size={13} />
                    </button>
                  ))}
                </div>
              </div>
            </div>
          </div>

          <div className="workspace-topology-surface relative min-h-0 flex-1 overflow-hidden">
            {workspaceView === 'decision' ? (
              <Suspense fallback={(
                <div className="flex h-full items-center justify-center gap-2 bg-muted text-xs text-muted-foreground">
                  <Loader2 size={14} className="animate-spin text-brand-ink" />正在加载决策推演工作台…
                </div>
              )}>
                <DecisionSimulationView
                  oid={catalog.oid}
                  releaseId={catalog.releaseId}
                  conversationId={conversationId}
                  activeRunId={decisionRunId}
                  running={decisionPending}
                />
              </Suspense>
            ) : workspaceView === 'trace' ? (
              <AgentCallChainView messages={messages} running={busy} />
            ) : workspaceView === 'data' ? (
              <Suspense fallback={(
                <div className="flex h-full items-center justify-center gap-2 bg-muted text-xs text-muted-foreground">
                  <Loader2 size={14} className="animate-spin text-brand-ink" />正在加载数据图谱工作台…
                </div>
              )}>
                <InstanceKnowledgeGraph
                  oid={catalog.oid}
                  releaseId={catalog.releaseId}
                  assistantSignal={graphSignal}
                  onAskAssistant={question => void send(question)}
                />
              </Suspense>
            ) : graphLoading ? (
              <div className="flex h-full flex-col items-center justify-center gap-3 bg-muted text-muted-foreground">
                <Loader2 size={22} className="animate-spin text-[var(--color-info)]" />
                <span className="text-xs">正在加载本体网络…</span>
              </div>
            ) : graphError ? (
              <div className="flex h-full items-center justify-center p-6">
                <div className="max-w-sm rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-4 py-3 text-sm text-[var(--color-danger)]">
                  <div className="mb-1 flex items-center gap-2 font-medium"><AlertTriangle size={15} />图谱加载失败</div>
                  <p className="text-xs leading-relaxed text-[var(--color-danger)]">{catalog.syncError || '请稍后刷新模型结构。'}</p>
                </div>
              </div>
            ) : !catalog.oid ? (
              <OntologyCardCarousel
                items={catalog.releasedOntologyList}
                onSelect={catalog.selectOntologyFromCard}
              />
            ) : (
              <OntologyNetworkView
                objectTypes={catalog.objectTypes}
                linkTypes={catalog.linkTypes}
                actions={catalog.actions}
                functions={catalog.functions}
                instancesCount={catalog.instancesCount}
                releaseId={catalog.releaseId}
                oid={catalog.oid}
              />
            )}
          </div>
        </section>

        <SplitHandle onPointerDown={startResize} />

        {/* 右卡：智能对话 */}
        <section data-testid="agent-chat-panel" className={`${panelClass} col-start-3 row-start-1 flex flex-col`}>
          <div className="flex h-14 shrink-0 items-center border-b border-[var(--color-border)] bg-card px-4">
            <div className="flex w-full min-w-0 items-center justify-between gap-2">
              <div className="flex min-w-0 flex-1 items-center gap-2">
                <div className="flex shrink-0 h-8 w-8 items-center justify-center rounded-md bg-brand-soft text-brand-ink">
                  <Bot size={18} />
                </div>
                <div className="min-w-0">
                  <h3 className="truncate text-sm font-semibold text-[var(--color-text-primary)]">智能对话</h3>
                  <p className="truncate text-[11px] text-[var(--color-text-tertiary)]">基于授权范围回答，并可生成行动提案</p>
                </div>
                {catalog.caps && !catalog.caps.enabled && (
                  <span className="inline-flex items-center gap-1 rounded-full bg-[var(--color-danger-bg)] px-2 py-0.5 text-[11px] font-medium text-[var(--color-danger)]">
                    <AlertTriangle size={11} />智能体已停用
                  </span>
                )}
              </div>
              <div className="flex shrink-0 items-center gap-1">
                <select
                  value={catalog.modelId}
                  onChange={event => catalog.setModelId(event.target.value)}
                  aria-label="选择对话模型"
                  disabled={!catalog.oid}
                  className="h-8 w-44 cursor-pointer appearance-none rounded-md border border-[var(--color-border)] bg-[var(--color-bg-base)] bg-no-repeat pl-2 pr-7 text-xs text-[var(--color-text-primary)] outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-30 disabled:cursor-not-allowed"
                  style={{ backgroundImage: selectArrow, backgroundPosition: 'right 6px center', backgroundSize: '10px' }}
                >
                  {catalog.llmModels.map((model: any) => <option key={model.id} value={model.id}>{model.name}</option>)}
                </select>
                <button
                  type="button"
                  onClick={() => setSentinelDrawerOpen(true)}
                  disabled={!catalog.oid || !catalog.releaseId}
                  aria-label="管理动态哨兵"
                  data-testid="dynamic-sentinel-button"
                  className="group/tip relative flex h-8 w-8 items-center justify-center rounded-md border border-brand-line bg-brand-soft text-brand-ink transition-colors hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink disabled:cursor-not-allowed disabled:opacity-30"
                >
                  <BellRing size={14} />
                  <span className="pointer-events-none absolute -bottom-8 left-1/2 -translate-x-1/2 whitespace-nowrap rounded-md border border-border bg-popover px-2 py-0.5 text-[11px] text-popover-foreground shadow-sm opacity-0 transition-opacity group-hover/tip:opacity-100">动态哨兵</span>
                </button>
                {isAdmin && (
                  <button onClick={() => setDrawerOpen(true)} disabled={!catalog.oid} aria-label="授权边界配置"
                    className="group/tip relative flex h-8 w-8 items-center justify-center rounded-md border border-brand-line bg-brand-soft text-brand-ink transition-colors hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink disabled:opacity-30 disabled:cursor-not-allowed">
                    <Shield size={14} />
                    <span className="pointer-events-none absolute -bottom-8 left-1/2 -translate-x-1/2 whitespace-nowrap rounded-md border border-border bg-popover px-2 py-0.5 text-[11px] text-popover-foreground shadow-sm opacity-0 transition-opacity group-hover/tip:opacity-100">授权边界配置</span>
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => navigate(catalog.oid
                    ? `/agent/reports/new?ontologyId=${encodeURIComponent(catalog.oid)}${conversationId ? `&conversationId=${encodeURIComponent(conversationId)}` : ''}`
                    : '/agent/reports')}
                  disabled={!catalog.oid}
                  aria-label={catalog.oid ? '生成分析报告' : '分析报告'}
                  className="group/tip relative flex h-8 w-8 items-center justify-center rounded-md border border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-[var(--color-info-bg)] text-[var(--color-info)] transition-colors hover:border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] hover:bg-[var(--color-info-bg)] hover:text-[var(--color-info)] disabled:cursor-not-allowed disabled:opacity-30"
                  title={catalog.oid ? '基于当前本体和会话生成可编辑分析报告模板' : '打开分析报告工作台'}
                >
                  <FileText size={14} />
                  <span className="pointer-events-none absolute -bottom-8 left-1/2 -translate-x-1/2 whitespace-nowrap rounded-md border border-border bg-popover px-2 py-0.5 text-[11px] text-popover-foreground shadow-sm opacity-0 transition-opacity group-hover/tip:opacity-100">{catalog.oid ? '生成分析报告' : '分析报告'}</span>
                </button>
                <div className="relative">
                  <button
                    type="button"
                    onClick={() => setShowHistory(value => !value)}
                    disabled={!catalog.oid}
                    aria-label="查看历史会话"
                    aria-expanded={showHistory}
                    data-testid="agent-session-history-button"
                    className={`group/tip relative flex h-8 w-8 items-center justify-center rounded-md border transition-colors active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${showHistory
                      ? 'border-brand bg-brand-mist text-brand-ink'
                      : 'border-brand-line bg-brand-soft text-brand-ink hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink'}`}
                  >
                    <History size={14} />
                    <span className="pointer-events-none absolute -bottom-8 left-1/2 -translate-x-1/2 whitespace-nowrap rounded-md border border-border bg-popover px-2 py-0.5 text-[11px] text-popover-foreground shadow-sm opacity-0 transition-opacity group-hover/tip:opacity-100">查看历史会话</span>
                    {backgroundRuns.length > 0 && (
                      <span className="absolute -right-1 -top-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-[var(--color-info)] px-1 text-[9px] font-semibold text-[var(--color-text-inverse)]">
                        {backgroundRuns.length}
                      </span>
                    )}
                  </button>
                  <SessionHistoryPopover
                    open={showHistory}
                    items={conversation.conversations}
                    currentId={conversationId}
                    onClose={() => setShowHistory(false)}
                    onCreate={handleResetChat}
                    onSelect={handleOpenConversation}
                    onExport={exportConversation}
                    onDelete={conversation.deleteConversation}
                    exportingId={exportingConversationId}
                    renderItemIcon={() => <Bot size={16} />}
                    emptyDescription="开始对话后，可随时回到之前的查询、分析与行动提案。"
                  />
                </div>
              </div>
            </div>
          </div>

          <div data-testid="agent-chat-region" className="workspace-topology-surface scrollbar-none flex-1 overflow-auto px-4 py-4">
            <AgentChatTranscript
              oid={catalog.oid}
              conversationId={conversationId}
              messages={messages}
              busy={busy}
              loadingMessages={conversation.loadingMessages}
              backgroundRuns={backgroundRuns}
              attentionRuns={attentionRuns}
              suggestions={catalog.suggested}
              onSend={send}
              onOpenBackgroundRun={openBackgroundRun}
              onRetryRun={retryRun}
              onReconnectRun={reconnectRun}
              onHighlightCitations={highlightCitations}
              onClearHighlight={clearHighlight}
            />
            <div ref={bottomRef} />
          </div>

          {/* pt/pb 取 2.5 使输入栏高度精确为 67px：顶部分割线距视口底部 72px
              （历史取值沿用，侧边栏底栏移除后保持输入栏高度不变）。 */}
          <div data-testid="agent-input-bar" className="border-t border-[var(--color-border)] bg-card px-4 pb-2.5 pt-2.5">
            <div className="relative flex items-center gap-2 rounded-lg border border-[var(--color-border)] bg-card py-1.5 pl-3 pr-1.5 transition-all focus-within:ring-2 focus-within:ring-ring">
              <input
                placeholder={catalog.oid ? (busy ? '可继续输入，回车进入追问队列…' : '问业务问题，或让它帮你预演一个操作…') : '请先选择一个本体'}
                value={input}
                onChange={event => setInput(event.target.value)}
                onKeyDown={event => {
                  // 平台标准提交语义（同超级助手）：输入法组合期/带修饰键的 Enter 不发送
                  if (event.key !== 'Enter' || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey
                    || event.nativeEvent.isComposing) return
                  event.preventDefault()
                  if (input.trim() && catalog.oid) {
                    void send(input)
                    setInput('')
                  }
                }}
                disabled={!catalog.oid}
                aria-label="本体助手消息"
                data-testid="agent-composer"
                className="min-w-0 flex-1 bg-transparent text-sm text-[var(--color-text-primary)] outline-none placeholder:text-[var(--color-text-tertiary)] disabled:opacity-50"
              />
              {busy ? (
                <button
                  type="button"
                  onClick={() => stopRun()}
                  aria-label="停止生成"
                  data-testid="agent-stop-button"
                  className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-viz-rose text-[var(--color-text-inverse)] transition-all duration-200 hover:bg-viz-rose"
                >
                  <Square size={13} fill="currentColor" />
                </button>
              ) : (
                <button onClick={() => { void send(input); setInput('') }} disabled={!input.trim() || !catalog.oid}
                  aria-label="发送消息"
                  data-testid="agent-send-button"
                  className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-brand-deep text-[var(--color-text-inverse)] transition-all duration-200 hover:bg-brand disabled:cursor-not-allowed disabled:opacity-25">
                  <Send size={14} />
                </button>
              )}
              {queueCount > 0 && (
                <button
                  type="button"
                  onClick={conversation.clearQueue}
                  title={`追问队列中有 ${queueCount} 条，点击清空`}
                  aria-label="清空追问队列"
                  data-testid="agent-queue-clear"
                  className="flex h-6 shrink-0 items-center gap-1 rounded-full border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-2 text-[11px] font-medium text-[var(--color-warning)] transition-colors hover:border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] hover:bg-[var(--color-warning-bg)]"
                >
                  排队 {queueCount}
                  <X size={11} />
                </button>
              )}
              <button
                type="button"
                onClick={() => setShowJump(value => !value)}
                disabled={myMessages.length === 0}
                title="我发送的消息 · 快速跳转"
                className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-md border transition-colors disabled:cursor-not-allowed disabled:opacity-30 ${showJump
                  ? 'border-brand-line bg-brand-soft text-brand-ink'
                  : 'border-[var(--color-border)] text-[var(--color-text-tertiary)] hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-secondary)]'}`}>
                <List size={15} />
              </button>

              {showJump && (
                <>
                  <div className="fixed inset-0 z-20" onClick={() => setShowJump(false)} />
                  <div className="absolute bottom-full right-0 z-30 mb-2 w-72 overflow-hidden rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-elevated)] shadow-lg">
                    <div className="flex items-center justify-between border-b border-[var(--color-border)] px-3 py-2">
                      <span className="text-[11px] font-medium text-[var(--color-text-secondary)]">我发送的消息</span>
                      <span className="text-[10px] text-[var(--color-text-tertiary)]">点击跳转 · 共 {myMessages.length} 条</span>
                    </div>
                    <div className="scrollbar-thin max-h-64 overflow-auto py-1">
                      {myMessages.length === 0 ? (
                        <div className="px-3 py-4 text-center text-xs text-[var(--color-text-tertiary)]">当前会话暂无发送记录</div>
                      ) : (
                        [...myMessages].reverse().map((message, index) => (
                          <button
                            key={message.id}
                            onClick={() => jumpToMessage(message.id)}
                            className="flex w-full items-start gap-2 px-3 py-1.5 text-left transition-colors hover:bg-[var(--color-bg-hover)]"
                          >
                            <span className="mt-0.5 shrink-0 font-mono text-[10px] text-[var(--color-text-tertiary)]">#{myMessages.length - index}</span>
                            <span className="min-w-0 flex-1 truncate text-xs text-[var(--color-text-secondary)]">{message.content}</span>
                          </button>
                        ))
                      )}
                    </div>
                  </div>
                </>
              )}
            </div>
          </div>
        </section>
      </div>

      <BoundaryDrawer oid={catalog.oid} open={drawerOpen} onClose={() => setDrawerOpen(false)} />
      <McpConnectionDialog
        open={mcpDialogOpen}
        onClose={() => setMcpDialogOpen(false)}
        oid={catalog.oid}
        ontologyName={catalog.selectedOntology?.name || '当前本体'}
        releaseVersion={catalog.selectedOntology?.current_release_version || catalog.selectedOntology?.version || ''}
        username={accountUsername}
        connectionConfirmed={!!catalog.oid && !!catalog.caps?.enabled}
      />
      <DynamicSentinelDrawer
        oid={catalog.oid}
        releaseId={catalog.releaseId}
        open={sentinelDrawerOpen}
        onClose={() => setSentinelDrawerOpen(false)}
        objectTypes={catalog.dynamicObjectTypes}
        linkTypes={catalog.dynamicLinkTypes}
        actions={catalog.dynamicActions}
      />
    </div>
  )
}
