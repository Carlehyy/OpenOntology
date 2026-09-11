import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import CodeMirror from '@uiw/react-codemirror'
import { EditorView } from '@codemirror/view'
import { indentSelection } from '@codemirror/commands'
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language'
import { tags } from '@lezer/highlight'
import { python } from '@codemirror/lang-python'
import '@fontsource/jetbrains-mono/400.css'
import '@fontsource/jetbrains-mono/600.css'
import {
  ArrowLeft, Play, Square, Save, Loader2, CheckCircle2, Circle, XCircle, AlertTriangle,
  FileCode2, Terminal, RotateCcw, Keyboard, History, HelpCircle, Rocket, Wand2, Download,
} from 'lucide-react'
import pipelinesApi from '@/api/v2/pipelines'
import type { Pipeline, ScriptExecutionResult, ScriptVersion } from '@/api/v2/pipelines'
import { toast } from 'sonner'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import {
  Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle,
} from '@/components/ui/sheet'
import PipelineEditWizard from '../PipelineEditWizard'
import { PYTHON_SCRIPT_TEMPLATE } from './template'
import { inferColumnTypes, parseTracebackLines, tidyPythonSource, TYPE_LABELS } from './scriptUtils'

// 编辑器观感：1.6 行高；字体（JetBrains Mono）与聚焦虚线去除在 index.css 全局
// 覆盖（@uiw 默认主题在扩展顺序上会后置，主题扩展压不住，见 index.css 注释）
const editorTheme = EditorView.theme({
  '.cm-content': {
    lineHeight: '1.6',
  },
})

// Python 语法高亮（GitHub light 风格）：默认浅色主题对比太弱，关键字/字符串/
// 注释/数字/函数/类名用高区分度配色，读代码时一眼可辨结构。
// 注释不用斜体：编辑器字体栈不含中文斜体字形，斜体中文注释会被浏览器机械倾斜发糊
const pythonHighlight = syntaxHighlighting(HighlightStyle.define([
  { tag: tags.keyword, color: '#cf222e', fontWeight: '600' },
  { tag: [tags.string, tags.docComment], color: '#0a3069' },
  { tag: tags.comment, color: '#6e7781' },
  { tag: [tags.number, tags.bool, tags.null], color: '#0550ae', fontWeight: '600' },
  { tag: [tags.function(tags.variableName), tags.function(tags.propertyName)], color: '#8250df' },
  { tag: [tags.className, tags.definition(tags.variableName)], color: '#953800' },
  { tag: tags.propertyName, color: '#116329' },
  { tag: tags.operator, color: '#cf222e' },
  { tag: tags.escape, color: '#0550ae' },
]))

const PREVIEW_ROWS = 20

const draftKey = (pipelineId: string) => `ob:python-script-draft:${pipelineId}`
const SPLIT_KEY = 'ob:python-script-split-v1'

interface Draft {
  script: string
  updatedAt: string
}

function readDraft(pipelineId: string): Draft | null {
  try {
    const raw = localStorage.getItem(draftKey(pipelineId))
    if (!raw) return null
    const parsed = JSON.parse(raw) as Draft
    return typeof parsed?.script === 'string' ? parsed : null
  } catch {
    return null
  }
}

function cellText(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function formatClock(iso?: string): string {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  } catch {
    return iso
  }
}

function formatDateTime(iso?: string | null): string {
  if (!iso) return '-'
  try {
    return new Date(iso).toLocaleString('zh-CN', {
      month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
    })
  } catch {
    return iso
  }
}

const EMPTY_RESULT: Omit<ScriptExecutionResult, 'ok' | 'error'> = {
  format_valid: false,
  format_error: null,
  row_count: 0,
  columns: [],
  sample: [],
  stdout: '',
  traceback: '',
  duration_ms: 0,
}

export default function PythonScriptPage() {
  const { pipelineId } = useParams<{ pipelineId: string }>()
  const navigate = useNavigate()

  const [pipeline, setPipeline] = useState<Pipeline | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [script, setScript] = useState('')
  const [savedScript, setSavedScript] = useState('')
  const [executing, setExecuting] = useState(false)
  const [saving, setSaving] = useState(false)
  const [result, setResult] = useState<ScriptExecutionResult | null>(null)
  const [resultKind, setResultKind] = useState<'execute' | 'save'>('execute')
  // 客户端门槛：只有「当前编辑器内容」执行通过且格式合法，保存才可点；
  // 服务端保存时仍会重跑复验（双重保障）。
  const [validatedScript, setValidatedScript] = useState<string | null>(null)
  // 最近一次执行过的内容（无论成败），驱动「已执行当前内容」checklist 项
  const [executedScript, setExecutedScript] = useState<string | null>(null)
  const [timeoutLimit, setTimeoutLimit] = useState<number | null>(null)
  const [elapsedSec, setElapsedSec] = useState(0)
  const [draftRestoredAt, setDraftRestoredAt] = useState<string | null>(null)
  const draftTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const cancelledByUser = useRef(false)
  const editorViewRef = useRef<EditorView | null>(null)
  const [showVersions, setShowVersions] = useState(false)
  const [versions, setVersions] = useState<ScriptVersion[] | null>(null)
  const [versionsLoading, setVersionsLoading] = useState(false)
  const [showHelp, setShowHelp] = useState(false)
  const [showWizard, setShowWizard] = useState(false)
  const [showNextSteps, setShowNextSteps] = useState(false)
  const [confirm, setConfirm] = useState<
    { kind: 'revert' } | { kind: 'restore'; version: ScriptVersion } | null
  >(null)

  const [splitPct, setSplitPct] = useState(() => {
    const raw = Number(localStorage.getItem(SPLIT_KEY))
    return raw >= 30 && raw <= 70 ? raw : 50
  })
  const splitContainerRef = useRef<HTMLDivElement | null>(null)

  const isPublished = pipeline?.status === 'published'
  const wrongEngine = pipeline && (pipeline.definition as { engine?: string } | null)?.engine !== 'python'
  const dirty = script !== savedScript
  const scriptDirtySinceValidation = validatedScript === null || script !== validatedScript
  const canSave = !!pipeline && !isPublished && !executing && !saving && !scriptDirtySinceValidation && script.trim().length > 0

  const load = useCallback(async () => {
    if (!pipelineId) return
    setLoading(true)
    setLoadError('')
    try {
      const pl = await pipelinesApi.get(pipelineId)
      setPipeline(pl)
      const saved = (pl.definition as { python?: { script?: string } } | null)?.python?.script || ''
      setSavedScript(saved)
      const draft = readDraft(pipelineId)
      // 草稿优先：用户上次关闭浏览器前的未保存改动不丢
      if (!saved && !draft) {
        setScript(PYTHON_SCRIPT_TEMPLATE)
      } else if (draft && draft.script !== saved) {
        setScript(draft.script)
        setDraftRestoredAt(draft.updatedAt)
      } else {
        setScript(saved)
      }
      setValidatedScript(null)
      setExecutedScript(null)
      setResult(null)
    } catch (e: unknown) {
      const err = e as { detail?: string; message?: string }
      setLoadError(err?.detail || err?.message || '流水线加载失败')
    } finally {
      setLoading(false)
    }
  }, [pipelineId])

  useEffect(() => { load() }, [load])

  // 编辑即缓存草稿（防抖 500ms）；已发布只读不写草稿
  useEffect(() => {
    if (!pipelineId || isPublished || !dirty) return
    if (draftTimer.current) clearTimeout(draftTimer.current)
    draftTimer.current = setTimeout(() => {
      try {
        localStorage.setItem(
          draftKey(pipelineId),
          JSON.stringify({ script, updatedAt: new Date().toISOString() } satisfies Draft),
        )
      } catch { /* 存储满等异常不影响编辑 */ }
    }, 500)
    return () => { if (draftTimer.current) clearTimeout(draftTimer.current) }
  }, [script, dirty, isPublished, pipelineId])

  const clearDraft = useCallback(() => {
    if (pipelineId) localStorage.removeItem(draftKey(pipelineId))
    setDraftRestoredAt(null)
  }, [pipelineId])

  // 执行耗时读秒：长脚本执行时让用户知道「还在跑、跑了多久」
  useEffect(() => {
    if (!executing) return
    const started = Date.now()
    setElapsedSec(0)
    const timer = setInterval(() => setElapsedSec(Math.floor((Date.now() - started) / 1000)), 500)
    return () => clearInterval(timer)
  }, [executing])

  const handleExecute = useCallback(async () => {
    if (!pipeline || executing) return
    if (!script.trim()) {
      toast.warning('脚本内容为空，无法执行')
      return
    }
    const controller = new AbortController()
    abortRef.current = controller
    setExecuting(true)
    setResultKind('execute')
    try {
      const res = await pipelinesApi.executeScript(pipeline.id, script, controller.signal)
      setResult(res)
      if (res.timeout_seconds) setTimeoutLimit(res.timeout_seconds)
      setExecutedScript(script)
      setValidatedScript(res.ok && res.format_valid ? script : null)
    } catch (e: unknown) {
      if (cancelledByUser.current) {
        cancelledByUser.current = false
        return
      }
      const err = e as { detail?: string; message?: string }
      setValidatedScript(null)
      setResult({
        ...EMPTY_RESULT,
        ok: false,
        error: err?.detail || err?.message || '执行请求失败',
      })
    } finally {
      setExecuting(false)
      abortRef.current = null
    }
  }, [pipeline, executing, script])

  const handleCancel = useCallback(() => {
    if (!pipeline || !executing) return
    cancelledByUser.current = true
    abortRef.current?.abort()
    // 尽力终止内核侧执行；失败不影响前端已取消的事实
    pipelinesApi.cancelScript(pipeline.id).catch(() => {})
    toast.info('已取消本次执行')
  }, [pipeline, executing])

  const handleSave = useCallback(async () => {
    if (!pipeline || saving) return
    if (!canSave) {
      toast.warning('当前内容还不能保存', { description: '请先执行编辑器中的当前内容，并通过输出格式校验。' })
      return
    }
    setSaving(true)
    setResultKind('save')
    try {
      const res = await pipelinesApi.saveScript(pipeline.id, script)
      setPipeline(res.pipeline)
      setSavedScript(script)
      clearDraft()
      setResult(res.execution)
      if (res.execution.timeout_seconds) setTimeoutLimit(res.execution.timeout_seconds)
      setShowNextSteps(true)
      toast.success('脚本已保存', { description: `输出 ${res.execution.row_count} 行，格式校验通过。` })
    } catch (e: unknown) {
      const err = e as { detail?: string; message?: string }
      setResult({
        ...EMPTY_RESULT,
        ok: false,
        error: err?.detail || err?.message || '保存请求失败',
      })
    } finally {
      setSaving(false)
    }
  }, [pipeline, saving, canSave, script, clearDraft])

  const handleRevert = () => {
    setScript(savedScript || PYTHON_SCRIPT_TEMPLATE)
    clearDraft()
    setValidatedScript(null)
    setExecutedScript(null)
  }

  const openVersions = async () => {
    if (!pipeline) return
    setShowVersions(true)
    setVersionsLoading(true)
    try {
      const res = await pipelinesApi.scriptVersions(pipeline.id)
      setVersions(res.items)
    } catch (e: unknown) {
      const err = e as { detail?: string; message?: string }
      toast.error('历史版本加载失败', { description: err?.detail || err?.message || '请稍后重试。' })
      setShowVersions(false)
    } finally {
      setVersionsLoading(false)
    }
  }

  const handleRestoreVersion = (version: ScriptVersion) => {
    setScript(version.script)
    setValidatedScript(null)
    setExecutedScript(null)
    setShowVersions(false)
    toast.info(`已恢复 v${version.version_no} 到编辑器`, { description: '恢复后需重新执行并通过格式校验，才能保存为最新版本。' })
  }

  const handleDiscardDraft = () => {
    setScript(savedScript || PYTHON_SCRIPT_TEMPLATE)
    clearDraft()
    setValidatedScript(null)
    setExecutedScript(null)
    toast.info('已放弃草稿，恢复为已保存版本')
  }

  const jumpToLine = useCallback((lineNo: number) => {
    const view = editorViewRef.current
    if (!view) return
    const clamped = Math.max(1, Math.min(lineNo, view.state.doc.lines))
    const line = view.state.doc.line(clamped)
    view.dispatch({
      selection: { anchor: line.from, head: line.to },
      effects: EditorView.scrollIntoView(line.from, { y: 'center' }),
    })
    view.focus()
  }, [])

  // 一键格式化：语法感知自动缩进（全文）+ 空白整理（Tab→空格、行尾空白、多余空行）
  const handleFormat = useCallback(() => {
    const view = editorViewRef.current
    if (!view || isPublished) return
    view.dispatch({ selection: { anchor: 0, head: view.state.doc.length } })
    indentSelection({ state: view.state, dispatch: view.dispatch })
    const before = view.state.doc.toString()
    const tidied = tidyPythonSource(before)
    if (tidied !== before) {
      const cursor = view.state.selection.main.head
      view.dispatch({
        changes: { from: 0, to: view.state.doc.length, insert: tidied },
        selection: { anchor: Math.min(cursor, tidied.length) },
      })
    }
    view.dispatch({ selection: { anchor: view.state.selection.main.head } })
    view.focus()
    toast.success('已格式化', { description: '已自动缩进并整理空白（Tab→空格、行尾空白、多余空行）。' })
  }, [isPublished])

  // 导出当前编辑器内容为 .py 文件：固定名 + 年月日时分，便于按时间归档
  const handleExport = useCallback(() => {
    if (!script.trim()) {
      toast.warning('脚本内容为空，无可导出内容')
      return
    }
    const now = new Date()
    const pad = (n: number) => String(n).padStart(2, '0')
    const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}${pad(now.getHours())}${pad(now.getMinutes())}`
    const filename = `python_script_${stamp}.py`
    const url = URL.createObjectURL(new Blob([script], { type: 'text/x-python;charset=utf-8' }))
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = filename
    anchor.click()
    URL.revokeObjectURL(url)
    toast.success('脚本已导出', { description: filename })
  }, [script])

  // 快捷键：Ctrl/⌘+Enter 执行，Ctrl/⌘+S 保存
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return
      if (e.key === 'Enter') {
        e.preventDefault()
        handleExecute()
      } else if (e.key.toLowerCase() === 's') {
        e.preventDefault()
        handleSave()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [handleExecute, handleSave])

  const sampleColumns = useMemo(() => {
    if (!result) return []
    if (result.columns.length > 0) return result.columns
    const cols: string[] = []
    for (const row of result.sample.slice(0, PREVIEW_ROWS)) {
      for (const key of Object.keys(row)) {
        if (!cols.includes(key)) cols.push(key)
      }
    }
    return cols
  }, [result])

  const columnTypes = useMemo(
    () => (result ? inferColumnTypes(result.sample, sampleColumns) : {}),
    [result, sampleColumns],
  )

  const tracebackLines = useMemo(
    () => (result?.traceback ? parseTracebackLines(result.traceback) : []),
    [result],
  )

  const onSplitDragStart = (e: React.PointerEvent) => {
    e.preventDefault()
    const startX = e.clientX
    const startPct = splitPct
    const container = splitContainerRef.current
    const onMove = (ev: PointerEvent) => {
      if (!container) return
      const width = container.getBoundingClientRect().width
      if (width <= 0) return
      const next = startPct + ((ev.clientX - startX) / width) * 100
      setSplitPct(Math.min(70, Math.max(30, Math.round(next))))
    }
    const onUp = () => {
      window.removeEventListener('pointermove', onMove)
      setSplitPct(current => {
        localStorage.setItem(SPLIT_KEY, String(current))
        return current
      })
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp, { once: true })
  }

  if (loading) {
    return <div className="p-8 text-center text-sm text-[var(--color-text-tertiary)]">加载中...</div>
  }
  if (loadError || !pipeline) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 p-16 text-center">
        <AlertTriangle size={28} className="text-[var(--color-warning)]" />
        <p className="text-sm text-muted-foreground">{loadError || '流水线不存在'}</p>
        <button onClick={() => navigate('/data/pipelines')} className="rounded-lg border px-4 py-2 text-sm hover:bg-muted">
          返回数据流水线
        </button>
      </div>
    )
  }
  if (wrongEngine) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 p-16 text-center">
        <FileCode2 size={28} className="text-[var(--color-text-tertiary)]" />
        <p className="text-sm text-muted-foreground">「{pipeline.name}」不是 Python 脚本流水线。</p>
        <button onClick={() => navigate('/data/pipelines')} className="rounded-lg border px-4 py-2 text-sm hover:bg-muted">
          返回数据流水线
        </button>
      </div>
    )
  }

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      {/* 本体助手式布局：左右两个自包含面板 + 拖拽分隔，整页不滚动、面板内滚动 */}
      <div
        ref={splitContainerRef}
        className="scrollbar-none grid min-h-0 flex-1 overflow-x-auto overflow-y-hidden p-1"
        style={{ gridTemplateColumns: `minmax(460px, ${splitPct}fr) 10px minmax(400px, ${100 - splitPct}fr)` }}
      >
        {/* 左：脚本工作台（顶部描述信息 / 中部编辑器 / 底部操作栏） */}
        <section className="flex min-h-0 min-w-0 flex-col overflow-hidden rounded-lg border border-border bg-card shadow-sm">
          {/* 面板头：当前脚本描述信息 */}
          <div className="flex h-14 shrink-0 items-center gap-3 border-b border-border bg-card px-4">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-viz-indigo-soft text-viz-indigo">
              <FileCode2 size={16} />
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <h3 className="truncate text-sm font-semibold text-foreground">{pipeline.name}</h3>
                <span className="inline-flex shrink-0 items-center rounded border border-viz-indigo-soft bg-viz-indigo-soft px-1.5 py-0.5 text-[10px] font-medium text-viz-indigo">
                  Python 脚本
                </span>
                <span className={`shrink-0 rounded-full border px-1.5 py-0.5 text-[10px] ${
                  isPublished
                    ? 'border-brand-line bg-brand-soft text-brand-ink'
                    : 'border-border bg-muted text-muted-foreground'}`}
                >
                  {isPublished ? '已发布' : '未发布'}
                </span>
              </div>
              <p className="truncate text-[11px] text-[var(--color-text-tertiary)]">
                {pipeline.description || 'Python 脚本流水线'}
                {isPublished && ' · 脚本已封版，只读'}
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-1.5">
              {!isPublished && (
                <button
                  onClick={() => setShowWizard(true)}
                  className="flex h-8 items-center gap-1 rounded-md border border-brand-line bg-brand-soft px-2.5 text-xs font-medium text-brand-ink transition hover:border-brand-line hover:bg-brand-soft"
                  title="打开编辑向导：执行预览 → 设置字段契约 → 发布"
                >
                  <Rocket size={13} /> 发布向导
                </button>
              )}
              <div className="relative">
                <button
                  onClick={() => setShowHelp(v => !v)}
                  className={`flex h-8 w-8 items-center justify-center rounded-md border transition ${
                    showHelp
                      ? 'border-viz-indigo-soft bg-viz-indigo-soft text-viz-indigo'
                      : 'border-border text-[var(--color-text-tertiary)] hover:bg-muted hover:text-muted-foreground'}`}
                  title="输出约定、执行环境与平台限制"
                >
                  <HelpCircle size={15} />
                </button>
                {showHelp && (
                  <>
                    <div className="fixed inset-0 z-10" onClick={() => setShowHelp(false)} />
                    <div className="absolute right-0 z-20 mt-1.5 w-80 rounded-xl border border-border bg-card p-4 text-xs leading-5 text-muted-foreground shadow-xl">
                      <h4 className="mb-2 text-sm font-semibold text-foreground">输出约定与执行环境</h4>
                      <ul className="list-disc space-y-1.5 pl-4">
                        <li>最终结果赋值给 <code className="rounded bg-muted px-1 font-mono">result</code>，类型 list[dict]：每行一个 {'{列名: 值}'} 对象；pandas DataFrame 可直接赋值。</li>
                        <li>执行不落库；「保存」时平台重新执行并复验输出格式，通过才写入。</li>
                        <li>执行环境自带 requests / httpx / pandas / pymysql / openpyxl 等依赖。</li>
                        <li>单次输出上限 50,000 行{timeoutLimit ? `；执行时限 ${timeoutLimit} 秒` : ''}，超限执行会失败。</li>
                        <li>任务池声明增量游标后，脚本上下文注入 <code className="rounded bg-muted px-1 font-mono">OB_RUN_PARAMS</code>（含 cursor_column / cursor_since / full_refresh），脚本可据此只拉取新数据。</li>
                        <li>流水线发布后脚本封版只读；变更需新建流水线。</li>
                      </ul>
                    </div>
                  </>
                )}
              </div>
              <button
                onClick={() => navigate('/data/pipelines')}
                className="flex h-8 w-8 items-center justify-center rounded-md border border-border text-[var(--color-text-tertiary)] transition hover:bg-muted hover:text-muted-foreground"
                title="返回数据流水线"
              >
                <ArrowLeft size={15} />
              </button>
            </div>
          </div>

          {isPublished && (
            <div className="flex shrink-0 items-center gap-2 border-b border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-4 py-2 text-xs text-[var(--color-warning)]">
              <AlertTriangle size={13} className="shrink-0" />
              流水线已发布，脚本已封版只读；仍可执行脚本核对输出。如需变更，请新建流水线。
            </div>
          )}
          {!isPublished && draftRestoredAt && (
            <div className="flex shrink-0 items-center gap-2 border-b border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-[var(--color-info-bg)] px-4 py-2 text-xs text-[var(--color-info)]">
              <FileCode2 size={13} className="shrink-0" />
              <span className="flex-1">已恢复你上次未保存的编辑草稿（{formatClock(draftRestoredAt)}）。</span>
              <button onClick={handleDiscardDraft} className="shrink-0 rounded border border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-card px-2 py-0.5 font-medium hover:bg-[var(--color-info-bg)]">
                放弃草稿
              </button>
            </div>
          )}

          {/* 编辑器填满面板剩余高度 */}
          <div className="min-h-0 flex-1">
            <CodeMirror
              value={script}
              onChange={setScript}
              extensions={[python(), pythonHighlight, editorTheme]}
              theme="light"
              readOnly={isPublished}
              height="100%"
              placeholder="# 在此编写取数脚本，最终结果赋值给 result"
              onCreateEditor={view => { editorViewRef.current = view }}
              basicSetup={{
                lineNumbers: true,
                foldGutter: true,
                autocompletion: true,
                bracketMatching: true,
                closeBrackets: true,
                indentOnInput: true,
                highlightActiveLine: true,
                highlightActiveLineGutter: true,
              }}
              style={{
                fontSize: '13.5px',
                height: '100%',
                backgroundColor: isPublished ? '#f8fafc' : undefined,
              }}
            />
          </div>

          {/* 底部操作栏 */}
          <div className="flex shrink-0 flex-wrap items-center gap-2 border-t border-border bg-card px-4 py-2.5">
            {executing ? (
              <button
                onClick={handleCancel}
                className="flex items-center gap-1.5 rounded-xl border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-3.5 py-1.5 text-sm font-medium text-[var(--color-danger)] transition hover:bg-[var(--color-danger-bg)]"
                title="取消本次执行（内核侧将终止）"
              >
                <Square size={13} /> 取消
              </button>
            ) : (
              <button
                onClick={handleExecute}
                disabled={saving}
                className="flex items-center gap-1.5 rounded-xl bg-brand-deep px-4 py-1.5 text-sm font-medium text-[var(--color-text-inverse)] transition hover:bg-brand-deep active:translate-y-px disabled:opacity-50"
                title="在内核中真实执行编辑器当前内容（⌘/Ctrl+Enter）"
              >
                <Play size={14} /> 执行
              </button>
            )}
            {!isPublished && (
              <>
                <button
                  onClick={handleSave}
                  disabled={!canSave}
                  title={canSave ? '保存脚本（⌘/Ctrl+S；平台会重新执行并复验输出格式）' : '保存前请先通过上方三项检查'}
                  className="flex items-center gap-1.5 rounded-xl border border-brand px-4 py-1.5 text-sm font-medium text-brand-ink transition hover:bg-brand-soft active:translate-y-px disabled:cursor-not-allowed disabled:border-border disabled:text-[var(--color-text-tertiary)]"
                >
                  {saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
                  {saving ? '校验并保存中...' : '保存'}
                </button>
                {dirty && (
                  <button
                    onClick={() => setConfirm({ kind: 'revert' })}
                    disabled={executing || saving}
                    className="flex items-center gap-1.5 rounded-xl border border-border px-3 py-1.5 text-sm text-muted-foreground transition hover:bg-muted disabled:opacity-50"
                    title="放弃当前修改，回退到最近一次保存的脚本"
                  >
                    <RotateCcw size={13} /> 放弃修改
                  </button>
                )}
                <button
                  onClick={handleFormat}
                  disabled={executing || saving}
                  className="flex items-center gap-1.5 rounded-xl border border-border px-3 py-1.5 text-sm text-muted-foreground transition hover:bg-muted disabled:opacity-50"
                  title="一键格式化：语法感知自动缩进 + 整理空白（Tab→空格、行尾空白、多余空行）"
                >
                  <Wand2 size={13} /> 格式化
                </button>
              </>
            )}
            <button
              onClick={openVersions}
              disabled={executing || saving}
              className="flex items-center gap-1.5 rounded-xl border border-border px-3 py-1.5 text-sm text-muted-foreground transition hover:bg-muted disabled:opacity-50"
              title="查看脚本的保存历史，可查看或恢复任一版本"
            >
              <History size={13} /> 历史版本
            </button>
            <button
              onClick={handleExport}
              disabled={executing || saving}
              className="flex items-center gap-1.5 rounded-xl border border-border px-3 py-1.5 text-sm text-muted-foreground transition hover:bg-muted disabled:opacity-50"
              title="导出当前脚本为 .py 文件（文件名含时间戳，便于归档）"
            >
              <Download size={13} /> 导出脚本
            </button>
            <span className="text-[11px] text-[var(--color-text-tertiary)]">
              {dirty
                ? <span className="flex items-center gap-1 text-[var(--color-warning)]"><span className="inline-block h-1.5 w-1.5 rounded-full bg-[var(--color-warning)]" />有未保存修改 · 草稿已自动缓存</span>
                : savedScript
                  ? `最近保存时间 · ${formatClock(pipeline.updated_at ?? undefined)}`
                  : '尚未保存'}
            </span>
            <span className="ml-auto hidden items-center gap-2 text-[11px] text-[var(--color-text-tertiary)] lg:flex">
              <Keyboard size={12} />
              <span><kbd className="rounded border border-border bg-muted px-1 font-mono">⌘/Ctrl+Enter</kbd> 执行</span>
              <span><kbd className="rounded border border-border bg-muted px-1 font-mono">⌘/Ctrl+S</kbd> 保存</span>
              <span className="font-mono">{script.split('\n').length} 行</span>
            </span>
          </div>
        </section>

        {/* 分栏拖拽手柄 */}
        <div
          role="separator"
          aria-orientation="vertical"
          aria-label="调整编辑器与结果区分栏比例"
          onPointerDown={onSplitDragStart}
          className="group flex h-full cursor-col-resize items-center justify-center"
        >
          <div className="h-16 w-1 rounded-full bg-[var(--color-bg-active)] transition group-hover:bg-brand group-active:bg-brand" />
        </div>

        {/* 右：脚本输出面板 */}
        <section className="flex min-h-0 min-w-0 flex-col overflow-hidden rounded-lg border border-border bg-card shadow-sm">
          <div className="flex h-14 shrink-0 items-center gap-3 border-b border-border bg-card px-4">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-brand-soft text-brand-ink">
              <Terminal size={16} />
            </div>
            <div className="min-w-0 flex-1">
              <h3 className="truncate text-sm font-semibold text-foreground">脚本输出</h3>
              <p className="truncate text-[11px] text-[var(--color-text-tertiary)]">执行结果、格式校验与数据样本</p>
            </div>
            {executing && (
              <span className="shrink-0 font-mono text-[11px] tabular-nums text-[var(--color-text-tertiary)]">
                已执行 {elapsedSec}s{timeoutLimit ? ` / ${timeoutLimit}s` : ''}
              </span>
            )}
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto bg-muted p-3">
            <ResultPanel
              result={result}
              resultKind={resultKind}
              executing={executing}
              saving={saving}
              elapsedSec={elapsedSec}
              timeoutLimit={timeoutLimit}
              onCancel={handleCancel}
              sampleColumns={sampleColumns}
              columnTypes={columnTypes}
              tracebackLines={tracebackLines}
              onJumpToLine={jumpToLine}
              nonEmpty={script.trim().length > 0}
              executedCurrent={executedScript === script && script.trim().length > 0}
              nextStep={
                isPublished
                  ? null
                  : showNextSteps && resultKind === 'save' && result?.ok && result.format_valid
                    ? 'publish'
                    : result?.ok && result.format_valid && dirty
                      ? 'save'
                      : null
              }
              onOpenWizard={() => setShowWizard(true)}
              onDismissNextSteps={() => setShowNextSteps(false)}
            />
          </div>
        </section>
      </div>

      {/* 历史版本抽屉 */}
      <VersionsDrawer
        open={showVersions}
        versions={versions}
        loading={versionsLoading}
        readOnly={!!isPublished}
        onClose={() => setShowVersions(false)}
        onRestore={version => {
          // 先关抽屉再弹确认：避免 Radix modal 的指针事件锁覆盖确认框
          setShowVersions(false)
          setConfirm({ kind: 'restore', version })
        }}
      />

      {/* 发布向导 */}
      {showWizard && (
        <PipelineEditWizard
          pipeline={pipeline}
          onClose={() => setShowWizard(false)}
          onSaved={updated => {
            setShowWizard(false)
            setPipeline(updated)
          }}
        />
      )}

      {/* 二次确认 */}
      <ConfirmDialog
        open={confirm !== null}
        title={confirm?.kind === 'restore' ? `恢复 v${confirm.version.version_no} 到编辑器` : '放弃修改'}
        description={
          confirm?.kind === 'restore'
            ? '当前编辑器中的内容将被该版本覆盖，未保存的修改会丢失；恢复后需重新执行校验才能保存。'
            : '将丢弃当前未保存的修改，回退到最近一次保存的脚本，草稿缓存一并清除。'
        }
        confirmText={confirm?.kind === 'restore' ? '恢复' : '放弃修改'}
        variant="danger"
        onConfirm={() => {
          if (confirm?.kind === 'restore') handleRestoreVersion(confirm.version)
          if (confirm?.kind === 'revert') handleRevert()
          setConfirm(null)
        }}
        onClose={() => setConfirm(null)}
      />
    </div>
  )
}

function CheckItem({ label, tone }: { label: string; tone: 'pass' | 'fail' | 'warn' | 'idle' }) {
  const icon = tone === 'pass'
    ? <CheckCircle2 size={12} className="text-brand-ink" />
    : tone === 'fail'
      ? <XCircle size={12} className="text-[var(--color-danger)]" />
      : tone === 'warn'
        ? <AlertTriangle size={12} className="text-[var(--color-warning)]" />
        : <Circle size={12} />
  const color = tone === 'pass'
    ? 'text-brand-ink'
    : tone === 'fail'
      ? 'text-[var(--color-danger)]'
      : tone === 'warn'
        ? 'text-[var(--color-warning)]'
        : 'text-[var(--color-text-tertiary)]'
  return (
    <span className={`inline-flex items-center gap-1 text-[11px] ${color}`}>
      {icon}
      {label}
    </span>
  )
}

function VersionsDrawer({
  open,
  versions,
  loading,
  readOnly,
  onClose,
  onRestore,
}: {
  open: boolean
  versions: ScriptVersion[] | null
  loading: boolean
  readOnly: boolean
  onClose: () => void
  onRestore: (version: ScriptVersion) => void
}) {
  const [expandedId, setExpandedId] = useState<string | null>(null)

  return (
    <Sheet open={open} onOpenChange={value => { if (!value) onClose() }}>
      <SheetContent aria-label="脚本历史版本">
        <SheetHeader>
          <SheetTitle>脚本历史版本</SheetTitle>
          <SheetDescription>
            每次保存冻结一版，最多保留最近 20 版；恢复后需重新执行校验才能保存
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 overflow-y-auto p-4">
          {loading ? (
            <div className="flex items-center justify-center gap-2 py-16 text-sm text-[var(--color-text-tertiary)]">
              <Loader2 size={15} className="animate-spin" /> 加载中...
            </div>
          ) : !versions || versions.length === 0 ? (
            <div className="flex flex-col items-center gap-2 py-16 text-center">
              <History size={26} className="text-[var(--color-text-tertiary)]" />
              <p className="text-sm text-muted-foreground">还没有保存历史</p>
              <p className="text-xs text-[var(--color-text-tertiary)]">第一次保存脚本后会在这里生成版本记录</p>
            </div>
          ) : (
            <div className="space-y-2">
              {versions.map((version, index) => (
                <div key={version.id} className="rounded-xl border border-border bg-card">
                  <div className="flex items-center gap-2 px-3.5 py-2.5">
                    <span className={`inline-flex h-6 min-w-10 items-center justify-center rounded-lg px-2 font-mono text-xs font-semibold ${
                      index === 0 ? 'bg-brand-soft text-brand-ink' : 'bg-muted text-muted-foreground'}`}
                    >
                      v{version.version_no}
                    </span>
                    {index === 0 && (
                      <span className="rounded border border-brand-line bg-brand-soft px-1.5 py-0.5 text-[10px] font-medium text-brand-ink">当前</span>
                    )}
                    <span className="text-xs text-muted-foreground">{formatDateTime(version.created_at)}</span>
                    <span className="ml-auto text-[11px] text-[var(--color-text-tertiary)]">
                      {version.row_count.toLocaleString()} 行 · {version.output_columns.length} 列 · {(version.duration_ms / 1000).toFixed(1)}s
                    </span>
                    <button
                      onClick={() => setExpandedId(expandedId === version.id ? null : version.id)}
                      className="rounded-lg border border-border px-2 py-1 text-[11px] text-muted-foreground transition hover:bg-muted"
                    >
                      {expandedId === version.id ? '收起' : '查看'}
                    </button>
                    {!readOnly && (
                      <button
                        onClick={() => onRestore(version)}
                        className="rounded-lg border border-viz-indigo-soft bg-viz-indigo-soft px-2 py-1 text-[11px] font-medium text-viz-indigo transition hover:bg-viz-indigo-soft"
                      >
                        恢复到编辑器
                      </button>
                    )}
                  </div>
                  {expandedId === version.id && (
                    <pre className="max-h-72 overflow-auto border-t border-border bg-muted p-3.5 font-mono text-[11px] leading-5 text-foreground">{version.script}</pre>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}

function ResultPanel({
  result,
  resultKind,
  executing,
  saving,
  elapsedSec,
  timeoutLimit,
  onCancel,
  sampleColumns,
  columnTypes,
  tracebackLines,
  onJumpToLine,
  nonEmpty,
  executedCurrent,
  nextStep,
  onOpenWizard,
  onDismissNextSteps,
}: {
  result: ScriptExecutionResult | null
  resultKind: 'execute' | 'save'
  executing: boolean
  saving: boolean
  elapsedSec: number
  timeoutLimit: number | null
  onCancel: () => void
  sampleColumns: string[]
  columnTypes: Record<string, string>
  tracebackLines: number[]
  onJumpToLine: (line: number) => void
  nonEmpty: boolean
  executedCurrent: boolean
  nextStep: 'save' | 'publish' | null
  onOpenWizard: () => void
  onDismissNextSteps: () => void
}) {
  if (executing || saving) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 rounded-xl border border-border bg-card p-10 text-sm text-muted-foreground">
        <div className="flex items-center gap-3">
          <Loader2 size={16} className="animate-spin text-brand-ink" />
          {executing ? '正在内核中执行脚本，请稍候…' : '正在重新执行并校验输出格式…'}
        </div>
        {executing && (
          <div className="flex items-center gap-3 text-xs text-[var(--color-text-tertiary)]">
            <span className="font-mono tabular-nums">
              已执行 {elapsedSec}s{timeoutLimit ? ` / 上限 ${timeoutLimit}s` : ''}
            </span>
            <button
              onClick={onCancel}
              className="flex items-center gap-1 rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] px-2 py-0.5 text-[var(--color-danger)] transition hover:bg-[var(--color-danger-bg)]"
            >
              <Square size={11} /> 取消
            </button>
          </div>
        )}
      </div>
    )
  }

  const success = !!result && result.ok && result.format_valid
  const formatTone: 'pass' | 'fail' | 'warn' | 'idle' = !result
    ? 'idle'
    : !result.ok
      ? 'fail'
      : result.format_valid
        ? 'pass'
        : 'warn'

  // 列类型概览：第 4 张指标卡——列数之外回答「这些列是什么类型」，
  // 直接为发布向导的字段契约预热
  const typeCounts = new Map<string, number>()
  for (const col of sampleColumns) {
    const type = columnTypes[col]
    if (type) typeCounts.set(type, (typeCounts.get(type) ?? 0) + 1)
  }
  const typeBreakdown = [...typeCounts.entries()]
    .map(([type, count]) => `${TYPE_LABELS[type as keyof typeof TYPE_LABELS] ?? type} ${count}`)
    .join(' · ')

  return (
    <div className="space-y-3">
      {/* 1. 当前脚本校验结果 */}
      <div className="space-y-2.5 rounded-xl border border-border bg-card p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h4 className="text-xs font-semibold text-foreground">当前脚本校验结果</h4>
          {!result ? (
            <span className="inline-flex items-center gap-1 rounded-lg border border-border bg-muted px-2.5 py-1 text-xs text-[var(--color-text-tertiary)]">
              <Circle size={12} /> 尚未执行
            </span>
          ) : success ? (
            <span className="inline-flex items-center gap-1 rounded-lg border border-brand-line bg-brand-soft px-2.5 py-1 text-xs font-medium text-brand-ink">
              <CheckCircle2 size={12} />
              {resultKind === 'save' ? '保存成功' : '执行成功'} · 输出格式校验通过
            </span>
          ) : result.ok ? (
            <span className="inline-flex items-center gap-1 rounded-lg border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-2.5 py-1 text-xs font-medium text-[var(--color-warning)]">
              <AlertTriangle size={12} /> 执行成功，但输出格式不符合平台要求
            </span>
          ) : (
            <span className="inline-flex items-center gap-1 rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-2.5 py-1 text-xs font-medium text-[var(--color-danger)]">
              <XCircle size={12} /> {resultKind === 'save' ? '保存失败' : '执行失败'}
            </span>
          )}
        </div>
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          <CheckItem tone={nonEmpty ? 'pass' : 'idle'} label="脚本非空" />
          <CheckItem tone={executedCurrent ? 'pass' : 'idle'} label="已执行当前内容" />
          <CheckItem tone={formatTone} label="输出格式符合规范" />
        </div>
        {result?.error && (
          <p className="rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-3 py-2 text-xs text-[var(--color-danger)]">{result.error}</p>
        )}
        {result?.traceback && (
          <div className="overflow-hidden rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)]">
            {tracebackLines.length > 0 && (
              <div className="flex flex-wrap items-center gap-1.5 border-b border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-3 py-2">
                <span className="text-[11px] text-[var(--color-danger)]">定位到编辑器：</span>
                {tracebackLines.map(line => (
                  <button
                    key={line}
                    onClick={() => onJumpToLine(line)}
                    className="rounded border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-card px-1.5 py-0.5 font-mono text-[11px] text-[var(--color-danger)] transition hover:bg-[var(--color-danger-bg)]"
                    title="跳转到脚本对应行"
                  >
                    第 {line} 行
                  </button>
                ))}
              </div>
            )}
            <pre className="max-h-56 overflow-auto bg-[var(--color-danger-bg)] p-3.5 font-mono text-[11px] leading-5 text-[var(--color-danger)]">{result.traceback}</pre>
          </div>
        )}
        {result?.format_error && (
          <p className="rounded-lg border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-3 py-2 text-xs text-[var(--color-warning)]">{result.format_error}</p>
        )}
      </div>

      {/* 2. 下一步操作 */}
      {nextStep === 'publish' && (
        <div className="flex flex-wrap items-center gap-2 rounded-xl border border-brand-line bg-brand-soft px-3 py-2.5">
          <Rocket size={14} className="shrink-0 text-brand-ink" />
          <p className="flex-1 text-xs leading-5 text-brand-ink">
            脚本已保存。<span className="font-medium">下一步：完成执行预览与字段契约并发布</span>，流水线才能被任务池调度运行。
          </p>
          <button
            onClick={onOpenWizard}
            className="rounded-lg bg-brand-deep px-3 py-1.5 text-xs font-medium text-[var(--color-text-inverse)] transition hover:bg-brand-deep"
          >
            前往发布向导
          </button>
          <button
            onClick={onDismissNextSteps}
            className="rounded-lg border border-brand-line px-2 py-1.5 text-xs text-brand-ink transition hover:bg-brand-soft"
          >
            知道了
          </button>
        </div>
      )}
      {nextStep === 'save' && (
        <div className="flex items-center gap-2 rounded-xl border border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-[var(--color-info-bg)] px-3 py-2.5 text-xs text-[var(--color-info)]">
          <Save size={13} className="shrink-0" />
          下一步：保存脚本（保存时平台会重新执行并复验输出格式），随后前往发布向导完成发布。
        </div>
      )}

      {/* 3. 执行数据：行数 / 列数 / 耗时 / 列类型，四卡横排 */}
      {result?.ok && (
        <div className="grid grid-cols-4 gap-2">
          {[
            { label: '输出行数', value: result.row_count.toLocaleString(), sub: '' },
            { label: '输出列数', value: String(result.columns.length), sub: '' },
            { label: '执行耗时', value: `${(result.duration_ms / 1000).toFixed(1)}s`, sub: '' },
            {
              label: '列类型',
              value: sampleColumns.length > 0 ? `${typeCounts.size} 种` : '-',
              sub: typeBreakdown,
            },
          ].map(item => (
            <div key={item.label} className="rounded-xl border border-border bg-card px-3 py-2" title={item.sub || undefined}>
              <div className="text-[11px] text-[var(--color-text-tertiary)]">{item.label}</div>
              <div className="truncate font-mono text-lg font-semibold tabular-nums text-foreground">{item.value}</div>
              {item.sub && <div className="truncate text-[10px] text-[var(--color-text-tertiary)]">{item.sub}</div>}
            </div>
          ))}
        </div>
      )}

      {/* 4. 数据样本：表头带推断类型（与字段契约词表一致，为发布第 3 步预热） */}
      {result?.ok && result.sample.length > 0 && (
        <div className="overflow-x-auto rounded-xl border border-border bg-card">
          <table className="w-full text-xs">
            <thead className="border-b border-border bg-muted">
              <tr>
                {sampleColumns.map(col => (
                  <th key={col} className="whitespace-nowrap px-3 py-2 text-left font-medium text-muted-foreground">
                    <span className="font-mono">{col}</span>
                    <span className="ml-1.5 rounded bg-muted px-1 py-0.5 text-[10px] font-normal text-[var(--color-text-tertiary)]">
                      {TYPE_LABELS[(columnTypes[col] as keyof typeof TYPE_LABELS) ?? 'string']}
                    </span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y">
              {result.sample.slice(0, PREVIEW_ROWS).map((row, idx) => (
                <tr key={idx} className="transition-colors hover:bg-muted">
                  {sampleColumns.map(col => (
                    <td key={col} className="max-w-[240px] truncate px-3 py-1.5 font-mono text-foreground" title={cellText(row[col])}>
                      {cellText(row[col])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          {result.row_count > PREVIEW_ROWS && (
            <p className="border-t border-border px-3 py-1.5 text-[11px] text-[var(--color-text-tertiary)]">
              仅展示前 {PREVIEW_ROWS} 行样本，共 {result.row_count.toLocaleString()} 行
            </p>
          )}
        </div>
      )}

      {result?.ok && result.row_count === 0 && (
        <p className="text-xs text-[var(--color-text-tertiary)]">脚本输出 0 行。</p>
      )}

      {/* 5. 脚本打印输出：默认折叠，展开可见全部内容 */}
      {result?.stdout && (
        <details className="rounded-xl border border-border bg-card">
          <summary className="flex cursor-pointer items-center gap-1.5 px-3 py-2 text-xs text-muted-foreground transition hover:text-foreground">
            <Terminal size={12} /> 脚本打印输出（stdout，{result.stdout.split('\n').length} 行）
          </summary>
          <pre className="max-h-72 overflow-auto border-t border-border bg-muted p-3.5 font-mono text-[11px] leading-5 text-foreground">{result.stdout}</pre>
        </details>
      )}

      {!result && (
        <div className="flex flex-col items-center gap-2 rounded-xl border-2 border-dashed border-border bg-card p-10 text-center">
          <Play size={26} className="text-[var(--color-text-tertiary)]" />
          <p className="text-sm font-medium text-muted-foreground">点击「执行」查看脚本输出</p>
          <p className="max-w-md text-xs leading-5 text-[var(--color-text-tertiary)]">
            这里会展示行数、列结构（含推断类型）、数据样本与打印输出。
          </p>
        </div>
      )}
    </div>
  )
}
