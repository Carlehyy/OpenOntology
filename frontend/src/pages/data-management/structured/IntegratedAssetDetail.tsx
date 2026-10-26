import { useCallback, useEffect, useRef, useState } from 'react'
import { Table } from 'antd'
import { Loader2, Pencil, Plus, RefreshCw, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { v4 as uuid } from 'uuid'
import {
  integratedDatasetsApi,
  type IntegratedAsset, type IntegratedRow, type IntegratedRows, type IntegratedSchema,
  type IntegratedSyncRun, type IntegratedVersion, type MutationReceipt,
} from '@/api/v2/integrated-datasets'
import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import {
  backendsLabel, dbxStateDetail, dbxStateLabel, integratedCell, integratedColumnLabel, integratedError,
  latestVersionStateLabel, parseObject, primaryKeyOf, receiptNeedsReconciliation, receiptStatusLabel,
  sourceMutationPayload, sourceReadDetail, sourceReadLabel, syncIsActive, syncIsSealed, syncStatusLabel,
  versionStateLabel,
} from './integratedModel'

type EditIntent = { mode: 'insert' | 'update' | 'delete'; row?: IntegratedRow; idempotencyKey: string }
type FailurePayload = {
  status?: number
  detail?: { code?: string; message?: string; run?: IntegratedSyncRun; mutation?: MutationReceipt }
  message?: string
}

const DEFAULT_SCHEMA: IntegratedSchema = { columns: [], primary_key: [], schema_fingerprint: '' }

export default function IntegratedAssetDetail({ asset, canManage, onChanged }: {
  asset: IntegratedAsset
  canManage: boolean
  onChanged: () => void
}) {
  const [schema, setSchema] = useState<IntegratedSchema>(DEFAULT_SCHEMA)
  const [versions, setVersions] = useState<IntegratedVersion[]>([])
  const [rows, setRows] = useState<IntegratedRows | null>(null)
  const [error, setError] = useState('')
  const [rowsError, setRowsError] = useState('')
  const [versionsError, setVersionsError] = useState('')
  const [loadingRows, setLoadingRows] = useState(false)
  const [loadingVersions, setLoadingVersions] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [loadingSync, setLoadingSync] = useState(true)
  const [sync, setSync] = useState<IntegratedSyncRun | null>(null)
  const [syncError, setSyncError] = useState('')
  const [syncRetry, setSyncRetry] = useState(0)
  const [intent, setIntent] = useState<EditIntent | null>(null)
  const [values, setValues] = useState('{}')
  const [saving, setSaving] = useState(false)
  const [attempted, setAttempted] = useState(false)
  const [mutationError, setMutationError] = useState('')
  const [receipt, setReceipt] = useState<MutationReceipt | null>(null)
  const [outcomeUnconfirmed, setOutcomeUnconfirmed] = useState(false)
  const [refreshingSchema, setRefreshingSchema] = useState(false)
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])

  const loadVersions = useCallback(async () => {
    setLoadingVersions(true)
    setVersionsError('')
    try {
      const response = await integratedDatasetsApi.versions(asset.id)
      if (alive.current) setVersions(response.items)
    } catch (failure) {
      if (alive.current) setVersionsError(integratedError(failure))
    } finally {
      if (alive.current) setLoadingVersions(false)
    }
  }, [asset.id])

  const applySealedRun = useCallback(async (run: IntegratedSyncRun) => {
    if (!syncIsSealed(run)) return
    await loadVersions()
    onChanged()
  }, [loadVersions, onChanged])

  const recoverSync = useCallback(async () => {
    setSyncError('')
    setLoadingSync(true)
    try {
      const response = await integratedDatasetsApi.syncRuns(asset.id)
      if (!alive.current) return
      const latest = response.items[0] ?? null
      setSync(latest)
      setSyncRetry(value => value + 1)
      if (latest) await applySealedRun(latest)
    } catch (failure) {
      if (alive.current) setSyncError(integratedError(failure))
    } finally {
      if (alive.current) setLoadingSync(false)
    }
  }, [asset.id, applySealedRun])

  useEffect(() => {
    let current = true
    integratedDatasetsApi.schema(asset.id)
      .then(response => { if (current) setSchema(response) })
      .catch(failure => { if (current) setError(`加载源结构失败：${integratedError(failure)}`) })
    void loadVersions()
    integratedDatasetsApi.syncRuns(asset.id)
      .then(response => { if (current) setSync(response.items[0] ?? null) })
      .catch(failure => { if (current) setSyncError(integratedError(failure)) })
      .finally(() => { if (current) setLoadingSync(false) })
    return () => { current = false }
  }, [asset.id, loadVersions])

  // Poll only server-persisted queued/starting/loading state. A failed run is
  // never retried automatically; the explicit recovery button only re-reads it.
  useEffect(() => {
    if (!sync || !syncIsActive(sync)) return
    const runId = sync.run_id
    let current = true
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const next = await integratedDatasetsApi.syncRun(asset.id, runId)
        if (!current) return
        setSync(next)
        setSyncError('')
        if (syncIsActive(next)) timer = setTimeout(poll, 1500)
        else await applySealedRun(next)
      } catch (failure) {
        if (current) setSyncError(`${integratedError(failure)}；同步状态暂不可核验，请重试状态核验，不要重复提交。`)
      }
    }
    timer = setTimeout(poll, 1000)
    return () => { current = false; clearTimeout(timer) }
  }, [asset.id, sync?.run_id, sync?.status, syncRetry, applySealedRun])

  const loadRows = async () => {
    setLoadingRows(true)
    setRowsError('')
    setRows(null)
    try {
      const response = await integratedDatasetsApi.rows(asset.id, 100)
      if (alive.current) setRows(response)
    } catch (failure) {
      if (alive.current) setRowsError(`源直读失败：${integratedError(failure)}`)
    } finally {
      if (alive.current) setLoadingRows(false)
    }
  }

  const refreshSchema = async () => {
    setRefreshingSchema(true)
    setError('')
    try {
      const refreshed = await integratedDatasetsApi.refreshSchema(asset.id)
      if (!alive.current) return
      setSchema({ columns: refreshed.columns, primary_key: refreshed.primary_key, schema_fingerprint: refreshed.schema_fingerprint })
      setRows(null)
      await loadVersions()
      onChanged()
    } catch (failure) {
      if (alive.current) setError(`刷新源结构失败：${integratedError(failure)}`)
    } finally {
      if (alive.current) setRefreshingSchema(false)
    }
  }

  const startSync = async () => {
    setSyncing(true)
    setError('')
    setSyncError('')
    try {
      const response = await integratedDatasetsApi.sync(asset.id)
      if (!alive.current) return
      setSync(response)
      await applySealedRun(response)
    } catch (failure) {
      if (!alive.current) return
      const payload = failure as FailurePayload
      setError(`同步请求失败：${integratedError(failure)}`)
      if (payload.detail?.run) setSync(payload.detail.run)
      else setSyncError('同步请求结果未知，请先核验任务状态，不要重复提交。')
    } finally {
      if (alive.current) setSyncing(false)
    }
  }

  const beginEdit = (mode: EditIntent['mode'], row?: IntegratedRow) => {
    if (outcomeUnconfirmed) return
    setIntent({ mode, row, idempotencyKey: uuid() })
    setValues(JSON.stringify(row?.values ?? {}, null, 2))
    setMutationError('')
    setReceipt(null)
    setAttempted(false)
    setOutcomeUnconfirmed(false)
  }

  const write = async () => {
    if (!intent || saving || outcomeUnconfirmed) return
    setSaving(true)
    setMutationError('')
    setReceipt(null)
    let submitted = false
    try {
      const primaryKey = schema.primary_key.length ? schema.primary_key : asset.primary_key
      const changes = intent.mode === 'delete' && intent.row
        ? { key: primaryKeyOf(primaryKey, intent.row.values) }
        : sourceMutationPayload(primaryKey, parseObject(values, '记录值'), intent.row?.values)
      const payload = {
        idempotency_key: intent.idempotencyKey,
        ...changes,
        ...(intent.row ? { expected_row_token: intent.row.row_token } : {}),
      }
      setAttempted(true)
      submitted = true
      const response = intent.mode === 'insert'
        ? await integratedDatasetsApi.insert(asset.id, payload)
        : intent.mode === 'update'
          ? await integratedDatasetsApi.update(asset.id, payload)
          : await integratedDatasetsApi.delete(asset.id, payload)
      if (!alive.current) return
      setReceipt(response)
      if (response.status === 'applied') {
        toast.success(`源表写入已应用（影响 ${response.affected_rows} 行）`)
        setIntent(null)
        await loadRows()
        onChanged()
        return
      }
      // conflict/failed/unknown/pending receipts are never success: keep the
      // modal open, keep the original idempotency key, and pause replay when
      // the source outcome cannot be confirmed.
      setOutcomeUnconfirmed(receiptNeedsReconciliation(response))
      setMutationError(`写入未确认成功：回执状态 ${receiptStatusLabel(response.status)}`)
    } catch (failure) {
      if (!alive.current) return
      const payload = failure as FailurePayload
      setMutationError(integratedError(failure))
      const mutation = payload.detail?.mutation
      if (mutation) setReceipt(mutation)
      const unconfirmed = submitted && (
        receiptNeedsReconciliation(mutation ?? null)
        || payload.detail?.code === 'UNKNOWN_REQUIRES_RECONCILIATION'
        || (!mutation && (!payload.status || payload.status >= 500))
      )
      setOutcomeUnconfirmed(unconfirmed)
    } finally {
      if (alive.current) setSaving(false)
    }
  }

  const refreshReceipt = async () => {
    if (!receipt || saving) return
    setSaving(true)
    try {
      const next = await integratedDatasetsApi.receipt(asset.id, receipt.id)
      if (!alive.current) return
      setReceipt(next)
      setOutcomeUnconfirmed(receiptNeedsReconciliation(next))
      if (next.status === 'applied') {
        setIntent(null)
        setMutationError('')
        await loadRows()
        onChanged()
      } else {
        setMutationError(`写入回执仍为 ${receiptStatusLabel(next.status)}；未确认成功，不能换新幂等键重放。`)
      }
    } catch (failure) {
      if (alive.current) setMutationError(integratedError(failure))
    } finally {
      if (alive.current) setSaving(false)
    }
  }

  const pk = schema.primary_key.length ? schema.primary_key : asset.primary_key
  const columns = schema.columns.length ? schema.columns : asset.columns
  const canWrite = canManage && asset.writable && asset.capabilities.write
  // Schema refresh is metadata, not a source write; the server still requires
  // owner/admin and reports ASSET_OWNER_REQUIRED for a non-owner editor.
  const canRefresh = canManage
  const canSync = canManage && asset.capabilities.sync
  const activeSync = syncIsActive(sync)

  return (
    <section data-testid="integrated-asset-detail" className="space-y-4 rounded-xl border border-border bg-card p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold">{asset.name}</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            {asset.schema_name}.{asset.table_name} · 连接 {asset.connection_id} · 资产 {asset.id}
          </p>
        </div>
        <Badge variant={canWrite ? 'success' : 'secondary'}>{canWrite ? '受控可写' : '只读'}</Badge>
      </div>

      <div className="grid gap-2 text-xs sm:grid-cols-2 lg:grid-cols-4">
        <div className="rounded-lg bg-muted p-3">
          连接状态：<strong>{sourceReadLabel(rows)}</strong>
          <p className="mt-1 text-muted-foreground">{sourceReadDetail(rows)}</p>
        </div>
        <div className="rounded-lg bg-muted p-3" data-testid="integrated-dbx-state">
          DBX 元数据：<strong>{dbxStateLabel(asset)}</strong>
          <p className="mt-1 break-all text-muted-foreground">{dbxStateDetail(asset)}</p>
          <p className="mt-1 text-muted-foreground">{backendsLabel(asset)}</p>
        </div>
        <div className="rounded-lg bg-muted p-3">
          数据版本：<strong>{latestVersionStateLabel(asset, versions)}</strong>
          <p className="mt-1 text-muted-foreground">只有完整封存版本才能用于精确离线查询与映射。</p>
        </div>
        <div className="rounded-lg bg-muted p-3">
          本体映射 / Sentinel：<strong>未核验</strong>
          <p className="mt-1 text-muted-foreground">本页没有映射、Trial/Promote 或 Sentinel 运行证据，不标记完成；请在对应本体版本中核验。</p>
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        <Button type="button" variant="outline" size="sm" onClick={loadRows} disabled={loadingRows || !asset.capabilities.read}>
          {loadingRows ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}加载源数据
        </Button>
        <Button
          type="button" variant="outline" size="sm"
          disabled={!canRefresh || refreshingSchema}
          title={!canRefresh ? '只有管理员和编辑者可刷新源结构' : '服务端仍要求资产 owner 或管理员'}
          onClick={refreshSchema}
        >
          {refreshingSchema && <Loader2 size={13} className="animate-spin" />}刷新源结构
        </Button>
        <Button
          type="button" variant="outline" size="sm"
          onClick={startSync}
          disabled={loadingSync || syncing || activeSync || !!syncError || !canSync || !asset.capabilities.sync}
        >
          {syncing && <Loader2 size={13} className="animate-spin" />}完整同步到版本
        </Button>
        <Button type="button" variant="outline" size="sm" onClick={loadVersions} disabled={loadingVersions}>
          {loadingVersions && <Loader2 size={13} className="animate-spin" />}刷新版本状态
        </Button>
        <Button
          type="button" variant="outline" size="sm"
          disabled={!canWrite || outcomeUnconfirmed || pk.length === 0}
          onClick={() => beginEdit('insert')}
        >
          <Plus size={13} />新增源记录
        </Button>
      </div>

      {!canWrite && (
        <p className="text-xs text-muted-foreground">
          写入未启用或当前角色无该资产写权限；只读查询仍可用。仅资产 owner 或管理员可写入，viewer 只读。
        </p>
      )}
      {error && <Alert variant="danger" role="alert">{error}</Alert>}
      {syncing && <p role="status" className="text-xs text-muted-foreground">正在同步，尚未确认完整版本。请勿重复提交。</p>}

      {sync && (
        <div role="status" data-testid="integrated-sync-state" className="space-y-1 rounded-lg bg-muted p-3 text-xs">
          <p>
            同步状态：{syncStatusLabel(sync.status)} · 任务 {sync.run_id} · 已读取 {sync.row_count} / {sync.expected_rows || '待源核验'}
          </p>
          <p className="text-muted-foreground">
            排队 {sync.created_at} · 最近进度 {sync.updated_at}{sync.completed_at ? ` · 结束 ${sync.completed_at}` : ''}
          </p>
          {sync.version_id && (
            <p>版本 {sync.version_id} · {syncIsSealed(sync) ? '完整封存已核验' : '完整性未确认'}</p>
          )}
          {sync.status === 'failed' && <p className="text-muted-foreground">失败任务已持久化；重试会产生新的 run 与快照，不续传旧任务。</p>}
          {sync.error && <p role="alert" className="text-destructive">{sync.error}</p>}
        </div>
      )}
      {syncError && (
        <Alert variant="danger" role="alert">
          {syncError}
          <Button type="button" variant="link" size="sm" className="ml-2 h-auto px-1" disabled={loadingSync} onClick={recoverSync}>重试同步状态</Button>
        </Alert>
      )}

      <details open>
        <summary className="cursor-pointer text-sm font-medium">源字段与主键</summary>
        <p className="my-2 break-all text-xs text-muted-foreground">结构指纹：{schema.schema_fingerprint || asset.schema_fingerprint}</p>
        <Table
          size="small"
          pagination={false}
          rowKey="name"
          dataSource={columns}
          scroll={{ x: 'max-content', y: 250 }}
          columns={[
            { title: '字段名', dataIndex: 'name' },
            // A configured field_config layer adds the logical display name and
            // the physical source key; unconfigured assets show only the name.
            ...(columns.some(column => column.source_key || column.display_name)
              ? [
                { title: '显示名', key: 'display_name', render: (_v: unknown, column: (typeof columns)[number]) => column.display_name || '—' },
                { title: '源字段', key: 'source_key', render: (_v: unknown, column: (typeof columns)[number]) => <span className="font-mono">{column.source_key ?? column.name}</span> },
              ]
              : []),
            { title: '类型', key: 'type', render: (_v: unknown, column: (typeof columns)[number]) => column.source_type && column.source_type !== column.type ? `${column.type}（源 ${column.source_type}）` : column.type },
            { title: '可为空', dataIndex: 'nullable', render: value => value ? '是' : '否' },
            { title: '主键', key: 'pk', render: (_value, column) => pk.includes(column.name) ? '主键' : '—' },
          ]}
        />
      </details>

      {rowsError && <Alert variant="danger" role="alert">{rowsError}</Alert>}
      {rows && (
        <div className="space-y-2" data-testid="integrated-source-rows">
          <p className="text-xs text-muted-foreground">
            源直读预览：最多 {rows.limit ?? 100} 行，不是总行数。读取时点 {rows.source_state.started_at} · 快照模式 {rows.source_state.kind}。
            全范围精确统计请用 SPL 查询。
          </p>
          <Table<IntegratedRow>
            size="small"
            pagination={false}
            rowKey={(_row, index) => String(index)}
            dataSource={rows.rows}
            scroll={{ x: 'max-content', y: 350 }}
            columns={[
              ...rows.columns.map(column => ({
                title: integratedColumnLabel(column),
                key: column.name,
                render: (_value: unknown, row: IntegratedRow) => <span className="font-mono text-xs">{integratedCell(row.values[column.name])}</span>,
              })),
              {
                title: '受控操作',
                key: 'actions',
                fixed: 'right' as const,
                render: (_value: unknown, row: IntegratedRow, index: number) => (
                  <div className="flex gap-2">
                    <Button
                      type="button" variant="outline" size="sm"
                      aria-label={`编辑源记录 ${index + 1}`}
                      disabled={!canWrite || outcomeUnconfirmed || pk.length === 0 || !row.row_token}
                      onClick={() => beginEdit('update', row)}
                    >
                      <Pencil size={12} />编辑
                    </Button>
                    <Button
                      type="button" variant="outline" size="sm"
                      aria-label={`删除源记录 ${index + 1}`}
                      disabled={!canWrite || outcomeUnconfirmed || pk.length === 0 || !row.row_token}
                      onClick={() => beginEdit('delete', row)}
                    >
                      <Trash2 size={12} />删除
                    </Button>
                  </div>
                ),
              },
            ]}
          />
        </div>
      )}

      <div>
        <h3 className="text-sm font-medium">同步版本</h3>
        {versionsError && <Alert variant="danger" role="alert" className="mt-2">{versionsError}</Alert>}
        {!versionsError && versions.length === 0 && <p className="mt-2 text-xs text-muted-foreground">尚无已核验的完整版本</p>}
        {versions.length > 0 && (
          <ul className="mt-2 space-y-1 text-xs text-muted-foreground">
            {versions.map(version => (
              <li key={version.id}>
                {version.id} · {versionStateLabel(version)} · 行数 {version.row_count}
                {version.run_id ? ` · run ${version.run_id}` : ''}
                {version.checksum ? ` · checksum ${version.checksum}` : ''}
              </li>
            ))}
          </ul>
        )}
      </div>

      {outcomeUnconfirmed && (
        <Alert variant="warning" role="alert">
          写入结果未知，已暂停新的源表写入，不能换新幂等键重放。请核查回执，或请管理员按幂等键 {intent?.idempotencyKey ?? receipt?.id ?? '（见回执）'} 与源记录核对。
        </Alert>
      )}
      {receipt && !intent && (
        <p className="text-xs text-muted-foreground">
          最近写入回执 {receipt.id} · {receiptStatusLabel(receipt.status)} · 影响 {receipt.affected_rows} 行
          {receipt.replayed ? ' · 幂等重放' : ''}
          {receipt.error_code ? ` · ${receipt.error_code}` : ''}
          <Button type="button" variant="link" size="sm" className="ml-2 h-auto px-1" disabled={saving} onClick={refreshReceipt}>核查回执</Button>
        </p>
      )}

      <Dialog open={!!intent} onOpenChange={open => { if (!open && !saving) setIntent(null) }}>
        <DialogContent dismissible={!saving} className="max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{intent?.mode === 'delete' ? '删除源记录' : intent?.mode === 'insert' ? '新增源记录' : '编辑源记录'}</DialogTitle>
            <DialogDescription>
              该操作直接写入外部源表，不是修改湖版本。更新和删除以原始主键及行令牌进行乐观并发检查；大整数、小数请使用 JSON 字符串。
            </DialogDescription>
          </DialogHeader>
          {intent?.mode === 'delete' ? (
            <pre className="max-h-64 overflow-auto rounded-lg bg-muted p-3 text-xs">{JSON.stringify(intent.row?.values, null, 2)}</pre>
          ) : (
            <label className="block space-y-1 text-xs font-medium">
              源记录值（JSON）
              <textarea
                aria-label="源记录值 JSON"
                disabled={saving || attempted}
                value={values}
                onChange={event => setValues(event.target.value)}
                rows={10}
                className="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </label>
          )}
          <p className="mt-2 text-xs text-muted-foreground">
            主键变更由服务端拒绝；更新/删除必须携带最近一次源读取得到的行令牌。发生冲突请取消并重新加载源数据，再基于最新记录编辑。
          </p>
          {mutationError && (
            <Alert variant="danger" role="alert" className="mt-3">
              {mutationError}
              <p className="mt-1 text-xs">
                冲突或未确认结果都不是成功；结果未知时不要重复提交或换新幂等键，请先核查回执。
              </p>
            </Alert>
          )}
          <p className="mt-2 break-all font-mono text-xs text-muted-foreground">幂等键：{intent?.idempotencyKey}</p>
          {receipt && (
            <p className="mt-2 text-xs text-muted-foreground">
              写入回执 {receipt.id} · {receiptStatusLabel(receipt.status)} · 影响 {receipt.affected_rows} 行
              {receipt.replayed ? ' · 幂等重放' : ''}
              {receipt.error_code ? ` · ${receipt.error_code}` : ''}
              <Button type="button" variant="link" size="sm" className="ml-2 h-auto px-1" disabled={saving} onClick={refreshReceipt}>核查回执</Button>
            </p>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" disabled={saving} onClick={() => setIntent(null)}>取消</Button>
            <Button
              type="button"
              variant={intent?.mode === 'delete' ? 'danger' : 'default'}
              onClick={write}
              disabled={saving || outcomeUnconfirmed}
            >
              {saving && <Loader2 size={14} className="animate-spin" />}确认写入源表
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  )
}
