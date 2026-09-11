import { useCallback, useEffect, useRef, useState } from 'react'
import {
  AlertCircle, Brain, CheckCircle2, Clock, FileArchive, FilePlus2, FileText, FolderPlus,
  FolderPen, FolderMinus, Image as ImageIcon, Loader2, Maximize2, Minimize2, Pencil,
  RefreshCw, Trash2, Upload, X,
} from 'lucide-react'

import {
  superAssistantApi,
  type PalaceFile,
  type PalaceFilePreview,
  type PalaceFolder,
  type PalaceGraph,
  type PalaceImportResult,
} from '@/api/superAssistant'
import { Button } from '@/components/ui/Button'
import { toast } from 'sonner'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import PalaceFileTree, { type PalaceInlineAction } from './PalaceFileTree'
import PalaceGraphPanel from './PalaceGraphPanel'
import PalaceMarkdown from './palaceMarkdown'
import { joinPalacePath } from './palaceTreeModel'

interface MemoryPalaceDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

/** 与后端记忆宫殿白名单同口径：文档白名单 + 图片（图片仅存储与预览） */
const PALACE_ACCEPT = '.csv,.xlsx,.xls,.json,.xml,.pdf,.docx,.doc,.pptx,.ppt,.md,.txt,.png,.jpg,.jpeg,.gif,.webp'

const STATUS_META: Record<string, { label: string; className: string }> = {
  draft: { label: '草稿', className: 'bg-slate-100 text-slate-500' },
  pending: { label: '待抽取', className: 'bg-slate-100 text-slate-500' },
  building: { label: '抽取中', className: 'bg-amber-50 text-amber-600' },
  built: { label: '已建图', className: 'bg-brand-soft text-brand-ink' },
  failed: { label: '失败', className: 'bg-red-50 text-red-600' },
}

interface PalaceEditorState {
  fileId: string
  filename: string
  loading: boolean
  /** 非空表示该文件不可在线编辑（格式不支持 / 内容超限），仅展示原因 */
  unsupported: string | null
  draft: string
  initial: string
  saving: boolean
}

interface PalacePreviewState {
  fileId: string
  loading: boolean
  data: PalaceFilePreview | null
}

const formatSize = (size: number) => {
  if (!Number.isFinite(size) || size <= 0) return '0 B'
  if (size >= 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`
  if (size >= 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${size} B`
}

/** apiClientV2 失败时 reject 的是响应体（FastAPI {detail}），优先透出其中文 detail（如 429 配额提示） */
const palaceError = (err: unknown, fallback: string): string => {
  const candidate = err as { detail?: unknown; message?: unknown } | null
  const text = candidate?.detail ?? candidate?.message
  return typeof text === 'string' && text ? text : fallback
}

/**
 * 记忆宫殿三栏工作台：左=文件树（上传/ZIP 导入入口），中=选中文档的
 * 阅读/编辑/图片预览，右=知识图谱（与文件双向联动：选中文件高亮其贡献
 * 节点，点节点定位来源文档）。
 */
export default function MemoryPalaceDialog({ open, onOpenChange }: MemoryPalaceDialogProps) {
  const [files, setFiles] = useState<PalaceFile[]>([])
  const [folders, setFolders] = useState<PalaceFolder[]>([])
  const [graph, setGraph] = useState<PalaceGraph | null>(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [selectedFileId, setSelectedFileId] = useState<string | null>(null)
  /** 当前选中目录（空串=根目录）：新建目录/笔记/上传的落点 */
  const [selectedDirPath, setSelectedDirPath] = useState('')
  /** 树内联输入行（新建目录/新建笔记/目录重命名），由工具栏按钮驱动 */
  const [inline, setInline] = useState<PalaceInlineAction | null>(null)
  const [preview, setPreview] = useState<PalacePreviewState | null>(null)
  const [editor, setEditor] = useState<PalaceEditorState | null>(null)
  const [imageUrl, setImageUrl] = useState<{ fileId: string; url: string } | null>(null)
  const [imageLoading, setImageLoading] = useState(false)
  const [importResult, setImportResult] = useState<PalaceImportResult | null>(null)
  const [showSkipped, setShowSkipped] = useState(false)
  const [replaceTarget, setReplaceTarget] = useState<string | null>(null)
  /** md 预览形态：默认渲染排版，可切源码（抽取文本即 markdown，txt 恒为源码） */
  const [previewMode, setPreviewMode] = useState<'render' | 'source'>('render')
  /** 工作台全屏：占满视口并放宽图谱栏（Esc 仍关闭弹窗） */
  const [maximized, setMaximized] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const zipInputRef = useRef<HTMLInputElement>(null)
  const replaceInputRef = useRef<HTMLInputElement>(null)

  /** 错误统一走 toast（error 音调自动 6s 消失）：内联横幅插拔会把三栏顶得上下跳动 */
  const showError = useCallback((err: unknown, fallback: string) => {
    toast.error(fallback, { description: palaceError(err, '') || undefined })
  }, [toast])

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const [fileRows, folderRows, graphData] = await Promise.all([
        superAssistantApi.palaceFiles(),
        superAssistantApi.palaceFolders(),
        superAssistantApi.palaceGraph(),
      ])
      setFiles(fileRows)
      setFolders(folderRows)
      setGraph(graphData)
    } catch (err) {
      showError(err, '知识图谱加载失败')
    } finally {
      setLoading(false)
    }
  }, [showError])

  useEffect(() => {
    if (open) void refresh()
  }, [open, refresh])

  // 弹窗关闭时清掉一次性视图状态，重开时重新开始
  useEffect(() => {
    if (!open) {
      setSelectedFileId(null)
      setSelectedDirPath('')
      setInline(null)
      setPreview(null)
      setEditor(null)
      setImportResult(null)
      setShowSkipped(false)
      setReplaceTarget(null)
      setPreviewMode('render')
      setMaximized(false)
    }
  }, [open])

  // 抽取进行中的轻量轮询：全部定格后自动停
  useEffect(() => {
    if (!open) return
    if (!files.some(item => item.status === 'pending' || item.status === 'building')) return
    const timer = setInterval(() => { void refresh() }, 4000)
    return () => clearInterval(timer)
  }, [open, files, refresh])

  const selected = files.find(file => file.id === selectedFileId) ?? null
  const editorDirty = editor !== null && !editor.loading && editor.unsupported === null && editor.draft !== editor.initial

  /** 编辑器有未保存修改时先确认；返回是否允许离开（同时关闭编辑器） */
  const requestLeaveEditor = () => {
    if (!editor) return true
    if (editorDirty && !window.confirm('当前编辑内容尚未保存，确定离开吗？离开后未保存的修改将丢失。')) return false
    setEditor(null)
    return true
  }

  const handleDialogOpenChange = (next: boolean) => {
    if (!next && !requestLeaveEditor()) return
    onOpenChange(next)
  }

  /** 选中文件（树点击 / 图谱节点定位 / 上传后自动选中）；切换前守住未保存编辑 */
  const handleSelectFile = useCallback((file: PalaceFile) => {
    if (editor && editor.fileId !== file.id && !requestLeaveEditor()) return
    setSelectedFileId(file.id)
    // 选中文件即把「当前目录」对齐到其归属目录（新建/上传落点跟随）
    setSelectedDirPath(file.path ?? '')
  }, [editor])

  /** 选中目录（树点击）：清掉文件选中，中间栏回到空态 */
  const handleSelectDir = useCallback((path: string) => {
    setSelectedFileId(null)
    setSelectedDirPath(path)
  }, [])

  const handleUpload = async (list: FileList | null) => {
    if (!list || list.length === 0) return
    setBusy(true)
    try {
      let last: PalaceFile | null = null
      for (const file of Array.from(list)) {
        last = await superAssistantApi.uploadPalaceFile(file, selectedDirPath)
      }
      await refresh()
      if (last) setSelectedFileId(last.id)
    } catch (err) {
      showError(err, '上传失败')    } finally {
      setBusy(false)
      if (inputRef.current) inputRef.current.value = ''
    }
  }

  const handleImportZip = async (list: FileList | null) => {
    const archive = list?.[0]
    if (!archive) return
    setBusy(true)
    try {
      const result = await superAssistantApi.importPalaceZip(archive)
      setImportResult(result)
      setShowSkipped(false)
      await refresh()
    } catch (err) {
      showError(err, 'ZIP 导入失败')    } finally {
      setBusy(false)
      if (zipInputRef.current) zipInputRef.current.value = ''
    }
  }

  const handleReplace = async (list: FileList | null) => {
    const file = list?.[0]
    const targetId = replaceTarget
    if (!file || !targetId) return
    setBusy(true)
    try {
      await superAssistantApi.replacePalaceFile(targetId, file)
      toast.success('文件已替换', { description: '新文件将自动重新抽取实体与关系。' })
      await refresh()
    } catch (err) {
      showError(err, '替换失败')    } finally {
      setBusy(false)
      setReplaceTarget(null)
      if (replaceInputRef.current) replaceInputRef.current.value = ''
    }
  }

  const handleDelete = async (fileId: string) => {
    setBusy(true)
    try {
      await superAssistantApi.deletePalaceFile(fileId)
      if (selectedFileId === fileId) setSelectedFileId(null)
      await refresh()
    } catch (err) {
      showError(err, '删除失败')    } finally {
      setBusy(false)
    }
  }

  const handleRebuild = async (fileId: string) => {
    setBusy(true)
    try {
      await superAssistantApi.rebuildPalaceFile(fileId)
      await refresh()
    } catch (err) {
      showError(err, '重建失败')    } finally {
      setBusy(false)
    }
  }

  const handleEdit = async (file: PalaceFile) => {
    if (!requestLeaveEditor()) return
    setEditor({ fileId: file.id, filename: file.filename, loading: true, unsupported: null, draft: '', initial: '', saving: false })
    try {
      const data = await superAssistantApi.palaceFilePreview(file.id)
      setEditor(prev => {
        if (!prev || prev.fileId !== file.id) return prev
        if (!data.previewable) return { ...prev, loading: false, unsupported: '该格式暂不支持在线编辑，仅 md/txt 可编辑。' }
        if (data.truncated) return { ...prev, loading: false, unsupported: '文件内容超出在线编辑上限，请本地编辑后重新上传。' }
        return { ...prev, loading: false, draft: data.content, initial: data.content }
      })
    } catch (err) {
      setEditor(prev => (!prev || prev.fileId !== file.id)
        ? prev
        : { ...prev, loading: false, unsupported: palaceError(err, '内容加载失败') })
    }
  }

  const handleSaveContent = async () => {
    if (!editor || editor.loading || editor.unsupported !== null) return
    setEditor(prev => prev ? { ...prev, saving: true } : prev)
    try {
      await superAssistantApi.updatePalaceFileContent(editor.fileId, editor.draft)
      setEditor(null)
      toast.success('已保存，图谱重建已排队', { description: '新内容将自动重新抽取实体与关系。' })
      await refresh()
    } catch (err) {
      setEditor(prev => prev ? { ...prev, saving: false } : prev)
      showError(err, '保存失败')    }
  }

  // ----- 目录管理（新建/重命名/删除/拖拽移动）与内联输入行 -----------------

  const selectedDirRow = folders.find(folder => folder.path === selectedDirPath) ?? null

  const handleCreateFolder = async (parentPath: string, name: string) => {
    setBusy(true)
    try {
      const created = await superAssistantApi.createPalaceFolder(joinPalacePath(parentPath, name))
      await refresh()
      setSelectedFileId(null)
      setSelectedDirPath(created.path)
    } catch (err) {
      showError(err, '新建目录失败')
    } finally {
      setBusy(false)
    }
  }

  const handleCreateNote = async (dirPath: string, filename: string) => {
    // 笔记创建仅 md：只填笔记名即可——.txt 后缀归一为 .md，缺失后缀自动补 .md
    const baseName = filename.replace(/\.txt$/i, '')
    const noteName = /\.md$/i.test(baseName) ? baseName : `${baseName}.md`
    setBusy(true)
    try {
      const created = await superAssistantApi.createPalaceNote(noteName, dirPath)
      await refresh()
      setSelectedFileId(created.id)
      setSelectedDirPath(created.path)
      void handleEdit(created)
    } catch (err) {
      showError(err, '新建笔记失败')
    } finally {
      setBusy(false)
    }
  }

  const handleRenameFolder = async (folderId: string, newPath: string) => {
    setBusy(true)
    try {
      const updated = await superAssistantApi.renamePalaceFolder(folderId, newPath)
      await refresh()
      setSelectedFileId(null)
      setSelectedDirPath(updated.path)
    } catch (err) {
      showError(err, '目录重命名失败')
    } finally {
      setBusy(false)
    }
  }

  const handleDeleteFolder = async () => {
    const row = selectedDirRow
    if (!row) return
    if (!window.confirm(`确定删除目录「${row.path}」吗？目录下仍有文件或子目录时将无法删除。`)) return
    setBusy(true)
    try {
      await superAssistantApi.deletePalaceFolder(row.id)
      setSelectedDirPath('')
      await refresh()
    } catch (err) {
      showError(err, '删除目录失败')
    } finally {
      setBusy(false)
    }
  }

  const handleMoveFile = async (fileId: string, targetPath: string) => {
    setBusy(true)
    try {
      await superAssistantApi.movePalaceFile(fileId, targetPath)
      await refresh()
    } catch (err) {
      showError(err, '移动文件失败')
    } finally {
      setBusy(false)
    }
  }

  const handleMoveFolder = async (folderId: string, targetPath: string) => {
    const source = folders.find(folder => folder.id === folderId)
    if (!source) return
    const name = source.path.slice(source.path.lastIndexOf('/') + 1)
    setBusy(true)
    try {
      await superAssistantApi.renamePalaceFolder(folderId, joinPalacePath(targetPath, name))
      await refresh()
    } catch (err) {
      showError(err, '移动目录失败')
    } finally {
      setBusy(false)
    }
  }

  /** 内联输入行提交：按动作分发（目录名/文件名在各自 handler 内再做校验） */
  const handleInlineSubmit = (value: string) => {
    const action = inline
    setInline(null)
    const name = value.trim()
    if (!action || !name || busy) return
    if (action.kind === 'new-folder') void handleCreateFolder(action.targetPath, name)
    else if (action.kind === 'new-note') void handleCreateNote(action.targetPath, name)
    else if (action.dirId) {
      const parent = action.targetPath.includes('/')
        ? action.targetPath.slice(0, action.targetPath.lastIndexOf('/'))
        : ''
      void handleRenameFolder(action.dirId, joinPalacePath(parent, name))
    }
  }

  // 选中文档（非图片、非编辑态）时自动加载抽取文本预览
  useEffect(() => {
    if (!open || !selected || selected.isImage) return
    if (editor?.fileId === selected.id) return
    let stale = false
    setPreview({ fileId: selected.id, loading: true, data: null })
    superAssistantApi.palaceFilePreview(selected.id)
      .then(data => { if (!stale) setPreview({ fileId: selected.id, loading: false, data }) })
      .catch(err => {
        if (stale) return
        setPreview(null)
        showError(err, '预览加载失败')
      })
    return () => { stale = true }
  }, [open, selected?.id, selected?.isImage, editor?.fileId])

  // 选中图片时经鉴权 axios 拉取原始字节并转 objectURL（img 标签带不了 Authorization）
  useEffect(() => {
    if (!open || !selected || !selected.isImage) return
    let revoked = false
    let created: string | null = null
    setImageLoading(true)
    superAssistantApi.palaceFileRaw(selected.id)
      .then(blob => {
        const url = URL.createObjectURL(blob)
        created = url
        if (revoked) URL.revokeObjectURL(url)
        else setImageUrl({ fileId: selected.id, url })
      })
      .catch(err => showError(err, '图片加载失败'))
      .finally(() => { if (!revoked) setImageLoading(false) })
    return () => {
      revoked = true
      if (created) URL.revokeObjectURL(created)
      setImageUrl(null)
    }
  }, [open, selected?.id, selected?.isImage])

  const building = files.some(item => item.status === 'pending' || item.status === 'building')
  const extracting = selected?.status === 'pending' || selected?.status === 'building'
  const editingThis = editor !== null && editor.fileId === selected?.id
  const isMarkdown = (selected?.filename ?? '').toLowerCase().endsWith('.md')

  const renderMiddleBody = () => {
    if (!selected) {
      return (
        <div className="flex flex-1 flex-col items-center justify-center gap-1 px-4 py-8 text-center">
          <FileText size={18} className="text-[var(--color-text-tertiary)]" />
          <p className="text-xs text-[var(--color-text-tertiary)]">
            在左侧选择一个文档查看内容；md/txt 可在线编辑，保存后自动重建图谱。
          </p>
        </div>
      )
    }
    if (editingThis) {
      return (
        <div data-testid="palace-file-editor" className="flex min-h-0 flex-1 flex-col gap-2 p-3">
          {editor.loading ? (
            <p className="flex items-center gap-1.5 text-xs text-[var(--color-text-tertiary)]">
              <Loader2 size={12} className="animate-spin" /> 正在加载文件内容…
            </p>
          ) : editor.unsupported ? (
            <p className="text-xs text-[var(--color-text-tertiary)]">{editor.unsupported}</p>
          ) : (
            <>
              <textarea
                aria-label={`编辑 ${editor.filename} 内容`}
                value={editor.draft}
                spellCheck={false}
                onChange={event => setEditor(prev => prev ? { ...prev, draft: event.target.value } : prev)}
                className="min-h-[220px] flex-1 w-full resize-none rounded-md border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-2 font-mono text-xs leading-5 text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
              <div className="flex items-center gap-2">
                <Button
                  size="sm"
                  onClick={() => { void handleSaveContent() }}
                  loading={editor.saving}
                  disabled={!editorDirty}
                >
                  保存并重建图谱
                </Button>
                <Button variant="outline" size="sm" onClick={requestLeaveEditor} disabled={editor.saving}>
                  取消
                </Button>
                {editorDirty && <span className="text-[11px] text-amber-600">有未保存修改</span>}
              </div>
            </>
          )}
        </div>
      )
    }
    if (selected.isImage) {
      return (
        <div data-testid="palace-file-image" className="flex min-h-0 flex-1 items-center justify-center overflow-auto p-3">
          {imageLoading ? (
            <p className="flex items-center gap-1.5 text-xs text-[var(--color-text-tertiary)]">
              <Loader2 size={12} className="animate-spin" /> 图片加载中…
            </p>
          ) : imageUrl?.fileId === selected.id ? (
            <img
              src={imageUrl.url}
              alt={selected.filename}
              className="max-h-full max-w-full rounded-md object-contain"
            />
          ) : (
            <p className="text-xs text-[var(--color-text-tertiary)]">图片加载失败，可刷新重试。</p>
          )}
        </div>
      )
    }
    return (
      <div data-testid="palace-file-preview" className="flex min-h-0 flex-1 flex-col p-3">
        {preview?.fileId === selected.id ? (
          preview.loading ? (
            <p className="flex items-center gap-1.5 text-xs text-[var(--color-text-tertiary)]">
              <Loader2 size={12} className="animate-spin" /> 内容加载中…
            </p>
          ) : !preview.data || !preview.data.previewable ? (
            <p className="text-xs text-[var(--color-text-tertiary)]">该格式暂不支持文本预览，可下载替换或重建图谱。</p>
          ) : !preview.data.content ? (
            /* 可预览但内容为空（新建草稿笔记）：给出行动指引而非空白 */
            <p className="text-xs text-[var(--color-text-tertiary)]">空笔记，点击右上「编辑」开始书写；保存后自动重建图谱。</p>
          ) : (
            <>
              {isMarkdown ? (
                previewMode === 'render' ? (
                  <div className="min-h-0 flex-1 overflow-y-auto pr-1">
                    <PalaceMarkdown text={preview.data.content} />
                  </div>
                ) : (
                  <pre className="min-h-0 flex-1 overflow-auto whitespace-pre-wrap break-all rounded-md bg-[var(--color-bg-elevated)] p-2 font-mono text-xs leading-5 text-[var(--color-text-primary)]">
                    {preview.data.content}
                  </pre>
                )
              ) : (
                <pre className="min-h-0 flex-1 overflow-auto whitespace-pre-wrap break-all rounded-md bg-[var(--color-bg-elevated)] p-2 font-mono text-xs leading-5 text-[var(--color-text-primary)]">
                  {preview.data.content}
                </pre>
              )}
              {preview.data.truncated && (
                <p className="mt-1 shrink-0 text-[11px] text-amber-600">内容已截断，仅展示前 60000 字符。</p>
              )}
            </>
          )
        ) : (
          <p className="flex items-center gap-1.5 text-xs text-[var(--color-text-tertiary)]">
            <Loader2 size={12} className="animate-spin" /> 内容加载中…
          </p>
        )}
      </div>
    )
  }

  return (
    <Dialog open={open} onOpenChange={handleDialogOpenChange}>
      <DialogContent className={maximized
          ? 'flex h-dvh w-screen max-w-none translate-x-0 translate-y-0 left-0 top-0 flex-col overflow-hidden rounded-none border-0'
          : 'flex h-[min(88dvh,880px)] w-[min(96vw,88rem)] flex-col overflow-hidden'}
        >
        <button
          type="button"
          data-testid="palace-fullscreen"
          aria-pressed={maximized}
          aria-label={maximized ? '退出全屏' : '全屏'}
          title={maximized ? '退出全屏' : '全屏'}
          onClick={() => setMaximized(value => !value)}
          className="absolute right-12 top-4 flex h-7 w-7 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-100 hover:text-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {maximized ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
        </button>
        <DialogHeader icon={<Brain size={18} />}>
          <DialogTitle className="flex items-center gap-2">
            知识图谱
            {building && (
              <span className="ml-2 flex items-center gap-1 text-xs font-normal text-amber-600">
                <Loader2 size={12} className="animate-spin" /> 图谱构建中…
              </span>
            )}
          </DialogTitle>
          <DialogDescription>
            上传的文档沉淀为跨会话长期知识：自动抽取实体关系构建图谱，选中文件可阅读编辑，图谱与文档双向联动。
          </DialogDescription>
        </DialogHeader>

        <div className={`grid min-h-0 flex-1 grid-cols-1 gap-3 overflow-y-auto lg:overflow-hidden ${
            maximized
              ? 'lg:grid-cols-[280px_minmax(0,1fr)_clamp(520px,40vw,760px)]'
              : 'lg:grid-cols-[248px_minmax(0,1fr)_clamp(400px,32vw,500px)]'
          }`}>

          {/* 左：文件树 */}
          <aside
            aria-label="知识图谱文件库"
            data-testid="super-assistant-palace-files"
            className="flex min-h-[240px] flex-col rounded-xl border border-[var(--color-border)] bg-white lg:min-h-0"
          >
            <input
              ref={inputRef}
              data-testid="palace-file-input"
              type="file"
              multiple
              accept={PALACE_ACCEPT}
              className="hidden"
              onChange={event => { void handleUpload(event.target.files) }}
            />
            <input
              ref={zipInputRef}
              data-testid="palace-zip-input"
              type="file"
              accept=".zip"
              className="hidden"
              onChange={event => { void handleImportZip(event.target.files) }}
            />
            <input
              ref={replaceInputRef}
              data-testid="palace-replace-input"
              type="file"
              accept={PALACE_ACCEPT}
              className="hidden"
              onChange={event => { void handleReplace(event.target.files) }}
            />
            <div className="flex items-center gap-1 border-b border-[var(--color-border)] px-2 py-1.5">
              <Button size="sm" className="h-7 px-2 text-xs" onClick={() => inputRef.current?.click()} loading={busy}>
                <Upload size={12} /> 上传
              </Button>
              <Button size="sm" variant="outline" className="h-7 px-2 text-xs" onClick={() => zipInputRef.current?.click()} disabled={busy}>
                <FileArchive size={12} /> ZIP
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="h-7 px-2 text-xs"
                onClick={() => { void refresh() }}
                disabled={loading || busy}
                aria-label="刷新文件库"
              >
                <RefreshCw size={12} className={loading ? 'animate-spin' : undefined} />
              </Button>
              <span className="ml-auto text-[10px] tabular-nums text-[var(--color-text-tertiary)]">{files.length} 个文件</span>
            </div>
            <div
              data-testid="palace-dir-toolbar"
              className="flex items-center gap-0.5 border-b border-[var(--color-border)] px-2 py-1"
            >
              <span
                className="min-w-0 flex-1 truncate text-[10px] text-[var(--color-text-tertiary)]"
                title={selectedDirPath ? selectedDirPath : '根目录'}
              >
                当前目录：/{selectedDirPath}
              </span>
              <Button
                variant="ghost"
                size="icon-sm"
                className="h-6 w-6 text-slate-400 hover:text-[var(--color-text-primary)]"
                onClick={() => setInline({ kind: 'new-folder', targetPath: selectedDirPath })}
                disabled={busy || inline !== null}
                title="在当前目录下新建子目录"
                aria-label="新建子目录"
                data-testid="palace-new-folder"
              >
                <FolderPlus size={13} />
              </Button>
              <Button
                variant="ghost"
                size="icon-sm"
                className="h-6 w-6 text-slate-400 hover:text-[var(--color-text-primary)]"
                onClick={() => setInline({ kind: 'new-note', targetPath: selectedDirPath })}
                disabled={busy || inline !== null}
                title="在当前目录下新建 md 笔记（文件名自动补 .md）"
                aria-label="新建笔记"
                data-testid="palace-new-note"
              >
                <FilePlus2 size={13} />
              </Button>
              <Button
                variant="ghost"
                size="icon-sm"
                className="h-6 w-6 text-slate-400 hover:text-[var(--color-text-primary)]"
                onClick={() => selectedDirRow && setInline({ kind: 'rename', targetPath: selectedDirRow.path, dirId: selectedDirRow.id })}
                disabled={busy || inline !== null || !selectedDirRow}
                title={selectedDirRow ? `重命名目录 ${selectedDirRow.path}` : '先在树中选中一个目录'}
                aria-label="重命名目录"
                data-testid="palace-rename-folder"
              >
                <FolderPen size={13} />
              </Button>
              <Button
                variant="ghost"
                size="icon-sm"
                className="h-6 w-6 text-slate-400 hover:bg-red-50 hover:text-red-600"
                onClick={() => { void handleDeleteFolder() }}
                disabled={busy || !selectedDirRow}
                title={selectedDirRow ? `删除空目录 ${selectedDirRow.path}` : '先在树中选中一个目录（仅可删除空目录）'}
                aria-label="删除目录"
                data-testid="palace-delete-folder"
              >
                <FolderMinus size={13} />
              </Button>
            </div>
            {importResult && (
              <div
                role="status"
                data-testid="palace-import-result"
                className="flex flex-col gap-1 border-b border-[var(--color-border)] bg-brand-soft px-3 py-2 text-xs text-brand-ink"
              >
                <div className="flex items-center gap-2">
                  <CheckCircle2 size={13} className="shrink-0" />
                  <span className="flex-1">导入 {importResult.created.length} 个，跳过 {importResult.skipped.length} 个（目录已按压缩包保留）</span>
                  {importResult.skipped.length > 0 && (
                    <button
                      type="button"
                      onClick={() => setShowSkipped(value => !value)}
                      aria-expanded={showSkipped}
                      className="rounded-md px-1.5 py-0.5 text-[11px] text-brand-ink transition-colors hover:bg-brand-mist focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      {showSkipped ? '收起跳过原因' : '查看跳过原因'}
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => setImportResult(null)}
                    aria-label="关闭导入结果"
                    className="flex h-5 w-5 shrink-0 items-center justify-center rounded-md text-brand-ink transition-colors hover:bg-brand-mist focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <X size={12} />
                  </button>
                </div>
                {showSkipped && importResult.skipped.length > 0 && (
                  <ul className="ml-5 list-disc space-y-0.5 text-[11px] text-brand-ink">
                    {importResult.skipped.map(item => (
                      <li key={item.filename}>{item.filename}：{item.reason}</li>
                    ))}
                  </ul>
                )}
              </div>
            )}
            {files.length === 0 && folders.length === 0 ? (
              <div className="flex flex-1 flex-col items-center justify-center gap-1 px-3 py-6 text-center">
                <FileText size={18} className="text-[var(--color-text-tertiary)]" />
                <p className="text-xs text-[var(--color-text-tertiary)]">
                  还没有文档。上传或导入 ZIP，自动抽取实体关系生成知识图谱。
                </p>
              </div>
            ) : (
              <PalaceFileTree
                files={files}
                folders={folders}
                selectedFileId={selectedFileId}
                selectedDirPath={selectedDirPath}
                onSelectFile={handleSelectFile}
                onSelectDir={handleSelectDir}
                onMoveFile={(fileId, targetPath) => { void handleMoveFile(fileId, targetPath) }}
                onMoveFolder={(folderId, targetPath) => { void handleMoveFolder(folderId, targetPath) }}
                inline={inline}
                onInlineSubmit={handleInlineSubmit}
                onInlineCancel={() => setInline(null)}
              />
            )}
          </aside>

          {/* 中：内容阅读/编辑/图片预览 */}
          <section
            aria-label="文档内容"
            className="flex min-h-[280px] flex-col rounded-xl border border-[var(--color-border)] bg-white lg:min-h-0"
          >
            {selected ? (
              <>
                <header className="flex flex-wrap items-center gap-2 border-b border-[var(--color-border)] px-3 py-2">
                  {selected.isImage ? (
                    <ImageIcon size={15} className="shrink-0 text-[var(--color-text-tertiary)]" />
                  ) : (
                    <FileText size={15} className="shrink-0 text-[var(--color-text-tertiary)]" />
                  )}
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm text-[var(--color-text-primary)]" title={selected.error || selected.filename}>
                      {selected.filename}
                    </p>
                    <p className="truncate text-[10px] tabular-nums text-[var(--color-text-tertiary)]">
                      {selected.path ? `${selected.path} · ` : ''}{formatSize(selected.size)} · 解析 {selected.extractedChars} 字符
                      {selected.status === 'built' && !selected.isImage && ` · ${selected.entityCount} 实体 / ${selected.relationCount} 关系`}
                      {selected.status === 'failed' && ' · 抽取失败，可重建'}
                    </p>
                  </div>
                  <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] ${STATUS_META[selected.status]?.className ?? STATUS_META.pending.className}`}>
                    {selected.status === 'building' && <Loader2 size={10} className="mr-0.5 inline animate-spin" />}
                    {selected.status === 'built' && <CheckCircle2 size={10} className="mr-0.5 inline" />}
                    {selected.status === 'failed' && <AlertCircle size={10} className="mr-0.5 inline" />}
                    {selected.status === 'pending' && <Clock size={10} className="mr-0.5 inline" />}
                    {STATUS_META[selected.status]?.label ?? '待抽取'}
                  </span>
                  {isMarkdown && !editingThis && (
                    <div
                      role="group"
                      aria-label="预览形态"
                      data-testid="palace-preview-mode"
                      className="flex shrink-0 items-center gap-0.5 rounded-lg bg-[var(--color-bg-hover)] p-0.5"
                    >
                      {([['render', '渲染'], ['source', '源码']] as const).map(([value, label]) => (
                        <button
                          key={value}
                          type="button"
                          aria-pressed={previewMode === value}
                          onClick={() => setPreviewMode(value)}
                          className={`h-5 rounded-md px-1.5 text-[11px] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                            previewMode === value
                              ? 'bg-white text-[var(--color-text-primary)] shadow-sm'
                              : 'text-[var(--color-text-secondary)] hover:text-[var(--color-text-primary)]'
                          }`}
                        >
                          {label}
                        </button>
                      ))}
                    </div>
                  )}
                  <span className="flex shrink-0 items-center gap-0.5">
                    {selected.editable && (
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        onClick={() => { void handleEdit(selected) }}
                        disabled={busy || extracting || editingThis}
                        title={extracting ? '抽取完成后才能编辑' : `编辑 ${selected.filename}`}
                        aria-label={`编辑 ${selected.filename}`}
                        className="h-6 w-6 text-slate-400 hover:text-[var(--color-text-primary)]"
                      >
                        <Pencil size={12} />
                      </Button>
                    )}
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      onClick={() => {
                        setReplaceTarget(selected.id)
                        replaceInputRef.current?.click()
                      }}
                      disabled={busy || extracting}
                      title={extracting ? '抽取完成后才能替换' : `用新文件替换 ${selected.filename}`}
                      aria-label={`替换 ${selected.filename}`}
                      className="h-6 w-6 text-slate-400 hover:text-[var(--color-text-primary)]"
                    >
                      <Upload size={12} />
                    </Button>
                    {(selected.status === 'failed' || (selected.status === 'built' && !selected.isImage)) && (
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        onClick={() => { void handleRebuild(selected.id) }}
                        disabled={busy}
                        title={`重建 ${selected.filename} 的知识图谱`}
                        aria-label={`重建 ${selected.filename} 的知识图谱`}
                        className="h-6 w-6 text-slate-400 hover:bg-amber-50 hover:text-amber-600"
                      >
                        <RefreshCw size={12} />
                      </Button>
                    )}
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      onClick={() => { void handleDelete(selected.id) }}
                      disabled={busy}
                      title={`删除 ${selected.filename}`}
                      aria-label={`删除 ${selected.filename}`}
                      className="h-6 w-6 text-slate-400 hover:bg-red-50 hover:text-red-600"
                    >
                      <Trash2 size={12} />
                    </Button>
                  </span>
                </header>
                {renderMiddleBody()}
              </>
            ) : (
              renderMiddleBody()
            )}
          </section>

          {/* 右：知识图谱 */}
          <aside
            aria-label="知识图谱面板"
            className="flex min-h-[360px] flex-col rounded-xl border border-[var(--color-border)] bg-white p-2 lg:min-h-0 lg:p-3"
          >
            <PalaceGraphPanel
              graph={graph}
              loading={loading}
              maximized={maximized}
              hasFiles={files.length > 0}
              files={files}
              selectedFileId={selectedFileId}
              onSelectFile={fileId => {
                const file = files.find(item => item.id === fileId)
                if (file) handleSelectFile(file)
              }}
              onRefresh={() => { void refresh() }}
              onError={showError}
            />
          </aside>
        </div>
      </DialogContent>
    </Dialog>
  )
}
