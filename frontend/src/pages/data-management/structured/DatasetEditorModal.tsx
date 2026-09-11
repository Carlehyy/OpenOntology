import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  CheckCircle2, ChevronLeft, ChevronRight, Download, FileSpreadsheet,
  KeyRound, Loader2, LockKeyhole, Pencil, Plus, Save, Trash2, Undo2, X, XCircle,
} from 'lucide-react'
import datasetsApi, {
  FIELD_TYPE_LABELS,
  type DatasetOverviewItem,
  type DatasetSchemaColumn,
} from '@/api/v2/datasets'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { PageSizeSelect } from '@/components/PageSizeSelect'

const PAGE_SIZES = [20, 50, 100, 200, 500, 1000] as const
const DEFAULT_PAGE_SIZE = 50

type CellMap = Record<string, string>
interface EditableRow {
  orig: CellMap
  cur: CellMap
  deleted: boolean
}

const toStr = (value: unknown) => {
  if (value == null) return ''
  if (typeof value === 'object') {
    try { return JSON.stringify(value) } catch { return String(value) }
  }
  return String(value)
}

const displayWidth = (value: string) => Array.from(value).reduce(
  (width, char) => width + ((char.codePointAt(0) ?? 0) > 0xff ? 2 : 1),
  0,
)

const columnLabel = (column: DatasetSchemaColumn | undefined, identifier: string) => {
  const displayName = column?.display_name?.trim()
  return displayName && displayName !== identifier ? `${displayName}（${identifier}）` : identifier
}

const jsonShape = (column: DatasetSchemaColumn | undefined): 'array' | 'object' | null => {
  for (const sample of column?.sample_values ?? []) {
    if (Array.isArray(sample)) return 'array'
    if (sample && typeof sample === 'object') return 'object'
    if (typeof sample !== 'string' || !sample.trim()) continue
    try {
      const parsed = JSON.parse(sample)
      if (Array.isArray(parsed)) return 'array'
      if (parsed && typeof parsed === 'object') return 'object'
    } catch { /* 样本只用于判断结构，不因旧样本异常中断编辑器 */ }
  }
  return null
}

const validateValue = (value: string, column: DatasetSchemaColumn | undefined): string => {
  const text = value.trim()
  if (!column) return ''
  if (!column.nullable && !text) return '此列不允许为空'
  if (!text || column.type === 'string') return ''
  if (column.type === 'integer' && !/^[+-]?[\d,]+$/.test(text)) return '请输入整数'
  if (column.type === 'float' && !Number.isFinite(Number(text.replaceAll(',', '')))) return '请输入数字'
  if (column.type === 'boolean' && !['true', 'false', 'yes', 'no', '1', '0'].includes(text.toLowerCase())) {
    return '请输入 true / false、yes / no 或 1 / 0'
  }
  if (column.type === 'timestamp' && !(
    /^\d{4}[-/]\d{1,2}[-/]\d{1,2}/.test(text)
    || /^\d{1,2}[-/]\d{1,2}[-/]\d{4}/.test(text)
    || /^\d{8}$/.test(text)
  )) return '请输入有效日期或时间'
  if (column.type === 'json') {
    try {
      const parsed = JSON.parse(text)
      const shape = jsonShape(column)
      if (shape === 'array' && !Array.isArray(parsed)) return '此列要求 JSON 数组，例如 ["a", "b"]'
      if (shape === 'object' && (!parsed || typeof parsed !== 'object' || Array.isArray(parsed))) {
        return '此列要求 JSON 对象，例如 {"key": "value"}'
      }
    } catch {
      return '请输入合法 JSON'
    }
  }
  return ''
}

const downloadBlob = (blob: Blob, filename: string) => {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

/** 人工数据集在线维护：分页查询、全量导出、逐格校验与行级编辑。 */
export default function DatasetEditorModal({ dataset, onClose, onSaved }: {
  dataset: DatasetOverviewItem
  onClose: () => void
  onSaved: () => void
}) {
  const [pk, setPk] = useState(dataset.primary_key || '')
  const pkCols = useMemo(() => pk.split(',').map(item => item.trim()).filter(Boolean), [pk])
  const [columns, setColumns] = useState<string[]>([])
  const [schemaColumns, setSchemaColumns] = useState<Record<string, DatasetSchemaColumn>>({})
  const [rows, setRows] = useState<EditableRow[]>([])
  const [inserts, setInserts] = useState<CellMap[]>([])
  const [versionNo, setVersionNo] = useState(0)
  const [totalRows, setTotalRows] = useState(0)
  const [offset, setOffset] = useState(0)
  const [pageSize, setPageSize] = useState<number>(DEFAULT_PAGE_SIZE)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [exporting, setExporting] = useState<'csv' | 'xlsx' | null>(null)
  const [error, setError] = useState('')
  const [info, setInfo] = useState('')
  const [cellErrors, setCellErrors] = useState<Record<string, string>>({})
  const [confirmClose, setConfirmClose] = useState(false)
  const [pkPanelOpen, setPkPanelOpen] = useState(!dataset.primary_key)
  const [pkDraft, setPkDraft] = useState<string[]>([])
  const [declaring, setDeclaring] = useState(false)

  const changeSummary = useMemo(() => {
    const deleted = rows.filter(row => row.deleted).length
    const updated = rows.filter(row => !row.deleted && columns.some(
      column => row.cur[column] !== row.orig[column],
    )).length
    const inserted = inserts.length
    return { updated, inserted, deleted, total: updated + inserted + deleted }
  }, [columns, inserts.length, rows])
  const dirty = changeSummary.total > 0

  const loadPage = async (nextOffset: number, limit = pageSize) => {
    setLoading(true)
    setError('')
    setCellErrors({})
    try {
      const result = await datasetsApi.previewLatest(dataset.id, limit, nextOffset)
      const nextColumns = result.columns ?? []
      setColumns(nextColumns)
      setRows((result.rows ?? []).map(raw => {
        const values: CellMap = {}
        nextColumns.forEach(column => { values[column] = toStr(raw[column]) })
        return { orig: { ...values }, cur: { ...values }, deleted: false }
      }))
      setInserts([])
      setVersionNo(result.version_no ?? 0)
      setTotalRows(result.total_rows ?? 0)
      setOffset(nextOffset)
    } catch (loadError) {
      const detail = (loadError as { detail?: string; message?: string })?.detail
      setError(detail || '数据加载失败，请重试')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void Promise.resolve().then(() => loadPage(0, DEFAULT_PAGE_SIZE))
    datasetsApi.schema(dataset.id)
      .then(result => setSchemaColumns(Object.fromEntries(
        (result.columns ?? []).map(column => [column.name, column]),
      )))
      .catch(() => setSchemaColumns({}))
  }, [dataset.id])

  const requestClose = useCallback(() => {
    if (saving) return
    if (dirty) setConfirmClose(true)
    else onClose()
  }, [dirty, onClose, saving])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !confirmClose) requestClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [confirmClose, requestClose])

  const columnMinWidths = useMemo(() => Object.fromEntries(columns.map(column => {
    const metadata = schemaColumns[column]
    const values = [
      columnLabel(metadata, column),
      ...rows.map(row => row.cur[column] ?? ''),
      ...inserts.map(row => row[column] ?? ''),
    ]
    // 只把内容宽度作为列的下限：少列时由表格均匀吸收剩余空间，
    // 长文本也不会把单列无限撑宽；列数较多时仍由表格容器提供横向滚动。
    const width = Math.min(36, Math.max(14, ...values.map(value => displayWidth(value) + 4)))
    return [column, width]
  })), [columns, inserts, rows, schemaColumns])

  const togglePkCol = (column: string) => setPkDraft(current =>
    current.includes(column) ? current.filter(item => item !== column) : [...current, column])

  const handleDeclare = async () => {
    if (!pkDraft.length) return
    setDeclaring(true)
    setError('')
    try {
      const result = await datasetsApi.declareContract(dataset.id, pkDraft.join(','))
      setPk(result.primary_key)
      setPkPanelOpen(false)
      setSchemaColumns(current => Object.fromEntries(Object.entries(current).map(([name, column]) => [
        name,
        { ...column, is_primary_key: pkDraft.includes(name), nullable: pkDraft.includes(name) ? false : column.nullable },
      ])))
      setInfo(`主键契约已声明：${result.primary_key}（已校验 ${result.rows_validated} 行）`)
      onSaved()
    } catch (declareError) {
      const detail = (declareError as { detail?: string; message?: string })?.detail
      setError(detail || '主键声明失败')
    } finally {
      setDeclaring(false)
    }
  }

  const clearCellError = (key: string) => setCellErrors(current => {
    if (!current[key]) return current
    const next = { ...current }
    delete next[key]
    return next
  })

  const setCell = (rowIndex: number, column: string, value: string) => {
    setRows(current => current.map((row, index) => index === rowIndex
      ? { ...row, cur: { ...row.cur, [column]: value } }
      : row))
    clearCellError(`row:${rowIndex}:${column}`)
  }

  const toggleDelete = (rowIndex: number) => setRows(current => current.map(
    (row, index) => index === rowIndex ? { ...row, deleted: !row.deleted } : row,
  ))

  const addInsert = () => setInserts(current => [
    ...current,
    Object.fromEntries(columns.map(column => [column, ''])) as CellMap,
  ])

  const setInsertCell = (rowIndex: number, column: string, value: string) => {
    setInserts(current => current.map((row, index) => index === rowIndex
      ? { ...row, [column]: value }
      : row))
    clearCellError(`insert:${rowIndex}:${column}`)
  }

  const removeInsert = (rowIndex: number) => setInserts(current =>
    current.filter((_, index) => index !== rowIndex))

  const pickKey = (original: CellMap) => Object.fromEntries(
    pkCols.map(column => [column, original[column] ?? '']),
  )

  const validateVisibleChanges = () => {
    const problems: Record<string, string> = {}
    rows.forEach((row, rowIndex) => {
      if (row.deleted) return
      columns.forEach(column => {
        const message = validateValue(row.cur[column], schemaColumns[column])
        if (message) problems[`row:${rowIndex}:${column}`] = message
      })
    })
    inserts.forEach((row, rowIndex) => columns.forEach(column => {
      const metadata = schemaColumns[column] ?? {
        name: column,
        display_name: column,
        type: 'string',
        nullable: !pkCols.includes(column),
        is_primary_key: pkCols.includes(column),
        sample_values: [],
      }
      const message = validateValue(row[column], metadata)
      if (message) problems[`insert:${rowIndex}:${column}`] = message
    }))
    setCellErrors(problems)
    const first = Object.entries(problems)[0]
    if (first) {
      const [, rowType, rowNumber, column] = first[0].match(/^(row|insert):(\d+):(.+)$/) ?? []
      const line = rowType === 'insert' ? `新增行 ${Number(rowNumber) + 1}` : `第 ${offset + Number(rowNumber) + 1} 行`
      setError(`${line}的「${columnLabel(schemaColumns[column], column)}」${first[1]}`)
      return false
    }
    return true
  }

  const handleSave = async () => {
    if (!validateVisibleChanges()) return
    const updates = rows
      .filter(row => !row.deleted && columns.some(column =>
        !pkCols.includes(column) && row.cur[column] !== row.orig[column]))
      .map(row => ({
        key: pickKey(row.orig),
        values: Object.fromEntries(columns
          .filter(column => !pkCols.includes(column) && row.cur[column] !== row.orig[column])
          .map(column => [column, row.cur[column]])),
      }))
    const deletes = rows.filter(row => row.deleted).map(row => ({ key: pickKey(row.orig) }))
    const insertOps = inserts.map(values => ({ values }))
    if (!updates.length && !deletes.length && !insertOps.length) {
      setInfo('没有需要保存的修改')
      return
    }
    setSaving(true)
    setError('')
    setInfo('')
    try {
      const result = await datasetsApi.editRows(dataset.id, {
        base_version_no: versionNo,
        updates,
        deletes,
        inserts: insertOps,
      })
      setInfo(`已保存为 v${result.version_no}：修改 ${result.updated} 行，新增 ${result.inserted} 行，删除 ${result.deleted} 行`)
      onSaved()
      const lastPageOffset = result.rowcount > 0
        ? Math.floor((result.rowcount - 1) / pageSize) * pageSize
        : 0
      await loadPage(Math.min(offset, lastPageOffset), pageSize)
    } catch (saveError) {
      const detail = (saveError as { detail?: string | { message?: string }; message?: string })?.detail
      setError(typeof detail === 'object' ? detail.message || '数据已更新，请刷新后重试' : detail || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const changePage = (nextOffset: number) => {
    if (dirty) { setError('有未保存的修改，请先保存或关闭后再翻页'); return }
    void loadPage(Math.max(0, nextOffset), pageSize)
  }

  const changePageSize = (nextSize: number) => {
    if (dirty) { setError('有未保存的修改，请先保存或关闭后再调整分页大小'); return }
    setPageSize(nextSize)
    void loadPage(0, nextSize)
  }

  const handleExport = async (format: 'csv' | 'xlsx') => {
    setExporting(format)
    setError('')
    try {
      const blob = await datasetsApi.export(dataset.id, format)
      downloadBlob(blob, `${dataset.name}.${format}`)
      setInfo(`已导出全部 ${totalRows} 行数据`)
    } catch (exportError) {
      const detail = (exportError as { detail?: string; message?: string })?.detail
      setError(detail || `${format.toUpperCase()} 导出失败`)
    } finally {
      setExporting(null)
    }
  }

  const canEditRows = pkCols.length > 0
  const pageEnd = Math.min(offset + rows.length, totalRows)
  const currentPage = totalRows ? Math.floor(offset / pageSize) + 1 : 1
  const totalPages = Math.max(1, Math.ceil(totalRows / pageSize))

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-accent p-4 backdrop-blur-[2px]">
      <div
        className="flex h-[78vh] max-h-[760px] min-h-[520px] w-[min(96vw,1440px)] flex-col overflow-hidden rounded-2xl border border-border bg-card shadow-[0_24px_80px_rgba(15,23,42,0.18)]"
        role="dialog"
        aria-modal="true"
        aria-labelledby="dataset-editor-title"
      >
        <div className="flex shrink-0 items-start gap-3 border-b border-border px-5 py-4">
          <span className="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-xl bg-brand-soft text-brand-ink">
            <Pencil size={15} />
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h3 id="dataset-editor-title" className="truncate text-sm font-semibold text-foreground">{dataset.name}</h3>
              <span className="text-xs tabular-nums text-[var(--color-text-tertiary)]">v{versionNo} · {totalRows} 行</span>
              {pkCols.length ? (
                <span className="inline-flex items-center gap-1 rounded-md bg-[var(--color-warning-bg)] px-2 py-1 text-[11px] font-medium text-[var(--color-warning)]" title="现有行的主键值已锁定">
                  <LockKeyhole size={10} /> 主键：{pk}
                </span>
              ) : (
                <span className="rounded-md bg-muted px-2 py-1 text-[11px] text-muted-foreground">未声明主键</span>
              )}
            </div>
            <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">
              {canEditRows ? '现有行主键不可修改；其他字段可直接编辑，保存后生成可回溯的新版本。' : '请先声明主键以修改或删除现有行；当前仍可新增行。'}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <button type="button" onClick={() => void handleExport('csv')} disabled={Boolean(exporting)}
              className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-border bg-card px-2.5 text-xs font-medium text-muted-foreground transition hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink disabled:opacity-50">
              {exporting === 'csv' ? <Loader2 size={12} className="animate-spin" /> : <Download size={12} />} 导出 CSV
            </button>
            <button type="button" onClick={() => void handleExport('xlsx')} disabled={Boolean(exporting)}
              className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-border bg-card px-2.5 text-xs font-medium text-muted-foreground transition hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink disabled:opacity-50">
              {exporting === 'xlsx' ? <Loader2 size={12} className="animate-spin" /> : <FileSpreadsheet size={12} />} 导出 Excel
            </button>
            <button type="button" onClick={requestClose} disabled={saving}
              className="grid h-8 w-8 place-items-center rounded-lg text-[var(--color-text-tertiary)] transition hover:bg-muted hover:text-foreground disabled:opacity-40"
              aria-label="关闭数据维护窗口"><X size={16} /></button>
          </div>
        </div>

        {pkPanelOpen && !pkCols.length && (
          <div className="shrink-0 border-b border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-5 py-3">
            <div className="flex flex-wrap items-center gap-2">
              <p className="mr-2 text-xs text-muted-foreground"><KeyRound size={11} className="mr-1 inline text-[var(--color-warning)]" />选择一个或多个列作为主键，声明后主键值将锁定。</p>
              {columns.map(column => {
                const selectedIndex = pkDraft.indexOf(column)
                return (
                  <button key={column} type="button" onClick={() => togglePkCol(column)}
                    className={`rounded-lg border px-2 py-1 text-xs transition ${selectedIndex >= 0 ? 'border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-card text-[var(--color-warning)]' : 'border-border bg-card text-muted-foreground hover:border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)]'}`}>
                    {selectedIndex >= 0 && <span className="mr-1 font-semibold">{selectedIndex + 1}.</span>}{columnLabel(schemaColumns[column], column)}
                  </button>
                )
              })}
              <button type="button" onClick={() => void handleDeclare()} disabled={declaring || !pkDraft.length}
                className="inline-flex h-7 items-center gap-1 rounded-lg bg-brand-deep px-3 text-xs font-medium text-[var(--color-text-inverse)] transition hover:bg-brand-deep disabled:opacity-40">
                {declaring ? <Loader2 size={11} className="animate-spin" /> : <KeyRound size={11} />} 声明主键
              </button>
            </div>
          </div>
        )}

        {(error || info) && (
          <div className={`mx-5 mt-3 flex shrink-0 items-center gap-2 rounded-lg border px-3 py-2 text-xs ${error ? 'border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] text-[var(--color-danger)]' : 'border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-[var(--color-success-bg)] text-[var(--color-success)]'}`}>
            {error ? <XCircle size={13} className="shrink-0" /> : <CheckCircle2 size={13} className="shrink-0" />}
            <span className="flex-1">{error || info}</span>
            <button type="button" onClick={() => { setError(''); setInfo('') }} className="text-current opacity-50 hover:opacity-100"><X size={12} /></button>
          </div>
        )}

        <div className="min-h-0 flex-1 overflow-hidden px-5 py-3">
          {loading ? (
            <div className="flex h-full items-center justify-center text-xs text-[var(--color-text-tertiary)]"><Loader2 size={14} className="mr-2 animate-spin" />正在查询数据…</div>
          ) : !columns.length ? (
            <div className="grid h-full place-items-center text-xs text-[var(--color-text-tertiary)]">暂无列结构，请通过“上传新版本”导入表格。</div>
          ) : (
            <div
              className="h-full max-w-full overflow-auto rounded-xl border border-border bg-card"
              data-testid="dataset-editor-grid"
            >
              <table className="w-max min-w-full border-separate border-spacing-0 text-xs">
              <thead className="sticky top-0 z-20">
                <tr>
                  <th className="sticky left-0 z-30 w-11 border-b border-r border-border bg-muted px-2 py-2 font-normal text-[var(--color-text-tertiary)]">#</th>
                  {columns.map(column => {
                    const metadata = schemaColumns[column]
                    const isPrimaryKey = pkCols.includes(column)
                    const isRequired = isPrimaryKey || metadata?.nullable === false
                    return (
                      <th key={column} style={{ minWidth: `${columnMinWidths[column]}ch` }}
                        className="border-b border-border bg-muted px-3 py-2 text-left font-medium text-foreground">
                        <span className="inline-flex flex-wrap items-center gap-1.5 whitespace-nowrap">
                          {columnLabel(metadata, column)}
                          {isPrimaryKey && <span className="inline-flex items-center gap-0.5 rounded bg-[var(--color-warning-bg)] px-1.5 py-0.5 text-[9px] font-medium text-[var(--color-warning)]"><KeyRound size={8} />主键</span>}
                          {!isPrimaryKey && isRequired && <span className="rounded bg-viz-rose-soft px-1.5 py-0.5 text-[9px] font-medium text-viz-rose">非空</span>}
                          {metadata?.type && <span className="rounded bg-[var(--color-bg-active)] px-1.5 py-0.5 text-[9px] font-normal text-muted-foreground">{FIELD_TYPE_LABELS[metadata.type] ?? metadata.type}</span>}
                        </span>
                      </th>
                    )
                  })}
                  <th className="sticky right-0 z-30 w-12 border-b border-l border-border bg-muted px-2 py-2" />
                </tr>
              </thead>
              <tbody>
                {rows.map((row, rowIndex) => (
                  <tr key={`${offset}-${rowIndex}`} className={row.deleted ? 'opacity-40' : 'hover:bg-muted'}>
                    <td className="sticky left-0 z-10 border-b border-r border-border bg-card px-2 py-1.5 text-center tabular-nums text-[var(--color-text-tertiary)]">{offset + rowIndex + 1}</td>
                    {columns.map(column => {
                      const primaryKey = pkCols.includes(column)
                      const changed = row.cur[column] !== row.orig[column]
                      const validationError = cellErrors[`row:${rowIndex}:${column}`]
                      return (
                        <td key={column} className={`border-b border-border p-0 align-top ${changed && !validationError ? 'bg-[var(--color-warning-bg)]' : ''}`}>
                          <input
                            value={row.cur[column]}
                            disabled={!canEditRows || primaryKey || row.deleted}
                            onChange={event => setCell(rowIndex, column, event.target.value)}
                            title={validationError || (primaryKey ? '主键值不可修改' : row.cur[column])}
                            aria-invalid={Boolean(validationError)}
                            style={{ minWidth: `${columnMinWidths[column]}ch` }}
                            className={`min-h-9 w-full px-3 py-2 outline-none transition ${
                              validationError
                                ? 'bg-[var(--color-danger-bg)] text-[var(--color-danger)] ring-2 ring-inset ring-[var(--color-danger)]'
                                : changed
                                  ? 'bg-[var(--color-warning-bg)] font-medium text-[var(--color-warning)] ring-2 ring-inset ring-[var(--color-warning)] focus:bg-[var(--color-warning-bg)]'
                                  : primaryKey
                                    ? 'cursor-not-allowed bg-muted font-medium text-muted-foreground'
                                    : 'bg-transparent text-foreground focus:bg-brand-soft'
                            } ${row.deleted ? 'line-through' : ''}`}
                          />
                          {validationError && <span className="block bg-[var(--color-danger-bg)] px-3 pb-1.5 text-[10px] text-[var(--color-danger)]">{validationError}</span>}
                        </td>
                      )
                    })}
                    <td className="sticky right-0 z-10 border-b border-l border-border bg-card px-2 text-center">
                      {canEditRows && (
                        <button type="button" onClick={() => toggleDelete(rowIndex)}
                          className={`grid h-7 w-7 place-items-center rounded-lg transition ${row.deleted ? 'text-[var(--color-text-tertiary)] hover:bg-muted hover:text-foreground' : 'text-[var(--color-text-tertiary)] hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)]'}`}
                          title={row.deleted ? '撤销删除' : '删除该行'}>
                          {row.deleted ? <Undo2 size={13} /> : <Trash2 size={13} />}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
                {inserts.map((row, rowIndex) => (
                  <tr key={`insert-${rowIndex}`} className="bg-[var(--color-success-bg)]">
                    <td className="sticky left-0 z-10 border-b border-r border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-[var(--color-success-bg)] px-2 py-1.5 text-center font-medium text-[var(--color-success)]">新</td>
                    {columns.map(column => {
                      const validationError = cellErrors[`insert:${rowIndex}:${column}`]
                      return (
                        <td key={column} className="border-b border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] p-0 align-top">
                          <input
                            value={row[column]}
                            placeholder={(pkCols.includes(column) || schemaColumns[column]?.nullable === false) ? '必填' : ''}
                            onChange={event => setInsertCell(rowIndex, column, event.target.value)}
                            title={validationError || row[column]}
                            aria-invalid={Boolean(validationError)}
                            style={{ minWidth: `${columnMinWidths[column]}ch` }}
                            className={`min-h-9 w-full bg-transparent px-3 py-2 text-foreground outline-none transition placeholder:text-[var(--color-warning)] focus:bg-brand-soft ${validationError ? 'bg-[var(--color-danger-bg)] ring-1 ring-inset ring-[var(--color-danger)]' : ''}`}
                          />
                          {validationError && <span className="block bg-[var(--color-danger-bg)] px-3 pb-1.5 text-[10px] text-[var(--color-danger)]">{validationError}</span>}
                        </td>
                      )
                    })}
                    <td className="sticky right-0 z-10 border-b border-l border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-[var(--color-success-bg)] px-2 text-center">
                      <button type="button" onClick={() => removeInsert(rowIndex)} className="grid h-7 w-7 place-items-center rounded-lg text-[var(--color-text-tertiary)] transition hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)]" title="移除新增行"><Trash2 size={13} /></button>
                    </td>
                  </tr>
                ))}
              </tbody>
              </table>
            </div>
          )}
        </div>

        <div className="flex shrink-0 flex-wrap items-center gap-3 border-t border-border bg-muted px-5 py-3">
          <button type="button" onClick={addInsert} disabled={loading || !columns.length}
            className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-border bg-card px-3 text-xs font-medium text-muted-foreground transition hover:border-brand-line hover:text-brand-ink disabled:opacity-40">
            <Plus size={12} /> 新增行
          </button>
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            每页
            <PageSizeSelect
              value={pageSize}
              onChange={changePageSize}
              sizes={PAGE_SIZES}
              ariaLabel="维护数据每页显示条数"
            />
            条
          </label>
          <div className="flex items-center gap-1 text-xs text-muted-foreground">
            <button type="button" onClick={() => changePage(offset - pageSize)} disabled={!offset || loading}
              className="grid h-8 w-8 place-items-center rounded-lg border border-border bg-card transition hover:border-brand-line hover:text-brand-ink disabled:opacity-35"><ChevronLeft size={13} /></button>
            <span className="min-w-40 text-center tabular-nums">第 {currentPage} / {totalPages} 页 · {totalRows ? `${offset + 1}–${pageEnd}` : 0} / {totalRows} 行</span>
            <button type="button" onClick={() => changePage(offset + pageSize)} disabled={pageEnd >= totalRows || loading}
              className="grid h-8 w-8 place-items-center rounded-lg border border-border bg-card transition hover:border-brand-line hover:text-brand-ink disabled:opacity-35"><ChevronRight size={13} /></button>
          </div>
          {dirty && (
            <span className="rounded-md border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-2.5 py-1.5 text-xs font-medium text-[var(--color-warning)]" role="status">
              有未保存的修改 · 共改动 {changeSummary.total} 行
              <span className="ml-1 font-normal text-[var(--color-warning)]">
                （修改 {changeSummary.updated} · 新增 {changeSummary.inserted} · 删除 {changeSummary.deleted}）
              </span>
            </span>
          )}
          <div className="ml-auto flex items-center gap-2">
            <button type="button" onClick={requestClose} disabled={saving}
              className="h-8 rounded-lg border border-border bg-card px-3 text-xs font-medium text-muted-foreground transition hover:bg-muted disabled:opacity-40">关闭</button>
            <button type="button" onClick={() => void handleSave()} disabled={saving || !dirty}
              className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-brand-deep px-3.5 text-xs font-medium text-[var(--color-text-inverse)] transition hover:bg-brand-deep disabled:opacity-40">
              {saving ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} />} 保存为新版本
            </button>
          </div>
        </div>
      </div>
      <ConfirmDialog
        open={confirmClose}
        title="放弃未保存的修改？"
        description="当前页面有尚未保存的新增、修改或删除。关闭后这些改动将无法恢复。"
        confirmText="放弃修改并关闭"
        variant="danger"
        onClose={() => setConfirmClose(false)}
        onConfirm={onClose}
      />
    </div>
  )
}
