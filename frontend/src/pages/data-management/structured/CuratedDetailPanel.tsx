import { Fragment, useEffect, useState, useCallback } from 'react'
import {
  X, CheckCircle, AlertTriangle, Clock,
  Loader2, Pencil, Plus, Minus, KeyRound, RefreshCw, Table2,
  ChevronLeft, ChevronRight, Download, FileSpreadsheet, LockKeyhole,
  Info, Save,
} from 'lucide-react'
import curatedApi, { type ReviewDiff, type ReviewRowEdit } from '@/api/v2/curated'
import { PageSizeSelect } from '@/components/PageSizeSelect'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import datasetsApi, { FIELD_TYPE_LABELS, type DatasetSchemaColumn } from '@/api/v2/datasets'

interface Props {
  datasetId: string
  datasetName: string
  datasetStatus: string
  pipelineName: string
  onClose: () => void
  onStatusChange: (id: string, status: string) => void
}

const STATUS_LABEL: Record<string, string> = {
  pending_review: '待审核',
  pending:        '待审核',
  in_review:      '待审核',
  approved:       '已审核',
  rejected:       '已拒绝',
}

const STATUS_STYLE: Record<string, string> = {
  pending_review: 'bg-[var(--color-warning-bg)] text-[var(--color-warning)] border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)]',
  pending:        'bg-[var(--color-warning-bg)] text-[var(--color-warning)] border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)]',
  in_review:      'bg-[var(--color-warning-bg)] text-[var(--color-warning)] border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)]',
  approved:       'bg-[var(--color-success-bg)] text-[var(--color-success)] border-[color-mix(in_srgb,var(--color-success)_35%,transparent)]',
  rejected:       'bg-viz-rose-soft text-viz-rose border-viz-rose-soft',
}

const STATUS_ICON = (status: string) => {
  if (status === 'approved') return <CheckCircle size={13} className="text-[var(--color-success)]" />
  if (status === 'rejected') return <AlertTriangle size={13} className="text-viz-rose" />
  return <Clock size={13} className="text-[var(--color-warning)]" />
}

const isPendingReview = (status: string) => (
  status === 'pending_review' || status === 'pending' || status === 'in_review'
)

type View = 'changes' | 'previous' | 'current'
type DataRow = Record<string, unknown>

const cellText = (value: unknown) => {
  if (value === null || value === undefined || value === '') return ''
  if (typeof value === 'object') {
    try { return JSON.stringify(value) } catch { return String(value) }
  }
  return String(value)
}

const cellComparable = (row: DataRow, column: string) => {
  if (!Object.prototype.hasOwnProperty.call(row, column)) return 'missing:'
  const value = row[column]
  return `present:${value === undefined ? 'undefined' : cellText(value)}`
}

const REVIEW_PAGE_SIZES = [20, 50, 100, 200, 500, 1000] as const
const DEFAULT_REVIEW_PAGE_SIZE = 50

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

function errorText(error: unknown, fallback: string): string {
  if (!error || typeof error !== 'object') return fallback
  const e = error as { detail?: unknown; data?: { detail?: unknown }; message?: unknown }
  const detail = e.detail ?? e.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (detail && typeof detail === 'object' && 'message' in detail) {
    const message = (detail as { message?: unknown }).message
    if (typeof message === 'string' && message.trim()) return message
  }
  return typeof e.message === 'string' && e.message.trim() ? e.message : fallback
}

function columnsFromRows(rows: DataRow[], preferred: string[] = []): string[] {
  const columns: string[] = []
  const seen = new Set<string>()
  const add = (column: string) => {
    if (!seen.has(column)) {
      seen.add(column)
      columns.push(column)
    }
  }
  preferred.forEach(add)
  rows.forEach(row => Object.keys(row).forEach(add))
  return columns
}

const columnDisplayText = (name: string, schemaColumn?: DatasetSchemaColumn) => {
  const displayName = schemaColumn?.display_name?.trim()
  return displayName && displayName !== name ? `${displayName}（${name}）` : name
}

function ColumnLabel({
  name, primaryKeys, schemaColumn,
}: {
  name: string
  primaryKeys: string[]
  schemaColumn?: DatasetSchemaColumn
}) {
  const isPrimaryKey = primaryKeys.includes(name)
  const isRequired = isPrimaryKey || schemaColumn?.nullable === false
  const displayName = schemaColumn?.display_name?.trim()
  const hasDistinctDisplayName = Boolean(displayName && displayName !== name)
  const isMissingDisplayName = schemaColumn?.display_name_configured === false
  return (
    <span className="inline-flex items-center gap-1.5" title={hasDistinctDisplayName ? `字段标识：${name}` : undefined}>
      <span>{columnDisplayText(name, schemaColumn)}</span>
      {isMissingDisplayName && (
        <span
          className="rounded border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-1 py-0.5 text-[9px] font-normal text-[var(--color-warning)]"
          title="来源字段契约中没有保存字段名称，当前仅显示字段标识"
        >未设置字段名称</span>
      )}
      {isPrimaryKey && (
        <span className="inline-flex items-center gap-0.5 rounded bg-[var(--color-warning-bg)] px-1.5 py-0.5 text-[9px] font-medium text-[var(--color-warning)]" title="主键列：已校验全量非空，用于稳定识别数据行">
          <KeyRound size={8} /> 主键 · 非空
        </span>
      )}
      {!isPrimaryKey && isRequired && (
        <span className="rounded bg-viz-rose-soft px-1.5 py-0.5 text-[9px] font-medium text-viz-rose">非空</span>
      )}
      {schemaColumn?.type && (
        <span className="rounded bg-[var(--color-bg-active)] px-1.5 py-0.5 text-[9px] font-normal text-muted-foreground">
          {FIELD_TYPE_LABELS[schemaColumn.type] ?? schemaColumn.type}
        </span>
      )}
    </span>
  )
}

/** 只读数据表（列取所有行键的并集，保持稳定表头） */
function ReadonlyTable({ rows, highlight, primaryKeys = [], startIndex = 0, schemaColumns = {}, fillAvailable = false }: {
  rows: DataRow[]
  highlight?: 'add' | 'del'
  primaryKeys?: string[]
  startIndex?: number
  schemaColumns?: Record<string, DatasetSchemaColumn>
  fillAvailable?: boolean
}) {
  if (!rows.length) return <div className={`grid place-items-center text-xs text-[var(--color-text-tertiary)] ${fillAvailable ? 'h-full rounded-xl border border-border bg-muted' : 'p-6'}`}>无数据</div>
  const cols = columnsFromRows(rows, [...primaryKeys, ...Object.keys(schemaColumns)])
  const tint = highlight === 'add' ? 'bg-[var(--color-success-bg)]' : highlight === 'del' ? 'bg-[var(--color-danger-bg)]' : ''
  return (
    <div
      className={fillAvailable ? 'h-full max-w-full overflow-auto rounded-xl border border-border bg-card' : 'max-w-full overflow-x-auto'}
      data-testid="curated-data-grid"
    >
      <table className="w-max min-w-full border-separate border-spacing-0 text-xs">
        <thead className="sticky top-0 z-20 bg-muted">
          <tr>
            <th className="sticky left-0 z-30 w-12 border-b border-r border-border bg-muted px-3 py-2.5 text-center font-normal text-[var(--color-text-tertiary)]">#</th>
            {cols.map(c => (
              <th key={c} className="min-w-[150px] whitespace-nowrap border-b border-border bg-muted px-4 py-2.5 text-left font-medium text-foreground">
                <ColumnLabel name={c} primaryKeys={primaryKeys} schemaColumn={schemaColumns[c]} />
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className={`${tint} transition-colors hover:bg-muted`}>
              <td className="sticky left-0 z-10 border-b border-r border-border bg-card px-3 py-2.5 text-center tabular-nums text-[var(--color-text-tertiary)] select-none">{startIndex + i + 1}</td>
              {cols.map(c => (
                <td key={c} className={`max-w-[320px] border-b border-border px-4 py-2.5 ${primaryKeys.includes(c) ? 'bg-[var(--color-warning-bg)] font-mono' : ''}`}>
                  <span className="block truncate text-foreground" title={cellText(row[c])}>
                    {cellText(row[c]) || <span className="text-[var(--color-text-tertiary)]">—</span>}
                  </span>
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** 待审核当前版本的安全编辑表：仅非主键字段可改，行身份严格来自 schema PK。 */
function EditableReviewTable({
  rows, rowPks, primaryKeys, startIndex, schemaColumns, pendingEdits, disabled, onCellChange,
}: {
  rows: DataRow[]
  /** 后端使用同一 encode_row_pk 生成，必须与 rows 当前页逐行对齐。 */
  rowPks: Array<string | null>
  primaryKeys: string[]
  startIndex: number
  schemaColumns: Record<string, DatasetSchemaColumn>
  pendingEdits: ReviewRowEdit[]
  disabled: boolean
  onCellChange: (rowPk: string, fieldName: string, oldValue: string, newValue: string) => void
}) {
  if (!rows.length) {
    return <div className="grid h-full place-items-center rounded-xl border border-border bg-muted text-xs text-[var(--color-text-tertiary)]">无数据</div>
  }
  const columns = columnsFromRows(rows, [...primaryKeys, ...Object.keys(schemaColumns)])
  const pendingByCell = new Map(
    pendingEdits.map(edit => [JSON.stringify([edit.row_pk, edit.field_name]), edit]),
  )

  return (
    <div className="h-full max-w-full overflow-auto rounded-xl border border-border bg-card" data-testid="curated-edit-grid">
      <table className="w-max min-w-full border-separate border-spacing-0 text-xs">
        <thead className="sticky top-0 z-20 bg-muted">
          <tr>
            <th className="sticky left-0 z-30 w-12 border-b border-r border-border bg-muted px-3 py-2.5 text-center font-normal text-[var(--color-text-tertiary)]">#</th>
            {columns.map(column => (
              <th key={column} className="min-w-[170px] whitespace-nowrap border-b border-border bg-muted px-4 py-2.5 text-left font-medium text-foreground">
                <ColumnLabel name={column} primaryKeys={primaryKeys} schemaColumn={schemaColumns[column]} />
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => {
            // 不用 id、第一列、分页序号或浏览器字符串化猜测行身份。
            const rowPk = rowPks[rowIndex] ?? null
            return (
              <tr key={`${rowPk ?? 'invalid-row'}-${rowIndex}`} className="transition-colors hover:bg-muted">
                <td className="sticky left-0 z-10 border-b border-r border-border bg-card px-3 py-2.5 text-center tabular-nums text-[var(--color-text-tertiary)] select-none">
                  {startIndex + rowIndex + 1}
                </td>
                {columns.map(column => {
                  const isPrimaryKey = primaryKeys.includes(column)
                  const originalValue = cellText(row[column])
                  const edit = rowPk
                    ? pendingByCell.get(JSON.stringify([rowPk, column]))
                    : undefined
                  const value = edit?.new_value ?? originalValue
                  const label = columnDisplayText(column, schemaColumns[column])
                  return (
                    <td
                      key={column}
                      className={`max-w-[360px] border-b border-border px-2 py-1.5 ${
                        isPrimaryKey ? 'bg-[var(--color-warning-bg)] font-mono' : edit ? 'bg-[var(--color-warning-bg)]' : ''
                      }`}
                    >
                      {isPrimaryKey ? (
                        <span className="flex min-h-8 items-center gap-1.5 px-2 text-foreground" title={originalValue}>
                          <LockKeyhole size={10} className="shrink-0 text-[var(--color-warning)]" />
                          <span className="block truncate">{originalValue || '—'}</span>
                        </span>
                      ) : (
                        <input
                          type="text"
                          inputMode={schemaColumns[column]?.type === 'integer' || schemaColumns[column]?.type === 'float' ? 'decimal' : undefined}
                          value={value ?? ''}
                          disabled={disabled || !rowPk}
                          onChange={event => {
                            if (rowPk) onCellChange(rowPk, column, originalValue, event.target.value)
                          }}
                          aria-label={`编辑 ${label}，行主键 ${rowPk ?? '无效'}`}
                          title={!rowPk ? '该行主键为空，系统拒绝猜测行身份' : undefined}
                          className={`h-8 w-full min-w-[150px] rounded-md border px-2.5 text-xs text-foreground outline-none transition ${
                            edit
                              ? 'border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] font-medium focus:border-[var(--color-warning)] focus-visible:ring-2 focus-visible:ring-ring'
                              : 'border-transparent bg-transparent hover:border-border hover:bg-card focus:bg-card focus-visible:ring-2 focus-visible:ring-ring'
                          } disabled:cursor-not-allowed disabled:bg-muted disabled:text-[var(--color-text-tertiary)]`}
                        />
                      )}
                    </td>
                  )
                })}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/** 更新行按「旧行 → 新行」成对展示；每条新行紧跟对应旧行，宽表可水平滚动。 */
function UpdatedRowsTable({
  updates, primaryKeys, schemaColumns,
}: {
  updates: Array<{ before: DataRow; after: DataRow }>
  primaryKeys: string[]
  schemaColumns: Record<string, DatasetSchemaColumn>
}) {
  if (!updates.length) return <div className="p-6 text-center text-xs text-[var(--color-text-tertiary)]">无更新行</div>
  const allRows = updates.flatMap(update => [update.before, update.after])
  const columns = columnsFromRows(allRows, [...primaryKeys, ...Object.keys(schemaColumns)])

  return (
    <div className="max-w-full overflow-x-auto rounded-lg border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-card" data-testid="curated-updated-grid">
      <table className="w-max min-w-full text-xs">
        <thead className="sticky top-0 z-10 border-b bg-muted">
          <tr>
            <th className="sticky left-0 z-20 min-w-[170px] border-r bg-muted px-3 py-2 text-left font-medium text-muted-foreground">对比行 / 变更列</th>
            {columns.map(column => (
              <th key={column} className="min-w-[140px] whitespace-nowrap px-4 py-2 text-left font-medium text-muted-foreground">
                <ColumnLabel name={column} primaryKeys={primaryKeys} schemaColumn={schemaColumns[column]} />
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {updates.map((update, index) => {
            const changedColumns = new Set(
              columns.filter(column => cellComparable(update.before, column) !== cellComparable(update.after, column)),
            )
            const changedColumnLabels = Array.from(changedColumns)
              .map(column => columnDisplayText(column, schemaColumns[column]))
              .join('、')
            return (
              <Fragment key={`${primaryKeys.map(key => cellText(update.after[key])).join('::') || 'row'}-${index}`}>
                <tr className="border-t border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-danger-bg)]">
                  <td className="sticky left-0 z-[5] min-w-[170px] border-r border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-danger-bg)] px-3 py-2 align-top">
                    <span className="inline-flex rounded bg-[var(--color-danger-bg)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--color-danger)]">更新前</span>
                    <span className="ml-1 text-[10px] text-[var(--color-text-tertiary)]">#{index + 1}</span>
                    <span className="mt-1 block max-w-[150px] truncate text-[9px] text-[var(--color-danger)]" title={`变更列：${changedColumnLabels}`}>
                      变更列：{changedColumnLabels}
                    </span>
                  </td>
                  {columns.map(column => {
                    const changed = changedColumns.has(column)
                    return (
                      <td key={column} className={`max-w-[280px] px-4 py-2 ${changed ? 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]' : 'text-muted-foreground'} ${primaryKeys.includes(column) ? 'font-mono' : ''}`}>
                        <span className={`block truncate ${changed ? 'line-through decoration-[var(--color-danger)]' : ''}`} title={cellText(update.before[column])}>
                          {cellText(update.before[column]) || <span className="text-[var(--color-text-tertiary)]">—</span>}
                        </span>
                      </td>
                    )
                  })}
                </tr>
                <tr className="border-b border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-success-bg)]">
                  <td className="sticky left-0 z-[5] min-w-[170px] border-r border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-success-bg)] px-3 py-2 align-top">
                    <span className="inline-flex rounded bg-[var(--color-success-bg)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--color-success)]">更新后</span>
                    <span className="ml-1 text-[9px] text-[var(--color-success)]">绿色为新值</span>
                  </td>
                  {columns.map(column => {
                    const changed = changedColumns.has(column)
                    return (
                      <td key={column} className={`max-w-[280px] px-4 py-2 ${changed ? 'bg-[var(--color-success-bg)] font-semibold text-[var(--color-success)] ring-1 ring-inset ring-[var(--color-success)]' : 'text-muted-foreground'} ${primaryKeys.includes(column) ? 'font-mono' : ''}`}>
                        <span className="block truncate" title={cellText(update.after[column])}>
                          {cellText(update.after[column]) || <span className="text-[var(--color-text-tertiary)]">—</span>}
                        </span>
                        {changed && <span className="mt-0.5 block text-[9px] font-medium text-[var(--color-success)]">已变更</span>}
                      </td>
                    )
                  })}
                </tr>
              </Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export default function CuratedDetailPanel({
  datasetId, datasetName, datasetStatus, pipelineName,
  onClose, onStatusChange,
}: Props) {
  const [status, setStatus] = useState(datasetStatus)
  const [view, setView] = useState<View>(() => isPendingReview(datasetStatus) ? 'changes' : 'current')

  const [diff, setDiff] = useState<ReviewDiff | null>(null)
  const [schemaColumns, setSchemaColumns] = useState<Record<string, DatasetSchemaColumn>>({})
  const [schemaLoadError, setSchemaLoadError] = useState('')
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [selectedReviewId, setSelectedReviewId] = useState<string | undefined>()
  const [pageOffset, setPageOffset] = useState(0)
  const [pageSize, setPageSize] = useState<number>(DEFAULT_REVIEW_PAGE_SIZE)
  const [switchingReview, setSwitchingReview] = useState(false)
  const [exporting, setExporting] = useState<'csv' | 'xlsx' | null>(null)
  const [exportMessage, setExportMessage] = useState('')

  const [actionError, setActionError] = useState('')
  const [reviewAction, setReviewAction] = useState<'approve' | 'reject' | null>(null)
  const [pendingEdits, setPendingEdits] = useState<ReviewRowEdit[]>([])
  const [savingEdits, setSavingEdits] = useState(false)
  const [editMessage, setEditMessage] = useState('')
  const [pendingConfirm, setPendingConfirm] = useState<null | { title: string; description: string; action: () => void }>(null)

  const reviewPending = isPendingReview(status)
  const hasUnsavedEdits = pendingEdits.length > 0
  const primaryKeys = diff?.pk ?? []
  const reviewIsStale = Boolean(diff?.review?.stale)
  const reviewSessionIsPending = !diff?.review || diff.review.status === 'pending'
  const canEditCurrentRows = (
    reviewPending
    && !reviewIsStale
    && reviewSessionIsPending
    && primaryKeys.length > 0
    && (diff?.current_row_pks?.length ?? -1) === (diff?.current?.rows.length ?? 0)
  )
  const requestClose = useCallback(() => {
    if (hasUnsavedEdits) {
      setPendingConfirm({
        title: '关闭并放弃未保存修改',
        description: '还有未保存的审核修改，关闭后将丢失。确认关闭吗？',
        action: () => { onClose() },
      })
      return
    }
    onClose()
  }, [hasUnsavedEdits, onClose])

  const loadDiff = useCallback(() => {
    setLoading(true)
    setLoadError('')
    curatedApi.reviewDiff(datasetId, pageSize, pageOffset, selectedReviewId)
      .then(res => {
        const wrapped = res as ReviewDiff & { data?: ReviewDiff }
        const d = wrapped.data ?? res
        setDiff(d)
      })
      .catch((error: unknown) => setLoadError(errorText(error, '数据加载失败，请稍后重试')))
      .finally(() => setLoading(false))
  }, [datasetId, pageOffset, pageSize, selectedReviewId])

  useEffect(() => { void Promise.resolve().then(loadDiff) }, [loadDiff])

  useEffect(() => {
    setSchemaLoadError('')
    datasetsApi.schema(datasetId)
      .then(result => setSchemaColumns(Object.fromEntries(
        (result.columns ?? []).map(column => [column.name, column]),
      )))
      .catch(() => {
        setSchemaColumns({})
        setSchemaLoadError('字段中文名加载失败，当前暂以字段标识展示')
      })
  }, [datasetId])

  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') requestClose() }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [requestClose])

  /** 新建审核时再次核对版本，防止“页面加载后恰好入湖新版本”导致批准未看过的数据。 */
  const reviewIdForLoadedVersion = async (): Promise<string> => {
    // 已决定的 review 是不可变审计记录；这里只复用当前版本仍待决定的审核。
    if (diff?.review?.id && diff.review.status === 'pending') return diff.review.id
    const session = await curatedApi.startReview(datasetId)
    const loadedVersionId = diff?.current?.dataset_version_id
    if (loadedVersionId && session.dataset_version_id && loadedVersionId !== session.dataset_version_id) {
      throw new Error('数据集刚刚产生了新版本，当前页面不是最新快照。请关闭本面板并重新打开后再审核。')
    }
    return session.review_id
  }

  const handleCellChange = (
    rowPk: string,
    fieldName: string,
    oldValue: string,
    newValue: string,
  ) => {
    setEditMessage('')
    setActionError('')
    setPendingEdits(previous => {
      const existing = previous.find(
        edit => edit.row_pk === rowPk && edit.field_name === fieldName,
      )
      const baseline = existing?.old_value ?? oldValue
      const withoutCell = previous.filter(
        edit => !(edit.row_pk === rowPk && edit.field_name === fieldName),
      )
      return newValue === baseline
        ? withoutCell
        : [...withoutCell, {
            row_pk: rowPk,
            field_name: fieldName,
            old_value: baseline,
            new_value: newValue,
          }]
    })
  }

  const handleSaveEdits = async () => {
    if (!pendingEdits.length || savingEdits) return
    if (!canEditCurrentRows) {
      setActionError(
        reviewIsStale
          ? '该审核对应旧版本，不能保存修改。请切换到最新版本后重新编辑。'
          : '当前审核状态不允许保存行级修改。',
      )
      return
    }

    setSavingEdits(true)
    setActionError('')
    setEditMessage('')
    const editsToSave = [...pendingEdits]
    try {
      const reviewId = await reviewIdForLoadedVersion()
      const result = await curatedApi.saveEdits(reviewId, editsToSave)
      setPendingEdits([])
      setSelectedReviewId(reviewId)
      setEditMessage(`已保存 ${result.saved} 处审核修改；批准后才会进入正式数据消费。`)

      // 重新读取同一审核版本，让类型归一化后的值、审核影响和审计快照保持一致。
      try {
        const response = await curatedApi.reviewDiff(
          datasetId, pageSize, pageOffset, reviewId,
        )
        const wrapped = response as ReviewDiff & { data?: ReviewDiff }
        setDiff(wrapped.data ?? response)
      } catch (error: unknown) {
        setActionError(`修改已保存，但快照刷新失败：${errorText(error, '请点击重新加载')}`)
      }
    } catch (error: unknown) {
      setActionError(`保存修改失败：${errorText(error, '请核对字段类型后重试')}`)
    } finally {
      setSavingEdits(false)
    }
  }

  const handleApprove = async () => {
    if (!reviewPending) return
    if (hasUnsavedEdits) {
      setActionError('还有未保存的行级修改。请先保存修改并核对刷新后的结果，再通过审核。')
      return
    }
    if (diff?.review?.stale) {
      setActionError('该审核仅对应旧版本，不能批准最新数据。请先切换并审阅最新版本。')
      return
    }
    setReviewAction('approve')
    setActionError('')
    setEditMessage('')
    try {
      const reviewId = await reviewIdForLoadedVersion()
      const result = await curatedApi.approveReview(reviewId) as {
        mapping_dispatch?: {
          status?: string
          error?: string
          durable?: boolean
          event_status?: string
        }
      }
      setStatus('approved')
      setView('current')
      setDiff(previous => previous?.review
        ? { ...previous, review: { ...previous.review, status: 'approved' } }
        : previous)
      onStatusChange(datasetId, 'approved')
      if (result?.mapping_dispatch?.status === 'failed') {
        setActionError(result.mapping_dispatch.error || '数据已批准，但自动灌入本体失败，请检查映射任务。')
      } else if (result?.mapping_dispatch?.status === 'queued') {
        setEditMessage('审核已通过；自动灌入已进入可靠队列，失败时会自动重试。')
      } else if (result?.mapping_dispatch?.status === 'success') {
        setEditMessage('审核已通过，自动灌入本体已完成。')
      }
    } catch (error: unknown) {
      setActionError(`批准失败：${errorText(error, '请稍后重试')}`)
    } finally { setReviewAction(null) }
  }

  const handleReject = async () => {
    if (!reviewPending) return
    if (hasUnsavedEdits) {
      setActionError('还有未保存的行级修改。请先保存或还原修改，再拒绝本次数据。')
      return
    }
    if (diff?.review?.stale) {
      setActionError('该审核仅对应旧版本，不能拒绝最新数据。请先切换并审阅最新版本。')
      return
    }
    setReviewAction('reject')
    setActionError('')
    try {
      const reviewId = await reviewIdForLoadedVersion()
      await curatedApi.rejectReview(reviewId)
      setStatus('rejected')
      setView('current')
      setDiff(previous => previous?.review
        ? { ...previous, review: { ...previous.review, status: 'rejected' } }
        : previous)
      onStatusChange(datasetId, 'rejected')
    } catch (error: unknown) {
      setActionError(`拒绝失败：${errorText(error, '请稍后重试')}`)
    } finally { setReviewAction(null) }
  }

  const doSwitchToLatestReview = async () => {
    setSwitchingReview(true)
    setActionError('')
    try {
      const session = await curatedApi.startReview(datasetId)
      setPendingEdits([])
      setEditMessage('')
      setLoading(true)
      setPageOffset(0)
      setSelectedReviewId(session.review_id)
    } catch (error: unknown) {
      setActionError(`切换最新版本失败：${errorText(error, '请稍后重试')}`)
    } finally {
      setSwitchingReview(false)
    }
  }

  const handleSwitchToLatestReview = () => {
    if (hasUnsavedEdits) {
      setPendingConfirm({
        title: '切换版本并放弃修改',
        description: '切换版本会丢弃当前未保存的修改，确认继续吗？',
        action: () => { void doSwitchToLatestReview() },
      })
      return
    }
    void doSwitchToLatestReview()
  }

  const handleExport = async (format: 'csv' | 'xlsx') => {
    if (status !== 'approved') return
    setExporting(format)
    setExportMessage('')
    setActionError('')
    try {
      const blob = await curatedApi.export(datasetId, format)
      downloadBlob(blob, `${datasetName}.${format}`)
      setExportMessage(`已导出当前已审核版本的全部 ${(diff?.current?.total ?? 0).toLocaleString()} 行数据`)
    } catch (error: unknown) {
      setActionError(`${format.toUpperCase()} 导出失败：${errorText(error, '请稍后重试')}`)
    } finally {
      setExporting(null)
    }
  }

  const delta = diff?.delta
  const changeCount = delta ? delta.added_count + delta.updated_count + delta.deleted_count : 0
  const pagedView = view === 'current' ? diff?.current : view === 'previous' ? diff?.previous : null
  const pageStart = pagedView?.total ? (pagedView.offset ?? pageOffset) + 1 : 0
  const pageEnd = pagedView
    ? Math.min((pagedView.offset ?? pageOffset) + pagedView.rows.length, pagedView.total)
    : 0
  const totalRows = diff?.current?.total ?? 0
  const pagedTotal = pagedView?.total ?? 0
  const currentPage = pagedTotal ? Math.floor((pagedView?.offset ?? pageOffset) / pageSize) + 1 : 1
  const totalPages = Math.max(1, Math.ceil(pagedTotal / pageSize))

  const switchPage = (nextOffset: number) => {
    setPageOffset(Math.max(0, nextOffset))
  }

  const changePageSize = (nextSize: number) => {
    setPageSize(nextSize)
    setPageOffset(0)
  }

  const VIEW_TABS: [View, string, number | null][] = [
    ['changes', '审核影响', delta ? changeCount : null],
    ['previous', '上一已批准版本全量', diff?.previous?.total ?? null],
    ['current', '本次接受后全量', diff?.current?.total ?? null],
  ]

  return (
    <>
      <div className="fixed inset-0 z-40 flex items-center justify-center bg-accent p-4 backdrop-blur-[2px]" onClick={requestClose}>
        <div
          className="z-50 flex h-[78vh] max-h-[760px] min-h-[520px] w-[min(96vw,1440px)] flex-col overflow-hidden rounded-2xl border border-border bg-card shadow-[0_24px_80px_rgba(15,23,42,0.18)]"
          onClick={e => e.stopPropagation()}
          role="dialog"
          aria-modal="true"
          aria-labelledby="curated-review-title"
        >
          {/* Header */}
          <div className="flex shrink-0 items-start gap-3 border-b border-border px-5 py-4">
            <span className="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-xl bg-brand-soft text-brand-ink">
              <Table2 size={15} />
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <h2 id="curated-review-title" className="truncate text-sm font-semibold text-foreground">{datasetName}</h2>
                <span className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-xs ${STATUS_STYLE[status] || 'border-border bg-muted text-muted-foreground'}`}>
                  {STATUS_ICON(status)}
                  {STATUS_LABEL[status] || status}
                </span>
                <span className="text-xs tabular-nums text-[var(--color-text-tertiary)]">
                  v{diff?.current?.version_no ?? '—'} · {totalRows.toLocaleString()} 行
                </span>
                {primaryKeys.length > 0 && (
                  <span className="inline-flex items-center gap-1 rounded-md bg-[var(--color-warning-bg)] px-2 py-1 text-[11px] font-medium text-[var(--color-warning)]" title="主键列已校验非空">
                    <LockKeyhole size={10} /> 主键：{primaryKeys.join('、')}
                  </span>
                )}
              </div>
              <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">
                来源流水线：{pipelineName} · {
                  reviewPending
                    ? '核对版本变化；如需修正，请在“本次接受后全量”中按主键编辑并保存。'
                    : status === 'rejected'
                      ? '当前版本已拒绝，仅保留审计快照，不会进入本体或正式数据消费。'
                      : '当前批准版本仅供查看，可分页浏览或导出全量数据。'
                }
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {status === 'approved' && (
                <>
                  <button type="button" onClick={() => void handleExport('csv')} disabled={Boolean(exporting)}
                    className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-border bg-card px-2.5 text-xs font-medium text-muted-foreground transition hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring active:scale-[0.98] disabled:opacity-50">
                    {exporting === 'csv' ? <Loader2 size={12} className="animate-spin" /> : <Download size={12} />} 导出 CSV
                  </button>
                  <button type="button" onClick={() => void handleExport('xlsx')} disabled={Boolean(exporting)}
                    className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-border bg-card px-2.5 text-xs font-medium text-muted-foreground transition hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring active:scale-[0.98] disabled:opacity-50">
                    {exporting === 'xlsx' ? <Loader2 size={12} className="animate-spin" /> : <FileSpreadsheet size={12} />} 导出 Excel
                  </button>
                </>
              )}
              <button onClick={requestClose} className="grid h-8 w-8 place-items-center rounded-lg text-[var(--color-text-tertiary)] transition hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" aria-label="关闭审核详情">
                <X size={16} />
              </button>
            </div>
          </div>

          {/* 顶部只承载审核说明，决策动作统一收口到底部操作栏。 */}
          {reviewPending ? (
            <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-5 py-3 text-xs text-[var(--color-warning)]">
              <span className="inline-flex items-center gap-1.5 font-semibold">
                <Clock size={13} className="text-[var(--color-warning)]" />
                发现新数据，请完成审核
              </span>
              <span className="text-[var(--color-warning)]">请核对审核影响、上一已批准版本全量与本次接受后全量，再在底部作出决定。</span>
              <span className="ml-auto inline-flex items-center gap-1 text-[var(--color-warning)]">
                <LockKeyhole size={11} />
                {primaryKeys.length
                  ? '审核影响只读；本次全量可修正非主键字段'
                  : '无稳定主键，审核快照只读'}
              </span>
            </div>
          ) : status === 'rejected' ? (
            <div className="flex shrink-0 items-center gap-2 border-b border-viz-rose-soft bg-viz-rose-soft px-5 py-3 text-xs text-viz-rose">
              <AlertTriangle size={14} className="shrink-0" />
              <span className="font-medium">当前版本已拒绝</span>
              <span className="text-viz-rose">以下仅展示被拒绝的审核快照，供审计追溯；不会进入本体或正式数据消费。</span>
            </div>
          ) : (
            <div className="flex shrink-0 items-center gap-2 border-b border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-[var(--color-success-bg)] px-5 py-3 text-xs text-[var(--color-success)]">
              <CheckCircle size={14} className="shrink-0" />
              <span className="font-medium">当前没有新数据需要审核</span>
              <span className="text-[var(--color-success)]">以下展示当前已批准的数据版本，内容只读；导出不受当前分页限制。</span>
            </div>
          )}

          {reviewIsStale && (
            <div className="flex shrink-0 items-start gap-3 border-b border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-6 py-3 text-xs text-[var(--color-warning)]">
              <AlertTriangle size={15} className="mt-0.5 shrink-0" />
              <div className="min-w-0 flex-1">
                <p className="font-semibold">审核版本已过期，所有审核写操作已禁用</p>
                <p className="mt-0.5 text-[var(--color-warning)]">
                  当前面板审阅的是 v{diff?.current?.version_no ?? '—'}，数据集最新版本为 v{diff?.review?.latest_version_no ?? '—'}。
                  为避免用旧快照批准新数据，请切换后重新核对变化。
                </p>
              </div>
              <button
                type="button"
                onClick={handleSwitchToLatestReview}
                disabled={switchingReview}
                className="inline-flex shrink-0 items-center gap-1 rounded-lg border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-card px-3 py-1.5 font-medium text-[var(--color-warning)] hover:bg-[var(--color-warning-bg)] disabled:opacity-50"
              >
                {switchingReview ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
                切换到最新版本
              </button>
            </div>
          )}

          {actionError && (
            <div className="flex shrink-0 items-start gap-2 border-b border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-6 py-2 text-xs text-[var(--color-danger)]" role="alert">
              <AlertTriangle size={13} className="mt-0.5 shrink-0" />
              <span className="flex-1">{actionError}</span>
              <button type="button" onClick={() => setActionError('')} className="text-[var(--color-danger)] hover:text-[var(--color-danger)]" aria-label="关闭错误提示">×</button>
            </div>
          )}

          {editMessage && (
            <div className="flex shrink-0 items-center gap-2 border-b border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-[var(--color-success-bg)] px-6 py-2 text-xs text-[var(--color-success)]" role="status">
              <CheckCircle size={13} className="shrink-0" />
              <span className="flex-1">{editMessage}</span>
              <button type="button" onClick={() => setEditMessage('')} className="text-[var(--color-success)] hover:text-[var(--color-success)]" aria-label="关闭保存提示">×</button>
            </div>
          )}

          {exportMessage && (
            <div className="mx-5 mt-3 flex shrink-0 items-center gap-2 rounded-lg border border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-[var(--color-success-bg)] px-3 py-2 text-xs text-[var(--color-success)]" role="status">
              <CheckCircle size={13} className="shrink-0" />
              <span className="flex-1">{exportMessage}</span>
              <button type="button" onClick={() => setExportMessage('')} className="text-[var(--color-success)] hover:text-[var(--color-success)]" aria-label="关闭导出提示">×</button>
            </div>
          )}

          {schemaLoadError && (
            <div className="flex shrink-0 items-center gap-2 border-b border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-6 py-2 text-xs text-[var(--color-warning)]">
              <AlertTriangle size={13} /> {schemaLoadError}
            </div>
          )}

          {!loading && !loadError && diff && reviewPending && primaryKeys.length === 0 && (
            <div className="flex shrink-0 items-start gap-2 border-b border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-[var(--color-info-bg)] px-5 py-2.5 text-xs text-[var(--color-info)]">
              <Info size={13} className="mt-0.5 shrink-0" />
              <span>
                当前流水线采用无主键模式，可以正常审核，但无法安全定位具体行，因此行级修正不可用。
                系统会按整行内容比较，字段变化将显示为“删除旧行 + 新增新行”；如需在审核中修正，请先在流水线数据契约中声明主键并重新入湖。
              </span>
            </div>
          )}

          {/* View switcher */}
          {!loading && !loadError && (
            <div className="flex shrink-0 items-center gap-1 border-b border-border bg-card px-5 py-2.5">
              {reviewPending ? VIEW_TABS.map(([v, label, count]) => (
                <button
                  key={v}
                  disabled={hasUnsavedEdits && v !== 'current'}
                  title={hasUnsavedEdits && v !== 'current' ? '请先保存或还原当前修改' : undefined}
                  onClick={() => {
                    setView(v)
                    if (v !== 'changes') setPageOffset(0)
                  }}
                  className={`px-3 py-1 text-xs rounded-full border transition-colors ${
                    view === v ? 'bg-accent text-[var(--color-text-inverse)] border-[var(--color-border-hover)]' : 'bg-card text-muted-foreground border-border hover:bg-muted'
                  } disabled:cursor-not-allowed disabled:opacity-45`}>
                  {label}{count !== null && <span className={`ml-1 ${view === v ? 'text-[var(--color-text-tertiary)]' : 'text-[var(--color-text-tertiary)]'}`}>{count}</span>}
                </button>
              )) : (
                <span className="inline-flex items-center gap-1.5 text-xs font-medium text-foreground">
                  <Table2 size={13} className={status === 'rejected' ? 'text-viz-rose' : 'text-brand-ink'} />
                  {status === 'rejected' ? '已拒绝版本快照' : '已批准全量数据'}
                </span>
              )}
              {reviewPending && (
                <span className="ml-2 text-[11px] text-[var(--color-text-tertiary)]">
                  {view === 'changes' && '审核影响（相对上一已批准版本，含人工修正）'}
                  {view === 'previous' && '上一已批准版本完整数据（分页查看，用于对照）'}
                  {view === 'current' && (
                    canEditCurrentRows
                      ? '如果接受本次变化，数据集将呈现的完整数据 · 可修正非主键字段'
                      : '如果接受本次变化，数据集将呈现的完整数据 · 只读预览'
                  )}
                </span>
              )}
            </div>
          )}

          {/* Body */}
          <div className={`min-h-0 flex-1 ${reviewPending && view === 'changes' ? 'overflow-auto' : 'overflow-hidden px-5 py-3'}`}>
            {loading ? (
              <div className="flex h-full items-center justify-center gap-2 text-sm text-[var(--color-text-tertiary)]">
                <Loader2 size={16} className="animate-spin" /> 加载中...
              </div>
            ) : loadError ? (
              <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center text-sm text-[var(--color-danger)]">
                <AlertTriangle size={24} className="opacity-70" />
                <p className="font-medium">审核数据加载失败</p>
                <p className="max-w-lg text-xs text-[var(--color-danger)]">{loadError}</p>
                <button type="button" onClick={loadDiff} className="mt-1 inline-flex items-center gap-1 rounded-lg border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-card px-3 py-1.5 text-xs hover:bg-[var(--color-danger-bg)]">
                  <RefreshCw size={12} /> 重新加载
                </button>
              </div>
            ) : !reviewPending ? (
              <ReadonlyTable
                rows={diff?.current?.rows ?? []}
                primaryKeys={primaryKeys}
                startIndex={diff?.current?.offset ?? pageOffset}
                schemaColumns={schemaColumns}
                fillAvailable
              />
            ) : view === 'changes' ? (
              <ChangesView delta={delta ?? null} prevNo={diff?.previous?.version_no ?? null}
                curNo={diff?.current?.version_no ?? null} primaryKeys={primaryKeys} schemaColumns={schemaColumns} />
            ) : view === 'previous' ? (
              diff?.previous?.version_no == null
                ? <div className="grid h-full place-items-center rounded-xl border border-border bg-muted text-sm text-[var(--color-text-tertiary)]">这是首个待审核版本，没有上一已批准版本可对照。</div>
                : <ReadonlyTable rows={diff.previous.rows} primaryKeys={primaryKeys} startIndex={diff.previous.offset ?? pageOffset} schemaColumns={schemaColumns} fillAvailable />
            ) : (
              canEditCurrentRows ? (
                <EditableReviewTable
                  rows={diff?.current?.rows ?? []}
                  rowPks={diff?.current_row_pks ?? []}
                  primaryKeys={primaryKeys}
                  startIndex={diff?.current?.offset ?? pageOffset}
                  schemaColumns={schemaColumns}
                  pendingEdits={pendingEdits}
                  disabled={savingEdits || Boolean(reviewAction)}
                  onCellChange={handleCellChange}
                />
              ) : (
                <ReadonlyTable
                  rows={diff?.current?.rows ?? []}
                  primaryKeys={primaryKeys}
                  startIndex={diff?.current?.offset ?? pageOffset}
                  schemaColumns={schemaColumns}
                  fillAvailable
                />
              )
            )}
          </div>

          {reviewPending ? (
            <div
              className="flex min-h-16 shrink-0 flex-wrap items-center gap-x-4 gap-y-2 border-t border-border bg-muted px-5 py-3"
              data-testid="curated-review-actions"
            >
              <div className="flex min-w-0 flex-1 flex-wrap items-center gap-3">
                {view === 'changes' ? (
                  <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
                    <Info size={12} className="shrink-0 text-brand-ink" />
                    审核影响相对上一已批准版本计算，并包含已保存的人工修正；更新前使用红色，更新后使用绿色，具体变更列会单独标出。
                  </span>
                ) : (
                  <>
                    <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
                      每页
                      <PageSizeSelect
                        value={pageSize}
                        onChange={changePageSize}
                        sizes={REVIEW_PAGE_SIZES}
                        ariaLabel="待审核数据每页显示条数"
                        disabled={hasUnsavedEdits}
                        title={hasUnsavedEdits ? '请先保存或还原当前修改' : undefined}
                      />
                      条
                    </label>
                    <div className="flex items-center gap-1 text-xs text-muted-foreground">
                      <button type="button" onClick={() => switchPage(pageOffset - pageSize)} disabled={pageOffset <= 0 || loading || hasUnsavedEdits}
                        aria-label="上一页"
                        className="grid h-8 w-8 place-items-center rounded-lg border border-border bg-card transition hover:border-brand-line hover:text-brand-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring active:scale-[0.98] disabled:opacity-35">
                        <ChevronLeft size={13} />
                      </button>
                      <span className="min-w-52 text-center tabular-nums">
                        第 {currentPage} / {totalPages} 页 · {pagedTotal ? `${pageStart.toLocaleString()}–${pageEnd.toLocaleString()}` : 0} / {pagedTotal.toLocaleString()} 行
                      </span>
                      <button type="button" onClick={() => switchPage(pageOffset + pageSize)} disabled={!pagedView?.has_more || loading || hasUnsavedEdits}
                        aria-label="下一页"
                        className="grid h-8 w-8 place-items-center rounded-lg border border-border bg-card transition hover:border-brand-line hover:text-brand-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring active:scale-[0.98] disabled:opacity-35">
                        <ChevronRight size={13} />
                      </button>
                    </div>
                    {view === 'current' && canEditCurrentRows && (
                      <div className="flex items-center gap-2">
                        <span className={`text-xs ${hasUnsavedEdits ? 'font-medium text-[var(--color-warning)]' : 'text-[var(--color-text-tertiary)]'}`} role="status">
                          {hasUnsavedEdits
                            ? `${pendingEdits.length} 处修改尚未保存`
                            : '非主键字段可编辑；主键用于稳定定位且不可修改'}
                        </span>
                        {hasUnsavedEdits && (
                          <button
                            type="button"
                            onClick={() => void handleSaveEdits()}
                            disabled={savingEdits || Boolean(reviewAction)}
                            className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-card px-3 text-xs font-semibold text-[var(--color-warning)] transition hover:bg-[var(--color-warning-bg)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-45"
                          >
                            {savingEdits ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} />}
                            保存 {pendingEdits.length} 处修改
                          </button>
                        )}
                      </div>
                    )}
                  </>
                )}
              </div>

              <div className="ml-auto flex shrink-0 items-center gap-2 border-l border-border pl-4">
                <button type="button" onClick={requestClose}
                  className="h-9 rounded-lg border border-border bg-card px-3.5 text-xs font-medium text-muted-foreground transition hover:border-border hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring active:scale-[0.98]">
                  关闭
                </button>
                <button type="button" onClick={handleReject} disabled={Boolean(reviewAction) || reviewIsStale || loading || savingEdits || hasUnsavedEdits}
                  title={reviewIsStale ? '审核版本已过期，请先切换到最新版本' : hasUnsavedEdits ? '请先保存或还原当前修改' : '拒绝当前审核版本'}
                  className="inline-flex h-9 items-center gap-1.5 rounded-lg border border-viz-rose-soft bg-card px-3.5 text-xs font-medium text-viz-rose transition hover:border-viz-rose-soft hover:bg-viz-rose-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-45">
                  {reviewAction === 'reject' ? <Loader2 size={13} className="animate-spin" /> : <AlertTriangle size={13} />}
                  拒绝本次数据
                </button>
                <button type="button" onClick={handleApprove} disabled={Boolean(reviewAction) || reviewIsStale || loading || savingEdits || hasUnsavedEdits}
                  title={reviewIsStale ? '审核版本已过期，请先切换到最新版本' : hasUnsavedEdits ? '请先保存或还原当前修改' : '通过当前审核版本'}
                  className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-[var(--color-success)] px-4 text-xs font-semibold text-[var(--color-text-inverse)] shadow-sm transition hover:bg-[var(--color-success)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-45">
                  {reviewAction === 'approve' ? <Loader2 size={13} className="animate-spin" /> : <CheckCircle size={13} />}
                  通过审核
                </button>
              </div>
            </div>
          ) : (
            <div className="flex shrink-0 flex-wrap items-center gap-3 border-t border-border bg-muted px-5 py-3">
              <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
                每页
                <PageSizeSelect
                  value={pageSize}
                  onChange={changePageSize}
                  sizes={REVIEW_PAGE_SIZES}
                  ariaLabel={status === 'rejected' ? '已拒绝快照每页显示条数' : '已批准数据每页显示条数'}
                />
                条
              </label>
              <div className="flex items-center gap-1 text-xs text-muted-foreground">
                <button type="button" onClick={() => switchPage(pageOffset - pageSize)} disabled={pageOffset <= 0 || loading}
                  aria-label="上一页"
                  className="grid h-8 w-8 place-items-center rounded-lg border border-border bg-card transition hover:border-brand-line hover:text-brand-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring active:scale-[0.98] disabled:opacity-35">
                  <ChevronLeft size={13} />
                </button>
                <span className="min-w-52 text-center tabular-nums">
                  第 {currentPage} / {totalPages} 页 · {pagedTotal ? `${pageStart.toLocaleString()}–${pageEnd.toLocaleString()}` : 0} / {pagedTotal.toLocaleString()} 行
                </span>
                <button type="button" onClick={() => switchPage(pageOffset + pageSize)} disabled={!diff?.current?.has_more || loading}
                  aria-label="下一页"
                  className="grid h-8 w-8 place-items-center rounded-lg border border-border bg-card transition hover:border-brand-line hover:text-brand-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring active:scale-[0.98] disabled:opacity-35">
                  <ChevronRight size={13} />
                </button>
              </div>
              <span className="inline-flex items-center gap-1 text-xs text-[var(--color-text-tertiary)]">
                <LockKeyhole size={11} /> 只读模式，不会修改数据
              </span>
              <button type="button" onClick={requestClose}
                className="ml-auto h-8 rounded-lg border border-border bg-card px-3 text-xs font-medium text-muted-foreground transition hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring active:scale-[0.98]">
                关闭
              </button>
            </div>
          )}
        </div>
      </div>

      <ConfirmDialog
        open={pendingConfirm !== null}
        onClose={() => setPendingConfirm(null)}
        onConfirm={() => { const c = pendingConfirm; setPendingConfirm(null); c?.action() }}
        title={pendingConfirm?.title ?? ''}
        description={pendingConfirm?.description}
        variant="warning"
      />
    </>
  )
}

/** 审核影响视图：相对上一已批准版本的新增/更新/删除，包含已保存的人工修正 */
function ChangesView({ delta, prevNo, curNo, primaryKeys, schemaColumns }: {
  delta: ReviewDiff['delta']
  prevNo: number | null
  curNo: number | null
  primaryKeys: string[]
  schemaColumns: Record<string, DatasetSchemaColumn>
}) {
  if (!delta) return <div className="p-8 text-center text-sm text-[var(--color-text-tertiary)]">暂无版本数据</div>
  const { added_count, updated_count, deleted_count, unchanged_count } = delta
  const noChange = added_count + updated_count + deleted_count === 0

  return (
    <div className="p-4 space-y-4">
      {/* 摘要 */}
      <div className="flex items-center gap-3 flex-wrap text-xs">
        <span className="text-muted-foreground">
          {prevNo == null ? '首个版本（全部为新增）' : `v${prevNo} → v${curNo}`}
        </span>
        {primaryKeys.length ? (
          <span className="inline-flex items-center gap-1 rounded-md bg-muted px-2 py-1 text-[10px] text-muted-foreground">
            <KeyRound size={9} /> 按主键 {primaryKeys.join('、')} 识别同一行
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 rounded-md bg-[var(--color-info-bg)] px-2 py-1 text-[10px] text-[var(--color-info)]">
            <Info size={9} /> 无主键 · 按整行比较，不单独识别更新
          </span>
        )}
        <span className="inline-flex items-center gap-1 text-[var(--color-success)]"><Plus size={12} />新增 {added_count}</span>
        <span className="inline-flex items-center gap-1 text-[var(--color-warning)]"><Pencil size={11} />更新 {updated_count}</span>
        <span className="inline-flex items-center gap-1 text-[var(--color-danger)]"><Minus size={12} />删除 {deleted_count}</span>
        <span className="text-[var(--color-text-tertiary)]">未变 {unchanged_count}</span>
        {delta.sample_truncated && <span className="text-[var(--color-text-tertiary)]">（每类仅展示部分样本）</span>}
      </div>

      {noChange && <div className="p-6 text-center text-sm text-[var(--color-text-tertiary)]">与上一已批准版本相比没有数据变化。</div>}

      {updated_count > 0 && (
        <section>
          <div className="mb-1.5 flex items-center justify-between gap-3">
            <h4 className="flex items-center gap-1 text-xs font-medium text-[var(--color-warning)]"><Pencil size={11} />更新的行（{updated_count}）</h4>
            <span className="text-[10px] text-[var(--color-text-tertiary)]">每条“更新后”紧跟对应“更新前”；绿色单元格为变更值</span>
          </div>
          <UpdatedRowsTable updates={delta.updated_sample} primaryKeys={primaryKeys} schemaColumns={schemaColumns} />
        </section>
      )}

      {added_count > 0 && (
        <section>
          <h4 className="text-xs font-medium text-[var(--color-success)] mb-1.5 flex items-center gap-1"><Plus size={12} />新增的行（{added_count}）</h4>
          <div className="overflow-hidden rounded-lg border"><ReadonlyTable rows={delta.added_sample} highlight="add" primaryKeys={primaryKeys} schemaColumns={schemaColumns} /></div>
        </section>
      )}

      {deleted_count > 0 && (
        <section>
          <h4 className="text-xs font-medium text-[var(--color-danger)] mb-1.5 flex items-center gap-1"><Minus size={12} />删除的行（{deleted_count}）</h4>
          <div className="overflow-hidden rounded-lg border"><ReadonlyTable rows={delta.deleted_sample} highlight="del" primaryKeys={primaryKeys} schemaColumns={schemaColumns} /></div>
        </section>
      )}

      {delta.sample_truncated && (
        <div className="flex items-start gap-2 rounded-lg border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-3 py-2 text-xs text-[var(--color-warning)]">
          <AlertTriangle size={13} className="mt-0.5 shrink-0" />
          <span>
            本次变化超过接口单次明细上限，当前已完整展示接口返回的明细，但并非全部 {changeCountText(delta)} 行。
            为避免误判，请结合“上一已批准版本全量 / 本次接受后全量”核对；后端需要提供变化明细分页后才能可靠展示全部记录。
          </span>
        </div>
      )}
    </div>
  )
}

function changeCountText(delta: NonNullable<ReviewDiff['delta']>): string {
  return String(delta.added_count + delta.updated_count + delta.deleted_count)
}
