/**
 * 业务澄清 — 草稿版本在线配置工作台
 *
 * 左侧配置工作区（业务场景七类模型 / 本体模型图谱编辑器 / 数据映射 / 需求文档
 * 四视图切换），右侧探索对话（SSE 流式 + 工具轨迹）随对话实时沉淀画布。
 * 定位：服务指定本体草稿版本的集中配置，不再承担从零创建本体。
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, Suspense } from 'react'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  Bot, Boxes, Check, CircleHelp, Compass, Copy, Download, ExternalLink, Files, FileText, FlaskConical, GitBranch, Globe2, History, Layers, Link2, List,
  Loader2, Paperclip, Plus, Send, Trash2, User, Wrench, X,
} from 'lucide-react'
import {
  explorationApi, streamExplorationChat,
  type BusinessCanvas, type BxAttachment, type BxDraft, type BxQuestion, type BxStep,
  type BxSession, type Completeness, type Readiness,
} from '@/api/exploration'
import { modelApi, ontologyApi } from '@/api/ontologies'
import { ontologyVersionApi } from '@/api/v2/ontology-versions'
import MermaidBlock from '@/components/MermaidBlock'
import { ConfirmModal } from '@/components/ui/Modal'
import { toast } from 'sonner'
import { writeTextToClipboard } from '@/utils/clipboard'
import Md from './Md'
import CanvasPanel from './CanvasPanel'
import ConsistencyPanel from './ConsistencyPanel'
import DocumentsView from './DocumentsView'
import DraftReviewDrawer from './DraftReviewDrawer'
import FileWorkspaceDrawer from './FileWorkspaceDrawer'
import { PipelinePositionHint } from './PipelinePositionHint'
import TrialPreflightDialog from './TrialPreflightDialog'
import { EXPLORE_VIEWS, parseExploreView, parsePendingNewSession, parseSessionBinding, resolveBoundSession, sessionBindingKey, shouldAutoSelectLatestSession, type ExploreView } from './sessionBinding'
import { SplitHandle, useSplitLayout } from '@/hooks/useSplitLayout'
import { LazyGraphWorkspace, LazyMappingWorkspace } from '@/components/explore/ExploreWorkbenchViews'
import type { ModelConfig } from '@/types/ontology'

const VIEW_ICONS: Record<ExploreView, typeof Layers> = {
  canvas: Layers,
  model: Boxes,
  mapping: Link2,
  docs: FileText,
}

interface ChatMsg {
  id: string
  role: 'user' | 'assistant'
  content: string
  steps: BxStep[]
  streaming?: boolean
  createdAt?: string
}

let _mid = 0
const nextId = () => `m-${Date.now()}-${_mid++}`

const SUGGESTIONS = [
  '我们是一家贸易公司，想梳理订单到回款的业务',
  '帮我梳理设备巡检与维修派工的业务模型',
  '我想把售后工单的处理流程建成本体',
]

// 与后端 allowed_upload_extensions 对齐
const ATTACH_ACCEPT = '.csv,.xlsx,.xls,.json,.xml,.pdf,.docx,.doc,.pptx,.ppt,.md,.txt'
const TEXTAREA_LINE_HEIGHT = 20
const TEXTAREA_MAX_LINES = 10
const TEXTAREA_MIN_HEIGHT = 28
const TEXTAREA_MAX_HEIGHT = TEXTAREA_LINE_HEIGHT * TEXTAREA_MAX_LINES + 8
// 可拖拽分栏布局抽到 @/hooks/useSplitLayout，与本体网络页共用同一实现。

const formatSize = (n: number) =>
  n < 1024 ? `${n} B`
    : n < 1024 * 1024 ? `${(n / 1024).toFixed(0)} KB`
      : `${(n / 1024 / 1024).toFixed(1)} MB`

const errorMessage = (error: unknown, fallback: string): string => {
  if (!error || typeof error !== 'object') return fallback
  const value = error as { detail?: string | { message?: string }; message?: string }
  return typeof value.detail === 'string' ? value.detail : value.detail?.message || value.message || fallback
}

const STEP_LABELS: Record<string, string> = {
  upsert_elements: '沉淀画布',
  remove_elements: '修正画布',
  raise_questions: '登记问题',
  resolve_questions: '销账',
  show_diagram: '生成图表',
  use_skill: '激活技能',
  web_search: '联网检索',
}

function StepTrace({ steps, running }: { steps: BxStep[]; running?: boolean }) {
  if (steps.length === 0 && !running) return null
  return (
    <div className="mb-3 rounded-lg bg-[var(--color-bg-base)] border border-[var(--color-border)] px-3 py-2.5 space-y-2">
      {steps.map((s, i) => (
        <div key={i}>
          <div className="flex items-start gap-2.5">
            <div className={`mt-px w-5 h-5 rounded-md flex items-center justify-center shrink-0 ${s.error
              ? 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]'
              : 'bg-brand-soft text-brand-ink'}`}>
              {s.tool === 'web_search' ? <Globe2 size={11} /> : <Wrench size={11} />}
            </div>
            <div className="min-w-0 text-xs leading-5">
              <span className={`font-medium ${s.error ? 'text-[var(--color-danger)]' : 'text-[var(--color-text-primary)]'}`}>
                {STEP_LABELS[s.tool] || s.tool}
              </span>
              <span className="text-[var(--color-text-tertiary)]"> · {s.summary}</span>
            </div>
          </div>
          {s.searchResults && s.searchResults.length > 0 && (
            <div className="ml-[30px] mt-1.5 space-y-1">
              {s.searchResults.map((result, index) => (
                <a
                  key={`${result.url}-${index}`}
                  href={result.url}
                  target="_blank"
                  rel="noreferrer"
                  title={result.snippet || result.title}
                  className="group/source flex min-w-0 items-center gap-1.5 rounded px-1.5 py-1 text-[11px] text-[var(--color-text-secondary)] transition-colors hover:bg-brand-soft hover:text-brand-ink"
                >
                  <span className="shrink-0 font-mono text-[10px] text-[var(--color-text-tertiary)]">[{index + 1}]</span>
                  <span className="min-w-0 flex-1 truncate">{result.title}</span>
                  <ExternalLink size={10} className="shrink-0 opacity-0 transition-opacity group-hover/source:opacity-100" />
                </a>
              ))}
            </div>
          )}
          {/* show_diagram 的确定性 mermaid 随步骤直接出现在对话流中，历史可回放 */}
          {s.diagram && (
            <div className="mt-2 ml-[30px]">
              <div className="text-[11px] font-medium text-[var(--color-text-secondary)] mb-1">
                {s.diagram.title}
                <span className="ml-1.5 font-normal text-[var(--color-text-tertiary)]">由画布确定性生成 · 请核对与实际是否一致</span>
              </div>
              <MermaidBlock chart={s.diagram.mermaid} title={s.diagram.title} warnings={s.diagram.warnings || []} />
            </div>
          )}
        </div>
      ))}
      {running && (
        <div className="flex items-center gap-2.5">
          <div className="w-5 h-5 rounded-md bg-brand-soft flex items-center justify-center shrink-0">
            <Loader2 size={11} className="animate-spin text-brand-ink" />
          </div>
          <span className="text-xs text-[var(--color-text-tertiary)]">
            {steps.length === 0 ? '正在理解业务，规划澄清问题…' : '正在把确认的信息沉淀进画布…'}
          </span>
        </div>
      )}
    </div>
  )
}

/** 开放堵门问题的快捷答复：点选候选值直接作为回答发送（定量闭环的最短路径） */
function QuickReplies({ questions, disabled, onAnswer, onCustom }: {
  questions: BxQuestion[]
  disabled?: boolean
  onAnswer: (text: string) => void
  onCustom: (prefill: string) => void
}) {
  if (questions.length === 0) return null
  return (
    <div className="mb-2 space-y-1.5">
      {questions.map(q => (
        <div key={q.id} className="flex items-start gap-2 rounded-lg border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-2.5 py-1.5">
          <CircleHelp size={13} className="mt-[3px] shrink-0 text-[var(--color-warning)]" />
          <div className="min-w-0 flex-1">
            <span className="text-xs text-[var(--color-warning)] leading-5">{q.question}</span>
            <span className="ml-2 inline-flex flex-wrap gap-1 align-middle">
              {(q.options || []).slice(0, 4).map(opt => (
                <button
                  key={opt}
                  disabled={disabled}
                  onClick={() => onAnswer(`「${q.question}」我的答复：${opt}`)}
                  className="px-2 py-0.5 rounded-md text-[11px] border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-card text-[var(--color-warning)] hover:bg-[var(--color-warning-bg)] disabled:opacity-40"
                >
                  {opt}
                </button>
              ))}
              <button
                disabled={disabled}
                onClick={() => onCustom(`「${q.question}」我的答复：`)}
                className="px-2 py-0.5 rounded-md text-[11px] border border-dashed border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] text-[var(--color-warning)] hover:bg-[var(--color-warning-bg)] disabled:opacity-40"
              >
                自定义…
              </button>
            </span>
          </div>
        </div>
      ))}
    </div>
  )
}

/** 未绑定本体版本时，本体模型/数据映射视图的引导占位 */
function BindRequiredHint() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 px-6 text-center">
      <span className="flex h-12 w-12 items-center justify-center rounded-full bg-[var(--color-bg-base)] text-[var(--color-text-tertiary)]">
        <GitBranch size={21} />
      </span>
      <p className="text-sm font-medium text-[var(--color-text-secondary)]">此视图需要绑定草稿态本体版本</p>
      <p className="max-w-sm text-xs leading-5 text-[var(--color-text-tertiary)]">
        从本体详情页「版本演进」选择草稿版本，点击「在线配置」进入工作台。
      </p>
    </div>
  )
}

const viewLoadingFallback = (
  <div className="flex h-full items-center justify-center gap-2 text-xs text-[var(--color-text-tertiary)]">
    <Loader2 size={15} className="animate-spin" /> 正在加载工作区…
  </div>
)

export default function ExplorationPage() {
  const { containerRef, sizes, startResize } = useSplitLayout()
  const queryClient = useQueryClient()
  // -- URL 绑定锚点（/explore?ontologyId=…&versionId=…，来自版本试跑门禁的补齐
  //    入口与页头「绑定本体」选择器） --
  const [searchParams, setSearchParams] = useSearchParams()
  // -- 「业务澄清」待建新会话锚点（/explore?session=new，来自本体管理首卡入口）：
  //    进入时保持空白待建态、不恢复最近会话；真实会话由首条消息/首个附件懒创建。
  //    与绑定参数同现时待建意图优先，避免未经输入就创建绑定会话。
  const pendingNewSession = useMemo(() => parsePendingNewSession(searchParams), [searchParams])
  const binding = useMemo(
    () => (pendingNewSession ? null : parseSessionBinding(searchParams)),
    [searchParams, pendingNewSession],
  )
  // -- 工作区视图锚点（?view=canvas|model|mapping|docs，缺省 canvas） --
  const view = useMemo(() => parseExploreView(searchParams), [searchParams])
  const navigate = useNavigate()
  // 保留式参数更新：切视图/换绑定时不丢其他锚点（ontologyId/versionId/session/view）
  const updateParams = useCallback((patch: Record<string, string | null>) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      for (const [key, value] of Object.entries(patch)) {
        if (value === null) next.delete(key)
        else next.set(key, value)
      }
      return next
    }, { replace: true })
  }, [setSearchParams])
  // -- 会话 --
  const { data: sessions = [], refetch: refetchSessions, isSuccess: sessionsLoaded } = useQuery({
    queryKey: ['bx-sessions'], queryFn: () => explorationApi.sessions(),
  })
  const [sid, setSid] = useState('')

  // -- 模型选择 --
  const { data: models = [] } = useQuery<ModelConfig[]>({ queryKey: ['models'], queryFn: () => modelApi.list() })
  const llmModels = models.filter(m => m.config_type === 'llm' || !m.config_type)
  const [modelId, setModelId] = useState('')

  // -- 对话 + 画布 --
  const [messages, setMessages] = useState<ChatMsg[]>([])
  const [canvas, setCanvas] = useState<BusinessCanvas | null>(null)
  const [completeness, setCompleteness] = useState<Completeness | null>(null)
  const [readiness, setReadiness] = useState<Readiness | null>(null)
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [webSearch, setWebSearch] = useState(false)
  const [showMessageHistory, setShowMessageHistory] = useState(false)
  const [showSessionHistory, setShowSessionHistory] = useState(false)
  const [copiedMessageId, setCopiedMessageId] = useState<string | null>(null)
  const [deleteSessionTarget, setDeleteSessionTarget] = useState<BxSession | null>(null)
  const [deletingSession, setDeletingSession] = useState(false)
  const [workspaceOpen, setWorkspaceOpen] = useState(false)
  const [reviewDraft, setReviewDraft] = useState<BxDraft | null>(null)
  const [genDocBusy, setGenDocBusy] = useState(false)
  const [banner, setBanner] = useState('')
  // -- 会话附件 --
  const [attachments, setAttachments] = useState<BxAttachment[]>([])
  const [uploads, setUploads] = useState<{ uid: string; name: string; ts: number }[]>([])
  const [attachError, setAttachError] = useState('')
  const fileInputRef = useRef<HTMLInputElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const chatScrollRef = useRef<HTMLDivElement>(null)
  const stickToBottomRef = useRef(true)
  // 异步结果必须归属于发起它的会话。慢模型/慢网络下切换会话时，
  // 旧 GET、SSE 或附件请求不得覆盖当前会话。
  const sidRef = useRef('')
  const sessionRequestRef = useRef(0)
  const sessionCreationRef = useRef<Promise<string> | null>(null)
  // 绑定态会话解析按绑定目标只执行一次，避免列表 refetch 与用户手动选择打架
  const bindingResolvedRef = useRef('')
  // 待建新会话意图出现时清空解析记录：允许用户随后再次选择同一绑定目标
  useEffect(() => {
    if (pendingNewSession) bindingResolvedRef.current = ''
  }, [pendingNewSession])
  const sendInFlightRef = useRef(false)
  const chatGenerationRef = useRef(0)
  const chatAbortRef = useRef<AbortController | null>(null)

  const selectSession = useCallback((id: string) => {
    sidRef.current = id
    setSid(id)
  }, [])

  const cancelActiveChat = useCallback(() => {
    chatGenerationRef.current += 1
    chatAbortRef.current?.abort()
    chatAbortRef.current = null
    setBusy(false)
  }, [])

  useEffect(() => () => {
    chatAbortRef.current?.abort()
  }, [])

  const myMessages = useMemo(() => messages.filter(m => m.role === 'user'), [messages])
  const jumpToMessage = useCallback((id: string) => {
    setShowMessageHistory(false)
    requestAnimationFrame(() => {
      document.getElementById(`explore-msg-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    })
  }, [])

  const copyMessage = async (message: ChatMsg) => {
    try {
      await writeTextToClipboard(message.content)
      setCopiedMessageId(message.id)
      window.setTimeout(() => {
        setCopiedMessageId(current => current === message.id ? null : current)
      }, 1500)
    } catch (error: unknown) {
      toast.error('复制失败', { description: errorMessage(error, '请稍后重试。') })
    }
  }

  // 按真实排版高度（含自动换行）伸展；十行后只让 textarea 内部滚动。
  useLayoutEffect(() => {
    const textarea = textareaRef.current
    if (!textarea) return
    // 以单行基线测量。0 会与 min-height 叠加，auto 在部分 Chromium
    // 版本会保留上次的内容高度；显式基线能让清空/发送后的高度可靠复位。
    textarea.style.height = `${TEXTAREA_MIN_HEIGHT}px`
    // 空值时 scrollHeight 会把两行 placeholder 也算进去，不能拿它判断扩展。
    const contentHeight = input ? textarea.scrollHeight : TEXTAREA_MIN_HEIGHT
    const nextHeight = Math.max(TEXTAREA_MIN_HEIGHT, Math.min(contentHeight, TEXTAREA_MAX_HEIGHT))
    textarea.style.height = `${nextHeight}px`
    textarea.style.overflowY = contentHeight > TEXTAREA_MAX_HEIGHT ? 'auto' : 'hidden'
  }, [input])

  const updateScrollStickiness = () => {
    const el = chatScrollRef.current
    if (!el) return
    stickToBottomRef.current = el.scrollHeight - el.clientHeight - el.scrollTop < 96
  }

  // 消息与「上传的附件」按时间合并成一条对话时间线 —— 附件直接体现在对话流中，
  // 而不是单独挂在输入框上方。
  type TLItem =
    | { key: string; ts: number; kind: 'msg'; msg: ChatMsg }
    | { key: string; ts: number; kind: 'att'; att: BxAttachment }
    | { key: string; ts: number; kind: 'up'; up: { uid: string; name: string; ts: number } }
  const timeline = useMemo<TLItem[]>(() => {
    const items: TLItem[] = []
    let lastTs = 0
    messages.forEach(m => {
      let t = m.createdAt ? Date.parse(m.createdAt) : NaN
      if (Number.isNaN(t)) t = lastTs + 1   // 缺时间戳时保持相对顺序
      lastTs = t
      items.push({ key: m.id, ts: t, kind: 'msg', msg: m })
    })
    attachments.forEach(a => items.push({
      key: `att-${a.id}`, ts: Date.parse(a.createdAt) || 0, kind: 'att', att: a,
    }))
    uploads.forEach(u => items.push({ key: u.uid, ts: u.ts, kind: 'up', up: u }))
    return items.sort((x, y) => x.ts - y.ts)
  }, [messages, attachments, uploads])

  useLayoutEffect(() => {
    const el = chatScrollRef.current
    if (!el || !stickToBottomRef.current || timeline.length === 0) return
    el.scrollTop = el.scrollHeight
  }, [timeline])

  const loadSession = useCallback(async (id: string) => {
    const requestId = ++sessionRequestRef.current
    cancelActiveChat()
    selectSession(id)
    setShowMessageHistory(false)
    setShowSessionHistory(false)
    setBanner('')
    setAttachError('')
    setAttachments([])
    setUploads([])
    setMessages([])
    setCanvas(null)
    setCompleteness(null)
    setReadiness(null)
    try {
      const [detail, sessionAttachments] = await Promise.all([
        explorationApi.session(id),
        explorationApi.attachments(id).catch(() => [] as BxAttachment[]),
      ])
      if (requestId !== sessionRequestRef.current || sidRef.current !== id) return
      setMessages((detail.messages || []).map(m => ({
        id: m.id, role: m.role, content: m.content, steps: m.steps || [], createdAt: m.createdAt,
      })))
      setCanvas(detail.canvas)
      setCompleteness(detail.completeness)
      setReadiness(detail.readiness)
      setAttachments(sessionAttachments)
    } catch (error: unknown) {
      if (requestId !== sessionRequestRef.current || sidRef.current !== id) return
      setBanner(errorMessage(error, '会话加载失败'))
    }
  }, [cancelActiveChat, selectSession])

  // 首次进入自动选中最近会话；绑定态由绑定解析决定选中；待建新会话（?session=new）
  // 保持空白，等首条输入经 ensureSession 懒创建后再进入常规选中逻辑
  useEffect(() => {
    if (binding) return
    if (!shouldAutoSelectLatestSession(pendingNewSession, Boolean(sid))) return
    if (sessions.length > 0) void loadSession(sessions[0].id)
  }, [sessions, sid, loadSession, binding, pendingNewSession])

  const newSession = async () => {
    const s = await explorationApi.createSession()
    await refetchSessions()
    await loadSession(s.id)
  }

  const removeSession = async (id: string) => {
    setDeletingSession(true)
    try {
      await explorationApi.deleteSession(id)
      if (id === sid) {
        cancelActiveChat()
        sessionRequestRef.current += 1
        selectSession('')
        setMessages([])
        setCanvas(null)
        setCompleteness(null)
        setReadiness(null)
        setAttachments([])
        setUploads([])
        setShowMessageHistory(false)
      }
      setDeleteSessionTarget(null)
      await refetchSessions()
      toast.success('会话已删除')
    } catch (error: unknown) {
      toast.error('会话删除失败', { description: errorMessage(error, '请稍后重试。') })
    } finally {
      setDeletingSession(false)
    }
  }

  // 无会话时懒创建（首条消息 / 首个附件都可能触发）；绑定态下创建带本体版本锚点
  const ensureSession = async (): Promise<string> => {
    if (sidRef.current) return sidRef.current
    // send 与 upload 可能在同一事件轮并发进入。创建请求必须是 single-flight，
    // 否则两边会各建一个会话，并把消息和附件写到不同 sid。
    if (sessionCreationRef.current) return sessionCreationRef.current
    const creation = explorationApi.createSession(
      undefined,
      binding ? { ontologyId: binding.ontologyId, ontologyVersionId: binding.versionId } : undefined,
    ).then(s => {
      // 创建等待期间若用户已主动选中其他会话，不再抢回焦点；
      // 本轮调用统一归属当前选中会话。
      const targetSid = sidRef.current || s.id
      if (!sidRef.current) selectSession(s.id)
      void refetchSessions()
      return targetSid
    })
    sessionCreationRef.current = creation
    try {
      return await creation
    } finally {
      if (sessionCreationRef.current === creation) sessionCreationRef.current = null
    }
  }

  // 绑定态会话解析：优先选中同绑定的既有会话，无匹配则经 ensureSession 创建绑定会话
  useEffect(() => {
    if (!binding || !sessionsLoaded) return
    const key = sessionBindingKey(binding)
    if (bindingResolvedRef.current === key) return
    const resolution = resolveBoundSession(sessions, binding, sid)
    bindingResolvedRef.current = key
    if (resolution.action === 'none') return
    if (resolution.action === 'select') {
      void loadSession(resolution.sessionId)
      return
    }
    void (async () => {
      try {
        const id = await ensureSession()
        await loadSession(id)
      } catch (error: unknown) {
        setBanner(errorMessage(error, '绑定会话创建失败，请重试'))
      }
    })()
  }, [binding, sessionsLoaded, sessions, sid, loadSession])

  // 绑定徽章数据：本体名 + 版本号（queryKey 与本体详情页/版本 Tab 一致，共享缓存）
  const currentSession = sessions.find(s => s.id === sid)
  const boundOntologyId = currentSession?.ontologyId || null
  const { data: boundOntology } = useQuery({
    queryKey: ['ontology', boundOntologyId],
    queryFn: () => ontologyApi.get(boundOntologyId!),
    enabled: Boolean(boundOntologyId),
  })
  const { data: boundVersionTree } = useQuery({
    queryKey: ['version-tree', boundOntologyId],
    queryFn: () => ontologyVersionApi.tree(boundOntologyId!),
    enabled: Boolean(boundOntologyId),
  })
  const boundVersionNumber = boundVersionTree?.versions.find(
    v => v.id === currentSession?.ontologyVersionId,
  )?.version_number

  // 配置工作区（本体模型/数据映射视图）的上下文：URL 绑定优先，其次会话记录
  const workbenchOntologyId = binding?.ontologyId || currentSession?.ontologyId || null
  const workbenchVersionId = binding?.versionId || currentSession?.ontologyVersionId || null

  // 绑定版本的语义一致性（本体模型视图顶部一致性面板与「本体模型」标签页漂移角标共用
  // 缓存，queryKey 与本体详情页结构说明弹窗一致；人工保存后经 GraphWorkspace.onSaved 失效重取）
  const { data: workbenchSemantic } = useQuery({
    queryKey: ['ontology-structure-doc', workbenchOntologyId, workbenchVersionId],
    queryFn: () => ontologyVersionApi.versionSemantic(workbenchOntologyId!, workbenchVersionId!),
    enabled: Boolean(workbenchOntologyId && workbenchVersionId),
  })
  const semanticIssueCount = (workbenchSemantic?.issues || []).length

  // 本体模型空视图的管线位置提示：结构快照计数（objectTypes/linkTypes/actions
  // 等合计）为 0 时说明画布成果尚未经「文档 → 草稿 → 应用」写入版本。
  // 文档列表与「需求文档」视图共用 queryKey 缓存，仅在模型视图挂载时请求。
  const structureCounts = workbenchSemantic?.overview?.structureCounts
  const structureTotal = structureCounts
    ? structureCounts.objectTypes + structureCounts.linkTypes
      + structureCounts.actions + structureCounts.functions
      + structureCounts.sentinels
    : null
  const { data: modelViewDocs = [] } = useQuery({
    queryKey: ['bx-documents', sid],
    queryFn: () => explorationApi.documents(sid!),
    enabled: view === 'model' && Boolean(sid),
  })

  // 绑定版本生命周期（版本树口径）：仅 editing 草稿提供「转为试跑态」入口；
  // 试跑成功后 lifecycle 变化会同步进 GraphWorkspace 的 key，强制重挂以按试跑态只读重读。
  const workbenchVersionNode = boundVersionTree?.versions.find(v => v.id === workbenchVersionId) || null
  const canEnterTrial = Boolean(
    workbenchOntologyId && workbenchVersionId
    && workbenchVersionNode?.node_kind === 'draft'
    && workbenchVersionNode.lifecycle_status === 'editing',
  )
  const [preflightOpen, setPreflightOpen] = useState(false)

  const pickFiles = () => fileInputRef.current?.click()

  const handleFiles = async (files: FileList | null) => {
    if (!files || files.length === 0) return
    setAttachError('')
    const targetSid = await ensureSession()
    // 本地交互一旦开始，之前同 sid 的 loadSession 快照已过时，不能再覆盖
    // 新上传的附件列表。
    sessionRequestRef.current += 1
    for (const file of Array.from(files)) {
      const uid = `up-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`
      setUploads(prev => [...prev, { uid, name: file.name, ts: Date.now() }])
      try {
        const att = await explorationApi.uploadAttachment(targetSid, file)
        if (sidRef.current === targetSid) {
          setAttachments(prev => [...prev, att])
          if (att.status !== 'ready') {
            setAttachError(`「${file.name}」未能加入模型上下文：${att.error || '内容抽取失败'}`)
          }
        }
      } catch (error: unknown) {
        if (sidRef.current === targetSid) {
          setAttachError(`「${file.name}」上传失败：${errorMessage(error, '无法读取文件内容')}`)
        }
      } finally {
        setUploads(prev => prev.filter(u => u.uid !== uid))
      }
    }
    if (fileInputRef.current) fileInputRef.current.value = ''
  }

  const removeAttachment = async (aid: string) => {
    const targetSid = sidRef.current
    if (!targetSid) return
    try {
      await explorationApi.deleteAttachment(targetSid, aid)
      if (sidRef.current === targetSid) {
        setAttachments(prev => prev.filter(a => a.id !== aid))
      }
    } catch (error: unknown) {
      if (sidRef.current === targetSid) {
        setAttachError(`文件删除失败：${errorMessage(error, '请稍后重试')}`)
      }
    }
  }

  const downloadAttachment = async (att: BxAttachment) => {
    if (!sid) return
    try {
      const blob = await explorationApi.downloadAttachment(sid, att.id)
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = att.filename
      anchor.click()
      URL.revokeObjectURL(url)
    } catch (error: unknown) {
      setAttachError(`「${att.filename}」下载失败：${errorMessage(error, '文件不可用')}`)
    }
  }

  const send = async (text?: string) => {
    const message = (text ?? input).trim()
    // React state 要到下一次渲染才更新，busy 不能阻止同一事件轮的双击。
    // 这个同步互斥必须在第一个 await 之前占用，覆盖创建会话和完整 SSE 生命周期。
    if (!message || busy || sendInFlightRef.current) return
    sendInFlightRef.current = true
    setBusy(true)
    setBanner('')

    try {
      const targetSid = await ensureSession()
      // loadSession 的慢 GET 可能仍在飞行；从此刻起它的消息/画布快照已过时。
      // 递增请求代际，使迟到结果无法覆盖下面即将加入的 user/assistant 消息。
      sessionRequestRef.current += 1
      const generation = ++chatGenerationRef.current
      chatAbortRef.current?.abort()
      const controller = new AbortController()
      chatAbortRef.current = controller
      setInput('')
      const now = new Date().toISOString()
      const assistantId = nextId()
      setMessages(prev => [...prev,
        { id: nextId(), role: 'user', content: message, steps: [], createdAt: now },
        { id: assistantId, role: 'assistant', content: '', steps: [], streaming: true, createdAt: now },
      ])

      const ownsCurrentView = () =>
        generation === chatGenerationRef.current && sidRef.current === targetSid
      const patchAssistant = (fn: (m: ChatMsg) => ChatMsg) => {
        if (!ownsCurrentView()) return
        setMessages(prev => prev.map(m => m.id === assistantId ? fn(m) : m))
      }

      try {
        await streamExplorationChat(targetSid, {
          message,
          modelId: modelId || undefined,
          webSearch,
        }, e => {
          if (!ownsCurrentView()) return
          if (e.type === 'step') {
            const step: BxStep = {
              tool: e.tool, arguments: e.arguments, summary: e.summary,
              durationMs: e.durationMs, error: e.error, diagram: e.diagram,
              searchResults: e.searchResults,
            }
            patchAssistant(m => ({ ...m, steps: [...m.steps, step] }))
            // agent 生成了文档/草稿 → 立即刷新文档列表（与「需求文档」视图
            // 共用 queryKey），避免 agent 说「已生成」而列表仍旧
            if (step.tool === 'generate_document' || step.tool === 'generate_draft') {
              void queryClient.invalidateQueries({ queryKey: ['bx-documents', targetSid] })
            }
          } else if (e.type === 'canvas') {
            setCanvas(e.canvas)
            setCompleteness(e.completeness)
            setReadiness(e.readiness)
          } else if (e.type === 'answer') {
            patchAssistant(m => ({ ...m, content: e.content, streaming: false }))
          } else if (e.type === 'error') {
            patchAssistant(m => ({ ...m, content: m.content || `⚠️ ${e.message}`, streaming: false }))
          }
        }, controller.signal)
      } catch (error: unknown) {
        if (!(error instanceof DOMException && error.name === 'AbortError')) {
          patchAssistant(m => ({ ...m, content: `⚠️ ${errorMessage(error, '请求失败')}`, streaming: false }))
        }
      } finally {
        patchAssistant(m => ({ ...m, streaming: false }))
        if (ownsCurrentView()) {
          chatAbortRef.current = null
          explorationApi.attachments(targetSid).then(value => {
            if (ownsCurrentView()) setAttachments(value)
          }).catch(() => { /* 非致命 */ })
          void refetchSessions()   // 标题可能已更新
        }
      }
    } catch (error: unknown) {
      setBanner(errorMessage(error, '会话创建失败，请重试'))
    } finally {
      sendInFlightRef.current = false
      setBusy(false)
    }
  }

  const generateDocument = async () => {
    if (!sid || genDocBusy) return
    setGenDocBusy(true)
    setBanner('')
    try {
      await explorationApi.generateDocument(sid, modelId || undefined)
    } catch (error: unknown) {
      setBanner(errorMessage(error, '文档生成失败'))
    } finally {
      setGenDocBusy(false)
    }
  }

  const canvasCount = completeness
    ? Object.values(completeness.counts).reduce((a, b) => a + b, 0) : 0
  const panelClass = 'min-h-0 min-w-0 overflow-hidden rounded-lg border border-[var(--color-border)] shadow-[0_1px_2px_rgba(15,23,42,0.05),0_12px_32px_-16px_rgba(15,23,42,0.18)]'

  // 开放堵门问题 → 输入框上方的快捷答复（最多展示 2 个，按登记顺序）
  const openBlocking = (canvas?.questions || [])
    .filter(q => q.status === 'open' && q.kind === 'blocking')
  const askInChat = (text: string) => { void send(text) }
  const prefillInput = (text: string) => {
    setInput(text)
    textareaRef.current?.focus()
  }

  return (
    <div className="explore-shell relative flex h-full min-h-[560px] overflow-hidden">
      <div
        ref={containerRef}
        className="scrollbar-none grid min-h-0 flex-1 overflow-x-auto overflow-y-hidden p-1"
        style={{ gridTemplateColumns: `minmax(520px, ${sizes[0]}fr) 4px minmax(320px, ${sizes[1]}fr)` }}
      >
      {/* 配置工作区：业务场景 / 本体模型 / 数据映射 / 需求文档 */}
      <aside className={`${panelClass} workspace-topology-surface flex flex-col`}>
        <div className="flex h-14 shrink-0 items-center gap-1 border-b border-[var(--color-border)] bg-card px-3" aria-label="切换工作区视图">
          {EXPLORE_VIEWS.map(item => {
            const active = view === item.id
            const Icon = VIEW_ICONS[item.id]
            return (
              <button
                key={item.id}
                type="button"
                aria-pressed={active}
                data-testid={`explore-view-${item.id}`}
                onClick={() => updateParams({ view: item.id === 'canvas' ? null : item.id })}
                className={`inline-flex h-7 items-center gap-1.5 rounded-md border px-2.5 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${active
                  ? 'border-brand-line bg-brand-soft text-brand-ink'
                  : 'border-transparent text-[var(--color-text-tertiary)] hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-secondary)]'}`}
              >
                <Icon size={13} />
                {item.label}
                {item.id === 'model' && semanticIssueCount > 0 && (
                  <span
                    data-testid="explore-model-drift-badge"
                    title="本体模型与业务语义存在漂移"
                    className="inline-flex min-w-4 items-center justify-center rounded-full bg-[var(--color-warning)] px-1 text-[9px] font-semibold leading-4 text-[var(--color-text-inverse)]"
                  >
                    {semanticIssueCount > 99 ? '99+' : semanticIssueCount}
                  </span>
                )}
              </button>
            )
          })}
        </div>
        <div className="min-h-0 flex-1 overflow-hidden">
          {view === 'canvas' && (
            <CanvasPanel
              sessionId={sid || undefined}
              canvas={canvas}
              completeness={completeness}
              readiness={readiness}
              onAsk={busy ? undefined : askInChat}
            />
          )}
          {view === 'model' && (
            workbenchOntologyId ? (
              <div className="flex h-full min-h-0 flex-col">
                {workbenchVersionId && (
                  <div className="shrink-0 border-b border-[var(--color-border)] bg-card px-3 py-2">
                    <div className="flex items-start gap-2">
                      <div className="min-w-0 flex-1">
                        <ConsistencyPanel
                          ontologyId={workbenchOntologyId}
                          versionId={workbenchVersionId}
                          onBackTranslate={busy ? undefined : askInChat}
                          onGotoDocs={() => updateParams({ view: 'docs' })}
                        />
                      </div>
                      {canEnterTrial && (
                        <button
                          type="button"
                          data-testid="explore-trial-entry"
                          onClick={() => setPreflightOpen(true)}
                          title="权威预检通过后把草稿转为试跑态（快照冻结，真实数据仅写入隔离空间）"
                          className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-md border border-[var(--color-warning)] bg-[var(--color-warning)] px-3 text-xs font-medium text-[var(--color-text-inverse)] transition-colors hover:border-[var(--color-warning)] hover:bg-[var(--color-warning)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-warning)]"
                        >
                          <FlaskConical size={13} /> 转为试跑态
                        </button>
                      )}
                    </div>
                  </div>
                )}
                {structureTotal === 0 && (
                  <PipelinePositionHint
                    readiness={readiness}
                    hasDocument={modelViewDocs.length > 0}
                    documentVersion={modelViewDocs[0]?.version ?? null}
                  />
                )}
                <div className="min-h-0 flex-1 overflow-hidden">
                  <Suspense fallback={viewLoadingFallback}>
                    <LazyGraphWorkspace
                      key={`${workbenchVersionId || 'runtime'}:${workbenchVersionNode?.lifecycle_status || 'unknown'}`}
                      ontologyId={workbenchOntologyId}
                      versionId={workbenchVersionId}
                      theme="light"
                      layout="embedded"
                      showHeader={false}
                      onBackToVersions={() => navigate(`/ontologies/${workbenchOntologyId}?tab=versions`)}
                      onDraftCreated={newVersionId => updateParams({ versionId: newVersionId })}
                      onSaved={() => {
                        void queryClient.invalidateQueries({
                          queryKey: ['ontology-structure-doc', workbenchOntologyId, workbenchVersionId],
                        })
                      }}
                    />
                  </Suspense>
                </div>
              </div>
            ) : (
              <BindRequiredHint />
            )
          )}
          {view === 'mapping' && (
            workbenchOntologyId ? (
              <Suspense fallback={viewLoadingFallback}>
                <LazyMappingWorkspace
                  ontologyId={workbenchOntologyId}
                  versionId={workbenchVersionId}
                  hideChromeNavigation
                  autoShowTutorial={false}
                />
              </Suspense>
            ) : (
              <BindRequiredHint />
            )
          )}
          {view === 'docs' && (
            sid ? (
              <DocumentsView
                sessionId={sid}
                binding={currentSession?.ontologyId ? {
                  ontologyId: currentSession.ontologyId,
                  versionId: currentSession.ontologyVersionId || null,
                  name: boundOntology?.name,
                } : null}
                onDraftCreated={draft => setReviewDraft(draft)}
                onGenerate={generateDocument}
                documentGenerating={genDocBusy}
                canGenerateDocument={canvasCount > 0}
              />
            ) : (
              <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center">
                <FileText size={22} className="text-[var(--color-text-tertiary)]" />
                <p className="text-sm text-[var(--color-text-secondary)]">先在右侧对话中澄清业务</p>
                <p className="text-xs text-[var(--color-text-tertiary)]">会话创建后即可生成并查看需求文档。</p>
              </div>
            )
          )}
        </div>
      </aside>

      <SplitHandle onPointerDown={startResize} label="调整配置工作区与探索对话宽度" />

      {/* 探索对话 */}
      <section className={`${panelClass} workspace-topology-surface flex flex-col`}>
        <header className="flex h-14 shrink-0 items-center border-b border-[var(--color-border)] bg-card px-4">
          <div className="flex w-full min-w-0 items-center justify-between gap-2">
            <div className="flex min-w-0 flex-1 items-center gap-2">
              <div className="flex shrink-0 h-8 w-8 items-center justify-center rounded-md bg-brand-soft text-brand-ink">
                <Compass size={18} />
              </div>
              <div className="min-w-0">
                <h3 className="truncate text-sm font-semibold text-[var(--color-text-primary)]">
                  {sessions.find(s => s.id === sid)?.title || '业务澄清'}
                </h3>
                <p className="truncate text-[11px] text-[var(--color-text-tertiary)]">通过对话澄清业务，沉淀七大模型与需求文档，在线完善本体模型</p>
              </div>
              {boundOntologyId && (
                <span
                  data-testid="session-binding-badge"
                  title="本会话绑定本体版本：草稿落地写入该版本，经试跑验证后发布生效"
                  className="inline-flex h-8 max-w-56 shrink-0 items-center gap-1.5 rounded-md border border-[var(--color-border)] bg-[var(--color-bg-base)] px-2.5 text-xs font-medium text-[var(--color-text-secondary)]"
                >
                  <GitBranch size={13} className="shrink-0 text-brand-ink" />
                  <span className="truncate">{boundOntology?.name || '…'}</span>
                  {boundVersionNumber && (
                    <span className="shrink-0 font-mono text-[11px] text-[var(--color-text-tertiary)]">{boundVersionNumber}</span>
                  )}
                </span>
              )}
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <Select
                value={modelId || '__none__'}
                onValueChange={value => setModelId(value === '__none__' ? '' : value)}
              >
                <SelectTrigger className="h-8 w-fit min-w-28 cursor-pointer rounded-md bg-card px-2 text-xs" aria-label="对话模型" title="对话模型">
                  <SelectValue placeholder="默认模型" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="__none__">默认模型</SelectItem>
                  {llmModels.map(m => <SelectItem key={m.id} value={m.id}>{m.name}</SelectItem>)}
                </SelectContent>
              </Select>
              <button
                onClick={() => setWorkspaceOpen(true)}
                disabled={!sid}
                data-testid="workspace-files-button"
                title="查看会话文件"
                aria-label="查看会话文件"
                className="group relative inline-flex h-8 items-center justify-center gap-1.5 rounded-md border border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-[var(--color-info-bg)] px-2 text-[11px] font-medium text-[var(--color-info)] transition-colors hover:border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] hover:bg-[var(--color-info-bg)] hover:text-[var(--color-info)] active:scale-[0.98] disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-info)]"
              >
                <Files size={15} />
                <span>会话文件</span>
                {attachments.length > 0 && (
                  <span className="absolute -right-1.5 -top-1.5 flex min-w-4 items-center justify-center rounded-full bg-brand px-1 text-[9px] font-semibold leading-4 text-[var(--color-text-inverse)]">
                    {attachments.length > 99 ? '99+' : attachments.length}
                  </span>
                )}
              </button>
              <div className="relative">
                <button
                  type="button"
                  onClick={() => setShowSessionHistory(value => !value)}
                  title="查看历史会话"
                  aria-label="查看历史会话"
                  aria-expanded={showSessionHistory}
                  data-testid="session-history-button"
                  className={`inline-flex h-8 items-center justify-center gap-1.5 rounded-md border px-2 text-[11px] font-medium transition-colors active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-viz-violet ${showSessionHistory
                    ? 'border-viz-violet-soft bg-viz-violet-soft text-viz-violet'
                    : 'border-viz-violet-soft bg-viz-violet-soft text-viz-violet hover:border-viz-violet-soft hover:bg-viz-violet-soft hover:text-viz-violet'}`}
                >
                  <History size={15} />
                  <span>历史会话</span>
                </button>
                {showSessionHistory && (
                  <>
                    <div className="fixed inset-0 z-20" onClick={() => setShowSessionHistory(false)} />
                    <div className="absolute right-0 top-full z-30 mt-[14px] w-[380px] overflow-hidden rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-elevated)] shadow-[0_18px_52px_rgba(15,23,42,0.16)] animate-slide-up">
                      <div className="flex items-center gap-3 border-b border-[var(--color-border)] px-4 py-2.5">
                        <span className="shrink-0 text-sm font-semibold text-[var(--color-text-primary)]">历史会话</span>
                        <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-xs text-brand-ink">
                          <span className="h-1.5 w-1.5 rounded-full bg-brand" />
                          共 <span className="font-semibold tabular-nums">{sessions.length}</span> 个
                        </span>
                        <button
                          type="button"
                          onClick={() => void newSession()}
                          className="ml-auto inline-flex h-8 items-center gap-1.5 rounded-lg bg-brand px-3 text-xs font-medium text-[var(--color-text-inverse)] transition-all hover:bg-brand-deep active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        >
                          <Plus size={13} /> 新建
                        </button>
                      </div>
                      <div className="scrollbar-thin max-h-[420px] overflow-y-auto overflow-x-hidden">
                        {sessions.length === 0 ? (
                          <div className="flex flex-col items-center px-6 py-14 text-center">
                            <span className="mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-[var(--color-bg-base)] text-[var(--color-text-tertiary)]">
                              <History size={21} />
                            </span>
                            <p className="text-sm font-medium text-[var(--color-text-secondary)]">还没有历史会话</p>
                            <p className="mt-1 text-xs leading-5 text-[var(--color-text-tertiary)]">新建会话后，可随时回到之前的探索过程。</p>
                          </div>
                        ) : <div className="divide-y divide-[var(--color-border)]">{sessions.map(session => (
                          <div
                            key={session.id}
                            className={`group flex items-center gap-2.5 px-4 py-2 transition-colors ${session.id === sid
                              ? 'bg-brand-soft'
                              : 'hover:bg-[var(--color-bg-hover)]'}`}
                          >
                            <button
                              type="button"
                              onClick={() => void loadSession(session.id)}
                              className="flex min-w-0 flex-1 items-center gap-3 text-left focus-visible:outline-none"
                            >
                              <span className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${session.id === sid
                                ? 'bg-brand-soft text-brand-ink'
                                : 'bg-muted text-muted-foreground'}`}>
                                <Compass size={16} />
                              </span>
                              <div className="min-w-0 flex-1">
                                <p className={`truncate text-sm font-medium ${session.id === sid ? 'text-brand-ink' : 'text-[var(--color-text-primary)]'}`} title={session.title}>
                                  {session.title}
                                </p>
                                <p className="mt-0.5 text-[10px] tabular-nums text-[var(--color-text-tertiary)]">
                                  {new Date(session.updatedAt).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}
                                </p>
                              </div>
                              {session.id === sid && <span className="rounded-md bg-card px-2 py-1 text-[10px] font-medium text-brand-ink">当前</span>}
                            </button>
                            <button
                              type="button"
                              onClick={() => setDeleteSessionTarget(session)}
                              title={`删除会话 ${session.title}`}
                              aria-label={`删除会话 ${session.title}`}
                              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-[var(--color-text-tertiary)] opacity-0 transition-all hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)] group-hover:opacity-100 focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-danger)]"
                            >
                              <Trash2 size={14} />
                            </button>
                          </div>
                        ))}</div>}
                      </div>
                    </div>
                  </>
                )}
              </div>
            </div>
          </div>
        </header>

        {banner && (
          <div className="px-4 py-2 text-xs text-[var(--color-danger)] bg-[var(--color-danger-bg)] border-b border-[var(--color-border)]">
            {banner}
          </div>
        )}

        <div
          ref={chatScrollRef}
          data-testid="exploration-chat-region"
          onScroll={updateScrollStickiness}
          className="scrollbar-none workspace-topology-surface flex-1 overflow-y-auto px-4 py-4"
        >
          {timeline.length === 0 && (
            <div className="h-full flex flex-col items-center justify-center gap-4">
              <div className="w-12 h-12 rounded-2xl bg-brand-soft flex items-center justify-center">
                <Compass size={22} className="text-brand-ink" />
              </div>
              <div className="text-center">
                <div className="text-sm font-medium text-[var(--color-text-primary)]">从描述你的业务开始</div>
                <div className="mt-1 text-xs text-[var(--color-text-tertiary)] max-w-md leading-relaxed">
                  我会通过提问帮你澄清业务，并把确认的信息实时沉淀为对象、主体、行为、事件、规则、流程、场景七类模型 —— 左侧业务场景随对话生长。
                </div>
              </div>
              <div className="flex flex-col gap-2 w-full max-w-md">
                {SUGGESTIONS.map(s => (
                  <button
                    key={s}
                    onClick={() => void send(s)}
                    className="text-left text-xs px-3.5 py-2.5 rounded-lg border border-[var(--color-border)] text-[var(--color-text-secondary)] hover:border-brand hover:text-brand-ink transition-colors"
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}

          <div className="max-w-3xl mx-auto space-y-5">
            {timeline.map(item => {
              // 上传的附件：作为用户侧的一条对话记录出现在对话流中
              if (item.kind === 'att' || item.kind === 'up') {
                const uploading = item.kind === 'up'
                const name = uploading ? item.up.name : item.att.filename
                return (
                  <div key={item.key} className="flex gap-3 flex-row-reverse">
                    <div className="w-7 h-7 rounded-lg flex items-center justify-center shrink-0 bg-[var(--color-nav-bg)] text-[var(--color-text-inverse)]">
                      <Paperclip size={14} />
                    </div>
                    <div className={`group flex items-center gap-2.5 rounded-xl border bg-card px-3 py-2 max-w-[85%] ${
                      uploading ? 'border-dashed border-[var(--color-border)]' : 'border-[var(--color-border)]'}`}>
                      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-brand-soft text-brand-ink">
                        {uploading ? <Loader2 size={15} className="animate-spin" /> : <FileText size={16} />}
                      </span>
                      <div className="min-w-0 text-left">
                        <div className="truncate text-sm font-medium text-[var(--color-text-primary)]" title={name}>{name}</div>
                        <div className="mt-0.5 text-[11px] text-[var(--color-text-tertiary)]">
                          {uploading
                            ? '上传中…'
                            : item.att.status === 'failed'
                              ? `读取失败 · ${item.att.error || '未加入模型上下文'}`
                              : item.att.source === 'agent'
                                ? `AI 工作草稿 · ${formatSize(item.att.fileSize)} · 未作为用户事实`
                                : `已读取 ${item.att.charCount.toLocaleString()} 字 · ${formatSize(item.att.fileSize)} · 仅本会话可见`}
                        </div>
                      </div>
                      {!uploading && (
                        <div className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
                          <button
                            onClick={() => void downloadAttachment(item.att)}
                            title="下载文件"
                            className="p-1 rounded text-[var(--color-text-tertiary)] hover:text-brand-ink hover:bg-[var(--color-bg-hover)]"
                          >
                            <Download size={13} />
                          </button>
                          <button
                            onClick={() => void removeAttachment(item.att.id)}
                            title="移除参考资料"
                            className="p-1 rounded text-[var(--color-text-tertiary)] hover:text-[var(--color-danger)] hover:bg-[var(--color-bg-hover)]"
                          >
                            <X size={13} />
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                )
              }
              const m = item.msg
              return (
                <div
                  key={item.key}
                  id={m.role === 'user' ? `explore-msg-${m.id}` : undefined}
                  className={`flex scroll-mt-4 gap-3 ${m.role === 'user' ? 'flex-row-reverse' : ''}`}
                >
                  <div className={`w-7 h-7 rounded-lg flex items-center justify-center shrink-0 ${m.role === 'user'
                    ? 'bg-[var(--color-nav-bg)] text-[var(--color-text-inverse)]' : 'bg-brand-soft text-brand-ink'}`}>
                    {m.role === 'user' ? <User size={14} /> : <Bot size={14} />}
                  </div>
                  <div className={`min-w-0 max-w-[85%] ${m.role === 'user' ? 'text-right' : ''}`}>
                    {m.role === 'assistant' && <StepTrace steps={m.steps} running={m.streaming} />}
                    {m.content && (
                      <>
                        <div className={`inline-block text-left rounded-xl px-3.5 py-2.5 ${m.role === 'user'
                          ? 'whitespace-pre-wrap break-words bg-[var(--color-nav-bg)] text-[var(--color-text-inverse)] text-sm leading-relaxed'
                          : 'bg-card border border-[var(--color-border)]'}`}>
                          {m.role === 'user' ? m.content : <Md text={m.content} />}
                        </div>
                        <div className={`mt-1 flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                          <button
                            type="button"
                            onClick={() => void copyMessage(m)}
                            title={m.role === 'user' ? '复制用户消息' : '复制助手回复'}
                            aria-label={m.role === 'user' ? '复制用户消息' : '复制助手回复'}
                            className={`inline-flex h-7 items-center gap-1 rounded-md px-1.5 text-[10px] transition-colors active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${copiedMessageId === m.id
                              ? 'bg-brand-soft text-brand-ink'
                              : 'text-[var(--color-text-tertiary)] hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-secondary)]'}`}
                          >
                            {copiedMessageId === m.id ? <Check size={12} /> : <Copy size={12} />}
                            {copiedMessageId === m.id ? '已复制' : '复制'}
                          </button>
                        </div>
                      </>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </div>

        <div
          data-testid="exploration-composer-region"
          className="workspace-topology-surface relative px-4 pb-4 pt-3"
        >
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept={ATTACH_ACCEPT}
            className="hidden"
            onChange={e => void handleFiles(e.target.files)}
          />
          <div className="max-w-3xl mx-auto">
            {attachError && (
              <div className="mb-1.5 text-[11px] text-[var(--color-danger)] truncate" title={attachError}>
                {attachError}
              </div>
            )}
            {/* 澄清账本的开放堵门问题 → 点选候选值即答（定量闭环最短路径） */}
            <QuickReplies
              questions={openBlocking.slice(0, 2)}
              disabled={busy}
              onAnswer={askInChat}
              onCustom={prefillInput}
            />
            {openBlocking.length > 2 && (
              <div className="mb-1.5 text-[11px] text-[var(--color-text-tertiary)]">
                还有 {openBlocking.length - 2} 个待澄清问题，见左侧业务场景视图「澄清账本」
              </div>
            )}
            {/* 消息输入框：回形针上传的附件直接体现在上方对话流中，输入框只承载本轮消息 */}
            <div
              data-testid="exploration-composer-shell"
              className="workspace-topology-surface relative overflow-visible rounded-xl border border-brand ring-1 ring-ring transition-colors focus-within:ring-ring"
            >
              <div data-testid="exploration-composer-input" className="px-3 pb-2 pt-2.5">
                <textarea
                  ref={textareaRef}
                  value={input}
                  onChange={e => setInput(e.target.value)}
                  onKeyDown={e => {
                    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send() }
                  }}
                  rows={1}
                  placeholder="描述业务、回答澄清问题…（Enter 发送，Shift+Enter 换行）"
                  aria-label="业务探索消息"
                  data-testid="exploration-composer"
                  className="scrollbar-thin block min-h-7 w-full resize-none bg-transparent py-1 text-sm leading-5 outline-none placeholder:text-[var(--color-text-tertiary)]"
                />
              </div>

              <div
                data-testid="exploration-composer-toolbar"
                className="flex min-h-12 items-center justify-between gap-3 px-2.5 py-2"
              >
                <div className="flex min-w-0 items-center gap-1">
                  <button
                    type="button"
                    onClick={pickFiles}
                    title="上传参考资料（仅本会话可见，用于辅助澄清业务）"
                    aria-label="上传参考资料"
                    className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-brand-ink active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <Paperclip size={16} />
                  </button>
                  <button
                    type="button"
                    onClick={() => setWebSearch(value => !value)}
                    aria-pressed={webSearch}
                    data-testid="web-search-toggle"
                    title={webSearch ? '联网搜索已开启，点击关闭' : '联网搜索已关闭，点击开启'}
                    className={`inline-flex h-8 shrink-0 items-center gap-1.5 rounded-lg border px-2 text-[11px] font-medium transition-all active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${webSearch
                      ? 'border-brand-line bg-brand-soft text-brand-ink'
                      : 'border-transparent text-[var(--color-text-tertiary)] hover:border-[var(--color-border)] hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-secondary)]'}`}
                  >
                    <Globe2 size={15} />
                    <span>联网</span>
                    <span className={`h-1.5 w-1.5 rounded-full transition-colors ${webSearch ? 'bg-brand' : 'bg-[var(--color-border)]'}`} />
                  </button>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  <button
                    type="button"
                    onClick={() => void send()}
                    disabled={busy || !input.trim()}
                    title="发送消息"
                    aria-label="发送消息"
                    className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-brand text-[var(--color-text-inverse)] transition-all hover:bg-brand-deep active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1"
                  >
                    {busy ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />}
                  </button>
                  <button
                    type="button"
                    onClick={() => setShowMessageHistory(value => !value)}
                    disabled={myMessages.length === 0}
                    title="我发送的消息 · 快速跳转"
                    aria-label="查看我发送的消息"
                    aria-expanded={showMessageHistory}
                    data-testid="message-history-button"
                    className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border transition-colors active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${showMessageHistory
                      ? 'border-brand-line bg-brand-soft text-brand-ink'
                      : 'border-[var(--color-border)] text-[var(--color-text-tertiary)] hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-secondary)]'}`}
                  >
                    <List size={15} />
                  </button>
                </div>
              </div>

              {showMessageHistory && (
                <>
                  <div className="fixed inset-0 z-20" onClick={() => setShowMessageHistory(false)} />
                  <div className="absolute bottom-full right-0 z-30 mb-2 w-72 overflow-hidden rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-elevated)] shadow-lg">
                    <div className="flex items-center justify-between border-b border-[var(--color-border)] px-3 py-2">
                      <span className="text-[11px] font-medium text-[var(--color-text-secondary)]">我发送的消息</span>
                      <span className="text-[10px] text-[var(--color-text-tertiary)]">点击跳转 · 共 {myMessages.length} 条</span>
                    </div>
                    <div className="scrollbar-thin max-h-64 overflow-auto py-1">
                      {[...myMessages].reverse().map((message, index) => (
                        <button
                          type="button"
                          key={message.id}
                          onClick={() => jumpToMessage(message.id)}
                          title={message.content}
                          className="flex w-full items-start gap-2 px-3 py-1.5 text-left transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:bg-[var(--color-bg-hover)] focus-visible:outline-none"
                        >
                          <span className="mt-0.5 shrink-0 font-mono text-[10px] text-[var(--color-text-tertiary)]">#{myMessages.length - index}</span>
                          <span className="min-w-0 flex-1 truncate text-xs text-[var(--color-text-secondary)]">{message.content}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      </section>
      </div>

      {workspaceOpen && sid && (
        <FileWorkspaceDrawer
          sessionId={sid}
          files={attachments}
          onFilesChange={setAttachments}
          onClose={() => setWorkspaceOpen(false)}
        />
      )}
      {reviewDraft && (
        <DraftReviewDrawer
          draft={reviewDraft}
          onClose={() => setReviewDraft(null)}
          onOpenModelView={() => { setReviewDraft(null); updateParams({ view: 'model' }) }}
        />
      )}
      {preflightOpen && workbenchOntologyId && workbenchVersionId && (
        <TrialPreflightDialog
          open
          ontologyId={workbenchOntologyId}
          versionId={workbenchVersionId}
          readiness={readiness}
          onClose={() => setPreflightOpen(false)}
          onTrialStarted={() => {
            void queryClient.invalidateQueries({ queryKey: ['version-tree', workbenchOntologyId] })
          }}
        />
      )}
      <ConfirmModal
        open={!!deleteSessionTarget}
        onClose={() => { if (!deletingSession) setDeleteSessionTarget(null) }}
        onConfirm={() => { if (deleteSessionTarget) void removeSession(deleteSessionTarget.id) }}
        title={deleteSessionTarget ? `删除「${deleteSessionTarget.title}」？` : '删除会话？'}
        description="该会话中的对话、附件与业务画布记录将被永久删除，此操作无法撤销。"
        confirmText="删除会话"
        variant="danger"
        loading={deletingSession}
      />
    </div>
  )
}
