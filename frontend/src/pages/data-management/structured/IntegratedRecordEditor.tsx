import { useId, useState } from 'react'
import { Pencil, Plus, RefreshCw, Trash2 } from 'lucide-react'
import type { IntegratedColumn, IntegratedRow, MutationReceipt } from '@/api/v2/integrated-datasets'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/Input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { cn } from '@/lib/utils'
import {
  jsonTextFromRecordForm, payloadFromRecordForm, recordFormFromRow,
  type RecordFormDraft, type RecordEditMode,
} from './integratedDetailModel'
import { parseObject, receiptStatusLabel } from './integratedModel'

export type RecordIntent = { mode: RecordEditMode | 'delete'; row?: IntegratedRow; idempotencyKey: string }

const TYPE_PLACEHOLDERS: Record<string, string> = {
  integer: '整数，如 42', decimal: '小数，如 12.50', float: '数字，如 1.5',
  date: '例如 2026-10-29',
  json: '{"key": "value"}', binary: 'Base64 文本', string: '文本',
}

/** Temporal examples follow the column's own time-zone flag (source sessions run in UTC). */
const TEMPORAL = new Set(['timestamp', 'date', 'time'])
const temporalPlaceholder = (column: IntegratedColumn): string => {
  if (column.type === 'timestamp') return column.timezone === true ? '例如 2026-10-29 04:31:54+00:00' : '例如 2026-10-29 12:00:00'
  if (column.type === 'time') return column.timezone === true ? '例如 04:31:54+00:00' : '例如 12:00:00'
  return TYPE_PLACEHOLDERS.date
}

/** Radix Select rejects an empty string value; sentinels stand for "not supplied". */
const UNSET = '__unset__'
const NULL_VALUE = '__null__'

function receiptGuidance(status: MutationReceipt['status'] | undefined): { title: string; detail: string } {
  if (status === 'failed') return { title: '源表拒绝了这次写入', detail: '填写的内容已保留，可以修改后再点「写入源表」，会使用新的请求重试。' }
  if (status === 'conflict') return { title: '这条记录在源表里已经被改过', detail: '请取消并刷新数据后再编辑。' }
  return { title: '没能确认写入结果', detail: '已暂停新的写入。请先核查回执，不要重复提交。' }
}

function Field({
  id, column, isKey, readOnly, value, isNull, error, onChange, onNullChange,
}: {
  id: string
  column: IntegratedColumn
  isKey: boolean
  readOnly: boolean
  value: string
  isNull: boolean
  error?: string
  onChange: (text: string) => void
  onNullChange: (isNull: boolean) => void
}) {
  const display = column.display_name?.trim()
  const hasAlias = !!display && display !== column.name
  const multiLine = column.type === 'json'
  return (
    <div className={multiLine ? 'sm:col-span-2' : undefined}>
      <label htmlFor={id} className="flex items-baseline gap-1.5 text-xs font-medium text-foreground">
        <span className="truncate">{hasAlias ? display : column.name}</span>
        {isKey && (
          <span className="shrink-0 rounded bg-[var(--color-primary-light)] px-1.5 text-[11px] font-medium text-[var(--color-primary)]">主键</span>
        )}
        {!isKey && !column.nullable && !readOnly && (
          <span aria-hidden="true" className="shrink-0 text-[var(--color-danger)]">*</span>
        )}
        {hasAlias && <span className="truncate font-mono text-[11px] font-normal text-muted-foreground">{column.name}</span>}
      </label>
      <div className="mt-1.5 flex items-center gap-2">
        {column.type === 'boolean' ? (
          <Select
            value={isNull ? NULL_VALUE : value === 'true' ? 'true' : value === 'false' ? 'false' : UNSET}
            onValueChange={next => {
              if (next === NULL_VALUE) onNullChange(true)
              else { onNullChange(false); onChange(next === UNSET ? '' : next) }
            }}
            disabled={readOnly}
          >
            <SelectTrigger id={id} className="w-full" aria-label={column.name}>
              <SelectValue placeholder="不填写" />
            </SelectTrigger>
            {/* 编辑弹窗位于 nested 层（自身遮罩 z = --z-modal+10）：下拉默认的 +1 会被
                本弹窗遮罩压住而无法用指针选择，须高于所在弹窗的遮罩。 */}
            <SelectContent className="z-[calc(var(--z-modal)+11)]">
              <SelectItem value={UNSET}>不填写</SelectItem>
              <SelectItem value="true">是</SelectItem>
              <SelectItem value="false">否</SelectItem>
              {column.nullable && <SelectItem value={NULL_VALUE}>NULL（置空）</SelectItem>}
            </SelectContent>
          </Select>
        ) : multiLine ? (
          <textarea
            id={id}
            rows={3}
            value={isNull ? '' : value}
            disabled={readOnly || isNull}
            placeholder={TEMPORAL.has(column.type) ? temporalPlaceholder(column) : (TYPE_PLACEHOLDERS[column.type] ?? 'JSON')}
            onChange={event => onChange(event.target.value)}
            className="min-h-[4.5rem] w-full rounded-md border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-3 py-2 font-mono text-xs leading-5 transition-colors placeholder:text-[var(--color-text-tertiary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
          />
        ) : (
          <Input
            id={id}
            value={isNull ? '' : value}
            disabled={readOnly || isNull}
            placeholder={TEMPORAL.has(column.type) ? temporalPlaceholder(column) : (TYPE_PLACEHOLDERS[column.type] ?? 'JSON 值')}
            aria-invalid={error ? true : undefined}
            onChange={event => onChange(event.target.value)}
            className={cn('h-8 text-xs', column.type !== 'string' && 'font-mono')}
          />
        )}
        {column.nullable && !readOnly && (
          <label className="flex shrink-0 items-center gap-1 text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={isNull}
              disabled={readOnly}
              onChange={event => onNullChange(event.target.checked)}
              className="size-3.5 accent-[var(--color-primary)]"
            />
            置空
          </label>
        )}
      </div>
      <div className="mt-1 flex min-h-4 items-center gap-2 text-[11px]">
        {error ? (
          <p role="alert" className="text-[var(--color-danger)]">{error}</p>
        ) : (
          <span className="truncate text-muted-foreground">
            {readOnly ? '主键不可修改' : column.type === 'boolean' ? '' : column.nullable ? '可留空' : '不允许为空'}
            {column.source_type && <span className="font-mono"> · {column.source_type}</span>}
          </span>
        )}
      </div>
    </div>
  )
}

/**
 * Nested editor for one source row. The field form is the default input: it
 * serialises per logical type so big integers and decimals keep their exact
 * digits. The raw JSON editor stays available for advanced values.
 */
export default function IntegratedRecordEditor({
  intent, columns, primaryKey, resource, values, saving, locked, outcomeUnconfirmed, receipt, mutationError,
  onValuesChange, onWrite, onRetryWrite, onClose, onRetry, onReloadAfterConflict, onRefreshReceipt, onCloseAutoFocus,
}: {
  intent: RecordIntent
  columns: IntegratedColumn[]
  primaryKey: string[]
  /** `schema.table` of the source table the write targets. */
  resource: string
  /** JSON-mode text, owned by the caller so failed writes keep their input. */
  values: string
  saving: boolean
  /** A settled attempt that was not a clean failure keeps the editor read-only. */
  locked: boolean
  outcomeUnconfirmed: boolean
  receipt: MutationReceipt | null
  mutationError: string
  onValuesChange: (next: string) => void
  onWrite: (valuesText: string) => void
  /** Resends a settled failure with the current input under a fresh idempotency key. */
  onRetryWrite: (valuesText: string) => void
  onClose: () => void
  onRetry: () => void
  onReloadAfterConflict: () => void
  onRefreshReceipt: () => void
  onCloseAutoFocus: (event: Event) => void
}) {
  const isDelete = intent.mode === 'delete'
  const [mode, setMode] = useState<'form' | 'json'>(isDelete ? 'json' : 'form')
  // Never changes during one editing session: updates diff against it.
  const [initial] = useState<RecordFormDraft>(() => recordFormFromRow(columns, intent.row?.values))
  const [draft, setDraft] = useState<RecordFormDraft>(() => recordFormFromRow(columns, intent.row?.values))
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [switchError, setSwitchError] = useState('')
  const baseId = useId()
  const editorLocked = locked || outcomeUnconfirmed
  const title = isDelete ? '删除源记录' : intent.mode === 'insert' ? '新增源记录' : '编辑源记录'

  const editField = (name: string, text: string) => {
    setDraft(current => ({ fields: { ...current.fields, [name]: text }, nulls: current.nulls.filter(item => item !== name) }))
    setErrors(current => {
      if (!current[name]) return current
      const next = { ...current }
      delete next[name]
      return next
    })
  }
  const editNull = (name: string, isNull: boolean) => {
    setDraft(current => ({
      fields: current.fields,
      nulls: isNull ? [...new Set([...current.nulls, name])] : current.nulls.filter(item => item !== name),
    }))
    setErrors(current => {
      if (!current[name]) return current
      const next = { ...current }
      delete next[name]
      return next
    })
  }
  const switchToJson = () => {
    onValuesChange(jsonTextFromRecordForm(columns, draft))
    setErrors({})
    setSwitchError('')
    setMode('json')
  }
  const switchToForm = () => {
    let parsed: Record<string, unknown>
    try {
      parsed = parseObject(values, '记录值')
    } catch (failure) {
      setSwitchError(`${(failure as Error).message}，无法切换为按字段填写`)
      return
    }
    setDraft(recordFormFromRow(columns, parsed))
    setErrors({})
    setSwitchError('')
    setMode('form')
  }
  const submit = () => {
    // A settled failure cannot be replayed under its key, so the same button
    // resends the current input as a new request instead of doing nothing.
    // A failed receipt only ever exists after an attempt, so it alone marks the resend path.
    const send = (text: string) => { (receipt?.status === 'failed' ? onRetryWrite : onWrite)(text) }
    if (intent.mode === 'delete' || mode === 'json') { send(values); return }
    const result = payloadFromRecordForm({
      columns, primaryKey, draft, initial, mode: intent.mode, original: intent.row?.values,
    })
    if (!result.ok) { setErrors(result.errors); return }
    send(JSON.stringify({ ...result.key, ...result.values }, null, 2))
  }

  // 回执引导只适用于已有回执或结果未知的写入。请求尚未发出（本地校验失败）或被
  // 直接拒绝时没有回执可言：如实显示原因本身，不能说成「没能确认写入结果」。
  const guidance = !!mutationError && (!!receipt || outcomeUnconfirmed) ? receiptGuidance(receipt?.status) : null

  return (
    <Dialog open onOpenChange={open => { if (!open) onClose() }}>
      <DialogContent
        dismissible={!saving}
        layer="nested"
        className="max-h-[85vh] w-[min(92vw,42rem)] overflow-y-auto"
        onCloseAutoFocus={onCloseAutoFocus}
      >
        <DialogHeader
          icon={isDelete ? <Trash2 size={18} /> : intent.mode === 'insert' ? <Plus size={18} /> : <Pencil size={18} />}
          iconClassName={isDelete ? 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]' : undefined}
        >
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>
            {isDelete ? `将从源表 ${resource} 删除这条记录，删除后无法恢复。` : `将直接写入源表 ${resource}，不是修改平台里的数据版本。`}
          </DialogDescription>
        </DialogHeader>

        {isDelete ? (
          <pre className="max-h-64 overflow-auto rounded-lg bg-muted p-3 font-mono text-xs leading-5">
            {JSON.stringify(intent.row?.values, null, 2)}
          </pre>
        ) : (
          <>
            <div className="mb-3 flex items-center gap-3">
              <div role="group" aria-label="填写方式" className="inline-flex rounded-lg border border-border p-0.5">
                {([['form', '按字段填写'], ['json', 'JSON']] as const).map(([value, label]) => (
                  <button
                    key={value}
                    type="button"
                    aria-pressed={mode === value}
                    disabled={editorLocked}
                    onClick={() => { if (mode !== value) (value === 'json' ? switchToJson : switchToForm)() }}
                    className={cn(
                      'rounded-md px-2.5 py-1 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50',
                      mode === value ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground',
                    )}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <span className="text-xs text-muted-foreground">
                {intent.mode === 'insert' ? '主键必填，其余留空表示不填写' : '只写入改动过的字段'}
              </span>
            </div>
            {switchError && <Alert variant="danger" role="alert" className="mb-3 text-xs">{switchError}</Alert>}
            {mode === 'form' ? (
              <div className="space-y-3" data-testid="integrated-record-form">
                <div className="grid gap-x-4 gap-y-3 sm:grid-cols-2">
                  {columns.map((column, index) => {
                    const isKey = primaryKey.includes(column.name)
                    return (
                      <Field
                        key={column.name}
                        id={`${baseId}-${index}`}
                        column={column}
                        isKey={isKey}
                        readOnly={editorLocked || (isKey && intent.mode === 'update')}
                        value={draft.fields[column.name] ?? ''}
                        isNull={draft.nulls.includes(column.name)}
                        error={errors[column.name]}
                        onChange={text => editField(column.name, text)}
                        onNullChange={isNull => editNull(column.name, isNull)}
                      />
                    )
                  })}
                </div>
                {errors.form && <p role="alert" className="text-xs text-[var(--color-danger)]">{errors.form}</p>}
              </div>
            ) : (
              <label className="block space-y-1 text-xs font-medium">
                源记录值（JSON）
                <textarea
                  aria-label="源记录值 JSON"
                  disabled={saving || editorLocked}
                  value={values}
                  onChange={event => onValuesChange(event.target.value)}
                  rows={10}
                  className="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                />
              </label>
            )}
          </>
        )}

        {guidance && (
          <Alert variant="danger" role="alert" className="mt-3">
            <p className="font-medium">{guidance.title}</p>
            <p className="mt-0.5 text-xs">{guidance.detail}</p>
            {receipt?.error_code || receipt?.error ? (
              <p className="mt-0.5 font-mono text-[11px] opacity-80">{[receipt.error_code, receipt.error].filter(Boolean).join(' · ')}</p>
            ) : null}
            {/* Recovery must live inside the modal: it blocks every page-level button. */}
            {receipt?.status === 'failed' && (
              <Button type="button" variant="link" size="sm" className="ml-2 h-auto px-1" disabled={saving} onClick={onRetry}>
                <RefreshCw size={13} />用新的请求重试
              </Button>
            )}
            {receipt?.status === 'conflict' && (
              <Button type="button" variant="link" size="sm" className="ml-2 h-auto px-1" disabled={saving} onClick={() => void onReloadAfterConflict()}>
                <RefreshCw size={13} />取消并刷新数据
              </Button>
            )}
          </Alert>
        )}
        {mutationError && !guidance && (
          <Alert variant="danger" role="alert" className="mt-3">{mutationError}</Alert>
        )}

        <details className="mt-3 text-xs text-muted-foreground">
          <summary className="cursor-pointer select-none">写入说明</summary>
          <ul className="mt-2 list-disc space-y-1 pl-4 leading-5">
            <li>写入直接作用于源表；平台保存的本地数据会在同步后更新。</li>
            <li>更新和删除前会核对该记录自上次读取后是否被改动，有改动会拒绝写入。</li>
            <li>大整数和小数按文本原样提交，不会丢失精度；留空表示不填写。</li>
            <li className="break-all font-mono text-[11px]">请求标识 {intent.idempotencyKey}</li>
          </ul>
        </details>

        {receipt && (
          <p className="mt-2 text-xs text-muted-foreground">
            写入回执 {receiptStatusLabel(receipt.status)} · 影响 {receipt.affected_rows} 行
            {receipt.replayed ? ' · 重复请求已忽略' : ''}
            <Button type="button" variant="link" size="sm" className="ml-2 h-auto px-1" disabled={saving} onClick={() => void onRefreshReceipt()}>
              核查回执
            </Button>
          </p>
        )}

        <DialogFooter>
          <Button type="button" variant="outline" disabled={saving} onClick={onClose}>取消</Button>
          <Button
            type="button"
            variant={isDelete ? 'danger' : 'default'}
            loading={saving}
            disabled={editorLocked || receipt?.status === 'conflict'}
            onClick={submit}
          >
            {isDelete ? '确认删除' : '写入源表'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
