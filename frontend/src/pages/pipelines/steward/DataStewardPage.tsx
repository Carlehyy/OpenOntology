/**
 * 数据管家 — 对话式新建与编排 n8n 数据流水线
 *
 * 对 n8n 的持久写权限只有两件事：新建流水线与编排未发布未启用的流水线；
 * 用户明确要求时可触发一次隔离执行预览，执行后恢复原启停状态且不写资产湖；
 * 另可在当前会话隔离空间内创建、编辑和删除文件。
 * 左侧：与数据管家对话（create_pipeline 新建骨架、update_workflow 补全编排）
 * 右侧：可编排流水线看板（只展示未发布、未启用的 n8n 流水线）。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import {
  AlertTriangle,
  FolderOpen,
  History,
  Monitor,
  Sparkles,
} from 'lucide-react'
import {
  downloadStewardConversation, downloadStewardFile, stewardApi, streamStewardChat,
  type StewardArtifact, type StewardConversationDTO, type StewardPipeline,
  type StewardStatus, type StewardStep,
} from '@/api/steward'
import { modelApi } from '@/api/ontologies'
import pipelinesApi from '@/api/v2/pipelines'
import type { Pipeline } from '@/api/v2/pipelines'
import type { ModelConfig } from '@/types/ontology'
import { toast } from 'sonner'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import SessionHistoryPopover from '@/components/SessionHistoryPopover'
import PipelineEditWizard from '../PipelineEditWizard'
import BrowserModal, { type BrowserDisplayMode } from './components/BrowserCollaboration'
import ConversationTimeline from './components/ConversationTimeline'
import ManagedPipelinesPanel from './components/ManagedPipelinesPanel'
import StewardComposer from './components/StewardComposer'
import WorkspaceModal from './components/WorkspaceModal'
import {
  buildStewardTimeline,
  errorText,
  formatBytes,
  type StewardChatMessage,
  type StewardPendingUpload,
} from './stewardModel'

let msgSeq = 0
const nextId = () => `m${Date.now()}_${msgSeq++}`
// ---------- 主页面 ----------

export default function DataStewardPage() {
  const [searchParams] = useSearchParams()

  const [status, setStatus] = useState<StewardStatus | null>(null)
  const [messages, setMessages] = useState<StewardChatMessage[]>([])
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [conversations, setConversations] = useState<StewardConversationDTO[]>([])
  const [showHistory, setShowHistory] = useState(false)
  const [exportingConversationId, setExportingConversationId] = useState<string | null>(null)
  const [showFiles, setShowFiles] = useState(false)
  const [browserDisplay, setBrowserDisplay] = useState<BrowserDisplayMode>('closed')
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [webSearch, setWebSearch] = useState(false)
  const { data: models = [] } = useQuery<ModelConfig[]>({
    queryKey: ['models'],
    queryFn: () => modelApi.list(),
  })
  const llmModels = models.filter(model => model.config_type === 'llm' || !model.config_type)
  const [modelId, setModelId] = useState('')
  const [showMessageHistory, setShowMessageHistory] = useState(false)
  const [files, setFiles] = useState<StewardArtifact[]>([])
  const [uploads, setUploads] = useState<StewardPendingUpload[]>([])
  const [fileError, setFileError] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)
  const abortRef = useRef<AbortController | null>(null)

  const [records, setRecords] = useState<StewardPipeline[]>([])
  const [recordsLoading, setRecordsLoading] = useState(true)
  const [expandedId, setExpandedId] = useState<string | null>(searchParams.get('record'))
  const [selectedRecordId, setSelectedRecordId] = useState<string | null>(searchParams.get('record'))
  const [editTarget, setEditTarget] = useState<Pipeline | null>(null)
  const n8nApiUrl = status?.n8n?.api_url ?? ''
  // 拖拽调整对话区/审批面板宽度（仅宽屏有效）
  const [chatWidthPct, setChatWidthPct] = useState(58)
  const chatContainerRef = useRef<HTMLDivElement>(null)
  const [isWide, setIsWide] = useState(typeof window !== 'undefined' && window.innerWidth >= 1280)

  const timeline = useMemo(
    () => buildStewardTimeline(messages, files, uploads),
    [files, messages, uploads],
  )
  useEffect(() => {
    const onResize = () => setIsWide(window.innerWidth >= 1280)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])
  const startResize = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    const container = chatContainerRef.current
    if (!container) return
    const onMove = (ev: MouseEvent) => {
      const rect = container.getBoundingClientRect()
      let pct = ((ev.clientX - rect.left) / rect.width) * 100
      pct = Math.max(30, Math.min(78, pct))
      setChatWidthPct(pct)
    }
    const onUp = () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      document.body.style.userSelect = ''
      document.body.style.cursor = ''
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
    document.body.style.userSelect = 'none'
    document.body.style.cursor = 'col-resize'
  }, [])

  const loadStatus = useCallback(() => {
    stewardApi.status().then(setStatus).catch(() => setStatus(null))
  }, [])

  // silent=true 用于后台轮询：不切 loading 态、失败也不清空面板，避免闪烁/闪空
  const loadRecords = useCallback((opts?: { silent?: boolean }) => {
    if (!opts?.silent) setRecordsLoading(true)
    return stewardApi.pipelines()
      .then(res => setRecords(Array.isArray(res) ? res : []))
      .catch(() => { if (!opts?.silent) setRecords([]) })
      .finally(() => { if (!opts?.silent) setRecordsLoading(false) })
  }, [])

  const loadConversations = useCallback(() => {
    stewardApi.conversations().then(res => setConversations(Array.isArray(res) ? res : [])).catch(() => {})
  }, [])

  const loadSessionFiles = useCallback((cid: string) => {
    return stewardApi.files(cid)
      .then(res => setFiles(Array.isArray(res) ? res : []))
      .catch(() => setFiles([]))
  }, [])

  useEffect(() => {
    const timer = window.setTimeout(() => { loadStatus(); void loadRecords(); loadConversations() }, 0)
    return () => window.clearTimeout(timer)
  }, [loadStatus, loadRecords, loadConversations])
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [timeline])
  useEffect(() => () => abortRef.current?.abort(), [])
  const selectedRecord = records.find(record => record.id === selectedRecordId) || null

  useEffect(() => {
    if (selectedRecordId && !recordsLoading && !records.some(record => record.id === selectedRecordId)) {
      setSelectedRecordId(null)
    }
  }, [records, recordsLoading, selectedRecordId])

  // 受管流水线面板尽量实时：HTTP 轮询（非 WebSocket）——数据管家或流水线列表新建/
  // 改动流水线后，无需手动刷新即可反映可编排的那批。仅在标签页可见时轮询，切回时立即刷新一次。
  useEffect(() => {
    const POLL_MS = 10_000
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const tick = async () => {
      if (stopped) return
      if (document.visibilityState === 'visible') await loadRecords({ silent: true })
      if (!stopped) timer = setTimeout(tick, POLL_MS)   // 上一轮完成后再排下一轮，避免重叠
    }
    timer = setTimeout(tick, POLL_MS)
    const onVisible = () => { if (document.visibilityState === 'visible') loadRecords({ silent: true }) }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      stopped = true
      if (timer) clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [loadRecords])

  const resetChat = () => {
    abortRef.current?.abort()
    setConversationId(null)
    setMessages([])
    setFiles([])
    setUploads([])
    setFileError('')
    setShowHistory(false)
    setShowMessageHistory(false)
  }

  const ensureConversation = useCallback(async (): Promise<string> => {
    if (conversationId) return conversationId
    const conv = await stewardApi.createConversation('新数据采集会话')
    setConversationId(conv.id)
    loadConversations()
    return conv.id
  }, [conversationId, loadConversations])

  const openFiles = async () => {
    try {
      await ensureConversation()
      setShowFiles(true)
    } catch { /* 顶部功能保持安静，弹窗由后续请求展示错误 */ }
  }

  const openBrowser = async () => {
    try {
      await ensureConversation()
      setBrowserDisplay('modal')
    } catch { /* 同上 */ }
  }

  const loadConversation = async (cid: string) => {
    try {
      const conv = await stewardApi.conversation(cid)
      setConversationId(cid)
      setMessages((conv.messages || []).map(m => ({
        id: m.id, role: m.role, content: m.content, steps: m.steps || [], createdAt: m.createdAt,
      })))
      setUploads([])
      setFileError('')
      await loadSessionFiles(cid)
      setShowHistory(false)
      setShowMessageHistory(false)
    } catch { /* 忽略 */ }
  }

  const removeConversation = async (cid: string) => {
    try {
      await stewardApi.deleteConversation(cid)
      if (cid === conversationId) resetChat()
      loadConversations()
    } catch { /* 忽略 */ }
  }

  const exportConversation = async (cid: string) => {
    if (exportingConversationId) return
    const conversation = conversations.find(item => item.id === cid)
    if (!conversation) return
    setExportingConversationId(cid)
    try {
      const payload = await downloadStewardConversation(cid, conversation.title)
      toast.success('会话 JSON 已导出', { description: `已保存 ${payload.conversation.messageCount} 条完整消息及执行轨迹。` })
    } catch (error: unknown) {
      toast.error('会话导出失败', { description: errorText(error, '无法读取完整会话，请稍后重试。') })
    } finally {
      setExportingConversationId(null)
    }
  }

  const uploadFiles = async (selected: FileList | null): Promise<boolean> => {
    if (!selected || selected.length === 0) return false
    setFileError('')
    let cid: string
    try {
      cid = await ensureConversation()
    } catch (error: unknown) {
      setFileError(errorText(error, '无法创建会话，附件未上传'))
      return false
    }
    for (const file of Array.from(selected)) {
      const uid = `upload-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`
      setUploads(previous => [...previous, { uid, name: file.name, ts: Date.now() }])
      try {
        const artifact = await stewardApi.uploadFile(cid, file)
        setFiles(previous => [...previous, artifact])
      } catch (error: unknown) {
        setFileError(`「${file.name}」上传失败：${errorText(error, '无法读取文件内容')}`)
      } finally {
        setUploads(previous => previous.filter(upload => upload.uid !== uid))
      }
    }
    loadConversations()
    return true
  }

  const removeUploadedFile = async (artifactId: string) => {
    if (!conversationId) return
    setFiles(previous => previous.filter(file => file.id !== artifactId))
    try {
      await stewardApi.deleteFile(conversationId, artifactId)
    } catch (error: unknown) {
      setFileError(errorText(error, '文件移除失败，请刷新后重试'))
      void loadSessionFiles(conversationId)
    }
  }

  const downloadUploadedFile = async (file: StewardArtifact) => {
    if (!conversationId) return
    try {
      await downloadStewardFile(conversationId, file.id, file.filename)
    } catch (error: unknown) {
      setFileError(`「${file.filename}」下载失败：${errorText(error, '文件不可用')}`)
    }
  }

  const send = async (preset?: string) => {
    const text = (preset ?? input).trim()
    if (!text || busy) return
    setInput('')
    setBusy(true)

    const target = selectedRecord
    const createdAt = new Date().toISOString()
    const userMsg: StewardChatMessage = {
      id: nextId(),
      role: 'user',
      content: text,
      steps: [],
      targetName: target?.name,
      createdAt,
    }
    const botMsg: StewardChatMessage = {
      id: nextId(),
      role: 'assistant',
      content: '',
      steps: [],
      loading: true,
      createdAt,
    }
    setMessages(prev => [...prev, userMsg, botMsg])

    const patchBot = (
      patch: Partial<StewardChatMessage>
        | ((message: StewardChatMessage) => Partial<StewardChatMessage>),
    ) =>
      setMessages(prev => prev.map(m =>
        m.id === botMsg.id ? { ...m, ...(typeof patch === 'function' ? patch(m) : patch) } : m))

    const ctrl = new AbortController()
    abortRef.current = ctrl
    let touched: string[] = []
    let activeConversationId = conversationId
    try {
      await streamStewardChat(
        {
          message: text,
          conversationId,
          modelId: modelId || undefined,
          targetRecordId: target?.id,
          webSearch,
        },
        e => {
          if (e.type === 'meta') {
            activeConversationId = e.conversationId
            setConversationId(e.conversationId)
          }
          else if (e.type === 'step') {
            const step: StewardStep = {
              tool: e.tool, arguments: e.arguments, summary: e.summary,
              durationMs: e.durationMs, ...(e.error ? { error: e.error } : {}),
              ...(e.preview ? { preview: e.preview } : {}),
              ...(e.searchResults ? { searchResults: e.searchResults } : {}),
            }
            patchBot(m => ({ steps: [...m.steps, step] }))
          } else if (e.type === 'answer') {
            touched = e.touchedPipelineIds || []
            patchBot({ content: e.content, loading: false })
          } else if (e.type === 'error') {
            patchBot({ error: e.message, loading: false })
          }
        },
        ctrl.signal,
      )
    } catch (err: unknown) {
      if (!(err instanceof DOMException && err.name === 'AbortError')) {
        patchBot({ error: (err as Error)?.message || '对话失败', loading: false })
      }
    } finally {
      patchBot({ loading: false })
      setBusy(false)
      loadConversations()
      if (activeConversationId) void loadSessionFiles(activeConversationId)
      // 本回合动过流水线 → 刷新受管流水线面板并展开最近触达的一条
      if (touched.length > 0) {
        loadRecords()
        setExpandedId(touched[touched.length - 1])
      }
    }
  }

  const n8nReady = !!status && status.n8n.configured && status.n8n.enabled && status.n8n.reachable !== false

  return (
    <div className="flex h-full flex-col bg-muted">
      {/* 前置条件提示 */}
      {status && !n8nReady && (
        <div className="flex items-center gap-2 px-4 py-2.5 rounded-lg border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] text-sm text-[var(--color-warning)] shrink-0">
          <AlertTriangle size={15} className="shrink-0" />
          <span className="flex-1">
            {!status.n8n.configured ? '启动配置缺少 n8n 地址或 API Key：请通过配置中心补齐 N8N_* 并重启平台。'
              : !status.n8n.enabled ? '当前测试环境注入的 n8n 集成处于停用状态。'
              : `n8n 无法连接：${status.n8n.error || '请检查服务是否在线'}`}
          </span>
        </div>
      )}
      {status && n8nReady && !status.llmReady && (
        <div className="flex items-center gap-2 px-4 py-2.5 rounded-lg border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] text-sm text-[var(--color-warning)] shrink-0">
          <AlertTriangle size={15} className="shrink-0" />
          <span className="flex-1">尚未配置对话模型：数据管家需要一个 LLM 才能工作。</span>
          <Link to="/models"
            className="flex items-center gap-1 text-xs px-2.5 py-1 bg-card border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] rounded-lg hover:bg-[var(--color-warning-bg)] shrink-0">
            去模型配置
          </Link>
        </div>
      )}

      {/* 主体：对话 + 草稿面板（窄屏纵向堆叠） */}
      <div ref={chatContainerRef} className="flex min-h-0 flex-1 gap-0 p-1 max-xl:flex-col max-xl:gap-1">
        {/* 对话区 */}
        <section style={isWide ? { width: `${chatWidthPct}%` } : undefined}
          className="flex h-full min-h-0 min-w-0 flex-col overflow-hidden rounded-2xl border border-border bg-card shadow-[0_8px_30px_rgba(15,23,42,0.04)] max-xl:min-h-[55%] max-xl:w-full">
          {/* 抬头：标题 + 操作按钮 */}
          <div className="flex shrink-0 items-center justify-between border-b border-border px-5 py-3.5">
            <div>
              <h2 className="flex items-center gap-2 text-sm font-semibold text-foreground">
                <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-brand-deep text-[var(--color-text-inverse)]"><Sparkles size={14} /></span>
                数据管家
              </h2>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <Select
                value={modelId || '__none__'}
                onValueChange={value => setModelId(value === '__none__' ? '' : value)}
              >
                <SelectTrigger aria-label="选择数据管家对话模型" title="对话模型" className="h-8 w-fit min-w-32 cursor-pointer rounded-md bg-card px-2 text-xs">
                  <SelectValue placeholder="默认模型" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="__none__">默认模型</SelectItem>
                  {llmModels.map(model => (
                    <SelectItem key={model.id} value={model.id}>{model.name}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <button
                onClick={openFiles}
                title="查看会话文件"
                aria-label="查看会话文件"
                className="group relative inline-flex h-8 w-8 items-center justify-center rounded-md border border-brand-line bg-brand-soft text-brand-ink transition-colors hover:border-brand-line hover:bg-brand-soft active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <FolderOpen size={15} />
                {files.length > 0 && (
                  <span className="absolute -right-1.5 -top-1.5 flex min-w-4 items-center justify-center rounded-full bg-brand px-1 text-[9px] font-semibold leading-4 text-[var(--color-text-inverse)]">
                    {files.length > 99 ? '99+' : files.length}
                  </span>
                )}
              </button>
              <button
                onClick={openBrowser}
                title={browserDisplay === 'pip' ? '恢复实时浏览器大窗口' : '打开实时浏览器'}
                aria-label={browserDisplay === 'pip' ? '恢复实时浏览器大窗口' : '打开实时浏览器'}
                className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-[var(--color-info-bg)] text-[var(--color-info)] transition-colors hover:border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] hover:bg-[var(--color-info-bg)] active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <Monitor size={15} />
              </button>
              <div className="relative">
                <button
                  type="button"
                  onClick={() => {
                    if (!showHistory) loadConversations()
                    setShowHistory(value => !value)
                  }}
                  title="查看会话记录"
                  aria-label="查看会话记录"
                  aria-expanded={showHistory}
                  className={`inline-flex h-8 w-8 items-center justify-center rounded-md border transition-colors active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${showHistory
                    ? 'border-viz-violet bg-viz-violet-soft text-viz-violet'
                    : 'border-viz-violet-soft bg-viz-violet-soft text-viz-violet hover:border-viz-violet-soft hover:bg-viz-violet-soft'}`}
                >
                  <History size={15} />
                </button>
                <SessionHistoryPopover
                  open={showHistory}
                  items={conversations}
                  currentId={conversationId}
                  onClose={() => setShowHistory(false)}
                  onCreate={resetChat}
                  onSelect={loadConversation}
                  onExport={exportConversation}
                  exportingId={exportingConversationId}
                  onDelete={removeConversation}
                  renderItemIcon={() => <Sparkles size={16} />}
                  emptyDescription="新建会话后，可随时回到之前的数据采集与流水线编排过程。"
                />
              </div>
            </div>
          </div>
          <div className="scrollbar-none flex-1 overflow-y-auto px-5 py-5">
            <ConversationTimeline
              timeline={timeline}
              busy={busy}
              bottomRef={bottomRef}
              onSuggested={send}
              onDownloadFile={downloadUploadedFile}
              onRemoveFile={removeUploadedFile}
            />
          </div>

          <StewardComposer
            records={records}
            recordsLoading={recordsLoading}
            selectedRecord={selectedRecord}
            selectedRecordId={selectedRecordId}
            messages={messages}
            input={input}
            busy={busy}
            webSearch={webSearch}
            fileError={fileError}
            n8nReady={n8nReady}
            showMessageHistory={showMessageHistory}
            onInputChange={setInput}
            onSelectRecord={recordId => {
              setSelectedRecordId(recordId)
              setExpandedId(recordId)
            }}
            onClearRecord={() => setSelectedRecordId(null)}
            onUploadFiles={uploadFiles}
            onToggleWebSearch={() => setWebSearch(value => !value)}
            onSend={send}
            onShowMessageHistoryChange={setShowMessageHistory}
          />
        </section>

        {/* 拖拽分隔条（仅宽屏，拖动调整对话区与受管流水线面板宽度） */}
        <div
          onMouseDown={startResize}
          className="hidden w-1 shrink-0 cursor-col-resize items-center justify-center xl:flex"
        >
          <div className="h-16 w-1 rounded-full bg-[var(--color-bg-active)] transition-all hover:h-24 hover:bg-brand" />
        </div>

        {/* 受管流水线面板 */}
        <ManagedPipelinesPanel
          records={records}
          loading={recordsLoading}
          expandedId={expandedId}
          onExpand={setExpandedId}
          onChanged={() => { loadRecords(); loadStatus() }}
          n8nApiUrl={n8nApiUrl}
          onOpenWizard={(pipelineId: string) => {
            pipelinesApi.get(pipelineId).then(setEditTarget).catch(() => {})
          }}
        />
      </div>

      {/* 编辑向导 */}
      {editTarget && (
        <PipelineEditWizard
          pipeline={editTarget}
          onClose={() => setEditTarget(null)}
          onSaved={() => { setEditTarget(null); loadRecords(); loadStatus() }}
        />
      )}

      {showFiles && conversationId && (
        <WorkspaceModal
          conversationId={conversationId}
          onClose={() => {
            setShowFiles(false)
            void loadSessionFiles(conversationId)
          }}
          formatBytes={formatBytes}
          errorText={errorText}
        />
      )}
      {browserDisplay !== 'closed' && conversationId && (
        <BrowserModal
          key={conversationId}
          conversationId={conversationId}
          mode={browserDisplay}
          onMinimize={() => setBrowserDisplay('pip')}
          onRestore={() => setBrowserDisplay('modal')}
          onClose={() => setBrowserDisplay('closed')}
          errorText={errorText}
        />
      )}
    </div>
  )
}
