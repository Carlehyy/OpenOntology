/**
 * 场景助手（对话式建模）— 布局对齐本体网络页（MYW-64）：左卡片为三维场景
 * 可视化，右卡片为场景助手对话框，中间可拖拽分栏；本路由在 Layout 中为
 * edge-to-edge 全高页，卡片间距与本体网络页保持一致（容器 p-1 + 卡片圆角边框）。
 *
 * 版本管理收进左卡白色顶栏右侧的「版本管理」按钮：点开浮层回看历史版本
 * （版本号 / 来源 / 备注 / 时间），选中任意版本即在画布预览，浮层内保留
 * 「回滚为当前」入口。目标场景与对话模型作为发送上下文放在右侧对话框的
 * 工具条上；历史会话仍在对话框顶栏。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Link } from 'react-router-dom'
import { AlertTriangle, ArrowLeft, Axis3d, Check, History, Send, Sparkles, Square, X } from 'lucide-react'
import { scenesApi } from '@/api/scenes'
import { createConversation, listConversations, listMessages, streamSceneChat } from '@/api/sceneAssistant'
import type { ConversationMessage, ConversationSummary, SceneSseEvent } from '@/types/sceneAssistant'
import type { SceneDefinition, SceneSummary, SceneVersionMeta } from '@/types/scene'
import { modelApi } from '@/api/ontologies'
import { Button } from '@/components/ui/Button'
import { LoadingState } from '@/components/ui/LoadingState'
import { ConfirmModal } from '@/components/ui/Modal'
import { toast } from 'sonner'
import SessionHistoryPopover, { type SessionHistoryItem } from '@/components/SessionHistoryPopover'
import { SplitHandle, useSplitLayout } from '@/hooks/useSplitLayout'
import TargetSceneSelector from './TargetSceneSelector'
import { SceneCanvas } from '@/lib/scene3d/SceneCanvas'

type TimelineItem =
  | { kind: 'user'; id: string; content: string }
  | { kind: 'assistant'; id: string; content: string }
  | { kind: 'system'; id: string; text: string; sceneId?: string; versionNo?: number }
  | { kind: 'error'; id: string; message: string; issues?: { path: string; message: string }[] }

let seq = 0
const nextId = () => 'tl-' + Date.now() + '-' + (seq++)
const NEW_SCENE = '__new__'

/** 与本体网络页两卡一致的卡片外观（内边距由分栏容器 p-1 统一提供）。 */
const panelClass = 'min-h-0 min-w-0 overflow-hidden rounded-lg border border-[var(--color-border)] shadow-[0_1px_2px_rgba(15,23,42,0.05),0_12px_32px_-16px_rgba(15,23,42,0.18)]'

function versionSourceLabel(source: string): string {
  return source === 'assistant' ? '助手' : source === 'clone' ? '克隆' : '手动'
}

function formatVersionTime(value: string | null): string {
  if (!value) return '时间未知'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '时间未知'
  return date.toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  })
}

function messagesToTimeline(messages: ConversationMessage[]): TimelineItem[] {
  const items: TimelineItem[] = []
  for (const message of messages) {
    if (message.role === 'user') {
      items.push({ kind: 'user', id: message.id, content: message.content })
    } else if (message.status === 'error') {
      items.push({ kind: 'error', id: message.id, message: message.content })
    } else if (message.version_no != null) {
      items.push({
        kind: 'system', id: message.id,
        text: '已应用 v' + message.version_no + ' · ' + message.content,
        versionNo: message.version_no,
      })
    } else {
      items.push({ kind: 'assistant', id: message.id, content: message.content })
    }
  }
  return items
}

export default function SceneModelingPage() {
  const queryClient = useQueryClient()
  const { containerRef, sizes, startResize } = useSplitLayout([72, 28])

  const draftsQuery = useQuery({
    queryKey: ['scenes', 'drafts-for-steward'],
    queryFn: () => scenesApi.list({ status: 'draft', page_size: 200 }),
  })
  const modelsQuery = useQuery({
    queryKey: ['models'],
    queryFn: () => modelApi.list(),
  })
  const conversationsQuery = useQuery({
    queryKey: ['scene-conversations'],
    queryFn: () => listConversations({ page_size: 50 }),
  })
  const llmModels = (modelsQuery.data ?? []).filter(
    model => model.config_type === 'llm' || !model.config_type)

  const [targetSceneId, setTargetSceneId] = useState<string>(NEW_SCENE)
  const [modelId, setModelId] = useState('')
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [timeline, setTimeline] = useState<TimelineItem[]>([])
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [showHistory, setShowHistory] = useState(false)
  const [showVersions, setShowVersions] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  const conversations: ConversationSummary[] = conversationsQuery.data?.items ?? []
  const historyItems: SessionHistoryItem[] = useMemo(
    () => conversations.map(conversation => ({
      id: conversation.id,
      title: conversation.title || '未命名会话',
      updatedAt: conversation.updated_at ?? conversation.created_at ?? '',
    })),
    [conversations],
  )

  // 左卡：绑定场景的版本与定义
  const boundSceneId =
    targetSceneId !== NEW_SCENE ? targetSceneId : liveBoundSceneId(timeline)
  const versionsQuery = useQuery({
    queryKey: ['scenes', boundSceneId, 'versions'],
    queryFn: () => scenesApi.versions(boundSceneId ?? ''),
    enabled: !!boundSceneId,
  })
  const versionList = versionsQuery.data?.items ?? []
  const [selectedVersionNo, setSelectedVersionNo] = useState<number | null>(null)
  useEffect(() => {
    if (versionList.length > 0) {
      setSelectedVersionNo(current =>
        current && versionList.some(v => v.version_no === current)
          ? current
          : versionList[0].version_no)
    }
  }, [versionList])
  const versionQuery = useQuery({
    queryKey: ['scenes', boundSceneId, 'version', selectedVersionNo],
    queryFn: () => scenesApi.version(boundSceneId ?? '', selectedVersionNo ?? 0),
    enabled: !!boundSceneId && selectedVersionNo != null,
  })
  const definition = (versionQuery.data?.definition ?? null) as SceneDefinition | null

  // 手动回滚：将当前查看的版本定义整体复制冻结为一个新草稿版本（历史不动、发布指针不受影响）。
  // 冻结成功即选中该新版本；后续 SSE scene_updated 仍会自动跟随最新版。
  const [rollbackOpen, setRollbackOpen] = useState(false)
  const nextVersionNo = versionList.reduce((max, v) => Math.max(max, v.version_no), 0) + 1
  const rollbackMutation = useMutation({
    mutationFn: () => scenesApi.saveDefinition(
      boundSceneId ?? '',
      definition as SceneDefinition,
      { note: '回滚自 v' + selectedVersionNo, source: 'manual' }),
    onSuccess: result => {
      void queryClient.invalidateQueries({ queryKey: ['scenes'] })
      setSelectedVersionNo(result.version.version_no)
      setRollbackOpen(false)
      toast.success('已回滚并生成新版本 v' + result.version.version_no)
    },
    onError: () => toast.error('回滚失败，请稍后重试'),
  })

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [timeline])

  const draftScenes: SceneSummary[] = useMemo(
    () => draftsQuery.data?.items ?? [], [draftsQuery.data])
  const boundSceneName = draftScenes.find(scene => scene.id === boundSceneId)?.name

  // 左卡顶栏副标题：绑定状态 + 版本概览
  const canvasSubtitle = !boundSceneId
    ? '从零新建 · 助手生成的白模将在此实时渲染'
    : [
        boundSceneName ?? '已绑定场景',
        versionList.length > 0
          ? `共 ${versionList.length} 个版本${selectedVersionNo != null ? ` · 预览 v${selectedVersionNo}` : ''}`
          : '暂无版本',
      ].join(' · ')

  function resetChat() {
    setConversationId(null)
    setTimeline([])
    setShowHistory(false)
  }

  async function loadConversation(conversationIdToLoad: string) {
    const conversation = conversations.find(item => item.id === conversationIdToLoad)
    const result = await listMessages(conversationIdToLoad)
    setConversationId(conversationIdToLoad)
    setTargetSceneId(conversation?.scene_id ?? NEW_SCENE)
    setTimeline(messagesToTimeline(result.items))
    setShowHistory(false)
  }

  async function ensureConversation(): Promise<string> {
    if (conversationId) return conversationId
    const created = await createConversation({
      scene_id: targetSceneId === NEW_SCENE ? null : targetSceneId,
      title: input.slice(0, 50),
      model_config_id: modelId || null,
    })
    setConversationId(created.id)
    return created.id
  }

  function applyEvent(event: SceneSseEvent) {
    if (event.event === 'text') {
      setTimeline(list => [...list, {
        kind: 'assistant', id: nextId(), content: event.data.content,
      }])
    } else if (event.event === 'scene_updated') {
      const data = event.data
      if (targetSceneId === NEW_SCENE) setTargetSceneId(data.scene_id)
      setTimeline(list => [...list, {
        kind: 'system', id: nextId(),
        text: '已应用 v' + data.version_no + ' · ' + data.note,
        sceneId: data.scene_id, versionNo: data.version_no,
      }])
      void queryClient.invalidateQueries({ queryKey: ['scenes'] })
      setSelectedVersionNo(data.version_no)
    } else if (event.event === 'error') {
      setTimeline(list => [...list, {
        kind: 'error', id: nextId(),
        message: event.data.message, issues: event.data.issues,
      }])
    }
  }

  async function send() {
    const content = input.trim()
    if (!content || streaming) return
    setStreaming(true)
    setInput('')
    setTimeline(list => [...list, { kind: 'user', id: nextId(), content }])
    const controller = new AbortController()
    abortRef.current = controller
    try {
      const conversationForChat = await ensureConversation()
      await streamSceneChat(
        { conversationId: conversationForChat, content, modelId: modelId || null },
        applyEvent,
        controller.signal,
      )
    } catch (error) {
      if ((error as Error).name !== 'AbortError') {
        toast.error(errorMessageText(error))
      }
    } finally {
      abortRef.current = null
      setStreaming(false)
      // 标题/更新时间可能变化：刷新会话历史
      void queryClient.invalidateQueries({ queryKey: ['scene-conversations'] })
    }
  }

  return (
    <div className="relative flex h-full min-h-[560px] overflow-hidden bg-[var(--color-bg-base)]">
      <div
        ref={containerRef}
        className="scrollbar-none grid min-h-0 flex-1 overflow-x-auto overflow-y-hidden p-1"
        style={{ gridTemplateColumns: `minmax(560px, ${sizes[0]}fr) 4px minmax(320px, ${sizes[1]}fr)` }}
      >
        {/* 左卡片：三维场景可视化 */}
        <section className={`${panelClass} flex flex-col`} aria-label="三维场景可视化" data-testid="scene-canvas-card">
          {/* 顶栏白色框：返回 / 标题信息 + 右侧「版本管理」按钮 */}
          <header className="relative flex h-14 shrink-0 items-center justify-between gap-2 border-b border-[var(--color-border)] bg-card px-4">
            <div className="flex min-w-0 items-center gap-2">
              <Link
                to="/scenes"
                aria-label="返回三维场景列表"
                title="返回三维场景列表"
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-[var(--color-text-tertiary)] transition-colors hover:bg-muted hover:text-muted-foreground"
              >
                <ArrowLeft size={16} />
              </Link>
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-brand-soft text-brand-ink">
                <Axis3d size={18} />
              </div>
              <div className="min-w-0">
                <h3 className="truncate text-sm font-semibold text-[var(--color-text-primary)]">三维场景可视化</h3>
                <p className="truncate text-[11px] text-[var(--color-text-tertiary)]" data-testid="scene-canvas-subtitle">
                  {canvasSubtitle}
                </p>
              </div>
            </div>
            <div className="relative shrink-0">
              <button
                type="button"
                onClick={() => setShowVersions(open => !open)}
                aria-expanded={showVersions}
                aria-haspopup="dialog"
                data-testid="version-history-button"
                title="查看历史版本"
                className={['inline-flex h-8 items-center gap-1.5 rounded-md px-2.5 text-xs font-medium transition-colors',
                  showVersions
                    ? 'bg-accent text-[var(--color-text-inverse)] shadow-sm hover:bg-accent'
                    : 'border border-[var(--color-border)] bg-card text-muted-foreground hover:bg-muted'].join(' ')}
              >
                <History size={13} /> 版本管理
              </button>
              {showVersions && (
                <>
                  <div className="fixed inset-0 z-20" onClick={() => setShowVersions(false)} aria-hidden="true" />
                  <section
                    role="dialog"
                    aria-label="版本管理"
                    data-testid="version-panel"
                    className="absolute right-0 top-full z-30 mt-2 w-[min(340px,calc(100vw-32px))] overflow-hidden rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-elevated)] shadow-[0_18px_52px_rgba(15,23,42,0.16)] animate-slide-up"
                  >
                    <header className="flex items-center justify-between gap-2 border-b border-[var(--color-border)] px-3 py-2">
                      <div className="min-w-0">
                        <p className="text-xs font-semibold text-foreground">版本历史</p>
                        <p className="truncate text-[10.5px] text-[var(--color-text-tertiary)]">选择任一版本即可在画布预览</p>
                      </div>
                      <button
                        type="button"
                        onClick={() => setShowVersions(false)}
                        aria-label="关闭版本历史"
                        className="flex h-6 w-6 shrink-0 items-center justify-center rounded text-[var(--color-text-tertiary)] hover:bg-muted"
                      >
                        <X size={13} />
                      </button>
                    </header>
                    <div className="scrollbar-thin max-h-72 overflow-y-auto p-1.5">
                      {!boundSceneId ? (
                        <p className="px-2 py-4 text-center text-[11px] leading-5 text-[var(--color-text-tertiary)]">
                          尚未绑定场景：助手生成首个版本后，即可在此回看。
                        </p>
                      ) : versionsQuery.isLoading ? (
                        <LoadingState className="py-6" />
                      ) : versionList.length === 0 ? (
                        <p className="px-2 py-4 text-center text-[11px] leading-5 text-[var(--color-text-tertiary)]">暂无版本</p>
                      ) : versionList.map(version => (
                        <VersionRow
                          key={version.id}
                          version={version}
                          selected={selectedVersionNo === version.version_no}
                          onSelect={() => setSelectedVersionNo(version.version_no)}
                        />
                      ))}
                    </div>
                    {!!boundSceneId && versionList.length >= 1 && definition && (
                      <footer className="border-t border-[var(--color-border)] px-3 py-2">
                        <Button
                          variant="outline"
                          className="h-7 w-full justify-center px-2.5 text-xs"
                          title="将该版本定义整体保存为一个新草稿版本（当前最新版保留）"
                          onClick={() => setRollbackOpen(true)}
                        >
                          回滚为当前
                        </Button>
                      </footer>
                    )}
                  </section>
                </>
              )}
            </div>
          </header>

          <div className="relative min-h-0 flex-1 overflow-hidden">
            {definition
              ? <SceneCanvas definition={definition} className="absolute inset-0" />
              : (
                <div className="flex h-full items-center justify-center p-6">
                  <div className="max-w-sm rounded-lg border border-dashed border-border bg-card px-5 py-6 text-center">
                    <Axis3d size={22} className="mx-auto mb-2 text-[var(--color-text-tertiary)]" />
                    <p className="text-sm font-medium text-foreground">
                      {boundSceneId ? '该场景还没有版本定义' : '尚未绑定场景'}
                    </p>
                    <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                      {boundSceneId
                        ? '在右侧描述需求，助手将生成第一个版本'
                        : '在右侧选择草稿场景或从零新建，助手生成的白模将实时呈现在这里'}
                    </p>
                  </div>
                </div>
              )}
          </div>
        </section>

        <SplitHandle onPointerDown={startResize} label="调整三维画布与对话区宽度" />

        {/* 右卡片：场景助手对话框 */}
        <aside className={`${panelClass} flex flex-col`} aria-label="场景助手对话框" data-testid="scene-chat-card">
          <header className="relative flex h-14 shrink-0 items-center justify-between gap-2 border-b border-[var(--color-border)] bg-card px-4">
            <div className="flex min-w-0 items-center gap-2">
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-brand-soft text-brand-ink">
                <Sparkles size={16} />
              </div>
              <div className="min-w-0">
                <h3 className="truncate text-sm font-semibold text-[var(--color-text-primary)]">场景助手</h3>
                <p className="truncate text-[11px] text-[var(--color-text-tertiary)]">对话式建模 · 定义变更自动冻结为版本</p>
              </div>
            </div>
            <button
              type="button"
              onClick={() => setShowHistory(true)}
              className="inline-flex h-8 shrink-0 items-center gap-1 rounded-lg bg-muted px-2.5 text-xs font-medium text-foreground transition-colors hover:bg-brand-soft hover:text-brand-ink"
              title="历史会话"
            >
              <History size={13} /> 历史会话
            </button>
            <SessionHistoryPopover
              open={showHistory}
              items={historyItems}
              currentId={conversationId}
              onClose={() => setShowHistory(false)}
              onCreate={resetChat}
              onSelect={id => void loadConversation(id)}
              renderItemIcon={() => <Sparkles size={16} />}
              emptyDescription="新建会话后，可随时回到之前的场景建设过程。"
            />
          </header>

          {/* 发送上下文工具条：目标场景 + 对话模型 */}
          <div className="flex shrink-0 items-center gap-2 border-b border-[var(--color-border)] bg-card px-3 py-2">
            <TargetSceneSelector
              targetSceneId={targetSceneId === NEW_SCENE ? null : targetSceneId}
              drafts={draftScenes}
              onChange={id => { resetChat(); setTargetSceneId(id ?? NEW_SCENE) }}
            />
            <Select
              value={modelId || '__none__'}
              onValueChange={value => setModelId(value === '__none__' ? '' : value)}
            >
              <SelectTrigger className="h-8 min-w-0 flex-1 rounded-md bg-card px-2 text-xs" aria-label="选择对话模型">
                <SelectValue placeholder="选择对话模型" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__none__">选择对话模型</SelectItem>
                {llmModels.map(model => (
                  <SelectItem key={model.id} value={model.id}>{model.name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {modelsQuery.isSuccess && llmModels.length === 0 && (
            <div className="shrink-0 px-3 pt-3">
              <div className="flex items-center gap-2 rounded-lg border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-3 py-2 text-xs text-[var(--color-warning)]">
                <AlertTriangle size={14} className="shrink-0" />
                <span className="flex-1">尚未配置对话模型：场景助手需要一个 LLM 才能工作。</span>
                <Link to="/models" className="flex items-center gap-1 rounded-lg border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-card px-2 py-1 text-xs hover:bg-[var(--color-warning-bg)]">去模型配置</Link>
              </div>
            </div>
          )}

          <div className="scrollbar-thin min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
            {timeline.length === 0 && (
              <div className="mt-10 space-y-2 text-center text-xs leading-5 text-[var(--color-text-tertiary)]">
                <Sparkles size={22} className="mx-auto text-brand-ink" />
                <p>描述你想构建的业务场景，例如：</p>
                <p>「建一个供应链园区：采购、仓库、生产三栋建筑，</p>
                <p>库位利用率超 95% 时告警」</p>
                <p>生成的定义会自动冻结为草稿场景的新版本。</p>
              </div>
            )}
            {timeline.map(item => {
              if (item.kind === 'user') {
                return (
                  <div key={item.id} className="flex justify-end">
                    <div className="max-w-[85%] rounded-lg bg-brand px-3 py-2 text-xs leading-5 text-[var(--color-text-inverse)]">
                      {item.content}
                    </div>
                  </div>
                )
              }
              if (item.kind === 'assistant') {
                return (
                  <div key={item.id} className="max-w-[90%] rounded-lg border border-border bg-muted px-3 py-2 text-xs leading-5 text-foreground">
                    {item.content}
                  </div>
                )
              }
              if (item.kind === 'system') {
                return (
                  <div key={item.id} className="rounded-md bg-brand-soft px-2.5 py-1.5 text-center text-[11px] text-brand-ink">
                    {item.text}
                  </div>
                )
              }
              return (
                <div key={item.id} className="rounded-md border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-2.5 py-1.5 text-[11px] text-[var(--color-danger)]">
                  <div className="flex items-center gap-1 font-medium">
                    <AlertTriangle size={12} /> {item.message}
                  </div>
                  {item.issues && item.issues.length > 0 && (
                    <ul className="mt-1 list-inside list-disc space-y-0.5">
                      {item.issues.map(issue => (
                        <li key={issue.path}>{issue.path}: {issue.message}</li>
                      ))}
                    </ul>
                  )}
                </div>
              )
            })}
            <div ref={bottomRef} />
          </div>
          {/* 输入栏对齐本体助手页（MYW-64 反馈）：外层 pt/pb 2.5 + 内层单行胶囊，
              发送/停止为胶囊内右侧的图标方钮。 */}
          <div className="border-t border-[var(--color-border)] bg-card px-4 pb-2.5 pt-2.5">
            <div className="relative flex items-center gap-2 rounded-lg border border-[var(--color-border)] bg-card py-1.5 pl-3 pr-1.5 transition-all focus-within:border-ring focus-within:ring-2 focus-within:ring-ring">
              <input
                value={input}
                onChange={event => setInput(event.target.value)}
                onKeyDown={event => {
                  if (event.key === 'Enter') {
                    event.preventDefault()
                    void send()
                  }
                }}
                maxLength={4000}
                placeholder={targetSceneId === NEW_SCENE ? '描述要从零构建的场景…' : '描述对当前场景的调整…'}
                className="min-w-0 flex-1 bg-transparent text-sm text-[var(--color-text-primary)] outline-none placeholder:text-[var(--color-text-tertiary)]"
              />
              {streaming ? (
                <button
                  type="button"
                  onClick={() => abortRef.current?.abort()}
                  aria-label="停止生成"
                  data-testid="scene-chat-stop"
                  className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-viz-rose text-[var(--color-text-inverse)] transition-all duration-200 hover:bg-viz-rose"
                >
                  <Square size={13} fill="currentColor" />
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => void send()}
                  disabled={!input.trim() || (llmModels.length > 0 && !modelId)}
                  aria-label="发送"
                  data-testid="scene-chat-send"
                  title="发送"
                  className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-brand-deep text-[var(--color-text-inverse)] transition-all duration-200 hover:bg-brand disabled:cursor-not-allowed disabled:opacity-25"
                >
                  <Send size={14} />
                </button>
              )}
            </div>
          </div>
        </aside>
      </div>

      <ConfirmModal
        open={rollbackOpen}
        onClose={() => { if (!rollbackMutation.isPending) setRollbackOpen(false) }}
        onConfirm={() => rollbackMutation.mutate()}
        title={'回滚到 v' + selectedVersionNo + '？'}
        description={
          '将把 v' + selectedVersionNo + ' 的定义复制冻结为新版本 v' + nextVersionNo
          + '（source=manual、备注 “回滚自 v' + selectedVersionNo + '”），'
          + '不会改动任何历史版本，也不会影响已发布指针。'}
        confirmText="确认回滚"
        loading={rollbackMutation.isPending}
      />
    </div>
  )
}

/** 版本浮层的单行：首行文本固定为 “v{n} · 来源”，便于测试按名定位。 */
function VersionRow({ version, selected, onSelect }: {
  version: SceneVersionMeta
  selected: boolean
  onSelect: () => void
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onSelect}
      title={version.note}
      className={'flex w-full items-start gap-2 rounded-lg px-2.5 py-2 text-left transition-colors '
        + (selected ? 'bg-brand-soft' : 'hover:bg-muted')}
    >
      <span className="min-w-0 flex-1">
        <span className={'block truncate text-xs font-medium ' + (selected ? 'text-brand-ink' : 'text-foreground')}>
          v{version.version_no} · {versionSourceLabel(version.source)}
        </span>
        {version.note && (
          <span className="mt-0.5 block truncate text-[11px] text-muted-foreground">{version.note}</span>
        )}
        <span className="mt-0.5 block text-[10px] tabular-nums text-[var(--color-text-tertiary)]">
          {formatVersionTime(version.created_at)}
        </span>
      </span>
      {selected && <Check size={14} className="mt-0.5 shrink-0 text-brand-ink" />}
    </button>
  )
}

function liveBoundSceneId(timeline: TimelineItem[]): string | null {
  for (let i = timeline.length - 1; i >= 0; i--) {
    const item = timeline[i]
    if (item.kind === 'system' && item.sceneId) return item.sceneId
  }
  return null
}

function errorMessageText(error: unknown): string {
  if (error && typeof error === 'object') {
    const detail = (error as { detail?: unknown }).detail
    if (typeof detail === 'string') return detail
  }
  return (error as Error)?.message || '对话失败，请稍后重试'
}

// 引用保持：ConversationMessage 类型供历史回放使用
export type { ConversationMessage }
