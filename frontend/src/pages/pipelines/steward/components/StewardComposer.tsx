import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import {
  CheckCircle2,
  ChevronDown,
  Globe2,
  List,
  Loader2,
  Paperclip,
  Search,
  Send,
  Workflow,
  X,
} from 'lucide-react'

import type {
  StewardChatMessage,
  StewardPipeline,
} from '../stewardModel'
import { filterStewardTargets } from '../stewardModel'


const ATTACH_ACCEPT = '.csv,.xlsx,.xls,.json,.xml,.pdf,.docx,.doc,.pptx,.ppt,.md,.txt'
const TEXTAREA_LINE_HEIGHT = 20
const TEXTAREA_MAX_LINES = 10
const TEXTAREA_MIN_HEIGHT = 28
const TEXTAREA_MAX_HEIGHT = TEXTAREA_LINE_HEIGHT * TEXTAREA_MAX_LINES + 8

interface StewardComposerProps {
  records: StewardPipeline[]
  recordsLoading: boolean
  selectedRecord: StewardPipeline | null
  selectedRecordId: string | null
  messages: StewardChatMessage[]
  input: string
  busy: boolean
  webSearch: boolean
  fileError: string
  n8nReady: boolean
  showMessageHistory: boolean
  onInputChange: (value: string) => void
  onSelectRecord: (recordId: string) => void
  onClearRecord: () => void
  onUploadFiles: (files: FileList | null) => boolean | Promise<boolean>
  onToggleWebSearch: () => void
  onSend: () => void | Promise<void>
  onShowMessageHistoryChange: (open: boolean) => void
}

export default function StewardComposer({
  records,
  recordsLoading,
  selectedRecord,
  selectedRecordId,
  messages,
  input,
  busy,
  webSearch,
  fileError,
  n8nReady,
  showMessageHistory,
  onInputChange,
  onSelectRecord,
  onClearRecord,
  onUploadFiles,
  onToggleWebSearch,
  onSend,
  onShowMessageHistoryChange,
}: StewardComposerProps) {
  const [targetMenuOpen, setTargetMenuOpen] = useState(false)
  const [targetSearch, setTargetSearch] = useState('')
  const targetPickerRef = useRef<HTMLDivElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const myMessages = useMemo(
    () => messages.filter(message => message.role === 'user'),
    [messages],
  )
  const filteredTargetRecords = useMemo(
    () => filterStewardTargets(records, targetSearch),
    [records, targetSearch],
  )

  useLayoutEffect(() => {
    const textarea = textareaRef.current
    if (!textarea) return
    textarea.style.height = `${TEXTAREA_MIN_HEIGHT}px`
    const contentHeight = input ? textarea.scrollHeight : TEXTAREA_MIN_HEIGHT
    textarea.style.height = `${Math.max(TEXTAREA_MIN_HEIGHT, Math.min(contentHeight, TEXTAREA_MAX_HEIGHT))}px`
    textarea.style.overflowY = contentHeight > TEXTAREA_MAX_HEIGHT ? 'auto' : 'hidden'
  }, [input])

  useEffect(() => {
    const onPointerDown = (event: MouseEvent) => {
      if (!targetPickerRef.current?.contains(event.target as globalThis.Node)) {
        setTargetMenuOpen(false)
      }
    }
    document.addEventListener('mousedown', onPointerDown)
    return () => document.removeEventListener('mousedown', onPointerDown)
  }, [])

  const jumpToMessage = (messageId: string) => {
    onShowMessageHistoryChange(false)
    requestAnimationFrame(() => {
      document.getElementById(`steward-msg-${messageId}`)?.scrollIntoView({
        behavior: 'smooth',
        block: 'center',
      })
    })
  }

  return (
    <div className="relative bg-card px-4 pb-4 pt-3">
      <input
        ref={fileInputRef}
        type="file"
        multiple
        accept={ATTACH_ACCEPT}
        className="hidden"
        onChange={event => {
          const inputElement = event.currentTarget
          void Promise.resolve(onUploadFiles(inputElement.files))
            .then(uploadCompleted => {
              if (uploadCompleted) inputElement.value = ''
            })
        }}
      />
      {fileError && (
        <div className="mb-1.5 truncate text-[11px] text-[var(--color-danger)]" title={fileError}>
          {fileError}
        </div>
      )}
      <div
        ref={targetPickerRef}
        data-testid="steward-composer-shell"
        className="relative overflow-visible rounded-xl border border-border bg-card transition-colors focus-within:ring-2 focus-within:ring-ring"
      >
        <div className="flex min-h-10 items-center gap-2 border-b border-border px-3.5 py-2">
          <span className="inline-flex shrink-0 items-center gap-1.5 text-[11px] font-medium text-muted-foreground">
            <Workflow size={12} className="text-brand-ink" /> 操作目标
          </span>
          <button
            type="button"
            disabled={busy}
            aria-haspopup="listbox"
            aria-expanded={targetMenuOpen}
            onClick={() => {
              setTargetMenuOpen(open => !open)
              setTargetSearch('')
            }}
            className={`flex min-w-0 flex-1 items-center gap-2 rounded-lg px-2 py-1 text-left text-xs transition ${
              selectedRecord
                ? 'bg-brand-soft text-brand-ink hover:bg-brand-soft'
                : 'text-[var(--color-text-tertiary)] hover:bg-muted hover:text-muted-foreground'
            } disabled:cursor-not-allowed disabled:opacity-60`}
          >
            <span className="min-w-0 flex-1 truncate">
              {selectedRecord ? selectedRecord.name : recordsLoading ? '正在加载可编排流水线…' : '选择一条可编排流水线（可选）'}
            </span>
            {!recordsLoading && (
              <span className="shrink-0 text-[10px] text-[var(--color-text-tertiary)]">
                {selectedRecord ? `${selectedRecord.summary.node_count} 个节点` : `${records.length} 条`}
              </span>
            )}
            <ChevronDown size={12} className={`shrink-0 transition-transform ${targetMenuOpen ? 'rotate-180' : ''}`} />
          </button>
          {selectedRecord && (
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                onClearRecord()
                setTargetMenuOpen(false)
                textareaRef.current?.focus()
              }}
              aria-label="清除目标流水线"
              title="清除目标流水线"
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-[var(--color-text-tertiary)] transition hover:bg-muted hover:text-foreground disabled:opacity-50"
            >
              <X size={13} />
            </button>
          )}
        </div>

        {targetMenuOpen && !busy && (
          <div className="absolute bottom-[calc(100%+8px)] left-0 right-0 z-40 overflow-hidden rounded-2xl border border-border bg-card shadow-[0_18px_52px_rgba(15,23,42,0.16)]">
            <div className="border-b border-border p-2.5">
              <div className="relative">
                <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-[var(--color-text-tertiary)]" />
                <input
                  autoFocus
                  value={targetSearch}
                  onChange={event => setTargetSearch(event.target.value)}
                  onKeyDown={event => {
                    if (event.key === 'Escape') setTargetMenuOpen(false)
                    event.stopPropagation()
                  }}
                  placeholder="搜索流水线名称或描述"
                  className="h-9 w-full rounded-xl border border-border bg-muted pl-8 pr-3 text-xs outline-none transition focus:bg-card"
                />
              </div>
            </div>
            <div role="listbox" aria-label="可编排流水线" className="max-h-64 overflow-y-auto p-1.5">
              {recordsLoading ? (
                <div className="flex items-center justify-center gap-2 py-8 text-xs text-[var(--color-text-tertiary)]">
                  <Loader2 size={13} className="animate-spin" /> 正在加载
                </div>
              ) : filteredTargetRecords.length === 0 ? (
                <div className="px-4 py-8 text-center text-xs leading-5 text-[var(--color-text-tertiary)]">
                  {records.length === 0 ? '当前没有可编排流水线，可先让数据管家新建一条。' : '没有匹配的可编排流水线。'}
                </div>
              ) : filteredTargetRecords.map(record => (
                <button
                  key={record.id}
                  type="button"
                  role="option"
                  aria-selected={selectedRecordId === record.id}
                  onClick={() => {
                    onSelectRecord(record.id)
                    setTargetMenuOpen(false)
                    setTargetSearch('')
                    textareaRef.current?.focus()
                  }}
                  className={`flex w-full items-start gap-2.5 rounded-xl px-3 py-2.5 text-left transition ${
                    selectedRecordId === record.id
                      ? 'bg-brand-soft text-brand-ink'
                      : 'text-foreground hover:bg-muted'
                  }`}
                >
                  <span className={`mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg ${
                    selectedRecordId === record.id ? 'bg-card text-brand-ink' : 'bg-muted text-muted-foreground'
                  }`}>
                    <Workflow size={13} />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-2">
                      <span className="truncate text-xs font-semibold">{record.name}</span>
                      <span className="shrink-0 text-[10px] text-[var(--color-text-tertiary)]">{record.summary.node_count} 个节点</span>
                    </span>
                    <span className="mt-0.5 block truncate text-[10px] text-[var(--color-text-tertiary)]">
                      {record.description || '暂未设置描述'}
                    </span>
                  </span>
                  {selectedRecordId === record.id && <CheckCircle2 size={14} className="mt-1 shrink-0 text-brand-ink" />}
                </button>
              ))}
            </div>
          </div>
        )}

        <div data-testid="steward-composer-input" className="px-3 pb-2 pt-2.5">
          <textarea
            ref={textareaRef}
            value={input}
            onChange={event => onInputChange(event.target.value)}
            onKeyDown={event => {
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault()
                void onSend()
              }
            }}
            rows={1}
            placeholder={n8nReady
              ? selectedRecord
                ? `告诉数据管家要如何操作「${selectedRecord.name}」…（Enter 发送，Shift+Enter 换行）`
                : '描述数据源或流水线需求…（Enter 发送，Shift+Enter 换行）'
              : '请先完成 n8n 配置'}
            disabled={busy}
            aria-label="数据管家消息"
            data-testid="steward-composer"
            className="scrollbar-thin block min-h-7 w-full resize-none bg-transparent py-1 text-sm leading-5 outline-none placeholder:text-[var(--color-text-tertiary)] disabled:opacity-50"
          />
        </div>
        <div
          data-testid="steward-composer-toolbar"
          className="flex min-h-12 items-center justify-between gap-3 px-2.5 py-2"
        >
          <div className="flex min-w-0 items-center gap-1">
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              title="上传会话附件（仅本会话可见）"
              aria-label="上传会话附件"
              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-[var(--color-text-tertiary)] transition-colors hover:bg-muted hover:text-brand-ink active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <Paperclip size={16} />
            </button>
            <button
              type="button"
              onClick={onToggleWebSearch}
              aria-pressed={webSearch}
              data-testid="steward-web-search-toggle"
              title={webSearch ? '联网搜索已开启，点击关闭' : '联网搜索已关闭，点击开启'}
              className={`inline-flex h-8 shrink-0 items-center gap-1.5 rounded-lg border px-2 text-[11px] font-medium transition-all active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${webSearch
                ? 'border-brand-line bg-brand-soft text-brand-ink'
                : 'border-transparent text-[var(--color-text-tertiary)] hover:border-border hover:bg-muted hover:text-muted-foreground'}`}
            >
              <Globe2 size={15} />
              <span>联网</span>
              <span className={`h-1.5 w-1.5 rounded-full transition-colors ${webSearch ? 'bg-brand' : 'bg-[var(--color-bg-active)]'}`} />
            </button>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              onClick={() => void onSend()}
              disabled={busy || !input.trim()}
              title="发送消息"
              aria-label="发送消息"
              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-brand text-[var(--color-text-inverse)] transition-all hover:bg-brand-deep active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1"
            >
              {busy ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />}
            </button>
            <button
              type="button"
              onClick={() => onShowMessageHistoryChange(!showMessageHistory)}
              disabled={myMessages.length === 0}
              title="我发送的消息 · 快速跳转"
              aria-label="查看我发送的消息"
              aria-expanded={showMessageHistory}
              data-testid="steward-message-history-button"
              className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border transition-colors active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${showMessageHistory
                ? 'border-brand-line bg-brand-soft text-brand-ink'
                : 'border-border text-[var(--color-text-tertiary)] hover:bg-muted hover:text-muted-foreground'}`}
            >
              <List size={15} />
            </button>
          </div>
        </div>
        {showMessageHistory && (
          <>
            <div className="fixed inset-0 z-20" onClick={() => onShowMessageHistoryChange(false)} />
            <div className="absolute bottom-full right-0 z-30 mb-2 w-72 overflow-hidden rounded-lg border border-border bg-card shadow-lg">
              <div className="flex items-center justify-between border-b border-border px-3 py-2">
                <span className="text-[11px] font-medium text-muted-foreground">我发送的消息</span>
                <span className="text-[10px] text-[var(--color-text-tertiary)]">点击跳转 · 共 {myMessages.length} 条</span>
              </div>
              <div className="scrollbar-thin max-h-64 overflow-auto py-1">
                {[...myMessages].reverse().map((message, index) => (
                  <button
                    type="button"
                    key={message.id}
                    onClick={() => jumpToMessage(message.id)}
                    title={message.content}
                    className="flex w-full items-start gap-2 px-3 py-1.5 text-left transition-colors hover:bg-muted focus-visible:bg-muted focus-visible:outline-none"
                  >
                    <span className="mt-0.5 shrink-0 font-mono text-[10px] text-[var(--color-text-tertiary)]">#{myMessages.length - index}</span>
                    <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">{message.content}</span>
                  </button>
                ))}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
