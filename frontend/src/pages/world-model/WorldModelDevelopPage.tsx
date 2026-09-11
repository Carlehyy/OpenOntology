import { formatDateTime } from '@/utils/datetime'
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
  Check,
  CheckCircle2,
  Copy,
  FileCode2,
  HelpCircle,
  History,
  Loader2,
  Maximize2,
  Minimize2,
  Play,
  Rocket,
  RotateCcw,
  Save,
  Terminal,
  TrendingUp,
  XCircle,
} from 'lucide-react'
import {
  apiError,
  worldModelApi,
  type ScriptExecutionResult,
  type ScriptVersionDetail,
  type ScriptVersionItem,
  type TestInput,
  type WorldModelProjectDetail,
  type WorldModelServiceInfo,
} from '@/api/worldModel'
import { ontologyApi } from '@/api/ontologies'
import { engineTypeLabel } from './WorldModelModelsPage'
import PublishServiceDialog from './PublishServiceDialog'
import TrajectoryPreview from './TrajectoryPreview'
import { extractTrajectorySummary } from './trajectorySummary'
import { validateTestInputText } from './testInputValidation'
import { toast } from 'sonner'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { writeTextToClipboard } from '@/utils/clipboard'
import {
  Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle,
} from '@/components/ui/sheet'

// 与数据通道 Python 脚本编辑页一致的编辑器观感（GitHub light 高亮）：
// 注释不用斜体，避免斜体中文字形被浏览器机械倾斜发糊
const editorTheme = EditorView.theme({
  '.cm-content': {
    lineHeight: '1.6',
  },
})

const pythonHighlight = syntaxHighlighting(HighlightStyle.define([
  { tag: tags.keyword, color: '#cf222e', fontWeight: '600' },
  { tag: [tags.string, tags.docComment], color: '#0a3069' },
  { tag: tags.comment, color: '#6e7781' },
  { tag: [tags.number, tags.bool, tags.null], color: '#0550ae' },
  { tag: [tags.function(tags.variableName), tags.function(tags.propertyName)], color: '#8250df' },
  { tag: [tags.className, tags.definition(tags.variableName)], color: '#953800' },
  { tag: tags.propertyName, color: '#116329' },
  { tag: tags.operator, color: '#cf222e' },
  { tag: tags.escape, color: '#0550ae' },
]))

const DEFAULT_TEST_INPUT = `{
  "context": { "current_value": 100 },
  "actions": [],
  "horizon": 6
}`

const draftKey = (projectId: string) => `ob:world-model-draft:${projectId}`
const testInputKey = (projectId: string) => `ob:world-model-test-input:${projectId}`

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

export default function WorldModelDevelopPage() {
  const { modelId } = useParams<{ modelId: string }>()
  const navigate = useNavigate()

  const [project, setProject] = useState<WorldModelProjectDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [script, setScript] = useState('')
  const [savedScript, setSavedScript] = useState('')
  const [testInputText, setTestInputText] = useState(DEFAULT_TEST_INPUT)
  const [executing, setExecuting] = useState(false)
  const [saving, setSaving] = useState(false)
  const [result, setResult] = useState<ScriptExecutionResult | null>(null)
  // 客户端门槛：只有「当前编辑器内容 + 当前测试入参」执行通过，保存才可点；
  // 服务端保存时仍会重跑复验（双重保障）。
  const [validatedKey, setValidatedKey] = useState<string | null>(null)
  const [draftRestoredAt, setDraftRestoredAt] = useState<string | null>(null)
  const draftTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const [showVersions, setShowVersions] = useState(false)
  const [versions, setVersions] = useState<ScriptVersionItem[] | null>(null)
  const [versionsLoading, setVersionsLoading] = useState(false)
  const [showHelp, setShowHelp] = useState(false)
  const [confirmRevert, setConfirmRevert] = useState(false)
  const [confirmRestoreVersionId, setConfirmRestoreVersionId] = useState<string | null>(null)
  const [services, setServices] = useState<WorldModelServiceInfo[]>([])
  const [ontologyNames, setOntologyNames] = useState<Record<string, string>>({})
  const [copiedServiceId, setCopiedServiceId] = useState<string | null>(null)
  const [publishOpen, setPublishOpen] = useState(false)
  const [publishVersions, setPublishVersions] = useState<ScriptVersionItem[]>([])
  const [insertingTs, setInsertingTs] = useState(false)
  const [confirmInsertTs, setConfirmInsertTs] = useState(false)
  // 测试入参编辑区放大模式：放大时隐藏结果面板，把整列让给入参编辑
  const [inputExpanded, setInputExpanded] = useState(false)
  // 版本预览：抽屉内展开查看某版脚本与当时测试入参（懒加载，缓存已取版本）
  const [previewVersionId, setPreviewVersionId] = useState<string | null>(null)
  const [versionDetails, setVersionDetails] = useState<Record<string, ScriptVersionDetail | 'error'>>({})

  const dirty = script !== savedScript
  const contentKey = `${script}\n${testInputText}`
  const canSave = dirty && !executing && !saving && validatedKey === contentKey

  // 测试入参即时校验：输入过程中定位 JSON 语法错误，不等点击「执行」才报错
  const testInputStatus = useMemo(() => validateTestInputText(testInputText), [testInputText])

  // 执行结果可图表化摘要：契约返回 trajectory（数值序列）时生成，否则为 null
  const trajectory = useMemo(
    () => (result?.ok ? extractTrajectorySummary(result.payload) : null),
    [result],
  )

  const parseTestInput = (): TestInput | null => {
    try {
      const parsed = JSON.parse(testInputText || '{}')
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        toast.error('测试入参格式不正确', { description: '测试入参必须是 JSON 对象。' })
        return null
      }
      return parsed as TestInput
    } catch {
      toast.error('测试入参不是有效 JSON', { description: '请检查测试入参的语法。' })
      return null
    }
  }

  useEffect(() => {
    if (!modelId) return
    setLoading(true)
    worldModelApi.getProject(modelId)
      .then(detail => {
        setProject(detail)
        const draft = readDraft(modelId)
        if (draft && draft.script !== detail.script) {
          setScript(draft.script)
          setDraftRestoredAt(draft.updatedAt)
        } else {
          setScript(detail.script)
        }
        setSavedScript(detail.script)
        const cachedInput = localStorage.getItem(testInputKey(modelId))
        if (cachedInput) setTestInputText(cachedInput)
      })
      .catch(error => setLoadError(apiError(error)))
      .finally(() => setLoading(false))
    worldModelApi.getServices(modelId)
      .then(rows => setServices(rows))
      .catch(() => setServices([]))
  }, [modelId])

  // 本体 id → 名称映射（多服务面板标注各服务绑定的本体）
  useEffect(() => {
    ontologyApi.list({ page_size: 200 })
      .then(result => setOntologyNames(
        Object.fromEntries(result.items.map(item => [item.id, item.name]))))
      .catch(() => setOntologyNames({}))
  }, [])

  // 草稿自动保存（防抖 800ms）
  useEffect(() => {
    if (!modelId || loading) return
    if (draftTimer.current) clearTimeout(draftTimer.current)
    draftTimer.current = setTimeout(() => {
      if (script !== savedScript) {
        localStorage.setItem(draftKey(modelId), JSON.stringify({ script, updatedAt: new Date().toISOString() }))
      } else {
        localStorage.removeItem(draftKey(modelId))
      }
    }, 800)
    return () => { if (draftTimer.current) clearTimeout(draftTimer.current) }
  }, [script, savedScript, modelId, loading])

  useEffect(() => {
    if (!modelId || loading) return
    const timer = setTimeout(() => localStorage.setItem(testInputKey(modelId), testInputText), 800)
    return () => clearTimeout(timer)
  }, [testInputText, modelId, loading])

  const execute = useCallback(async () => {
    if (!modelId || executing) return
    const testInput = parseTestInput()
    if (!testInput) return
    setInputExpanded(false)
    setExecuting(true)
    setResult(null)
    const controller = new AbortController()
    abortRef.current = controller
    try {
      const execution = await worldModelApi.executeScript(modelId, script, testInput, controller.signal)
      setResult(execution)
      if (execution.ok) {
        setValidatedKey(`${script}\n${testInputText}`)
      } else {
        setValidatedKey(null)
      }
    } catch (error) {
      if ((error as { name?: string })?.name !== 'AbortError') {
        setValidatedKey(null)
        setResult(null)
        toast.error('执行失败', { description: apiError(error) })
      }
    } finally {
      setExecuting(false)
      abortRef.current = null
    }
  }, [modelId, executing, script, testInputText])

  const save = useCallback(async () => {
    if (!modelId || !canSave) return
    const testInput = parseTestInput()
    if (!testInput) return
    setSaving(true)
    try {
      const saveResult = await worldModelApi.saveScript(modelId, script, testInput)
      if (!saveResult.ok) {
        setResult(saveResult.execution)
        setValidatedKey(null)
        toast.error('保存前复核未通过', { description: saveResult.execution.error ?? '脚本执行失败，未保存。' })
        return
      }
      setSavedScript(script)
      setValidatedKey(null)
      if (modelId) localStorage.removeItem(draftKey(modelId))
      setDraftRestoredAt(null)
      // 缺陷修复：保存成功后立即刷新版本数，发布按钮无需刷新页面即可解锁
      setProject(current => current ? {
        ...current,
        version_count: Math.max(current.version_count ?? 0, saveResult.version_no ?? 0),
      } : current)
      toast.success(`已保存为版本 v${saveResult.version_no}`)
    } catch (error) {
      toast.error('保存失败', { description: apiError(error) })
    } finally {
      setSaving(false)
    }
  }, [modelId, canSave, script])

  const openVersions = useCallback(async () => {
    if (!modelId) return
    setShowVersions(true)
    setPreviewVersionId(null)
    setVersionsLoading(true)
    try {
      setVersions(await worldModelApi.listVersions(modelId))
    } catch (error) {
      toast.error('版本列表加载失败', { description: apiError(error) })
    } finally {
      setVersionsLoading(false)
    }
  }, [modelId])

  const restoreVersion = useCallback(async (versionId: string) => {
    if (!modelId) return
    try {
      const detail = await worldModelApi.getVersion(modelId, versionId)
      setScript(detail.script)
      // 该版当时验证通过的测试入参一并回退，避免「脚本与入参从未组合通过过」的错配状态
      if (detail.test_input) setTestInputText(JSON.stringify(detail.test_input, null, 2))
      setValidatedKey(null)
      // 草稿横幅描述的是此前恢复的本地草稿，已被版本回退取代，需同步清除
      setDraftRestoredAt(null)
      setPreviewVersionId(null)
      setShowVersions(false)
      toast.success(`已恢复 v${detail.version_no} 的脚本内容`, { description: '恢复后请重新执行并保存。' })
    } catch (error) {
      toast.error('版本恢复失败', { description: apiError(error) })
    } finally {
      setConfirmRestoreVersionId(null)
    }
  }, [modelId])

  // 取消执行：中断客户端等待（内核随服务端收尾销毁），编辑内容不受影响
  const cancelExecution = useCallback(() => {
    abortRef.current?.abort()
  }, [])

  const revertToSaved = () => {
    setScript(savedScript)
    setValidatedKey(null)
    // 缺陷修复：恢复到已保存后，本地草稿已不再存在，需同步清除草稿横幅，
    // 避免页头同时出现「已保存」与「已恢复 XX 草稿」两种矛盾状态
    setDraftRestoredAt(null)
    if (modelId) localStorage.removeItem(draftKey(modelId))
    setConfirmRevert(false)
  }

  // 版本抽屉内展开/收起某版的脚本与测试入参预览（懒加载 getVersion，失败可重试）
  const toggleVersionPreview = useCallback(async (version: ScriptVersionItem) => {
    if (!modelId) return
    if (previewVersionId === version.id) {
      setPreviewVersionId(null)
      return
    }
    setPreviewVersionId(version.id)
    const cached = versionDetails[version.id]
    if (cached && cached !== 'error') return
    try {
      const detail = await worldModelApi.getVersion(modelId, version.id)
      setVersionDetails(previous => ({ ...previous, [version.id]: detail }))
    } catch {
      setVersionDetails(previous => ({ ...previous, [version.id]: 'error' }))
    }
  }, [modelId, previewVersionId, versionDetails])

  // 打开发布对话框：先拉版本列表供选择（默认最新）
  const openPublish = useCallback(async () => {
    if (!modelId) return
    try {
      setPublishVersions(await worldModelApi.listVersions(modelId))
      setPublishOpen(true)
    } catch (error) {
      toast.error('版本列表加载失败', { description: apiError(error) })
    }
  }, [modelId])

  // 插入官方时序推演示例（ARIMA/SARIMA）：脚本与默认测试入参一并替换，
  // 未保存修改先确认再覆盖；插入后需重新执行并保存（与版本恢复同一纪律）。
  const insertTimeSeriesTemplate = useCallback(async () => {
    if (!modelId || insertingTs) return
    setInsertingTs(true)
    try {
      const template = await worldModelApi.getTimeSeriesTemplate()
      setScript(template.script)
      setTestInputText(JSON.stringify(template.test_input, null, 2))
      setValidatedKey(null)
      setConfirmInsertTs(false)
      toast.success('已插入时序推演示例（ARIMA/SARIMA）', { description: '已同步替换默认测试入参（36 点季节序列），执行通过后保存即可发布。' })
    } catch (error) {
      toast.error('时序示例加载失败', { description: apiError(error) })
    } finally {
      setInsertingTs(false)
    }
  }, [modelId, insertingTs])

  const requestInsertTs = useCallback(() => {
    if (dirty) {
      setConfirmInsertTs(true)
    } else {
      void insertTimeSeriesTemplate()
    }
  }, [dirty, insertTimeSeriesTemplate])

  const toggleServiceStatus = useCallback(async (svc: WorldModelServiceInfo) => {
    const next = svc.status === 'online' ? 'offline' : 'online'
    try {
      const updated = await worldModelApi.setServiceStatusById(svc.id, next)
      setServices(current => current.map(item => (item.id === svc.id ? { ...item, status: updated.status } : item)))
      toast.success(next === 'online' ? '服务已上线' : '服务已下线')
    } catch (error) {
      toast.error('状态切换失败', { description: apiError(error) })
    }
  }, [toast])

  const copyEndpoint = useCallback((svc: WorldModelServiceInfo) => {
    if (!svc.endpoint_path) return
    writeTextToClipboard(svc.endpoint_path).then(() => {
      setCopiedServiceId(svc.id)
      window.setTimeout(() => setCopiedServiceId(null), 1400)
    }).catch(() => undefined)
  }, [])

  if (loading) {
    return <div className="flex h-full items-center justify-center text-sm text-[var(--color-text-tertiary)]">正在加载推演模型…</div>
  }
  if (loadError || !project) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3">
        <p className="text-sm text-[var(--color-danger)]">{loadError || '推演模型不存在'}</p>
        <button
          type="button"
          onClick={() => navigate('/world-model/models')}
          className="rounded-lg border border-border bg-card px-3 py-1.5 text-xs text-muted-foreground hover:bg-muted"
        >
          返回推演模型列表
        </button>
      </div>
    )
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* 页头 */}
      <header className="mb-3 flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={() => navigate('/world-model/models')}
          className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-border bg-card text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          aria-label="返回推演模型列表"
        >
          <ArrowLeft size={15} />
        </button>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h1 className="truncate text-base font-semibold text-[var(--color-text-primary)]">{project.name}</h1>
            <span className="inline-flex shrink-0 items-center rounded-md bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground">
              {engineTypeLabel(project.engine_type)}
            </span>
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
            onClick={openVersions}
            className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-border bg-card px-3 text-xs text-muted-foreground transition-colors hover:bg-muted"
          >
            <History size={14} /> 历史版本
          </button>
          <button
            type="button"
            onClick={requestInsertTs}
            disabled={insertingTs}
            title="插入官方时序推演示例：ARIMA/SARIMA 建模与预测（覆盖当前脚本内容）"
            className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-border bg-card px-3 text-xs text-muted-foreground transition-colors hover:bg-muted disabled:opacity-50"
          >
            {insertingTs ? <Loader2 size={14} className="animate-spin" /> : <TrendingUp size={14} />} 时序示例
          </button>
          <button
            type="button"
            onClick={() => void openPublish()}
            disabled={project.version_count === 0}
            title={project.version_count === 0 ? '先执行通过并保存一个版本，才能发布' : services.length ? '重新发布（同一本体重发覆盖，切换目标本体新增服务）' : '发布为推演服务'}
            className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-brand-line bg-brand-soft px-3 text-xs font-medium text-brand-ink transition-colors hover:bg-brand-mist focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-40"
          >
            <Rocket size={14} /> {services.length ? '重新发布' : '发布'}
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
              onClick={execute}
              disabled={saving}
              className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-[var(--color-nav-bg)] px-3.5 text-xs font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
            >
              <Play size={14} /> 执行
            </button>
          )}
          {dirty && !executing && !saving && !canSave && (
            <span className="inline-flex items-center text-[11px] text-[var(--color-warning)]">
              先执行通过，才能保存当前内容
            </span>
          )}
          <button
            type="button"
            onClick={save}
            disabled={!canSave}
            title={canSave ? '保存脚本' : '先执行通过，才能保存当前内容'}
            className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-brand px-3.5 text-xs font-medium text-white transition-colors hover:bg-brand-deep disabled:bg-muted disabled:text-muted-foreground"
          >
            {saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
            保存
          </button>
        </div>
      </header>

      {/* 推演服务面板：多本体发布后一个项目可有 N 个服务，逐个展示端点 / 状态 / 上下线 */}
      {services.map(service => (
        <section
          key={service.id}
          data-testid="world-model-service-panel"
          className={`mb-3 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-xl border px-4 py-2.5 ${service.status === 'online'
            ? 'border-brand-line bg-brand-soft'
            : 'border-border bg-muted'}`}
        >
          <span className={`inline-flex items-center rounded-md px-1.5 py-0.5 text-[11px] font-medium ${service.status === 'online' ? 'bg-brand-mist text-brand-ink' : 'bg-muted text-muted-foreground'}`}>
            {service.status === 'online' ? '在线' : '已下线'}
          </span>
          <span className="text-sm font-medium text-[var(--color-text-primary)]">{service.name}</span>
          {service.applicable_object_types?.ontology_id && (
            <span
              className="inline-flex items-center rounded-md border border-[var(--color-border)] bg-card px-1.5 py-0.5 text-[11px] text-[var(--color-text-secondary)]"
              title={`绑定本体：${service.applicable_object_types.ontology_id}`}
            >
              {ontologyNames[service.applicable_object_types.ontology_id]
                || service.applicable_object_types.ontology_id.slice(0, 8)}
            </span>
          )}
          {service.version_no !== null && (
            <span className="text-[11px] text-[var(--color-text-tertiary)]">v{service.version_no}</span>
          )}
          {service.endpoint_path && (
            <span className="flex min-w-0 items-center gap-1 rounded-md border border-[var(--color-border)] bg-card px-2 py-1">
              <code className="truncate font-mono text-[11px] text-muted-foreground">POST {service.endpoint_path}</code>
              <button
                type="button"
                onClick={() => copyEndpoint(service)}
                aria-label={copiedServiceId === service.id ? '端点已复制' : '复制调用端点'}
                className="inline-flex shrink-0 items-center gap-1 rounded px-1 text-[10px] text-muted-foreground transition-colors hover:text-brand-ink"
              >
                {copiedServiceId === service.id ? <Check size={11} /> : <Copy size={11} />}
                {copiedServiceId === service.id ? '已复制' : '复制'}
              </button>
            </span>
          )}
          <span className="ml-auto flex items-center gap-2">
            <button
              type="button"
              onClick={() => void toggleServiceStatus(service)}
              className="inline-flex h-7 items-center rounded-md border border-[var(--color-border)] bg-card px-2.5 text-[11px] text-[var(--color-text-secondary)] transition-colors hover:bg-muted"
            >
              {service.status === 'online' ? '下线' : '上线'}
            </button>
          </span>
        </section>
      ))}

      {/* 主区域：左编辑器，右入参+结果 */}
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 lg:grid-cols-2">
        <section className="flex min-h-[420px] flex-col overflow-hidden rounded-xl border border-border bg-card">
          <div className="flex items-center gap-2 border-b border-border px-3 py-2 text-xs text-muted-foreground">
            <FileCode2 size={14} />
            <span>推演脚本（Python）</span>
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
          <section
            className={`flex flex-col overflow-hidden rounded-xl border border-border bg-card ${inputExpanded ? 'min-h-0 flex-1' : ''}`}
            style={inputExpanded ? undefined : { maxHeight: '38%' }}
          >
            <div className="flex items-center gap-2 border-b border-border px-3 py-2 text-xs text-muted-foreground">
              <Terminal size={14} />
              <span>测试入参（context / actions / horizon）</span>
              {!testInputStatus.ok && (
                <span className="text-[11px] text-destructive">JSON 无效</span>
              )}
              <button
                type="button"
                onClick={() => setInputExpanded(value => !value)}
                aria-label={inputExpanded ? '收起入参编辑区' : '放大入参编辑区'}
                title={inputExpanded ? '收起，恢复下方结果区' : '放大为整列编辑长序列参数'}
                className="ml-auto inline-flex h-6 w-6 items-center justify-center rounded text-muted-foreground transition-colors hover:bg-muted hover:text-muted-foreground"
              >
                {inputExpanded ? <Minimize2 size={13} /> : <Maximize2 size={13} />}
              </button>
            </div>
            <textarea
              value={testInputText}
              onChange={event => { setTestInputText(event.target.value); setValidatedKey(null) }}
              spellCheck={false}
              className="min-h-[110px] flex-1 resize-none px-3 py-2 font-mono text-xs leading-5 text-foreground focus:outline-none"
              aria-label="测试入参 JSON"
              aria-invalid={!testInputStatus.ok}
            />
            {!testInputStatus.ok && (
              <div className="flex items-start gap-1.5 border-t border-[var(--color-danger-bg)] bg-[var(--color-danger-bg)] px-3 py-1.5 text-[11px] leading-4 text-destructive">
                <AlertCircle size={12} className="mt-0.5 shrink-0" />
                <span className="break-words">测试入参 JSON 无效：{testInputStatus.message}</span>
              </div>
            )}
          </section>

          {!inputExpanded && (
          <section className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border border-border bg-card">
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
                  点击「执行」在内核中试运行 simulate(context, actions, horizon)，此处展示返回值与标准输出。
                </p>
              ) : (
                <div className="space-y-3">
                  {result.ok ? (
                    trajectory ? (
                      <div>
                        <p className="mb-1.5 text-[11px] font-medium text-muted-foreground">simulate 返回值 · 轨迹预览</p>
                        <TrajectoryPreview summary={trajectory} payload={result.payload} />
                      </div>
                    ) : (
                      <div>
                        <p className="mb-1 text-[11px] font-medium text-muted-foreground">simulate 返回值</p>
                        <pre className="overflow-auto rounded-lg bg-muted p-2.5 text-xs leading-5 text-foreground">
                          {JSON.stringify(result.payload, null, 2)}
                        </pre>
                      </div>
                    )
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
          )}
        </div>
      </div>

      {/* 历史版本抽屉 */}
      <Sheet open={showVersions} onOpenChange={setShowVersions}>
        <SheetContent className="w-[420px] overflow-y-auto sm:max-w-[420px]">
          <SheetHeader>
            <SheetTitle>历史版本</SheetTitle>
            <SheetDescription>每次保存冻结一版，最多保留 20 版；恢复后需重新执行并保存。</SheetDescription>
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
                      <p className="text-sm font-medium text-foreground">v{version.version_no}</p>
                      <p className="mt-0.5 text-[11px] text-muted-foreground">
                        {formatDateTime(version.created_at)} · {version.duration_ms} ms
                      </p>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <button
                        type="button"
                        onClick={() => void toggleVersionPreview(version)}
                        aria-expanded={previewVersionId === version.id}
                        className="inline-flex h-7 items-center rounded-md border border-border px-2.5 text-xs text-muted-foreground transition-colors hover:bg-muted"
                      >
                        {previewVersionId === version.id ? '收起' : '查看'}
                      </button>
                      <button
                        type="button"
                        onClick={() => { setShowVersions(false); setConfirmRestoreVersionId(version.id) }}
                        className="inline-flex h-7 items-center gap-1 rounded-md border border-border px-2.5 text-xs text-muted-foreground transition-colors hover:bg-muted"
                      >
                        <RotateCcw size={12} /> 恢复
                      </button>
                    </div>
                  </div>
                  {previewVersionId === version.id && (
                    <div className="mt-2.5 space-y-2 border-t border-border pt-2.5">
                      {(() => {
                        const detail = versionDetails[version.id]
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
                              <p className="mb-1 text-[11px] font-medium text-muted-foreground">该版测试入参</p>
                              <pre className="max-h-28 overflow-auto rounded-md bg-muted p-2 font-mono text-[11px] leading-4 text-muted-foreground">
                                {detail.test_input ? JSON.stringify(detail.test_input, null, 2) : '（未记录测试入参）'}
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
            <SheetTitle>推演脚本契约</SheetTitle>
            <SheetDescription>平台与推演模型之间的统一接口约定</SheetDescription>
          </SheetHeader>
          <div className="mt-4 space-y-4 text-xs leading-6 text-muted-foreground">
            <section>
              <h3 className="text-sm font-semibold text-foreground">入口函数</h3>
              <pre className="mt-2 overflow-auto rounded-lg bg-muted p-3 font-mono text-[11px] leading-5">
{`def simulate(context, actions, horizon):
    ...
    return {...}  # JSON 可序列化`}
              </pre>
            </section>
            <section>
              <h3 className="text-sm font-semibold text-foreground">参数</h3>
              <ul className="mt-1 list-disc space-y-1 pl-4">
                <li><code>context: dict</code> — 当前状态快照（来自数字孪生/知识图谱的业务对象状态）</li>
                <li><code>actions: list</code> — 候选行动列表；无干预推演（纯预测）时为空列表</li>
                <li><code>horizon: int</code> — 推演时域（步数/期数，语义由模型自行定义）</li>
              </ul>
            </section>
            <section>
              <h3 className="text-sm font-semibold text-foreground">返回值</h3>
              <p className="mt-1">JSON 可序列化的 dict，建议包含 <code>trajectory</code>（各时点轨迹）、<code>confidence</code>（置信度）、<code>boundary</code>（适用边界说明）。</p>
              <p className="mt-1 text-muted-foreground">当 <code>trajectory</code> 为数值序列或等宽的数值二维数组时，执行结果面板会自动绘制轨迹折线预览，原始 JSON 折叠保留。</p>
            </section>
            <section>
              <h3 className="text-sm font-semibold text-foreground">可用的时序/科学计算库</h3>
              <p className="mt-1">
                脚本在平台 Python 内核中执行，可直接使用 numpy、pandas、duckdb 与
                statsmodels（ARIMA/SARIMAX、指数平滑、ACF/PACF 等）。
                点击页头「时序示例」可一键插入官方 ARIMA/SARIMA 建模与预测脚本。
              </p>
            </section>
            <section>
              <h3 className="text-sm font-semibold text-foreground">保存规则</h3>
              <ul className="mt-1 list-disc space-y-1 pl-4">
                <li>只有当前内容与测试入参执行通过，保存按钮才可用</li>
                <li>保存时服务端会重新执行复核（双重保障），通过才落库</li>
                <li>每次保存冻结一个历史版本，可随时恢复</li>
              </ul>
            </section>
          </div>
        </SheetContent>
      </Sheet>

      <ConfirmDialog
        open={confirmInsertTs}
        title="插入时序推演示例？"
        description="当前未保存的修改将被示例脚本覆盖丢弃，此操作无法撤销。建议先执行并保存当前内容。"
        confirmText="插入示例"
        onConfirm={() => void insertTimeSeriesTemplate()}
        onClose={() => setConfirmInsertTs(false)}
      />
      <ConfirmDialog
        open={confirmRevert}
        title="恢复到已保存内容？"
        description="当前未保存的修改将被丢弃，此操作无法撤销。"
        confirmText="恢复"
        onConfirm={revertToSaved}
        onClose={() => setConfirmRevert(false)}
      />
      <ConfirmDialog
        open={confirmRestoreVersionId !== null}
        title="恢复到该历史版本？"
        description="当前编辑器中的未保存修改将被覆盖丢弃；若该版本记录了测试入参，入参编辑区也会一并回退。恢复后需重新执行并保存。"
        confirmText="恢复"
        onConfirm={() => { if (confirmRestoreVersionId) void restoreVersion(confirmRestoreVersionId) }}
        onClose={() => setConfirmRestoreVersionId(null)}
      />
      {project && (
        <PublishServiceDialog
          open={publishOpen}
          onClose={() => setPublishOpen(false)}
          project={project}
          versions={publishVersions}
          service={services[0] ?? null}
          onPublished={() => {
            // 多本体发布：重新拉取服务列表，同步项目卡摘要（状态取最近更新的服务）
            worldModelApi.getServices(project.id)
              .then(rows => {
                setServices(rows)
                setProject(current => current ? {
                  ...current,
                  status: 'published',
                  service_count: rows.length,
                  service_status: rows.length
                    ? (rows[0].status === 'offline' ? 'offline' as const : 'online' as const)
                    : null,
                } : current)
              })
              .catch(() => undefined)
          }}
        />
      )}
    </div>
  )
}
