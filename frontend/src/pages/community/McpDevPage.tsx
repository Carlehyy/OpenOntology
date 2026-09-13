import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import CodeMirror from '@uiw/react-codemirror'
import { EditorView } from '@codemirror/view'
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language'
import { tags } from '@lezer/highlight'
import { python } from '@codemirror/lang-python'
import '@fontsource/jetbrains-mono/400.css'
import '@fontsource/jetbrains-mono/600.css'
import {
  AlertCircle,
  ArrowLeft,
  CheckCircle2,
  Code2,
  FileCode2,
  HelpCircle,
  History,
  Loader2,
  Play,
  Rocket,
  RotateCcw,
  Save,
  ScanSearch,
  Terminal,
  XCircle,
} from 'lucide-react'
import { toast } from 'sonner'
import { mcpDevApi } from '@/api/community'
import type {
  McpDevExecuteResult,
  McpDevProjectDetail,
  McpDevToolManifestEntry,
  McpDevVersion,
  McpDevVersionDetail,
} from '@/api/community'
import { apiError } from '@/api/worldModel'
import { formatDateTime } from '@/utils/datetime'
import { validateJsonObject } from '@/utils/jsonInput'
import { Modal } from '@/components/ui/Modal'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import {
  Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle,
} from '@/components/ui/sheet'

// 与推演服务 / 数据通道 Python 编辑页一致的编辑器观感（GitHub light 高亮）
const editorTheme = EditorView.theme({
  '.cm-content': {
    lineHeight: '1.6',
  },
})

const pythonHighlight = syntaxHighlighting(HighlightStyle.define([
  { tag: tags.keyword, color: 'var(--color-syntax-keyword)', fontWeight: '600' },
  { tag: [tags.string, tags.docComment], color: 'var(--color-syntax-string)' },
  { tag: tags.comment, color: 'var(--color-syntax-comment)' },
  { tag: [tags.number, tags.bool, tags.null], color: 'var(--color-syntax-number)' },
  { tag: [tags.function(tags.variableName), tags.function(tags.propertyName)], color: 'var(--color-syntax-function)' },
  { tag: [tags.className, tags.definition(tags.variableName)], color: 'var(--color-syntax-class)' },
  { tag: tags.propertyName, color: 'var(--color-syntax-property)' },
  { tag: tags.operator, color: 'var(--color-syntax-keyword)' },
  { tag: tags.escape, color: 'var(--color-syntax-number)' },
]))

const draftKey = (projectId: string) => `ob:mcp-dev-draft:${projectId}`

interface Draft {
  script: string
  updatedAt: string
}

function readDraft(projectId: string): Draft | null {
  try {
    const raw = localStorage.getItem(draftKey(projectId))
    if (!raw) return null
    const parsed = JSON.parse(raw) as Draft
    return typeof parsed?.script === 'string' ? parsed : null
  } catch {
    return null
  }
}

const schemaPlaceholder = (schema: any): unknown => {
  if (!schema || typeof schema !== 'object') return ''
  if ('default' in schema) return schema.default
  switch (schema.type) {
    case 'object': return {}
    case 'array': return []
    case 'integer':
    case 'number': return 0
    case 'boolean': return false
    default: return ''
  }
}

const argumentsTemplate = (tool: McpDevToolManifestEntry): string => {
  const properties = (tool.input_schema as { properties?: Record<string, unknown> } | undefined)?.properties || {}
  return JSON.stringify(
    Object.fromEntries(
      Object.entries(properties).map(([key, schema]) => [key, schemaPlaceholder(schema)]),
    ),
    null,
    2,
  )
}

export default function McpDevPage() {
  const { projectId } = useParams<{ projectId: string }>()
  const navigate = useNavigate()

  const [project, setProject] = useState<McpDevProjectDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [script, setScript] = useState('')
  const [savedScript, setSavedScript] = useState('')
  const [tools, setTools] = useState<McpDevToolManifestEntry[]>([])
  const [selectedTool, setSelectedTool] = useState<string | null>(null)
  const [argsText, setArgsText] = useState('')
  const [executing, setExecuting] = useState(false)
  const [saving, setSaving] = useState(false)
  const [result, setResult] = useState<McpDevExecuteResult | null>(null)
  // 客户端门槛：当前脚本内容解析通过（工具清单可导出），保存才可点；
  // 服务端保存时仍会重新内省复核（双重保障）
  const [validatedKey, setValidatedKey] = useState<string | null>(null)
  const [draftRestoredAt, setDraftRestoredAt] = useState<string | null>(null)
  const draftTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const [showVersions, setShowVersions] = useState(false)
  const [versions, setVersions] = useState<McpDevVersion[] | null>(null)
  const [versionsLoading, setVersionsLoading] = useState(false)
  const [showHelp, setShowHelp] = useState(false)
  const [confirmRevert, setConfirmRevert] = useState(false)
  const [confirmRestoreVersionNo, setConfirmRestoreVersionNo] = useState<number | null>(null)
  // 版本抽屉内懒加载详情
  const [previewVersionNo, setPreviewVersionNo] = useState<number | null>(null)
  const [versionDetails, setVersionDetails] = useState<Record<number, McpDevVersionDetail | 'error'>>({})
  // 发布对话框
  const [publishOpen, setPublishOpen] = useState(false)
  const [publishVersions, setPublishVersions] = useState<McpDevVersion[]>([])
  const [publishVersionNo, setPublishVersionNo] = useState<number | null>(null)
  const [publishDisplayName, setPublishDisplayName] = useState('')
  const [publishDescription, setPublishDescription] = useState('')
  const [publishing, setPublishing] = useState(false)

  const dirty = script !== savedScript
  const canSave = dirty && !executing && !saving && validatedKey === script
  const selectedToolMeta = useMemo(
    () => tools.find(tool => tool.name === selectedTool) ?? null,
    [tools, selectedTool],
  )
  const argsStatus = useMemo(
    () => {
      const validation = validateJsonObject(argsText)
      return validation.issue
        ? { ok: false, message: validation.issue.message }
        : { ok: true, message: '' }
    },
    [argsText],
  )

  const selectTool = useCallback((tool: McpDevToolManifestEntry) => {
    setSelectedTool(tool.name)
    const sample = project?.tool_samples?.[tool.name]
    setArgsText(sample ? JSON.stringify(sample, null, 2) : argumentsTemplate(tool))
    setResult(null)
  }, [project])

  const applyProject = useCallback((detail: McpDevProjectDetail, initial: boolean) => {
    setProject(detail)
    if (initial) {
      const draft = projectId ? readDraft(projectId) : null
      if (draft && draft.script !== detail.script) {
        setScript(draft.script)
        setDraftRestoredAt(draft.updatedAt)
      } else {
        setScript(detail.script)
      }
      setSavedScript(detail.script)
    }
  }, [projectId])

  useEffect(() => {
    if (!projectId) return
    setLoading(true)
    mcpDevApi.getProject(projectId)
      .then(detail => {
        applyProject(detail, true)
        // 工具清单与样例参数从最近保存版本恢复（不触发执行）
        mcpDevApi.listVersions(projectId)
          .then(rows => {
            if (!rows.length) return
            return mcpDevApi.getVersion(projectId, rows[0].version_no)
          })
          .then(version => {
            if (!version) return
            setTools(version.tool_manifest)
            const first = version.tool_manifest[0]
            if (first) {
              setSelectedTool(first.name)
              const sample = version.tool_samples?.[first.name]
              setArgsText(sample ? JSON.stringify(sample, null, 2) : argumentsTemplate(first))
            }
          })
          .catch(() => undefined)
      })
      .catch(error => setLoadError(apiError(error)))
      .finally(() => setLoading(false))
  }, [projectId, applyProject])

  // 草稿自动保存（防抖 800ms，与推演服务同纪律）
  useEffect(() => {
    if (!projectId || loading) return
    if (draftTimer.current) clearTimeout(draftTimer.current)
    draftTimer.current = setTimeout(() => {
      if (script !== savedScript) {
        localStorage.setItem(draftKey(projectId), JSON.stringify({ script, updatedAt: new Date().toISOString() }))
      } else {
        localStorage.removeItem(draftKey(projectId))
      }
    }, 800)
    return () => { if (draftTimer.current) clearTimeout(draftTimer.current) }
  }, [script, savedScript, projectId, loading])

  const introspect = useCallback(async () => {
    if (!projectId || executing) return
    setExecuting(true)
    setResult(null)
    const controller = new AbortController()
    abortRef.current = controller
    try {
      const execution = await mcpDevApi.execute(projectId, { script }, controller.signal)
      setResult(execution)
      if (execution.ok) {
        setValidatedKey(script)
        const parsed = execution.tools ?? []
        setTools(parsed)
        setSelectedTool(current => {
          if (current && parsed.some(tool => tool.name === current)) return current
          return parsed[0]?.name ?? null
        })
      } else {
        setValidatedKey(null)
      }
    } catch (error) {
      if ((error as { name?: string })?.name !== 'AbortError') {
        setValidatedKey(null)
        toast.error('解析失败', { description: apiError(error) })
      }
    } finally {
      setExecuting(false)
      abortRef.current = null
    }
  }, [projectId, executing, script])

  const runTool = useCallback(async () => {
    if (!projectId || !selectedTool || executing) return
    let arguments_: Record<string, unknown>
    try {
      arguments_ = JSON.parse(argsText || '{}')
      if (!arguments_ || typeof arguments_ !== 'object' || Array.isArray(arguments_)) {
        throw new Error('入参必须是 JSON 对象')
      }
    } catch {
      toast.error('工具入参不是有效 JSON 对象')
      return
    }
    setExecuting(true)
    setResult(null)
    const controller = new AbortController()
    abortRef.current = controller
    try {
      const execution = await mcpDevApi.execute(projectId, {
        script, tool_name: selectedTool, arguments: arguments_,
      }, controller.signal)
      setResult(execution)
      if (execution.ok) {
        setValidatedKey(script)
        // 试跑成功后样例参数已在服务端自动记录，乐观同步到本地状态
        setProject(current => current ? {
          ...current,
          tool_samples: { ...current.tool_samples, [selectedTool]: arguments_ },
        } : current)
      } else {
        setValidatedKey(null)
      }
    } catch (error) {
      if ((error as { name?: string })?.name !== 'AbortError') {
        toast.error('试跑失败', { description: apiError(error) })
      }
    } finally {
      setExecuting(false)
      abortRef.current = null
    }
  }, [projectId, selectedTool, executing, script, argsText])

  const save = useCallback(async () => {
    if (!projectId || !canSave) return
    setSaving(true)
    try {
      const saveResult = await mcpDevApi.save(projectId, { script })
      if (!saveResult.ok) {
        setResult({
          ok: false, tools: null, payload: null, stdout: '',
          error: saveResult.error, traceback: saveResult.traceback,
          duration_ms: saveResult.duration_ms,
        })
        setValidatedKey(null)
        toast.error('保存前复核未通过', { description: saveResult.error ?? '脚本解析失败，未保存。' })
        return
      }
      setSavedScript(script)
      setValidatedKey(null)
      setTools(saveResult.tools)
      if (projectId) localStorage.removeItem(draftKey(projectId))
      setDraftRestoredAt(null)
      setProject(current => current ? {
        ...current,
        version_count: Math.max(current.version_count, saveResult.version_no ?? 0),
      } : current)
      toast.success(`已保存为版本 v${saveResult.version_no}`)
    } catch (error) {
      toast.error('保存失败', { description: apiError(error) })
    } finally {
      setSaving(false)
    }
  }, [projectId, canSave, script])

  const openVersions = useCallback(async () => {
    if (!projectId) return
    setShowVersions(true)
    setPreviewVersionNo(null)
    setVersionsLoading(true)
    try {
      setVersions(await mcpDevApi.listVersions(projectId))
    } catch (error) {
      toast.error('版本列表加载失败', { description: apiError(error) })
    } finally {
      setVersionsLoading(false)
    }
  }, [projectId])

  const toggleVersionPreview = useCallback(async (version: McpDevVersion) => {
    if (!projectId) return
    if (previewVersionNo === version.version_no) {
      setPreviewVersionNo(null)
      return
    }
    setPreviewVersionNo(version.version_no)
    const cached = versionDetails[version.version_no]
    if (cached && cached !== 'error') return
    try {
      const detail = await mcpDevApi.getVersion(projectId, version.version_no)
      setVersionDetails(previous => ({ ...previous, [version.version_no]: detail }))
    } catch {
      setVersionDetails(previous => ({ ...previous, [version.version_no]: 'error' }))
    }
  }, [projectId, previewVersionNo, versionDetails])

  const restoreVersion = useCallback(async (versionNo: number) => {
    if (!projectId) return
    try {
      const detail = await mcpDevApi.getVersion(projectId, versionNo)
      setScript(detail.script)
      setTools(detail.tool_manifest)
      const first = detail.tool_manifest[0]
      if (first) {
        setSelectedTool(first.name)
        const sample = detail.tool_samples?.[first.name]
        setArgsText(sample ? JSON.stringify(sample, null, 2) : argumentsTemplate(first))
      }
      setValidatedKey(null)
      setDraftRestoredAt(null)
      setPreviewVersionNo(null)
      setShowVersions(false)
      toast.success(`已恢复 v${detail.version_no} 的脚本内容`, { description: '恢复后请重新解析并保存。' })
    } catch (error) {
      toast.error('版本恢复失败', { description: apiError(error) })
    } finally {
      setConfirmRestoreVersionNo(null)
    }
  }, [projectId])

  const openPublish = useCallback(async () => {
    if (!projectId) return
    try {
      const rows = await mcpDevApi.listVersions(projectId)
      setPublishVersions(rows)
      setPublishVersionNo(rows[0]?.version_no ?? null)
      setPublishDisplayName(project?.display_name ?? '')
      setPublishDescription(project?.description ?? '')
      setPublishOpen(true)
    } catch (error) {
      toast.error('版本列表加载失败', { description: apiError(error) })
    }
  }, [projectId, project])

  const publish = useCallback(async () => {
    if (!projectId || !publishVersionNo || publishing) return
    setPublishing(true)
    try {
      const published = await mcpDevApi.publish(projectId, {
        version_no: publishVersionNo,
        display_name: publishDisplayName,
        description: publishDescription,
      })
      setProject(current => current ? {
        ...current,
        status: 'published',
        published_version_no: published.version_no,
        display_name: publishDisplayName || current.display_name,
        description: publishDescription || current.description,
      } : current)
      setPublishOpen(false)
      toast.success(
        `已发布为 MCP（v${published.version_no}，${published.tools.length} 个工具）`,
        { description: '已写入插件社区清单；默认停用，可在超级助手设置中启用。' },
      )
    } catch (error) {
      toast.error('发布未通过', { description: apiError(error) })
    } finally {
      setPublishing(false)
    }
  }, [projectId, publishVersionNo, publishing, publishDisplayName, publishDescription])

  const cancelExecution = useCallback(() => {
    abortRef.current?.abort()
  }, [])

  const revertToSaved = () => {
    setScript(savedScript)
    setValidatedKey(null)
    setDraftRestoredAt(null)
    if (projectId) localStorage.removeItem(draftKey(projectId))
    setConfirmRevert(false)
  }

  if (loading) {
    return <div className="flex h-full items-center justify-center text-sm text-[var(--color-text-tertiary)]">正在加载开发项目…</div>
  }
  if (loadError || !project) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3">
        <p className="text-sm text-[var(--color-danger)]">{loadError || '开发项目不存在'}</p>
        <button
          type="button"
          onClick={() => navigate('/community/plugins')}
          className="rounded-lg border border-border bg-card px-3 py-1.5 text-xs text-muted-foreground hover:bg-muted"
        >
          返回插件社区
        </button>
      </div>
    )
  }

  const published = project.status === 'published'

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* 页头 */}
      <header className="mb-3 flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={() => navigate('/community/plugins')}
          className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-border bg-card text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          aria-label="返回插件社区"
        >
          <ArrowLeft size={15} />
        </button>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h1 className="truncate text-base font-semibold text-[var(--color-text-primary)]">
              {project.display_name || project.name}
            </h1>
            <span className="inline-flex shrink-0 items-center rounded-md bg-brand-soft px-1.5 py-0.5 text-[11px] font-medium text-brand-ink">自研 MCP</span>
            {published && (
              <span className="shrink-0 text-[11px] text-muted-foreground" title={`已绑定 v${project.published_version_no}，重新发布前线上始终执行该冻结版本`}>
                已发布 v{project.published_version_no}
              </span>
            )}
            {dirty
              ? <span className="shrink-0 text-[11px] text-[var(--color-warning)]">未保存</span>
              : <span className="shrink-0 text-[11px] text-muted-foreground">已保存</span>}
            {draftRestoredAt && (
              <span className="shrink-0 text-[11px] text-brand-ink">
                已恢复 {formatDateTime(draftRestoredAt)} 的草稿
              </span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setShowHelp(true)}
            className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-border bg-card px-3 text-xs text-muted-foreground transition-colors hover:bg-muted"
          >
            <HelpCircle size={14} /> 契约说明
          </button>
          <button
            type="button"
            onClick={() => void openVersions()}
            className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-border bg-card px-3 text-xs text-muted-foreground transition-colors hover:bg-muted"
          >
            <History size={14} /> 历史版本
          </button>
          <button
            type="button"
            onClick={() => void openPublish()}
            disabled={project.version_count === 0}
            title={project.version_count === 0 ? '先解析通过并保存一个版本，才能发布' : '发布校验通过后写入插件社区清单'}
            className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-brand-line bg-brand-soft px-3 text-xs font-medium text-brand-ink transition-colors hover:bg-brand-mist focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-40"
          >
            <Rocket size={14} /> {published ? '重新发布' : '发布为 MCP'}
          </button>
          {dirty && (
            <button
              type="button"
              onClick={() => setConfirmRevert(true)}
              className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-border bg-card px-3 text-xs text-muted-foreground transition-colors hover:bg-muted"
            >
              <RotateCcw size={14} /> 恢复到已保存
            </button>
          )}
          {executing ? (
            <button
              type="button"
              onClick={cancelExecution}
              className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-border bg-card px-3.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted"
            >
              <Loader2 size={14} className="animate-spin" /> 取消执行
            </button>
          ) : (
            <button
              type="button"
              onClick={() => void introspect()}
              disabled={saving}
              title="在内核中解析当前脚本，导出工具清单"
              className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-[var(--color-nav-bg)] px-3.5 text-xs font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
            >
              <ScanSearch size={14} /> 解析工具
            </button>
          )}
          {dirty && !executing && !saving && !canSave && (
            <span className="inline-flex items-center text-[11px] text-[var(--color-warning)]">
              先解析通过，才能保存当前内容
            </span>
          )}
          <button
            type="button"
            onClick={save}
            disabled={!canSave}
            title={canSave ? '保存脚本' : '先解析通过，才能保存当前内容'}
            className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-brand px-3.5 text-xs font-medium text-white transition-colors hover:bg-brand-deep disabled:bg-muted disabled:text-muted-foreground"
          >
            {saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
            保存
          </button>
        </div>
      </header>

      {/* 主区域：左编辑器，右工具+入参+结果 */}
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 lg:grid-cols-2">
        <section className="flex min-h-[420px] flex-col overflow-hidden rounded-xl border border-border bg-card">
          <div className="flex items-center gap-2 border-b border-border px-3 py-2 text-xs text-muted-foreground">
            <FileCode2 size={14} />
            <span>MCP 工具脚本（Python）</span>
          </div>
          <div className="min-h-0 flex-1">
            <CodeMirror
              value={script}
              onChange={value => { setScript(value); setValidatedKey(null) }}
              extensions={[python(), editorTheme, pythonHighlight]}
              height="100%"
              style={{ height: '100%' }}
              basicSetup={{ lineNumbers: true, foldGutter: true }}
            />
          </div>
        </section>

        <div className="flex min-h-0 flex-col gap-3">
          <section className="flex max-h-[42%] min-h-[180px] flex-col overflow-hidden rounded-xl border border-border bg-card">
            <div className="flex items-center gap-2 border-b border-border px-3 py-2 text-xs text-muted-foreground">
              <Code2 size={14} />
              <span>工具清单（{tools.length} 个）</span>
              <span className="text-[11px] text-[var(--color-text-tertiary)]">点「解析工具」刷新</span>
            </div>
            <div className="min-h-0 flex-1 overflow-auto p-2">
              {tools.length === 0 ? (
                <p className="px-2 py-4 text-xs text-muted-foreground">
                  尚未解析出工具：用 <code className="rounded bg-muted px-1 font-mono">@mcp_tool</code> 装饰函数后点「解析工具」。
                </p>
              ) : (
                <div className="space-y-1.5">
                  {tools.map(tool => (
                    <button
                      key={tool.name}
                      type="button"
                      onClick={() => selectTool(tool)}
                      aria-pressed={selectedTool === tool.name}
                      className={`w-full rounded-lg border px-3 py-2 text-left transition-colors ${selectedTool === tool.name ? 'border-brand-line bg-brand-soft' : 'border-border bg-card hover:bg-muted'}`}
                    >
                      <span className="flex items-center justify-between gap-2">
                        <span className="truncate font-mono text-xs font-semibold text-foreground">{tool.name}</span>
                        {project.tool_samples?.[tool.name] !== undefined && (
                          <span className="shrink-0 rounded-full border border-border bg-card px-1.5 py-0.5 text-[10px] text-muted-foreground">有样例</span>
                        )}
                      </span>
                      <span className="mt-0.5 line-clamp-1 block text-[11px] text-muted-foreground" title={tool.description || '暂无描述'}>
                        {tool.description || '（缺少描述：发布闸门会拒绝）'}
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          </section>

          <div className="flex min-h-0 flex-1 flex-col gap-3">
            <div className="flex max-h-[38%] flex-col overflow-hidden rounded-xl border border-border bg-card">
              <div className="flex items-center gap-2 border-b border-border px-3 py-2 text-xs text-muted-foreground">
                <Terminal size={14} />
                <span>试跑入参{selectedToolMeta ? ` · ${selectedToolMeta.name}` : ''}</span>
                {!argsStatus.ok && (
                  <span className="text-[11px] text-destructive">JSON 无效</span>
                )}
                <button
                  type="button"
                  onClick={() => void runTool()}
                  disabled={!selectedTool || executing}
                  className="ml-auto inline-flex h-7 items-center gap-1.5 rounded-md bg-[var(--color-nav-bg)] px-2.5 text-[11px] font-medium text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <Play size={12} /> 试跑
                </button>
              </div>
              <textarea
                value={argsText}
                onChange={event => setArgsText(event.target.value)}
                spellCheck={false}
                disabled={!selectedTool}
                className="min-h-[84px] flex-1 resize-none px-3 py-2 font-mono text-xs leading-5 text-foreground focus:outline-none disabled:bg-muted disabled:text-muted-foreground"
                aria-label="工具试跑入参 JSON"
                aria-invalid={!argsStatus.ok}
              />
              {!argsStatus.ok && (
                <div className="flex items-start gap-1.5 border-t border-[var(--color-danger-bg)] bg-[var(--color-danger-bg)] px-3 py-1.5 text-[11px] leading-4 text-destructive">
                  <AlertCircle size={12} className="mt-0.5 shrink-0" />
                  <span className="break-words">入参 JSON 无效：{argsStatus.message}</span>
                </div>
              )}
            </div>

            <section className="flex min-h-[160px] flex-1 flex-col overflow-hidden rounded-xl border border-border bg-card">
              <div className="flex items-center gap-2 border-b border-border px-3 py-2 text-xs text-muted-foreground">
                {result
                  ? result.ok
                    ? <CheckCircle2 size={14} className="text-brand-ink" />
                    : <XCircle size={14} className="text-destructive" />
                  : <Terminal size={14} />}
                <span>执行结果</span>
                {result && (
                  <span className="ml-auto text-[11px] tabular-nums text-muted-foreground">
                    {result.duration_ms} ms
                  </span>
                )}
              </div>
              <div className="min-h-0 flex-1 overflow-auto px-3 py-2">
                {executing ? (
                  <div className="flex h-full items-center justify-center gap-2 text-xs text-muted-foreground">
                    <Loader2 size={14} className="animate-spin" /> 内核执行中…
                  </div>
                ) : !result ? (
                  <p className="text-xs text-muted-foreground">
                    点「试跑」以样例入参调用选中的工具；试跑成功的入参会自动记为发布校验用的样例参数。
                  </p>
                ) : (
                  <div className="space-y-3">
                    {result.ok ? (
                      <div>
                        <p className="mb-1 text-[11px] font-medium text-muted-foreground">返回值</p>
                        <pre className="overflow-auto rounded-lg bg-muted p-2.5 text-xs leading-5 text-foreground">
                          {JSON.stringify(result.payload, null, 2)}
                        </pre>
                      </div>
                    ) : (
                      <div>
                        <p className="mb-1 text-[11px] font-medium text-destructive">执行失败</p>
                        <p className="text-xs text-destructive">{result.error}</p>
                        {result.traceback && (
                          <pre className="mt-2 overflow-auto rounded-lg bg-[var(--color-danger-bg)] p-2.5 text-[11px] leading-5 text-destructive">
                            {result.traceback}
                          </pre>
                        )}
                      </div>
                    )}
                    {result.stdout && (
                      <div>
                        <p className="mb-1 text-[11px] font-medium text-muted-foreground">标准输出</p>
                        <pre className="max-h-48 overflow-auto rounded-lg bg-muted p-2.5 text-[11px] leading-5 text-muted-foreground">
                          {result.stdout}
                        </pre>
                      </div>
                    )}
                  </div>
                )}
              </div>
            </section>
          </div>
        </div>
      </div>

      {/* 历史版本抽屉 */}
      <Sheet open={showVersions} onOpenChange={setShowVersions}>
        <SheetContent className="w-[420px] overflow-y-auto sm:max-w-[420px]">
          <SheetHeader>
            <SheetTitle>历史版本</SheetTitle>
            <SheetDescription>每次保存冻结一版，最多保留 20 版；恢复后需重新解析并保存。</SheetDescription>
          </SheetHeader>
          <div className="mt-4 space-y-2">
            {versionsLoading ? (
              <p className="py-8 text-center text-xs text-muted-foreground">加载版本列表…</p>
            ) : !versions?.length ? (
              <p className="py-8 text-center text-xs text-muted-foreground">暂无历史版本，保存一次后此处会出现记录。</p>
            ) : (
              versions.map(version => (
                <div key={version.id} className="rounded-lg border border-border px-3 py-2.5">
                  <div className="flex items-center justify-between">
                    <div>
                      <p className="text-sm font-medium text-foreground">
                        v{version.version_no}
                        {project.published_version_no === version.version_no && (
                          <span className="ml-1.5 rounded-full border border-brand-line bg-brand-soft px-1.5 py-0.5 text-[10px] font-medium text-brand-ink">线上</span>
                        )}
                      </p>
                      <p className="mt-0.5 text-[11px] text-muted-foreground">
                        {formatDateTime(version.created_at)} · {version.tool_count} 个工具 · {version.duration_ms} ms
                      </p>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <button
                        type="button"
                        onClick={() => void toggleVersionPreview(version)}
                        aria-expanded={previewVersionNo === version.version_no}
                        className="inline-flex h-7 items-center rounded-md border border-border px-2.5 text-xs text-muted-foreground transition-colors hover:bg-muted"
                      >
                        {previewVersionNo === version.version_no ? '收起' : '查看'}
                      </button>
                      <button
                        type="button"
                        onClick={() => { setShowVersions(false); setConfirmRestoreVersionNo(version.version_no) }}
                        className="inline-flex h-7 items-center gap-1 rounded-md border border-border px-2.5 text-xs text-muted-foreground transition-colors hover:bg-muted"
                      >
                        <RotateCcw size={12} /> 恢复
                      </button>
                    </div>
                  </div>
                  {previewVersionNo === version.version_no && (
                    <div className="mt-2.5 space-y-2 border-t border-border pt-2.5">
                      {(() => {
                        const detail = versionDetails[version.version_no]
                        if (!detail || detail === 'error') {
                          return (
                            <div className="flex items-center justify-between text-[11px] text-muted-foreground">
                              <span className="inline-flex items-center gap-1.5">
                                {detail === 'error'
                                  ? '版本内容加载失败'
                                  : <><Loader2 size={12} className="animate-spin" /> 加载版本内容…</>}
                              </span>
                              <button
                                type="button"
                                onClick={() => void toggleVersionPreview(version)}
                                className="rounded-md border border-border px-2 py-0.5 text-[11px] text-muted-foreground transition-colors hover:bg-muted"
                              >
                                重试
                              </button>
                            </div>
                          )
                        }
                        return (
                          <>
                            <div>
                              <p className="mb-1 text-[11px] font-medium text-muted-foreground">该版工具清单</p>
                              <pre className="max-h-32 overflow-auto rounded-md bg-muted p-2 font-mono text-[11px] leading-4 text-muted-foreground">
                                {detail.tool_manifest.map(tool => `${tool.name} — ${tool.description || '（缺描述）'}`).join('\n') || '（无工具）'}
                              </pre>
                            </div>
                            <div>
                              <p className="mb-1 text-[11px] font-medium text-muted-foreground">该版脚本</p>
                              <pre className="max-h-64 overflow-auto rounded-md bg-muted p-2 font-mono text-[11px] leading-4 text-foreground">{detail.script}</pre>
                            </div>
                          </>
                        )
                      })()}
                    </div>
                  )}
                </div>
              ))
            )}
          </div>
        </SheetContent>
      </Sheet>

      {/* 契约说明抽屉 */}
      <Sheet open={showHelp} onOpenChange={setShowHelp}>
        <SheetContent className="w-[460px] overflow-y-auto sm:max-w-[460px]">
          <SheetHeader>
            <SheetTitle>自研 MCP 工具契约</SheetTitle>
            <SheetDescription>平台与工具函数之间的统一接口约定</SheetDescription>
          </SheetHeader>
          <div className="mt-4 space-y-4 text-xs leading-6 text-muted-foreground">
            <section>
              <h3 className="text-sm font-semibold text-foreground">声明工具</h3>
              <pre className="mt-2 overflow-auto rounded-lg bg-muted p-3 font-mono text-[11px] leading-5">
{`@mcp_tool(description="查询指定城市天气")
def get_weather(city: str, days: int = 3) -> dict:
    ...
    return {...}  # JSON 可序列化`}
              </pre>
              <ul className="mt-1 list-disc space-y-1 pl-4">
                <li>每个被 <code>@mcp_tool</code> 装饰的顶层函数 = 一个 MCP 工具，工具名即函数名</li>
                <li>入参从类型注解推导 JSON Schema（str/int/float/bool/dict/list 与默认值）</li>
                <li><code>description</code> 必填：描述缺失的工具会被发布闸门拒绝</li>
              </ul>
            </section>
            <section>
              <h3 className="text-sm font-semibold text-foreground">个人变量</h3>
              <p className="mt-1">
                个人设置中的环境变量与隐私变量以只读字典 <code>OB_ENV</code> / <code>OB_SECRET</code>
                注入（例如 <code>OB_SECRET.get(&quot;MY_API_KEY&quot;</code>)），仅本次执行可见、不落库不回显。
                调用外部接口可直接使用 requests / httpx。
              </p>
            </section>
            <section>
              <h3 className="text-sm font-semibold text-foreground">保存与发布</h3>
              <ul className="mt-1 list-disc space-y-1 pl-4">
                <li>只有当前内容解析通过，保存按钮才可用；保存时服务端会重新复核</li>
                <li>每次保存冻结一个历史版本（保留 20 版），已发布的 MCP 始终执行发布绑定的版本</li>
                <li>发布闸门：每个工具描述完备 + 有成功试跑过的样例参数，并按样例参数真实执行一遍</li>
                <li>发布产物默认停用且逐次确认，可在超级助手设置中启用</li>
              </ul>
            </section>
            <section>
              <h3 className="text-sm font-semibold text-foreground">安全边界</h3>
              <p className="mt-1">
                工具在平台独立 Python 内核中执行（无平台凭据）。请勿在返回值或打印输出中输出密钥明文：
                agent 调用链路会自动把已知个人变量值打码为 <code>***</code>，但最稳妥的做法是只返回业务数据。
              </p>
            </section>
          </div>
        </SheetContent>
      </Sheet>

      {/* 发布对话框 */}
      <Modal
        open={publishOpen}
        onClose={() => { if (!publishing) setPublishOpen(false) }}
        title={published ? `重新发布 · ${project.display_name || project.name}` : '发布为 MCP'}
        description="发布校验会按样例参数真实执行每个工具，全部通过后才写入插件社区清单。"
        size="lg"
        contentClassName="p-5"
        footer={(
          <div className="flex justify-end gap-3">
            <button
              type="button"
              onClick={() => setPublishOpen(false)}
              disabled={publishing}
              className="min-h-10 min-w-24 rounded-xl border border-border bg-card px-4 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted"
            >
              取消
            </button>
            <button
              type="button"
              onClick={() => void publish()}
              disabled={publishing || !publishVersionNo}
              className="inline-flex min-h-10 min-w-24 items-center justify-center gap-2 rounded-xl bg-brand px-4 text-xs font-medium text-[var(--color-text-inverse)] transition-colors hover:bg-brand-deep disabled:cursor-not-allowed disabled:opacity-45"
            >
              {publishing && <Loader2 size={13} className="animate-spin" />}
              {published ? '重新发布' : '发布'}
            </button>
          </div>
        )}
      >
        <div className="space-y-4 text-xs">
          <label className="block">
            <span className="mb-1 block font-medium text-foreground">发布版本</span>
            <Select
              value={publishVersionNo !== null ? String(publishVersionNo) : undefined}
              onValueChange={value => setPublishVersionNo(Number(value) || null)}
              disabled={publishing || publishVersions.length === 0}
            >
              <SelectTrigger className="h-10 w-full rounded-lg bg-card px-3 text-sm" aria-label="选择发布版本">
                <SelectValue placeholder="选择版本" />
              </SelectTrigger>
              <SelectContent>
                {publishVersions.map(version => (
                  <SelectItem key={version.id} value={String(version.version_no)}>
                    v{version.version_no} · {version.tool_count} 个工具 · {formatDateTime(version.created_at)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <span className="mt-1 block text-[11px] text-muted-foreground">
              已发布的 MCP 始终执行绑定版本，重新发布后才切换到新版本。
            </span>
          </label>
          <label className="block">
            <span className="mb-1 block font-medium text-foreground">显示名称</span>
            <input
              value={publishDisplayName}
              onChange={event => setPublishDisplayName(event.target.value)}
              disabled={publishing}
              maxLength={200}
              className="w-full rounded-lg border border-border bg-card px-3 py-2 text-sm text-foreground focus-visible:ring-2 focus-visible:ring-ring"
            />
          </label>
          <label className="block">
            <span className="mb-1 block font-medium text-foreground">描述</span>
            <textarea
              value={publishDescription}
              onChange={event => setPublishDescription(event.target.value)}
              disabled={publishing}
              maxLength={500}
              rows={2}
              className="w-full resize-none rounded-lg border border-border bg-card px-3 py-2 text-sm text-foreground focus-visible:ring-2 focus-visible:ring-ring"
            />
          </label>
        </div>
      </Modal>

      <ConfirmDialog
        open={confirmRevert}
        title="恢复到已保存内容？"
        description="当前未保存的修改将被丢弃，此操作无法撤销。"
        confirmText="恢复"
        onConfirm={revertToSaved}
        onClose={() => setConfirmRevert(false)}
      />
      <ConfirmDialog
        open={confirmRestoreVersionNo !== null}
        title="恢复到该历史版本？"
        description="当前编辑器中的未保存修改将被覆盖丢弃；恢复后需重新解析并保存。"
        confirmText="恢复"
        onConfirm={() => { if (confirmRestoreVersionNo !== null) void restoreVersion(confirmRestoreVersionNo) }}
        onClose={() => setConfirmRestoreVersionNo(null)}
      />
    </div>
  )
}
