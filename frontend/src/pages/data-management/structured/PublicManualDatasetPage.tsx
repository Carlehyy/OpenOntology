import { formatDateTime } from '@/utils/datetime'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { PageSizeSelect } from '@/components/PageSizeSelect'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { AlertCircle, CheckCircle2, ChevronLeft, ChevronRight, Clock3, Database, ExternalLink, Eye, KeyRound, Loader2, Plus, RefreshCw, Save, Trash2, Undo2, XCircle } from 'lucide-react'
import { useParams } from 'react-router-dom'
import { publicManualSharingApi, type PublicManualDataset } from '@/api/public-manual-sharing'

const PAGE_SIZES = [20, 50, 100, 200, 500] as const
type CellMap = Record<string, string>
type EditRow = { orig: CellMap; cur: CellMap; deleted: boolean }

const str = (value: unknown) => value == null ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value)
const fmt = (iso?: string | null) => formatDateTime(iso, { fallback: '—' })

function validateValue(column: string, type: string, value: string): string | null {
  if (value === '') return null
  if (type === 'integer' && !/^[+-]?\d+$/.test(value)) return `「${column}」必须是整数`
  if (type === 'float' && !Number.isFinite(Number(value))) return `「${column}」必须是数字`
  if (type === 'boolean' && !['true', 'false', '1', '0', '是', '否'].includes(value.toLowerCase())) return `「${column}」必须是布尔值（true/false、1/0 或 是/否）`
  if (type === 'timestamp' && Number.isNaN(Date.parse(value))) return `「${column}」必须是合法日期时间`
  if (type === 'json') { try { JSON.parse(value) } catch { return `「${column}」必须是合法 JSON` } }
  return null
}

export default function PublicManualDatasetPage() {
  const { token = '' } = useParams()
  const [data, setData] = useState<PublicManualDataset | null>(null)
  const [rows, setRows] = useState<EditRow[]>([])
  const [inserts, setInserts] = useState<CellMap[]>([])
  const [offset, setOffset] = useState(0)
  const [pageSize, setPageSize] = useState<number>(50)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [pendingConfirm, setPendingConfirm] = useState<null | { title: string; description: string; action: () => void }>(null)

  const columns = data?.dataset.columns || []
  const pkCols = useMemo(() => (data?.dataset.primary_key || '').split(',').map(v => v.trim()).filter(Boolean), [data?.dataset.primary_key])
  const canEdit = data?.share.permission === 'edit'
  const pending = canEdit && (data?.changes.some(item => item.status === 'pending') || false)
  const editable = canEdit && !pending
  const dirty = inserts.length > 0 || rows.some(row => row.deleted || columns.some(col => row.cur[col] !== row.orig[col]))
  const columnLabel = (column: string) => {
    const displayName = data?.dataset.column_meta?.[column]?.display_name?.trim()
    return displayName && displayName !== column ? `${displayName}（${column}）` : column
  }
  const isNonNull = (column: string) => pkCols.includes(column) || data?.dataset.column_meta?.[column]?.nullable === false

  const hydrate = useCallback((result: PublicManualDataset, off: number) => {
    const cols = result.dataset.columns
    setRows(result.dataset.rows.map(raw => {
      const map = Object.fromEntries(cols.map(col => [col, str(raw[col])])) as CellMap
      return { orig: { ...map }, cur: { ...map }, deleted: false }
    }))
    setInserts([]); setData(result); setOffset(off)
  }, [])

  const load = useCallback(async (off: number) => {
    setLoading(true); setError('')
    try { hydrate(await publicManualSharingApi.get(token, off, pageSize), off) }
    catch (e) { setError(e instanceof Error ? e.message : '分享数据加载失败') }
    finally { setLoading(false) }
  }, [hydrate, pageSize, token])
  useEffect(() => { void Promise.resolve().then(() => load(0)) }, [load])

  const switchPage = (next: number) => {
    if (dirty) { setError('当前页有未提交修改，请先保存或刷新放弃后再翻页'); return }
    void load(Math.max(0, next))
  }

  const handleRefreshClick = () => {
    if (!dirty) { void load(offset); return }
    setPendingConfirm({
      title: '刷新并放弃修改',
      description: '放弃当前未提交的修改并刷新？',
      action: () => { void load(offset) },
    })
  }

  const handlePageSizeChange = (next: number) => {
    if (dirty) {
      setPendingConfirm({
        title: '切换分页大小并放弃修改',
        description: '切换分页大小将放弃当前未提交的修改，是否继续？',
        action: () => { setPageSize(next) },
      })
      return
    }
    setPageSize(next)
  }

  const validate = (): string | null => {
    const activeRows = [...rows.filter(row => !row.deleted).map(row => row.cur), ...inserts]
    for (const row of activeRows) {
      for (const col of columns) {
        if (isNonNull(col) && !String(row[col] || '').trim()) return `「${columnLabel(col)}」不能为空`
        const issue = validateValue(columnLabel(col), data?.dataset.column_types[col] || 'string', row[col] || '')
        if (issue) return issue
      }
    }
    if (pkCols.length) {
      const keys = activeRows.map(row => pkCols.map(col => String(row[col] || '').trim()).join('\u001f'))
      if (new Set(keys).size !== keys.length) return `当前页存在重复主键（${pkCols.join(' + ')}）`
    }
    return null
  }

  const save = async () => {
    if (!data) return
    const issue = validate()
    if (issue) { setError(issue); return }
    const keyOf = (row: CellMap) => Object.fromEntries(pkCols.map(col => [col, row[col] || '']))
    const updates = rows.filter(row => !row.deleted && columns.some(col => row.cur[col] !== row.orig[col])).map(row => ({
      key: keyOf(row.orig), values: Object.fromEntries(columns.filter(col => row.cur[col] !== row.orig[col]).map(col => [col, row.cur[col]])),
    }))
    const deletes = rows.filter(row => row.deleted).map(row => ({ key: keyOf(row.orig) }))
    const insertOps = inserts.filter(row => Object.values(row).some(Boolean)).map(values => ({ values }))
    if (!updates.length && !deletes.length && !insertOps.length) { setError('没有需要提交的修改'); return }
    setSaving(true); setError(''); setSuccess('')
    try {
      await publicManualSharingApi.submit(token, { base_version_no: data.dataset.version_no, updates, deletes, inserts: insertOps })
      setSuccess('修改已通过数据合法性校验并提交审批。平台批准后才会正式生效。')
      await load(offset)
    } catch (e) { setError(e instanceof Error ? e.message : '提交失败') }
    finally { setSaving(false) }
  }

  if (loading && !data) return <div className="grid min-h-screen place-items-center bg-muted text-sm text-muted-foreground"><span className="flex items-center gap-2"><Loader2 size={16} className="animate-spin" />正在打开分享数据...</span></div>
  if (!data) return <div className="grid min-h-screen place-items-center bg-muted p-6"><div className="max-w-md rounded-2xl border bg-card p-8 text-center shadow-sm"><XCircle className="mx-auto mb-3 text-[var(--color-danger)]" /><h1 className="font-semibold">无法访问该分享</h1><p className="mt-2 text-sm text-muted-foreground">{error || '链接不存在或已失效'}</p><a href={`${window.location.pathname}#/overview`} className="mt-5 inline-flex items-center gap-1 text-sm text-[var(--color-success)]">访问平台主页 <ExternalLink size={13} /></a></div></div>

  const end = Math.min(offset + rows.length, data.dataset.total_rows)
  return <div className="min-h-screen bg-muted text-foreground">
    <header className="border-b bg-card"><div className="mx-auto flex max-w-[1500px] items-center gap-3 px-5 py-3"><span className="grid h-9 w-9 place-items-center rounded-xl bg-[var(--color-success)] text-[var(--color-text-inverse)]"><Database size={17} /></span><div className="min-w-0 flex-1"><h1 className="truncate text-sm font-semibold">{data.dataset.name}</h1><p className="text-xs text-[var(--color-text-tertiary)]">人工数据集在线{data.share.permission === 'edit' ? '维护' : '查看'} · v{data.dataset.version_no} · {data.dataset.total_rows} 行</p></div><span className={`inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs ${data.share.permission === 'edit' ? 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]' : 'bg-[var(--color-info-bg)] text-[var(--color-info)]'}`}>{data.share.permission === 'edit' ? <Save size={12} /> : <Eye size={12} />}{data.share.permission === 'edit' ? '可编辑，审批后生效' : '仅查看'}</span><a href={`${window.location.pathname}#/overview`} className="ml-2 inline-flex items-center gap-1 text-xs text-[var(--color-success)] hover:underline">访问平台主页 <ExternalLink size={12} /></a></div></header>

    <main className="mx-auto max-w-[1500px] space-y-4 p-5">
      {data.share.label && <p className="rounded-xl border border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-[var(--color-success-bg)] px-4 py-2.5 text-xs text-[var(--color-success)]">分享说明：{data.share.label}</p>}
      {canEdit && pending && <div className="flex items-center gap-2 rounded-xl border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-4 py-3 text-sm text-[var(--color-warning)]"><Clock3 size={15} />本次修改正在审批中，审批完成前暂不可继续编辑。可在下方查看进度与意见。</div>}
      {error && <div className="flex items-center gap-2 rounded-xl border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-4 py-3 text-sm text-[var(--color-danger)]"><AlertCircle size={15} /><span className="flex-1">{error}</span><button onClick={() => setError('')}><XCircle size={14} /></button></div>}
      {success && <div className="flex items-center gap-2 rounded-xl border border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] bg-[var(--color-success-bg)] px-4 py-3 text-sm text-[var(--color-success)]"><CheckCircle2 size={15} />{success}</div>}

      <section className="overflow-hidden rounded-2xl border bg-card shadow-sm">
        <div className="flex items-center gap-3 border-b px-4 py-3"><div className="flex-1 text-xs text-muted-foreground">{pkCols.length ? <span className="inline-flex items-center gap-1 rounded-md border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] bg-[var(--color-warning-bg)] px-2 py-1 text-[var(--color-warning)]"><KeyRound size={10} />主键：{pkCols.join(' + ')}</span> : '未声明主键：仅支持新增行'}</div><button onClick={handleRefreshClick} className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"><RefreshCw size={12} />刷新</button></div>
        <div className="overflow-auto p-4">
          <table className="w-max min-w-full border-separate border-spacing-0 text-xs"><thead className="sticky top-0"><tr><th className="border-b border-r bg-muted px-2 py-2 text-center font-normal text-[var(--color-text-tertiary)]">#</th>{columns.map(col => <th key={col} className="border-b bg-muted px-3 py-2 text-center font-medium text-muted-foreground"><div className="flex items-center justify-center gap-1.5 whitespace-nowrap"><span>{columnLabel(col)}</span>{pkCols.includes(col) && <span className="inline-flex items-center gap-0.5 rounded bg-[var(--color-warning-bg)] px-1.5 py-0.5 text-[10px] font-normal text-[var(--color-warning)]"><KeyRound size={8} />主键</span>}{isNonNull(col) && <span className="rounded bg-viz-rose-soft px-1.5 py-0.5 text-[10px] font-normal text-viz-rose">非空</span>}{data.dataset.column_types[col] && data.dataset.column_types[col] !== 'string' && <small className="font-normal text-[var(--color-text-tertiary)]">{data.dataset.column_types[col]}</small>}</div></th>)}{canEdit && <th className="border-b bg-muted" />}</tr></thead>
            <tbody>{rows.map((row, index) => <tr key={index} className={row.deleted ? 'opacity-40' : ''}><td className="border-b border-r px-2 text-center text-[var(--color-text-tertiary)]">{offset + index + 1}</td>{columns.map(col => <td key={col} className="border-b p-0 text-center">{canEdit ? <input value={row.cur[col]} disabled={!editable || row.deleted || !pkCols.length} onChange={e => setRows(list => list.map((item, i) => i === index ? { ...item, cur: { ...item.cur, [col]: e.target.value } } : item))} className={`w-full min-w-[8rem] bg-transparent px-3 py-2 text-center outline-none focus:bg-[var(--color-success-bg)] disabled:cursor-not-allowed ${row.cur[col] !== row.orig[col] ? 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]' : ''}`} /> : <div className="w-full min-w-[8rem] px-3 py-2 text-center">{row.cur[col] || '—'}</div>}</td>)}{canEdit && <td className="border-b px-2">{editable && pkCols.length > 0 && <button onClick={() => setRows(list => list.map((item, i) => i === index ? { ...item, deleted: !item.deleted } : item))} className="text-[var(--color-text-tertiary)] hover:text-[var(--color-danger)]">{row.deleted ? <Undo2 size={13} /> : <Trash2 size={13} />}</button>}</td>}</tr>)}
              {canEdit && inserts.map((row, index) => <tr key={`new-${index}`} className="bg-[var(--color-success-bg)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"><td className="border-b border-r px-2 text-center text-[var(--color-success)]">新</td>{columns.map(col => <td key={col} className="border-b p-0 text-center"><input value={row[col]} placeholder={isNonNull(col) ? '必填' : ''} onChange={e => setInserts(list => list.map((item, i) => i === index ? { ...item, [col]: e.target.value } : item))} className="w-full min-w-[8rem] bg-transparent px-3 py-2 text-center outline-none focus:bg-[var(--color-success-bg)]" /></td>)}<td className="border-b px-2"><button onClick={() => setInserts(list => list.filter((_, i) => i !== index))} className="text-[var(--color-text-tertiary)] hover:text-[var(--color-danger)]"><Trash2 size={13} /></button></td></tr>)}</tbody>
          </table>
        </div>
        <div className="flex flex-wrap items-center gap-3 border-t bg-muted px-4 py-3">{canEdit && <button onClick={() => setInserts(list => [...list, Object.fromEntries(columns.map(col => [col, '']))])} disabled={!editable} className="inline-flex items-center gap-1 rounded-lg border bg-card px-3 py-1.5 text-xs text-muted-foreground disabled:opacity-40"><Plus size={12} />新增行</button>}<div className="flex items-center gap-1 text-xs text-muted-foreground"><button aria-label="上一页" onClick={() => switchPage(offset - pageSize)} disabled={offset === 0} className="rounded p-1 hover:bg-card disabled:opacity-30"><ChevronLeft size={14} /></button><span>{data.dataset.total_rows === 0 ? '0' : `${offset + 1}–${end}`} / {data.dataset.total_rows} 行</span><button aria-label="下一页" onClick={() => switchPage(offset + pageSize)} disabled={end >= data.dataset.total_rows} className="rounded p-1 hover:bg-card disabled:opacity-30"><ChevronRight size={14} /></button></div><label className="flex items-center gap-1.5 text-xs text-muted-foreground">每页<PageSizeSelect value={pageSize} onChange={handlePageSizeChange} sizes={PAGE_SIZES} ariaLabel="公开数据集每页显示条数" className="h-7 w-16 rounded-md bg-card px-2 text-xs" />行</label>{canEdit && dirty && <span className="text-xs text-[var(--color-warning)]">有未提交修改</span>}{canEdit && <button onClick={save} disabled={!editable || !dirty || saving} className="ml-auto inline-flex items-center gap-1.5 rounded-lg bg-[var(--color-success)] px-4 py-2 text-xs font-medium text-[var(--color-text-inverse)] hover:bg-[var(--color-success)] disabled:opacity-40">{saving ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />}保存并提交审批</button>}</div>
      </section>

      {canEdit && <section className="rounded-2xl border bg-card p-4 shadow-sm"><h2 className="mb-3 text-sm font-semibold">审批进度</h2>{data.changes.length === 0 ? <p className="text-xs text-[var(--color-text-tertiary)]">尚未提交过修改</p> : <div className="space-y-2">{data.changes.map(change => <div key={change.id} className="flex items-start gap-3 rounded-xl border px-3 py-3"><span className={`grid h-8 w-8 place-items-center rounded-lg ${change.status === 'pending' ? 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]' : change.status === 'approved' ? 'bg-[var(--color-success-bg)] text-[var(--color-success)]' : 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]'}`}>{change.status === 'pending' ? <Clock3 size={14} /> : change.status === 'approved' ? <CheckCircle2 size={14} /> : <XCircle size={14} />}</span><div><p className="text-xs font-medium">{change.status === 'pending' ? '等待平台审批' : change.status === 'approved' ? `已批准${change.applied_version_no ? `，生效为 v${change.applied_version_no}` : ''}` : '已驳回'}</p><p className="mt-1 text-[11px] text-[var(--color-text-tertiary)]">{fmt(change.submitted_at)} · 修改 {change.summary.updated || 0} / 新增 {change.summary.inserted || 0} / 删除 {change.summary.deleted || 0}</p>{change.review_comment && <p className="mt-1.5 text-xs text-muted-foreground">审批意见：{change.review_comment}</p>}</div></div>)}</div>}</section>}
    </main>

    <ConfirmDialog
      open={pendingConfirm !== null}
      onClose={() => setPendingConfirm(null)}
      onConfirm={() => { const c = pendingConfirm; setPendingConfirm(null); c?.action() }}
      title={pendingConfirm?.title ?? ''}
      description={pendingConfirm?.description}
      variant="warning"
    />
  </div>
}
