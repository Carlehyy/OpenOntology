import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { PageSizeSelect } from '@/components/PageSizeSelect'
import {
  CheckCircle2, ChevronLeft, ChevronRight, Eye, FileSpreadsheet,
  KeyRound, Loader2, Plus, Table2, Trash2, Upload, X, XCircle,
} from 'lucide-react'
import datasetsApi, {
  FIELD_TYPE_LABELS,
  type CreateTableResult,
  type DatasetImportJob,
  type DatasetImportStatus,
} from '@/api/v2/datasets'
import { CONTRACT_FIELD_TYPES } from '@/api/v2/pipelines'

const PREVIEW_PAGE_SIZES = [20, 50, 100, 200] as const
const ACCEPTED_EXTENSIONS = ['csv', 'xlsx', 'xls']

interface ColDraft {
  id: string
  sourceKey: string
  name: string
  displayName: string
  type: string
  pk: boolean
  nullable: boolean
}

const createColumnId = () => globalThis.crypto?.randomUUID?.()
  ?? `column-${Date.now()}-${Math.random().toString(36).slice(2)}`

const emptyColumn = (): ColDraft => ({
  id: createColumnId(), sourceKey: '', name: '', displayName: '',
  type: 'string', pk: false, nullable: true,
})

const FIELD_KEY_PATTERN = /^[a-z][a-z0-9_]*$/

const fileExtension = (file: File) => file.name.split('.').pop()?.toLowerCase() ?? ''
const withoutExtension = (filename: string) => filename.replace(/\.[^.]+$/, '')
const cellText = (value: unknown) => {
  if (value == null) return ''
  if (typeof value === 'object') {
    try { return JSON.stringify(value) } catch { return String(value) }
  }
  return String(value)
}

const formatBytes = (bytes: number) => {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

const STATUS_PROGRESS: Partial<Record<DatasetImportStatus, number>> = {
  uploading: 0,
  queued: 5,
  parsing: 35,
  ready: 100,
  import_queued: 5,
  importing: 40,
  completed: 100,
}

const STATUS_LABELS: Partial<Record<DatasetImportStatus, string>> = {
  uploading: '正在上传文件',
  queued: '文件上传完成，等待后台解析',
  parsing: '后端正在解析全部数据',
  ready: '表格解析完成',
  import_queued: '等待后台创建数据集',
  importing: '正在校验并保存数据集',
  completed: '数据集创建完成',
  failed: '处理失败',
}

/** 统一建表流程：上传一个表格自动识别，或直接定义空表。 */
export default function CreateTableModal({ onClose, onCreated }: {
  onClose: () => void
  onCreated: (result: CreateTableResult) => void
}) {
  const fileInputRef = useRef<HTMLInputElement>(null)
  const progressStartedAtRef = useRef<number | null>(null)
  const [file, setFile] = useState<File | null>(null)
  const [blankMode, setBlankMode] = useState(false)
  const [name, setName] = useState('')
  const [columns, setColumns] = useState<ColDraft[]>([])
  const [rows, setRows] = useState<string[][]>([])
  const [rowCount, setRowCount] = useState(0)
  const [sheetName, setSheetName] = useState('')
  const [importJobId, setImportJobId] = useState('')
  const [importStatus, setImportStatus] = useState<DatasetImportStatus | null>(null)
  const [importPhase, setImportPhase] = useState('')
  const [progressMode, setProgressMode] = useState<'inspection' | 'import'>('inspection')
  const [uploadProgress, setUploadProgress] = useState(0)
  const [serverProgress, setServerProgress] = useState(0)
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const [parsing, setParsing] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [dragging, setDragging] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [previewOpen, setPreviewOpen] = useState(false)
  const [previewPage, setPreviewPage] = useState(1)
  const [previewPageSize, setPreviewPageSize] = useState<number>(20)

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !submitting && !parsing) onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose, parsing, submitting])

  useEffect(() => {
    if (!file || (!parsing && !submitting)) return
    const tick = () => {
      const startedAt = progressStartedAtRef.current
      if (startedAt != null) {
        setElapsedSeconds(Math.max(0, Math.floor((Date.now() - startedAt) / 1000)))
      }
    }
    tick()
    const timer = window.setInterval(tick, 250)
    return () => window.clearInterval(timer)
  }, [file, parsing, submitting])

  const previewPages = Math.max(1, Math.ceil(rows.length / previewPageSize))
  const previewRows = useMemo(() => rows.slice(
    (previewPage - 1) * previewPageSize,
    previewPage * previewPageSize,
  ), [previewPage, previewPageSize, rows])
  const duplicateFieldKeys = useMemo(() => {
    const counts = new Map<string, number>()
    columns.forEach(column => {
      const identifier = column.name.trim()
      if (identifier) counts.set(identifier, (counts.get(identifier) ?? 0) + 1)
    })
    return new Set(
      [...counts.entries()]
        .filter(([, count]) => count > 1)
        .map(([identifier]) => identifier),
    )
  }, [columns])

  const applyImportJob = useCallback((job: DatasetImportJob) => {
    setImportStatus(job.status)
    setImportPhase(job.phase || STATUS_LABELS[job.status] || '')
    setServerProgress(Math.max(0, Math.min(100, job.progress ?? STATUS_PROGRESS[job.status] ?? 0)))
    if (job.status === 'ready') {
      setUploadProgress(100)
      const nextColumns = job.columns ?? []
      setColumns(nextColumns.map(column => ({
        id: createColumnId(),
        sourceKey: column.name,
        name: '',
        displayName: column.name,
        type: column.type || 'string',
        pk: false,
        nullable: true,
      })))
      setRows((job.preview_rows ?? []).map(row =>
        nextColumns.map(column => cellText(row[column.name]))))
      setRowCount(job.rowcount ?? 0)
      setSheetName(job.sheet_name || '第一个工作表')
      setPreviewPage(1)
      setPreviewOpen(false)
      setParsing(false)
      setSubmitting(false)
      setNotice(`后端已解析第一个工作表「${job.sheet_name || '第一个工作表'}」，共 ${job.rowcount ?? 0} 行`)
      return
    }
    if (job.status === 'completed' && job.result) {
      setParsing(false)
      setSubmitting(false)
      onCreated(job.result)
      return
    }
    if (job.status === 'failed') {
      setParsing(false)
      setSubmitting(false)
      const message = job.error || '表格解析失败'
      setError(progressMode === 'import'
        ? `${message}（修正字段设置后可直接重新导入，无需重新上传文件）`
        : message)
    }
  }, [onCreated, progressMode])

  useEffect(() => {
    if (!importJobId || !['queued', 'parsing', 'import_queued', 'importing'].includes(importStatus ?? '')) return
    let cancelled = false
    const poll = async () => {
      try {
        const job = await datasetsApi.importStatus(importJobId)
        if (!cancelled) applyImportJob(job)
      } catch (pollError) {
        if (!cancelled) {
          const detail = (pollError as { detail?: string; message?: string })?.detail
          setParsing(false)
          setSubmitting(false)
          setError(detail || '无法查询后台导入进度，请稍后重试')
        }
      }
    }
    const timer = window.setInterval(() => { void poll() }, 1200)
    void poll()
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [applyImportJob, importJobId, importStatus])

  const uploadForServerParsing = async (selected: File): Promise<boolean> => {
    setParsing(true)
    setError('')
    setNotice('')
    setFile(selected)
    setBlankMode(false)
    setName(withoutExtension(selected.name))
    setRows([])
    setRowCount(0)
    setColumns([])
    setSheetName('')
    setImportJobId('')
    setImportStatus('uploading')
    setImportPhase('正在上传文件')
    setProgressMode('inspection')
    setUploadProgress(0)
    setServerProgress(0)
    setElapsedSeconds(0)
    progressStartedAtRef.current = Date.now()
    try {
      const job = await datasetsApi.startImport(selected, setUploadProgress)
      setUploadProgress(100)
      setImportJobId(job.job_id)
      applyImportJob(job)
      return true
    } catch (parseError) {
      setFile(null)
      setRows([])
      setRowCount(0)
      setColumns([])
      setImportJobId('')
      setImportStatus(null)
      setImportPhase('')
      setUploadProgress(0)
      setServerProgress(0)
      progressStartedAtRef.current = null
      const detail = (parseError as { detail?: string; message?: string })?.detail
      setError(detail || (parseError instanceof Error ? parseError.message : '表格上传失败'))
      setParsing(false)
      return false
    }
  }

  const acceptFiles = (incoming: File[]) => {
    const accepted = incoming.filter(candidate => ACCEPTED_EXTENSIONS.includes(fileExtension(candidate)))
    if (!accepted.length) {
      setError('仅支持 CSV、XLSX 或 XLS 表格')
      return
    }
    if (accepted[0].size > 200 * 1024 * 1024) {
      setError('文件超过 200 MB，请压缩或拆分后再上传')
      return
    }
    const ignoredMessage = incoming.length > 1
      ? `一次只能保留一个表格，已选用「${accepted[0].name}」，其余 ${incoming.length - 1} 个文件已忽略`
      : ''
    void uploadForServerParsing(accepted[0]).then(success => {
      if (success && ignoredMessage) setNotice(ignoredMessage)
    })
  }

  const removeFile = () => {
    setFile(null)
    setRows([])
    setRowCount(0)
    setColumns([])
    setName('')
    setSheetName('')
    setImportJobId('')
    setImportStatus(null)
    setImportPhase('')
    setProgressMode('inspection')
    setUploadProgress(0)
    setServerProgress(0)
    setElapsedSeconds(0)
    progressStartedAtRef.current = null
    setParsing(false)
    setSubmitting(false)
    setPreviewOpen(false)
    setNotice('')
    if (fileInputRef.current) fileInputRef.current.value = ''
  }

  const enterBlankMode = () => {
    removeFile()
    setBlankMode(true)
    setColumns([emptyColumn(), emptyColumn()])
  }

  const setColumn = (index: number, patch: Partial<ColDraft>) => {
    setColumns(current =>
      current.map((column, columnIndex) => columnIndex === index ? { ...column, ...patch } : column))
    setError('')
  }

  const addColumn = () => setColumns(current => [...current, emptyColumn()])
  const removeColumn = (index: number) => setColumns(current =>
    current.filter((_, columnIndex) => columnIndex !== index))

  const validate = () => {
    if (!file && !blankMode) return '请上传一个表格，或选择直接定义空表'
    // failed（提交后校验失败）允许修正字段直接重试；其余非 ready 态仍须等待解析
    if (file && importStatus !== 'ready' && importStatus !== 'failed') return '请等待后端完成表格解析'
    if (!name.trim()) return '请填写数据集名称'
    const configured = columns.filter(column =>
      Boolean(file || column.name.trim() || column.displayName.trim()))
    if (!configured.length) return '至少需要定义一列'
    const seen = new Set<string>()
    for (const [index, column] of configured.entries()) {
      const identifier = column.name.trim()
      if (!identifier) {
        return `请为「${column.displayName.trim() || column.sourceKey || `第 ${index + 1} 列`}」填写字段标识`
      }
      if (!FIELD_KEY_PATTERN.test(identifier)) {
        return `字段标识「${identifier}」不合法：必须以小写字母开头，且只能包含小写字母、数字和下划线`
      }
      if (seen.has(identifier)) return `字段标识「${identifier}」重复`
      seen.add(identifier)
    }
    return ''
  }

  const handleSubmit = async () => {
    const validationError = validate()
    if (validationError) { setError(validationError); return }
    const configured = columns.filter(column =>
      Boolean(file || column.name.trim() || column.displayName.trim()))
    const payload = {
      name: name.trim(),
      columns: configured.map(column => ({
        name: column.name.trim(),
        source_key: column.sourceKey.trim() || column.name.trim(),
        display_name: column.displayName.trim() || column.name.trim(),
        type: column.type,
        nullable: column.pk ? false : column.nullable,
      })),
      primary_key: configured.filter(column => column.pk).map(column => column.name.trim()).join(','),
    }
    setSubmitting(true)
    setError('')
    if (file) {
      setProgressMode('import')
      setImportStatus('import_queued')
      setImportPhase('正在提交导入任务')
      setServerProgress(0)
      setElapsedSeconds(0)
      progressStartedAtRef.current = Date.now()
    }
    try {
      const result = file
        ? await datasetsApi.commitImport(importJobId, payload)
        : await datasetsApi.createTable(payload)
      if (file) {
        applyImportJob(result as DatasetImportJob)
      } else {
        onCreated(result as CreateTableResult)
      }
    } catch (submitError) {
      const detail = (submitError as { detail?: string; message?: string })?.detail
      setError(detail || '创建失败，请检查字段设置后重试')
      setSubmitting(false)
    }
  }

  const hasSource = Boolean(file || blankMode)
  const progressPercent = importStatus === 'uploading'
    ? Math.round(uploadProgress * 0.45)
    : progressMode === 'inspection'
      ? Math.min(100, Math.round(45 + serverProgress * 0.55))
      : serverProgress
  const progressLabel = importStatus === 'uploading'
    ? `正在上传文件 ${uploadProgress}%`
    : importPhase || (importStatus ? STATUS_LABELS[importStatus] : '') || '等待处理'
  const progressActive = Boolean(
    importStatus && ['uploading', 'queued', 'parsing', 'import_queued', 'importing'].includes(importStatus),
  )
  const progressFailed = importStatus === 'failed'

  return (
    <Dialog
      open
      onOpenChange={next => { if (!next && !submitting && !parsing) onClose() }}
    >
      <DialogContent
        className="flex max-h-[86vh] w-[min(96vw,1120px)] flex-col p-0"
        dismissible={!(submitting || parsing)}
      >
        <DialogHeader
          className="mb-0 shrink-0 border-b border-border px-5 pb-3 pt-4"
          icon={<Table2 size={18} />}
        >
          <DialogTitle>在线新建表格</DialogTitle>
          <DialogDescription>
            上传一个现有表格自动识别名称与字段，或直接定义一张空表；创建前可统一检查字段契约。
          </DialogDescription>
        </DialogHeader>

        <main className="min-h-0 flex-1 overflow-auto">
          <section className="border-b border-border px-5 py-4">
            <div className="mb-2 flex items-center justify-between">
              <div><h4 className="text-xs font-semibold text-foreground">数据来源</h4><p className="mt-0.5 text-[11px] text-[var(--color-text-tertiary)]">最多保留一个 CSV 或 Excel 表格</p></div>
              {!blankMode && <button type="button" onClick={enterBlankMode} className="text-xs font-medium text-brand-ink hover:text-brand-ink">没有文件，直接定义空表</button>}
            </div>
            <input ref={fileInputRef} type="file" multiple className="hidden" accept=".csv,.xlsx,.xls"
              onChange={event => { acceptFiles(Array.from(event.target.files ?? [])); event.target.value = '' }} />
            {file ? (
              <div>
                <div className="flex items-center gap-3 rounded-xl bg-muted px-4 py-3">
                  <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-[var(--color-success-bg)] text-[var(--color-success)]"><FileSpreadsheet size={18} /></span>
                  <div className="min-w-0 flex-1"><p className="truncate text-sm font-medium text-foreground">{file.name}</p><p className="mt-0.5 text-[11px] text-[var(--color-text-tertiary)]">{formatBytes(file.size)}{sheetName ? ` · 工作表「${sheetName}」 · ${columns.length} 列 · ${rowCount} 行` : ' · 正在等待后端解析'}</p></div>
                  <button type="button" onClick={() => fileInputRef.current?.click()} disabled={parsing || submitting} className="text-xs font-medium text-brand-ink hover:text-brand-ink disabled:opacity-40">替换</button>
                  <button type="button" onClick={removeFile} disabled={parsing || submitting} className="grid h-8 w-8 place-items-center rounded-lg text-[var(--color-text-tertiary)] transition hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)] disabled:opacity-40" aria-label="移除当前表格"><Trash2 size={14} /></button>
                </div>
                {importStatus && (
                  <div className="mt-2 px-1" data-testid="dataset-import-progress">
                    <div className="mb-1 flex items-center justify-between gap-3 text-[11px]">
                      <span className={progressFailed ? 'text-[var(--color-danger)]' : 'text-muted-foreground'}>{progressLabel}</span>
                      <span className="shrink-0 tabular-nums text-[var(--color-text-tertiary)]">已耗时 {elapsedSeconds} 秒</span>
                    </div>
                    <div className="h-1 overflow-hidden rounded-full bg-muted">
                      <div
                        className={`h-full rounded-full transition-[width] duration-300 ${progressFailed ? 'bg-[var(--color-danger)]' : importStatus === 'ready' || importStatus === 'completed' ? 'bg-[var(--color-success)]' : 'bg-brand'} ${progressActive ? 'animate-pulse' : ''}`}
                        style={{ width: `${Math.max(progressFailed ? 100 : 2, progressPercent)}%` }}
                        role="progressbar"
                        aria-label="表格导入进度"
                        aria-valuemin={0}
                        aria-valuemax={100}
                        aria-valuenow={progressPercent}
                      />
                    </div>
                  </div>
                )}
              </div>
            ) : blankMode ? (
              <div className="flex items-center gap-3 rounded-xl bg-brand-soft px-4 py-3 text-xs text-brand-ink"><CheckCircle2 size={15} />已选择直接定义空表，创建后可在“维护数据”中逐行录入。<button type="button" onClick={() => { setBlankMode(false); setColumns([]) }} className="ml-auto font-medium hover:text-brand-ink">重新选择</button></div>
            ) : (
              <div onDragEnter={event => { event.preventDefault(); setDragging(true) }} onDragOver={event => event.preventDefault()}
                onDragLeave={() => setDragging(false)} onDrop={event => { event.preventDefault(); setDragging(false); acceptFiles(Array.from(event.dataTransfer.files)) }}
                className={`flex min-h-28 cursor-pointer flex-col items-center justify-center rounded-xl border border-dashed px-5 py-5 text-center transition ${dragging ? 'border-brand bg-brand-soft' : 'border-border bg-muted hover:border-brand-line hover:bg-brand-soft'}`}
                onClick={() => fileInputRef.current?.click()}>
                {parsing ? <Loader2 size={20} className="mb-2 animate-spin text-brand-ink" /> : <Upload size={20} className="mb-2 text-brand-ink" />}
                <p className="text-sm font-medium text-foreground">{parsing ? '正在上传并等待后端解析…' : '拖入表格，或点击选择文件'}</p>
                <p className="mt-1 text-[11px] text-[var(--color-text-tertiary)]">同时选择多个文件时只保留第一个，其余文件自动忽略</p>
              </div>
            )}
          </section>

          {hasSource && (
            <>
              <section className="border-b border-border px-5 py-4">
                <label className="mb-1.5 block text-xs font-semibold text-foreground">数据集名称</label>
                <input value={name} onChange={event => setName(event.target.value)} placeholder="例如：设备台账"
                  className="h-9 w-full rounded-lg border border-border px-3 text-sm outline-none transition focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring" />
              </section>

              <section className="border-b border-border px-5 py-4">
                <div className="mb-2 flex items-end justify-between gap-4">
                  <div><h4 className="text-xs font-semibold text-foreground">字段设置</h4><p className="mt-0.5 text-[11px] text-[var(--color-text-tertiary)]">{file ? '中文名已从上传文件带入；请为每一列填写稳定且唯一的字段标识。' : '中文名用于界面展示；字段标识必须唯一，且由小写字母、数字和下划线组成，并以小写字母开头。'}主键与非空约束会在创建时校验全部数据。</p></div>
                  {blankMode && <button type="button" onClick={addColumn} className="inline-flex h-7 items-center gap-1 text-xs font-medium text-brand-ink hover:text-brand-ink"><Plus size={12} />添加字段</button>}
                </div>
                <div className="overflow-x-auto rounded-xl border border-border">
                  <table className="w-full min-w-[780px] text-xs">
                    <thead className="border-b border-border bg-muted text-muted-foreground"><tr>
                      <th className="px-3 py-2 text-left font-medium">中文名</th>
                      <th className="px-3 py-2 text-left font-medium">字段标识</th>
                      <th className="w-40 px-3 py-2 text-left font-medium">数据类型</th>
                      <th className="w-20 px-3 py-2 text-center font-medium">非空</th>
                      <th className="w-20 px-3 py-2 text-center font-medium"><span className="inline-flex items-center gap-1"><KeyRound size={10} className="text-[var(--color-warning)]" />主键</span></th>
                      {blankMode && <th className="w-12" />}
                    </tr></thead>
                    <tbody className="divide-y border-border">{columns.map((column, index) => (
                      <tr key={column.id} className="hover:bg-muted">
                        <td className="p-1.5"><input value={column.displayName} onChange={event => setColumn(index, { displayName: event.target.value })} placeholder="例如：设备名称" className="h-8 w-full min-w-36 rounded-md border border-border px-2 outline-none focus-visible:border-ring" /></td>
                        <td className="p-1.5"><div><input value={column.name} onChange={event => setColumn(index, { name: event.target.value })} placeholder="例如：device_name" autoCapitalize="none" autoCorrect="off" spellCheck={false} title="字段标识必须唯一；以小写字母开头，仅允许小写字母、数字和下划线" aria-invalid={duplicateFieldKeys.has(column.name.trim()) || undefined} aria-describedby={duplicateFieldKeys.has(column.name.trim()) ? `field-key-error-${column.id}` : undefined} className={`h-8 w-full min-w-36 rounded-md border px-2 font-mono outline-none transition ${duplicateFieldKeys.has(column.name.trim()) ? 'border-[var(--color-danger)] bg-[var(--color-danger-bg)] focus:border-[var(--color-danger)] focus-visible:ring-2 focus-visible:ring-ring' : 'border-border focus-visible:border-ring'}`} />{duplicateFieldKeys.has(column.name.trim()) && <p id={`field-key-error-${column.id}`} className="mt-1 text-[10px] text-[var(--color-danger)]" role="alert">字段标识重复，请为每一列使用唯一标识</p>}</div></td>
                        <td className="p-1.5"><Select value={column.type} onValueChange={value => setColumn(index, { type: value })}><SelectTrigger className="h-8 w-full rounded-md bg-card px-2 text-xs" aria-label={`${column.name} 字段类型`}><SelectValue /></SelectTrigger><SelectContent>{CONTRACT_FIELD_TYPES.map(type => <SelectItem key={type} value={type}>{FIELD_TYPE_LABELS[type] ?? type}（{type}）</SelectItem>)}</SelectContent></Select></td>
                        <td className="p-1.5 text-center"><input type="checkbox" checked={!column.nullable || column.pk} disabled={column.pk} onChange={event => setColumn(index, { nullable: !event.target.checked })} className="accent-[var(--color-nav-bg)]" aria-label={`${column.name} 非空`} /></td>
                        <td className="p-1.5 text-center"><input type="checkbox" checked={column.pk} onChange={event => setColumn(index, { pk: event.target.checked, nullable: event.target.checked ? false : column.nullable })} className="accent-[var(--color-warning)]" aria-label={`${column.name} 主键`} /></td>
                        {blankMode && <td className="p-1.5 text-center"><button type="button" onClick={() => removeColumn(index)} disabled={columns.length <= 1} className="grid h-7 w-7 place-items-center rounded-md text-[var(--color-text-tertiary)] transition hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)] disabled:opacity-30"><Trash2 size={12} /></button></td>}
                      </tr>
                    ))}</tbody>
                  </table>
                </div>
              </section>

              {file && (
                <section className="px-5 py-4">
                  <button type="button" onClick={() => setPreviewOpen(current => !current)} disabled={!rows.length} className="inline-flex h-8 items-center gap-1.5 text-xs font-medium text-brand-ink hover:text-brand-ink disabled:opacity-40"><Eye size={13} />{previewOpen ? '收起数据样例' : `查看数据样例（前 ${rows.length} 行）`}</button>
                  {previewOpen && <div className="mt-2 overflow-hidden rounded-xl border border-border">
                    <div className="max-h-72 overflow-auto" data-testid="dataset-preview-scroll"><table className="w-max min-w-full table-auto text-xs" data-testid="dataset-preview-table"><thead className="sticky top-0 z-10 border-b border-border bg-muted"><tr><th className="w-12 px-3 py-2 text-left font-medium text-[var(--color-text-tertiary)]">#</th>{columns.map(column => <th key={column.id} className="whitespace-nowrap px-3 py-2 text-left font-medium text-muted-foreground">{column.name ? (column.displayName && column.displayName !== column.name ? `${column.displayName}（${column.name}）` : column.name) : `${column.displayName || column.sourceKey}（待填写字段标识）`}</th>)}</tr></thead><tbody className="divide-y border-border">{previewRows.map((row, rowIndex) => <tr key={`${previewPage}-${rowIndex}`} className="hover:bg-muted"><td className="px-3 py-2 tabular-nums text-[var(--color-text-tertiary)]">{(previewPage - 1) * previewPageSize + rowIndex + 1}</td>{columns.map((column, columnIndex) => <td key={column.id} className="whitespace-nowrap px-3 py-2 text-muted-foreground" title={row[columnIndex]}>{row[columnIndex]}</td>)}</tr>)}</tbody></table></div>
                    <div className="flex items-center justify-end gap-2 border-t border-border bg-muted px-3 py-2 text-xs text-muted-foreground"><label className="flex items-center gap-1">每页<PageSizeSelect value={previewPageSize} onChange={size => { setPreviewPageSize(size); setPreviewPage(1) }} sizes={PREVIEW_PAGE_SIZES} ariaLabel="结构预览每页显示条数" className="h-7 w-16 rounded-md bg-card px-1.5 text-xs" />条</label><button type="button" onClick={() => setPreviewPage(page => Math.max(1, page - 1))} disabled={previewPage <= 1} className="grid h-7 w-7 place-items-center rounded-md border border-border bg-card disabled:opacity-30"><ChevronLeft size={12} /></button><span className="min-w-20 text-center tabular-nums">{previewPage} / {previewPages}</span><button type="button" onClick={() => setPreviewPage(page => Math.min(previewPages, page + 1))} disabled={previewPage >= previewPages} className="grid h-7 w-7 place-items-center rounded-md border border-border bg-card disabled:opacity-30"><ChevronRight size={12} /></button></div>
                  </div>}
                </section>
              )}
            </>
          )}
        </main>

        {(error || notice) && <div className={`mx-5 mb-3 flex shrink-0 items-start gap-2 rounded-lg border px-3 py-2 text-xs ${error ? 'border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] text-[var(--color-danger)]' : 'border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] text-[var(--color-warning)]'}`}>{error ? <XCircle size={13} className="mt-0.5 shrink-0" /> : <CheckCircle2 size={13} className="mt-0.5 shrink-0" />}<span className="flex-1">{error || notice}</span><button type="button" onClick={() => { setError(''); setNotice('') }}><X size={12} /></button></div>}

        <footer className="flex shrink-0 items-center justify-end gap-2 border-t border-border bg-muted px-5 py-3">
          <button type="button" onClick={onClose} disabled={submitting} className="h-8 rounded-lg border border-border bg-card px-3 text-xs font-medium text-muted-foreground transition hover:bg-muted disabled:opacity-40">取消</button>
          <button type="button" onClick={() => void handleSubmit()} disabled={submitting || parsing || !hasSource || duplicateFieldKeys.size > 0} className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-brand-deep px-4 text-xs font-medium text-[var(--color-text-inverse)] transition hover:bg-brand-deep disabled:opacity-40">{submitting ? <Loader2 size={12} className="animate-spin" /> : file ? <Upload size={12} /> : <Table2 size={12} />}{file ? '导入并创建' : '创建空表'}</button>
        </footer>
      </DialogContent>
    </Dialog>
  )
}
