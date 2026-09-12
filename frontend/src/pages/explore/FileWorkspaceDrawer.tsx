import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Check, ChevronDown, ChevronRight, Copy, Download, Eye, File, FileCode2, FilePlus2, Folder,
  FolderOpen, FoldVertical, Loader2, Pencil, Save, Trash2, UnfoldVertical, X,
} from 'lucide-react'
import {
  explorationApi, type BxAttachment, type BxWorkspacePreview, type BxWorkspaceText,
} from '@/api/exploration'
import { writeTextToClipboard } from '@/utils/clipboard'
import Md from './Md'

const formatSize = (n: number) => n < 1024 ? `${n} B`
  : n < 1024 * 1024 ? `${(n / 1024).toFixed(0)} KB`
    : `${(n / 1024 / 1024).toFixed(1)} MB`

const errorText = (error: unknown, fallback: string) => {
  if (!error || typeof error !== 'object') return fallback
  const value = error as { detail?: string | { message?: string }; message?: string }
  const detail = value.detail
  if (typeof detail === 'string') return detail
  if (detail?.message) return detail.message
  return value.message || fallback
}

interface TreeDirectory {
  name: string
  path: string
  directories: Map<string, TreeDirectory>
  files: BxAttachment[]
}

type TreeRow =
  | { type: 'directory'; key: string; name: string; path: string; depth: number; childCount: number }
  | { type: 'file'; key: string; file: BxAttachment; name: string; depth: number }

const directoryPaths = (files: BxAttachment[]) => {
  const paths = new Set<string>()
  files.forEach(file => {
    const parts = (file.relativePath || file.filename).split('/').filter(Boolean)
    parts.slice(0, -1).forEach((_, index) => paths.add(parts.slice(0, index + 1).join('/')))
  })
  return paths
}

const buildTreeRows = (files: BxAttachment[], collapsed: Set<string>): TreeRow[] => {
  const root: TreeDirectory = { name: '', path: '', directories: new Map(), files: [] }
  files.forEach(file => {
    const parts = (file.relativePath || file.filename).split('/').filter(Boolean)
    let cursor = root
    parts.slice(0, -1).forEach(part => {
      const path = cursor.path ? `${cursor.path}/${part}` : part
      if (!cursor.directories.has(part)) {
        cursor.directories.set(part, { name: part, path, directories: new Map(), files: [] })
      }
      cursor = cursor.directories.get(part)!
    })
    cursor.files.push(file)
  })

  const rows: TreeRow[] = []
  const visit = (directory: TreeDirectory, depth: number) => {
    [...directory.directories.values()]
      .sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'))
      .forEach(child => {
        rows.push({
          type: 'directory', key: `dir-${child.path}`, name: child.name, path: child.path, depth,
          childCount: child.directories.size + child.files.length,
        })
        if (!collapsed.has(child.path)) visit(child, depth + 1)
      })
    directory.files
      .sort((a, b) => (a.filename || '').localeCompare(b.filename || '', 'zh-CN'))
      .forEach(file => rows.push({
        type: 'file', key: file.id, file,
        name: (file.relativePath || file.filename).split('/').pop() || file.filename,
        depth,
      }))
  }
  visit(root, 0)
  return rows
}

export default function FileWorkspaceDrawer({ sessionId, files, onFilesChange, onClose }: {
  sessionId: string
  files: BxAttachment[]
  onFilesChange: (files: BxAttachment[]) => void
  onClose: () => void
}) {
  const [activeId, setActiveId] = useState('')
  const [loadingId, setLoadingId] = useState('')
  const [editor, setEditor] = useState<BxWorkspaceText | null>(null)
  const [preview, setPreview] = useState<BxWorkspacePreview | null>(null)
  const [viewMode, setViewMode] = useState<'preview' | 'edit'>('preview')
  const [draft, setDraft] = useState('')
  const [newPath, setNewPath] = useState('')
  const [creating, setCreating] = useState(false)
  const [saving, setSaving] = useState(false)
  const [confirmId, setConfirmId] = useState('')
  const [error, setError] = useState('')
  const initialDirectoryPaths = useRef(directoryPaths(files)).current // 仅用于弹窗首次打开态
  const [collapsedDirs, setCollapsedDirs] = useState<Set<string>>(() => new Set(initialDirectoryPaths))
  const knownDirectoryPaths = useRef(new Set(initialDirectoryPaths))
  const [copied, setCopied] = useState(false)
  const previewRequestRef = useRef(0)

  const sorted = useMemo(() => [...files].sort((a, b) =>
    Date.parse(b.updatedAt || b.createdAt) - Date.parse(a.updatedAt || a.createdAt)), [files])
  const allDirectoryPaths = useMemo(() => directoryPaths(files), [files])
  const rows = useMemo(() => buildTreeRows(sorted, collapsedDirs), [sorted, collapsedDirs])
  const activeFile = files.find(file => file.id === activeId) || null

  // 文件可能在弹窗打开后才加载完成：新出现的目录仍保持默认折叠，同时保留用户
  // 已经手动展开的既有目录。
  useEffect(() => {
    const known = knownDirectoryPaths.current
    setCollapsedDirs(current => {
      const next = new Set([...current].filter(path => allDirectoryPaths.has(path)))
      allDirectoryPaths.forEach(path => {
        if (!known.has(path)) next.add(path)
      })
      return next
    })
    knownDirectoryPaths.current = new Set(allDirectoryPaths)
  }, [allDirectoryPaths])

  useEffect(() => {
    setCopied(false)
  }, [activeId])

  useEffect(() => {
    if (!copied) return
    const timer = window.setTimeout(() => setCopied(false), 1600)
    return () => window.clearTimeout(timer)
  }, [copied])

  const openFile = useCallback(async (file: BxAttachment) => {
    const requestId = ++previewRequestRef.current
    setActiveId(file.id)
    setCopied(false)
    setCreating(false)
    setViewMode('preview')
    setConfirmId('')
    setError('')
    setLoadingId(file.id)
    setEditor(null)
    setPreview(null)
    try {
      const content = await explorationApi.attachmentPreview(sessionId, file.id)
      if (requestId !== previewRequestRef.current) return
      setPreview(content)
      setDraft(content.content)
      if (content.editable) {
        setEditor({
          id: content.id,
          relativePath: content.relativePath,
          content: content.content,
          version: content.version,
        })
      }
    } catch (e) {
      if (requestId !== previewRequestRef.current) return
      setError(errorText(e, '文件预览失败'))
    } finally {
      if (requestId === previewRequestRef.current) setLoadingId('')
    }
  }, [sessionId])

  useEffect(() => {
    if (!activeId && !creating && sorted.length > 0) void openFile(sorted[0])
  }, [activeId, creating, openFile, sorted])

  const download = async (file: BxAttachment) => {
    setError(''); setLoadingId(file.id)
    try {
      const blob = await explorationApi.downloadAttachment(sessionId, file.id)
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = file.filename
      anchor.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      setError(errorText(e, '文件下载失败'))
    } finally { setLoadingId('') }
  }

  const copy = async () => {
    if (!editor) return
    setError('')
    try {
      await writeTextToClipboard(draft)
      setCopied(true)
    } catch (e) {
      setError(errorText(e, '文件内容复制失败'))
    }
  }

  const save = async () => {
    if (!editor || saving) return
    const targetId = editor.id
    const requestId = previewRequestRef.current
    setSaving(true); setError('')
    try {
      const updated = await explorationApi.updateWorkspaceText(sessionId, targetId, {
        content: draft, expectedVersion: editor.version,
      })
      if (requestId !== previewRequestRef.current) return
      onFilesChange(files.map(file => file.id === updated.id ? updated : file))
      setEditor({ ...editor, content: draft, version: updated.version, sha256: updated.sha256 })
      setPreview(current => current ? { ...current, content: draft, version: updated.version } : current)
      setViewMode('preview')
    } catch (e) {
      setError(errorText(e, '文件保存失败'))
    } finally { setSaving(false) }
  }

  const create = async () => {
    if (!newPath.trim() || saving) return
    setSaving(true); setError('')
    try {
      const created = await explorationApi.createWorkspaceText(sessionId, {
        path: newPath.trim(), content: draft,
      })
      onFilesChange([...files, created])
      setCreating(false); setNewPath('')
      await openFile(created)
    } catch (e) {
      setError(errorText(e, '文件创建失败'))
    } finally { setSaving(false) }
  }

  const remove = async (file: BxAttachment) => {
    setLoadingId(file.id); setError('')
    try {
      await explorationApi.deleteAttachment(sessionId, file.id)
      const remaining = files.filter(item => item.id !== file.id)
      onFilesChange(remaining)
      setActiveId('')
      setEditor(null)
      setPreview(null)
      setConfirmId('')
    } catch (e) {
      setError(errorText(e, '文件删除失败'))
    } finally { setLoadingId('') }
  }

  const beginCreate = () => {
    previewRequestRef.current += 1
    setCreating(true)
    setActiveId('')
    setEditor(null)
    setPreview(null)
    setNewPath('notes/业务记录.md')
    setDraft('')
    setError('')
  }

  const extension = (preview?.relativePath || '').split('.').pop()?.toLowerCase()
  const markdownPreview = extension === 'md'
  const htmlPreview = extension === 'html' || extension === 'htm'

  return (
    <div
      data-testid="workspace-dialog-backdrop"
      className="fixed inset-0 z-50 flex items-start justify-center bg-accent px-4 pt-[7vh] backdrop-blur-[1px]"
    >
      <section
        data-testid="workspace-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="workspace-dialog-title"
        className="flex h-[78vh] min-h-[520px] w-[1120px] max-w-[94vw] flex-col overflow-hidden rounded-xl border border-border bg-[var(--color-bg-elevated)] shadow-[0_24px_80px_rgba(15,23,42,0.22)]"
      >
        <header className="flex items-center justify-between border-b border-[var(--color-border)] px-5 py-3.5">
          <div>
            <h2 id="workspace-dialog-title" className="text-sm font-semibold text-[var(--color-text-primary)]">文件清单</h2>
            <p className="mt-0.5 text-[11px] text-[var(--color-text-tertiary)]">
              {files.length} 个文件 · 文本类可在线编辑，Office、PDF 等可查看抽取内容
            </p>
          </div>
          <button aria-label="关闭文件清单" onClick={onClose}
            className="rounded-md p-1.5 text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            <X size={17} />
          </button>
        </header>

        <div className="flex min-h-0 flex-1">
          <aside className="flex w-72 shrink-0 flex-col border-r border-[var(--color-border)] bg-muted">
            <div className="px-3 pb-2 pt-3">
              <button
                onClick={beginCreate}
                className="inline-flex w-full items-center justify-center gap-1.5 rounded-md border border-dashed border-brand-line bg-card px-3 py-2 text-xs font-medium text-brand-ink transition-colors hover:bg-brand-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <FilePlus2 size={14} /> 新建文本文件
              </button>
            </div>
            <div className="flex items-center gap-1.5 px-3 pb-1.5 pt-1 text-[10px] font-semibold uppercase tracking-[0.08em] text-[var(--color-text-tertiary)]">
              <Folder size={12} />
              <span>文件树</span>
              <div className="ml-auto flex items-center gap-0.5">
                <button
                  type="button"
                  aria-label="展开全部文件夹"
                  title="展开全部"
                  disabled={allDirectoryPaths.size === 0}
                  onClick={() => setCollapsedDirs(new Set())}
                  className="inline-flex h-6 w-6 items-center justify-center rounded text-[var(--color-text-tertiary)] transition-colors hover:bg-card hover:text-brand-ink active:scale-[0.96] disabled:cursor-not-allowed disabled:opacity-35 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <UnfoldVertical size={13} />
                </button>
                <button
                  type="button"
                  aria-label="折叠全部文件夹"
                  title="折叠全部"
                  disabled={allDirectoryPaths.size === 0}
                  onClick={() => setCollapsedDirs(new Set(allDirectoryPaths))}
                  className="inline-flex h-6 w-6 items-center justify-center rounded text-[var(--color-text-tertiary)] transition-colors hover:bg-card hover:text-brand-ink active:scale-[0.96] disabled:cursor-not-allowed disabled:opacity-35 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <FoldVertical size={13} />
                </button>
              </div>
            </div>
            <div className="scrollbar-thin min-h-0 flex-1 overflow-y-auto px-2 pb-3">
              {rows.length === 0 && (
                <div className="px-4 py-10 text-center text-xs leading-5 text-[var(--color-text-tertiary)]">
                  当前会话还没有文件。<br />可从对话输入框上传，或在此新建记录。
                </div>
              )}
              {rows.map(row => row.type === 'directory' ? (
                <button
                  key={row.key}
                  type="button"
                  aria-expanded={!collapsedDirs.has(row.path)}
                  aria-label={`${row.name}，下一级 ${row.childCount} 项`}
                  onClick={() => setCollapsedDirs(current => {
                    const next = new Set(current)
                    if (next.has(row.path)) next.delete(row.path); else next.add(row.path)
                    return next
                  })}
                  className="flex h-8 w-full items-center gap-1.5 rounded-md pr-2 text-left text-xs font-medium text-[var(--color-text-secondary)] transition-colors hover:bg-card"
                  style={{ paddingLeft: `${8 + row.depth * 16}px` }}
                >
                  {collapsedDirs.has(row.path) ? <ChevronRight size={13} /> : <ChevronDown size={13} />}
                  {collapsedDirs.has(row.path) ? <Folder size={14} className="text-[var(--color-warning)]" /> : <FolderOpen size={14} className="text-[var(--color-warning)]" />}
                  <span className="min-w-0 flex-1 truncate">{row.name}</span>
                  <span
                    aria-hidden="true"
                    className="ml-auto shrink-0 tabular-nums text-[10px] font-medium text-[var(--color-text-tertiary)]"
                  >
                    {row.childCount}
                  </span>
                </button>
              ) : (
                <button
                  key={row.key}
                  type="button"
                  onClick={() => void openFile(row.file)}
                  title={row.file.relativePath || row.file.filename}
                  className={`flex min-h-9 w-full items-center gap-2 rounded-md py-1.5 pr-2 text-left transition-colors ${activeId === row.file.id
                    ? 'bg-brand-soft text-brand-ink'
                    : 'text-[var(--color-text-secondary)] hover:bg-card'}`}
                  style={{ paddingLeft: `${25 + row.depth * 16}px` }}
                >
                  {loadingId === row.file.id
                    ? <Loader2 size={14} className="shrink-0 animate-spin text-brand-ink" />
                    : row.file.editable
                      ? <FileCode2 size={14} className="shrink-0 text-brand-ink" />
                      : <File size={14} className="shrink-0 text-muted-foreground" />}
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-xs font-medium">{row.name}</span>
                    <span className="mt-0.5 block text-[9px] text-[var(--color-text-tertiary)]">
                      {formatSize(row.file.fileSize)} · v{row.file.version}
                    </span>
                  </span>
                </button>
              ))}
            </div>
          </aside>

          <main className="flex min-w-0 flex-1 flex-col bg-[var(--color-bg-elevated)]">
            {creating ? (
              <>
                <div className="border-b border-[var(--color-border)] px-5 py-3">
                  <h3 className="text-xs font-semibold text-[var(--color-text-primary)]">新建文本文件</h3>
                  <p className="mt-0.5 text-[10px] text-[var(--color-text-tertiary)]">支持 md、txt、csv、json、xml、yaml 与 mermaid</p>
                </div>
                <div className="flex min-h-0 flex-1 flex-col p-5">
                  <label className="mb-1.5 text-[11px] font-medium text-[var(--color-text-secondary)]">会话内相对路径</label>
                  <input value={newPath} onChange={event => setNewPath(event.target.value)}
                    className="mb-3 rounded-md border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 py-2 text-xs outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring" />
                  <textarea value={draft} onChange={event => setDraft(event.target.value)} placeholder="输入文件内容…"
                    className="scrollbar-thin min-h-0 flex-1 resize-none rounded-md border border-[var(--color-border)] bg-[var(--color-bg-base)] p-4 font-mono text-xs leading-5 outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring" />
                  <div className="mt-3 flex justify-end">
                    <button onClick={() => void create()} disabled={!newPath.trim() || saving}
                      className="inline-flex h-8 items-center justify-center gap-1.5 rounded-md bg-brand px-3 text-xs font-medium text-[var(--color-text-inverse)] transition-colors hover:bg-brand-deep disabled:opacity-40">
                      {saving ? <Loader2 size={13} className="animate-spin" /> : <FilePlus2 size={13} />}创建文件
                    </button>
                  </div>
                </div>
              </>
            ) : activeFile ? (
              <>
                <div className="flex min-h-[58px] items-center justify-between gap-4 border-b border-[var(--color-border)] px-5 py-2.5">
                  <div className="min-w-0">
                    <div className="truncate text-xs font-semibold text-[var(--color-text-primary)]">{activeFile.relativePath || activeFile.filename}</div>
                    <div className="mt-0.5 text-[10px] text-[var(--color-text-tertiary)]">
                      {formatSize(activeFile.fileSize)} · v{activeFile.version} · {activeFile.editable ? '可编辑文本' : '只读预览'}
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-1.5">
                    {editor && (
                      <div className="mr-1 flex items-center gap-1">
                        {viewMode === 'edit' && (
                          <button onClick={() => void save()} disabled={saving || draft === editor.content}
                            className="inline-flex h-8 items-center gap-1.5 rounded-md bg-brand px-3 text-xs font-medium text-[var(--color-text-inverse)] transition-[background-color,box-shadow,transform] duration-200 hover:bg-brand-deep active:scale-[0.97] disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2">
                            {saving ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />}保存
                          </button>
                        )}
                        <button onClick={() => void copy()} title="复制文件内容" aria-label={copied ? '已复制' : '复制文件内容'}
                          className={`inline-flex h-8 items-center gap-1.5 rounded-md border px-2.5 text-xs font-medium transition-[background-color,border-color,color,transform] duration-200 active:scale-[0.97] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 ${copied
                            ? 'border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-[var(--color-success-bg)] text-[var(--color-success)]'
                            : 'border-[var(--color-border)] bg-[var(--color-bg-elevated)] text-[var(--color-text-secondary)] hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink'}`}>
                          {copied ? <Check size={13} /> : <Copy size={13} />}{copied ? '已复制' : '复制'}
                        </button>
                        <div data-testid="workspace-view-mode-switch" className="relative grid grid-cols-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-base)] p-0.5">
                          <span
                            data-testid="workspace-view-mode-indicator"
                            className={`pointer-events-none absolute left-0.5 top-0.5 h-[calc(100%-4px)] w-[calc(50%-2px)] rounded-md bg-brand shadow-sm transition-transform duration-300 ease-out ${viewMode === 'edit' ? 'translate-x-full' : 'translate-x-0'}`}
                          />
                          <button
                            type="button"
                            aria-pressed={viewMode === 'preview'}
                            onClick={() => setViewMode('preview')}
                            className={`relative z-10 inline-flex h-7 items-center justify-center gap-1 rounded-md px-2 text-[11px] font-medium transition-colors duration-200 ${viewMode === 'preview' ? 'text-[var(--color-text-inverse)]' : 'text-[var(--color-text-tertiary)] hover:text-[var(--color-text-secondary)]'}`}
                          >
                            <Eye size={12} />预览
                          </button>
                          <button
                            type="button"
                            aria-pressed={viewMode === 'edit'}
                            onClick={() => setViewMode('edit')}
                            className={`relative z-10 inline-flex h-7 items-center justify-center gap-1 rounded-md px-2 text-[11px] font-medium transition-colors duration-200 ${viewMode === 'edit' ? 'text-[var(--color-text-inverse)]' : 'text-[var(--color-text-tertiary)] hover:text-[var(--color-text-secondary)]'}`}
                          >
                            <Pencil size={12} />编辑
                          </button>
                        </div>
                      </div>
                    )}
                    <button onClick={() => void download(activeFile)} title="下载文件"
                      className="inline-flex h-8 items-center gap-1.5 rounded-md border border-[var(--color-border)] px-2.5 text-xs text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)]">
                      {loadingId === activeFile.id ? <Loader2 size={13} className="animate-spin" /> : <Download size={13} />}下载
                    </button>
                    {confirmId === activeFile.id ? (
                      <>
                        <button onClick={() => void remove(activeFile)} className="h-8 rounded-md bg-viz-rose px-2.5 text-xs font-medium text-[var(--color-text-inverse)] hover:bg-viz-rose">确认删除</button>
                        <button onClick={() => setConfirmId('')} className="h-8 rounded-md px-2 text-xs text-[var(--color-text-tertiary)] hover:bg-[var(--color-bg-hover)]">取消</button>
                      </>
                    ) : (
                      <button onClick={() => setConfirmId(activeFile.id)} title="删除文件"
                        className="inline-flex h-8 items-center gap-1.5 rounded-md border border-viz-rose-soft bg-viz-rose-soft px-2.5 text-xs font-medium text-viz-rose transition-colors hover:border-viz-rose-soft hover:bg-viz-rose-soft active:bg-viz-rose-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                        <Trash2 size={13} />删除
                      </button>
                    )}
                  </div>
                </div>

                <div className={`scrollbar-thin min-h-0 flex-1 ${htmlPreview && viewMode === 'preview'
                  ? 'overflow-hidden bg-card p-0'
                  : 'overflow-auto p-5'}`}>
                  {loadingId === activeFile.id && !preview ? (
                    <div className="flex h-full items-center justify-center gap-2 text-xs text-[var(--color-text-tertiary)]">
                      <Loader2 size={15} className="animate-spin" />正在读取文件…
                    </div>
                  ) : viewMode === 'edit' && editor ? (
                    <textarea value={draft} onChange={event => setDraft(event.target.value)}
                      aria-label="文件内容编辑器"
                      className="scrollbar-thin h-full min-h-[320px] w-full resize-none rounded-md border border-[var(--color-border)] bg-[var(--color-bg-base)] p-4 font-mono text-xs leading-5 outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring" />
                  ) : preview?.content && htmlPreview ? (
                    <iframe
                      data-testid="html-file-preview"
                      title={`${activeFile.filename} 网页预览`}
                      srcDoc={preview.content}
                      sandbox="allow-scripts"
                      referrerPolicy="no-referrer"
                      className="h-full min-h-[320px] w-full border-0 bg-card"
                    />
                  ) : preview?.content ? (
                    <div className="mx-auto max-w-4xl">
                      {!preview.editable && (
                        <div className="mb-3 rounded-md bg-muted px-3 py-2 text-[10px] text-[var(--color-text-tertiary)]">
                          当前展示从原文件确定性抽取的只读内容，原样式请下载文件查看。
                        </div>
                      )}
                      {preview.truncated && (
                        <div className="mb-3 rounded-md bg-[var(--color-warning-bg)] px-3 py-2 text-[10px] text-[var(--color-warning)]">文件较大，当前仅展示已抽取的部分内容。</div>
                      )}
                      {markdownPreview ? <Md text={preview.content} /> : (
                        <pre className="whitespace-pre-wrap break-words font-mono text-xs leading-6 text-[var(--color-text-secondary)]">{preview.content}</pre>
                      )}
                    </div>
                  ) : (
                    <div className="flex h-full flex-col items-center justify-center text-center text-xs leading-5 text-[var(--color-text-tertiary)]">
                      <File size={26} className="mb-2 text-[var(--color-text-tertiary)]" />
                      该文件暂无可预览的文本内容。<br />可使用右上角「下载」查看原文件。
                    </div>
                  )}
                </div>
              </>
            ) : (
              <div className="flex h-full flex-col items-center justify-center text-center text-xs leading-5 text-[var(--color-text-tertiary)]">
                <FileCode2 size={28} className="mb-2 text-[var(--color-text-tertiary)]" />
                从左侧文件树选择文件。<br />可预览常见文档，文本类文件支持直接编辑。
              </div>
            )}
            {error && <div className="border-t border-viz-rose-soft bg-viz-rose-soft px-5 py-2 text-[11px] text-viz-rose">{error}</div>}
          </main>
        </div>
      </section>
    </div>
  )
}
