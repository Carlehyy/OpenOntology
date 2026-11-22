import { Fragment, useCallback, useEffect, useRef, useState } from 'react'
import { ConfigProvider, Table } from 'antd'
import type { ColumnType, ColumnsType } from 'antd/es/table'
import {
  Archive, ChevronDown, CircleStop, Database, Loader2, Pencil, PlugZap, Plus, RefreshCcw, RefreshCw, RotateCw,
  Trash2, Unlink,
} from 'lucide-react'
import { toast } from 'sonner'
import { v4 as uuid } from 'uuid'
import {
  integratedDatasetsApi, isIntegratedCdcStatus,
  type IntegratedAsset, type IntegratedColumn, type IntegratedReplica,
  type IntegratedReplicaPreflightResult, type IntegratedRow, type IntegratedRows,
  type IntegratedSchema, type IntegratedSyncRun, type IntegratedVersion, type MutationReceipt,
} from '@/api/v2/integrated-datasets'
import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { EmptyState } from '@/components/ui/LoadingState'
import { TooltipProvider } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import IntegratedRecordEditor, { type RecordIntent } from './IntegratedRecordEditor'
import {
  cellDisplayText, cdcCaptureView, dataAvailabilityView, dataUnavailableReason, formatTimestamp,
  metadataServiceView, previewFootnote, replicaErrorCodeHint, shortId, syncFacts, toReplica,
  type CdcPollView,
} from './integratedDetailModel'
import {
  REPLICA_PENDING_SYNC_NOTE, dbxState, integratedColumnLabel, integratedError, isReplicaStatusPayload,
  parseObject, primaryKeyOf, receiptNeedsReconciliation, receiptStatusLabel, replicaCoversWrite,
  replicaNotRegistered, replicaReadable, replicaSyncBaseline, sourceMutationPayload, syncIsActive, syncIsSealed,
  syncStatusLabel, versionIsVerified,
} from './integratedModel'
import {
  Disclosure, FooterAction, Hint, InfoRow, SectionTitle, StatusItem, TABLE_THEME, TEMPORAL_TYPES, refocus,
} from './integratedDetailUi'

type FailurePayload = {
  status?: number
  detail?: { code?: string; message?: string; run?: IntegratedSyncRun; mutation?: MutationReceipt }
  message?: string
}

const DEFAULT_SCHEMA: IntegratedSchema = { columns: [], primary_key: [], schema_fingerprint: '' }

/** Automatic CDC status refresh cadence while the detail dialog is mounted. */
const CDC_POLL_INTERVAL_MS = 5000

/** CDC status is a read-only server fact; failed or malformed fetches never degrade to the last healthy state. */
type CdcPollState = CdcPollView

export default function IntegratedAssetDetail({ asset, canManage, onChanged, onDecommissioned, onClose }: {
  asset: IntegratedAsset
  canManage: boolean
  onChanged: () => void
  onDecommissioned?: () => void
  /** Closes the dialog from the footer; Esc and the close button use the same route. */
  onClose: () => void
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
  const [intent, setIntent] = useState<RecordIntent | null>(null)
  // Bumped per editing session so the editor's form draft never leaks across rows.
  const [editorSession, setEditorSession] = useState(0)
  const [values, setValues] = useState('{}')
  const [saving, setSaving] = useState(false)
  const [attempted, setAttempted] = useState(false)
  const [mutationError, setMutationError] = useState('')
  const [receipt, setReceipt] = useState<MutationReceipt | null>(null)
  const [outcomeUnconfirmed, setOutcomeUnconfirmed] = useState(false)
  const [refreshingSchema, setRefreshingSchema] = useState(false)
  const [connectionName, setConnectionName] = useState('')
  // 受控注销：确认弹窗 + 结构化 409 明细（历史/映射/流水线/任务）
  const [decommissioning, setDecommissioning] = useState(false)
  const [decommissionOpen, setDecommissionOpen] = useState(false)
  const [decommissionError, setDecommissionError] = useState('')
  // 版本与同步默认收起；进行中的同步运行必须可见，因此 activeSync 时自动展开——
  // 用户仍可手动收起（不强行抢回）。
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [fieldsOpen, setFieldsOpen] = useState(true)
  // 本地副本控制面：状态轮询 + 重新同步/预检/暂停。状态是服务端事实，失败或
  // 形态不符一律显示不可核验，绝不沿用上次健康态。
  const [replica, setReplica] = useState<IntegratedReplica | null>(null)
  const [replicaUnverified, setReplicaUnverified] = useState(false)
  const [rebuilding, setRebuilding] = useState(false)
  const [rebuildOpen, setRebuildOpen] = useState(false)
  const [rebuildError, setRebuildError] = useState('')
  const [preflighting, setPreflighting] = useState(false)
  const [preflight, setPreflight] = useState<IntegratedReplicaPreflightResult | null>(null)
  const [stopping, setStopping] = useState(false)
  const [stopOpen, setStopOpen] = useState(false)
  const [sourceWritePendingSync, setSourceWritePendingSync] = useState(false)
  // 写入成功后的副本追平基线：轮询看到覆盖确认严格越过基线才撤下"等待同步"提示，
  // 不能用固定超时假装副本已包含该写入。
  const pendingSync = useRef(false)
  const pendingSyncBaseline = useRef<string | null>(null)
  const [cdcPoll, setCdcPoll] = useState<CdcPollState>({ phase: 'checking' })
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])

  // Data is loaded once per dialog once reads are admitted; rows are re-read on demand.
  const autoLoaded = useRef(false)
  const rowsAbort = useRef<AbortController | null>(null)
  useEffect(() => () => rowsAbort.current?.abort(), [])

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
      .catch(failure => { if (current) setError(`加载表结构失败：${integratedError(failure)}`) })
    void loadVersions()
    integratedDatasetsApi.syncRuns(asset.id)
      .then(response => { if (current) setSync(response.items[0] ?? null) })
      .catch(failure => { if (current) setSyncError(integratedError(failure)) })
      .finally(() => { if (current) setLoadingSync(false) })
    // The header shows the connection's name; a failure here only costs the label.
    integratedDatasetsApi.connections()
      .then(list => { if (current) setConnectionName(list.find(item => item.id === asset.connection_id)?.name ?? '') })
      .catch(() => { if (current) setConnectionName('') })
    return () => { current = false }
  }, [asset.id, asset.connection_id, loadVersions])

  // 自动变更捕获状态：仅在本详情挂载期间轮询（无手动开关）；订阅由已发布
  // 远程映射驱动。失败或形态不符的响应一律显示不可核验，绝不沿用上次健康态。
  // 切换资产或卸载时中止在途请求并复位为核验中，避免把上一资产的状态带入新资产。
  useEffect(() => {
    setCdcPoll({ phase: 'checking' })
    let current = true
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const payload = await integratedDatasetsApi.cdc(asset.id, controller.signal)
        // 载荷还必须属于当前资产：asset_id 不匹配视为不可核验，避免串显其他资产的状态。
        if (current) setCdcPoll(isIntegratedCdcStatus(payload) && payload.asset_id === asset.id
          ? { phase: 'ok', status: payload }
          : { phase: 'unavailable' })
      } catch {
        if (current) setCdcPoll({ phase: 'unavailable' })
      } finally {
        if (current) timer = setTimeout(poll, CDC_POLL_INTERVAL_MS)
      }
    }
    void poll()
    return () => { current = false; controller.abort(); clearTimeout(timer) }
  }, [asset.id])

  useEffect(() => {
    setReplica(asset.replica ?? null)
    setReplicaUnverified(false)
    pendingSync.current = false
    pendingSyncBaseline.current = null
    setSourceWritePendingSync(false)
    let current = true
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const payload = await integratedDatasetsApi.replica(asset.id, controller.signal)
        if (!current) return
        if (!isReplicaStatusPayload(payload, asset.id)) {
          setReplicaUnverified(true)
          return
        }
        setReplica(toReplica(payload))
        setReplicaUnverified(false)
        // 只有服务端确认的覆盖 cut 严格越过写入基线，才证明本次写已进入副本。
        // payload.applied_at 前进可能只是其他/更早 TX，不能作为撤提示依据。
        if (pendingSync.current
            && replicaCoversWrite(payload.coverage_confirmed_at, pendingSyncBaseline.current)) {
          pendingSync.current = false
          setSourceWritePendingSync(false)
        }
      } catch (failure) {
        if (!current) return
        if (replicaNotRegistered(failure)) {
          // 历史资产可能没有副本行：这是明确的"未初始化"，不是状态查询故障。
          setReplica(null)
          setReplicaUnverified(false)
          return
        }
        setReplicaUnverified(true)
      } finally {
        if (current) timer = setTimeout(poll, 3000)
      }
    }
    void poll()
    return () => { current = false; controller.abort(); clearTimeout(timer) }
  }, [asset.id])

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

  const replicaCanRead = !replicaUnverified && replicaReadable(replica)
  // Only a server statement that reads are rejected drops rows already on
  // screen; a failed status poll leaves them and only marks the strip unknown.
  const replicaDenied = !!replica && !replicaUnverified && replica.complete !== true

  const loadRows = useCallback(async () => {
    rowsAbort.current?.abort()
    const controller = new AbortController()
    rowsAbort.current = controller
    setLoadingRows(true)
    setRowsError('')
    try {
      const response = await integratedDatasetsApi.rows(asset.id, 100, controller.signal)
      if (alive.current) setRows(response)
    } catch (failure) {
      if (alive.current && !controller.signal.aborted) {
        setRows(null)
        setRowsError(`数据读取失败：${integratedError(failure)}`)
      }
    } finally {
      if (alive.current) setLoadingRows(false)
    }
  }, [asset.id])

  // Rows already on screen must not outlive the reads that made them valid.
  useEffect(() => {
    if (!replicaDenied) return
    rowsAbort.current?.abort()
    autoLoaded.current = false
    setRows(null)
    setRowsError('')
  }, [replicaDenied])

  useEffect(() => {
    if (autoLoaded.current || !asset.capabilities.read || !replicaCanRead) return
    autoLoaded.current = true
    void loadRows()
  }, [asset.capabilities.read, replicaCanRead, replica, loadRows])

  const rebuildReplica = async () => {
    setRebuilding(true)
    setRebuildError('')
    try {
      await integratedDatasetsApi.rebuildReplica(asset.id)
      if (!alive.current) return
      setRebuildOpen(false)
      toast.success('已开始重新同步；完成后可在这里查询')
      const payload = await integratedDatasetsApi.replica(asset.id)
      if (alive.current && isReplicaStatusPayload(payload, asset.id)) setReplica(toReplica(payload))
      onChanged()
    } catch (failure) {
      if (alive.current) setRebuildError(integratedError(failure))
    } finally {
      if (alive.current) setRebuilding(false)
    }
  }

  const stopReplica = async () => {
    setStopping(true)
    setError('')
    try {
      await integratedDatasetsApi.stopReplica(asset.id)
      if (!alive.current) return
      setStopOpen(false)
      toast.success('已暂停同步；重新同步后可恢复查询')
      const payload = await integratedDatasetsApi.replica(asset.id)
      if (alive.current && isReplicaStatusPayload(payload, asset.id)) setReplica(toReplica(payload))
      onChanged()
    } catch (failure) {
      if (alive.current) setError(`暂停同步失败：${integratedError(failure)}`)
    } finally {
      if (alive.current) setStopping(false)
    }
  }

  const runPreflight = async () => {
    setPreflighting(true)
    setPreflight(null)
    try {
      const result = await integratedDatasetsApi.replicaPreflight(asset.id)
      if (alive.current) setPreflight(result)
    } catch (failure) {
      if (alive.current) setPreflight({ ok: false, code: integratedError(failure) })
    } finally {
      if (alive.current) setPreflighting(false)
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
      autoLoaded.current = false
      await loadVersions()
      onChanged()
    } catch (failure) {
      if (alive.current) setError(`同步表结构失败：${integratedError(failure)}`)
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
      setError(`创建离线快照失败：${integratedError(failure)}`)
      if (payload.detail?.run) setSync(payload.detail.run)
      else setSyncError('请求结果未知，请先核验任务状态，不要重复提交。')
    } finally {
      if (alive.current) setSyncing(false)
    }
  }

  const beginEdit = (mode: RecordIntent['mode'], row?: IntegratedRow, trigger?: HTMLElement) => {
    if (outcomeUnconfirmed) return
    editorTrigger.current = trigger ?? null
    setIntent({ mode, row, idempotencyKey: uuid() })
    setValues(JSON.stringify(row?.values ?? {}, null, 2))
    setMutationError('')
    setReceipt(null)
    setAttempted(false)
    setOutcomeUnconfirmed(false)
    setEditorSession(value => value + 1)
  }

  const retryFailed = () => {
    if (!intent || saving || receipt?.status !== 'failed') return
    setIntent({ ...intent, idempotencyKey: uuid() })
    setReceipt(null)
    setMutationError('')
    setAttempted(false)
    setOutcomeUnconfirmed(false)
  }

  const reloadAfterConflict = async () => {
    if (saving) return
    setIntent(null)
    setReceipt(null)
    setMutationError('')
    setAttempted(false)
    setOutcomeUnconfirmed(false)
    if (replicaCanRead) await loadRows()
  }

  // A settled conflict key cannot be replayed, so closing the editor after a
  // conflict must take the cancel-and-reload path; otherwise the stale row
  // token stays and every write button remains locked with no way out.
  const closeEditor = () => {
    if (receipt?.status === 'conflict') { void reloadAfterConflict(); return }
    if (!saving) setIntent(null)
  }

  /** A settled failure may be resent, but only under a fresh idempotency key. */
  const writeAfterFailure = (valuesText?: string) => {
    if (!intent || saving || receipt?.status !== 'failed' || outcomeUnconfirmed) return
    const fresh = { ...intent, idempotencyKey: uuid() }
    setIntent(fresh)
    setReceipt(null)
    setMutationError('')
    setAttempted(false)
    write(valuesText, fresh)
  }

  const write = (valuesText?: string, fresh?: RecordIntent) => {
    const active = fresh ?? intent
    // A resent edit carries its own fresh key; otherwise a settled attempt is never replayed.
    if (!active || saving || outcomeUnconfirmed || (!fresh && (attempted || receipt?.status === 'conflict'))) return
    setSaving(true)
    setMutationError('')
    setReceipt(null)
    let submitted = false
    void (async () => {
      try {
        const primaryKey = schema.primary_key.length ? schema.primary_key : asset.primary_key
        const changes = active.mode === 'delete' && active.row
          ? { key: primaryKeyOf(primaryKey, active.row.values) }
          : sourceMutationPayload(primaryKey, parseObject(valuesText ?? values, '记录值'), active.row?.values)
        const payload = {
          idempotency_key: active.idempotencyKey,
          ...changes,
          ...(active.row ? { expected_row_token: active.row.row_token } : {}),
        }
        setAttempted(true)
        submitted = true
        const response = active.mode === 'insert'
          ? await integratedDatasetsApi.insert(asset.id, payload)
          : active.mode === 'update'
            ? await integratedDatasetsApi.update(asset.id, payload)
            : await integratedDatasetsApi.delete(asset.id, payload)
        if (!alive.current) return
        setReceipt(response)
        if (response.status === 'applied') {
          toast.success(`已写入源表（影响 ${response.affected_rows} 行）`)
          // 源写立即生效，但本地副本要等复制追平；以成功回执的平台 applied_at
          // 为基线（缺失时用客户端收到成功响应的时刻），只有覆盖确认 cut 严格
          // 越过基线才撤提示——不按定时器猜测，也不复用无关副本 applied_at。
          pendingSyncBaseline.current = replicaSyncBaseline(response.applied_at)
          pendingSync.current = true
          setSourceWritePendingSync(true)
          setIntent(null)
          await loadRows()
          onChanged()
          return
        }
        if (response.status === 'failed') {
          // A failed receipt is terminal for this key, but the user's edit can
          // be retried safely with a fresh key after the source rejected it.
          setOutcomeUnconfirmed(false)
          setMutationError(integratedError({ detail: response.error ?? response.error_code }))
        } else {
          // Conflicts and unknown/pending outcomes must not be replayed with the
          // same key. Conflicts require a fresh source read; unknown/pending
          // outcomes require receipt reconciliation.
          setOutcomeUnconfirmed(true)
          setMutationError(integratedError({ detail: response.error ?? response.error_code }))
        }
      } catch (failure) {
        if (!alive.current) return
        const payload = failure as FailurePayload
        setMutationError(integratedError(failure))
        const mutation = payload.detail?.mutation
        if (mutation) setReceipt(mutation)
        const unconfirmed = submitted && (
          receiptNeedsReconciliation(mutation ?? null)
          || mutation?.status === 'conflict'
          || payload.detail?.code === 'UNKNOWN_REQUIRES_RECONCILIATION'
          || (!mutation && (!payload.status || payload.status >= 500))
        )
        setAttempted(!!mutation || unconfirmed)
        setOutcomeUnconfirmed(unconfirmed)
      } finally {
        if (alive.current) setSaving(false)
      }
    })()
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
        if (replicaCanRead) await loadRows()
        onChanged()
      } else {
        setOutcomeUnconfirmed(next.status !== 'failed')
        setMutationError(integratedError({ detail: next.error ?? next.error_code }))
      }
    } catch (failure) {
      if (alive.current) setMutationError(integratedError(failure))
    } finally {
      if (alive.current) setSaving(false)
    }
  }

  const decommission = async () => {
    setDecommissioning(true)
    setDecommissionError('')
    try {
      await integratedDatasetsApi.decommission(asset.id)
      if (!alive.current) return
      setDecommissionOpen(false)
      onDecommissioned?.()
      onChanged()
    } catch (failure) {
      // 结构化 409：历史/映射绑定/流水线/任务明细在 message 中汇总；
      // 历史与审计记录不会被删除，这里只如实展示服务端拒绝原因。
      if (alive.current) setDecommissionError(integratedError(failure))
    } finally {
      if (alive.current) setDecommissioning(false)
    }
  }

  const pk = schema.primary_key.length ? schema.primary_key : asset.primary_key
  const columns = schema.columns.length ? schema.columns : asset.columns
  const canWrite = canManage && asset.writable && asset.capabilities.write
  // Schema refresh is metadata, not a source write; the server still requires
  // owner/admin and reports ASSET_OWNER_REQUIRED for a non-owner editor.
  const canRefresh = canManage
  const canSync = canManage && asset.capabilities.sync
  const canStop = canSync && (asset.capabilities.stop ?? true)
  const activeSync = syncIsActive(sync)
  useEffect(() => {
    if (activeSync) setAdvancedOpen(true)
  }, [activeSync])

  const availability = dataAvailabilityView(replica, replicaUnverified)
  const capture = cdcCaptureView(cdcPoll)
  const metadata = metadataServiceView(asset)
  const unavailable = dataUnavailableReason(replica, replicaUnverified)
  const replicaInitializing = !replicaUnverified
    && !!replica && ['pending', 'initializing', 'catching_up'].includes(replica.status)
  const replicaStopped = !replicaUnverified && replica?.status === 'stopped'
  const replicaRegistered = !!replica && !replicaUnverified
  const writeLocked = outcomeUnconfirmed || pk.length === 0
  const resource = `${asset.schema_name}.${asset.table_name}`
  const connectionLabel = connectionName ? `「${connectionName}」` : shortId(asset.connection_id)
  const verifiedVersions = versions.filter(versionIsVerified).length
  const openRebuild = (trigger?: HTMLElement) => {
    rebuildTrigger.current = trigger ?? null
    setRebuildError('')
    setRebuildOpen(true)
  }

  const previewColumns: ColumnsType<IntegratedRow> = [
    ...(rows?.columns ?? []).map((column): ColumnType<IntegratedRow> => {
      const text = (value: unknown) => cellDisplayText(column, value)
      const numeric = column.type === 'integer' || column.type === 'float' || column.type === 'decimal'
      return {
        title: integratedColumnLabel(column),
        key: column.name,
        dataIndex: ['values', column.name],
        width: numeric ? 128 : column.type === 'boolean' ? 96
          : TEMPORAL_TYPES.has(column.type) ? 176 : column.type === 'json' || column.type === 'binary' ? 240 : undefined,
        align: numeric ? 'right' : 'left',
        ellipsis: true,
        render: (value: unknown) => (
          <span
            title={text(value)}
            className={cn('block truncate font-mono text-[13px]', value === null && 'text-[var(--color-text-tertiary)]')}
          >
            {text(value)}
          </span>
        ),
      }
    }),
    {
      title: '',
      key: 'actions',
      fixed: 'right',
      width: 96,
      render: (_value: unknown, row: IntegratedRow, index: number) => (
        <div className="flex items-center justify-end gap-1">
          <Hint content="编辑" wrap={!!writeLocked || !row.row_token}>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              aria-label={`编辑源记录 ${index + 1}`}
              disabled={!canWrite || writeLocked || !row.row_token}
              onClick={event => beginEdit('update', row, event.currentTarget)}
              className="text-muted-foreground hover:text-foreground"
            >
              <Pencil size={14} />
            </Button>
          </Hint>
          <Hint content="删除" wrap={!!writeLocked || !row.row_token}>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              aria-label={`删除源记录 ${index + 1}`}
              disabled={!canWrite || writeLocked || !row.row_token}
              onClick={event => beginEdit('delete', row, event.currentTarget)}
              className="text-muted-foreground hover:bg-[var(--color-danger-bg)] hover:text-[var(--color-danger)]"
            >
              <Trash2 size={14} />
            </Button>
          </Hint>
        </div>
      ),
    },
  ]

  const fieldColumns: ColumnsType<IntegratedColumn> = [
    {
      title: '字段',
      key: 'name',
      render: (_value: unknown, column) => {
        const display = column.display_name?.trim()
        const sourceKey = column.source_key && column.source_key !== column.name ? column.source_key : ''
        return (
          <div className="min-w-0">
            <div className="flex items-baseline gap-1.5">
              <span className="font-mono text-[13px] font-medium text-foreground">{column.name}</span>
              {display && display !== column.name && <span className="truncate text-xs text-muted-foreground">{display}</span>}
            </div>
            {sourceKey && <div className="truncate font-mono text-xs text-muted-foreground">源 {sourceKey}</div>}
          </div>
        )
      },
    },
    {
      title: '类型',
      key: 'type',
      render: (_value: unknown, column) => (
        <span className="text-[13px]">
          {column.type}
          {column.source_type && column.source_type !== column.type && (
            <span className="ml-1.5 font-mono text-xs text-muted-foreground">{column.source_type}</span>
          )}
        </span>
      ),
    },
    {
      title: '约束',
      key: 'constraints',
      width: 160,
      render: (_value: unknown, column) => (
        <div className="flex items-center gap-1.5">
          {pk.includes(column.name) && <Badge>主键</Badge>}
          {!column.nullable && <span className="text-xs text-muted-foreground">非空</span>}
        </div>
      ),
    },
  ]

  const versionColumns: ColumnsType<IntegratedVersion> = [
    {
      title: '版本',
      key: 'id',
      render: (_value: unknown, version) => (
        <span className="font-mono text-xs" title={version.id}>
          {shortId(version.id)}
          {version.version_no ? <span className="ml-1.5 font-sans text-muted-foreground">#{version.version_no}</span> : null}
        </span>
      ),
    },
    {
      title: '状态',
      key: 'status',
      render: (_value: unknown, version) => (
        <Badge variant={versionIsVerified(version) ? 'success' : 'warning'}>
          {versionIsVerified(version) ? '已封存' : '未核验'}
        </Badge>
      ),
    },
    { title: '行数', dataIndex: 'row_count', key: 'row_count', align: 'right', width: 96 },
    {
      title: '创建时间',
      key: 'created_at',
      width: 176,
      render: (_value: unknown, version) => <span className="tabular-nums">{formatTimestamp(version.created_at)}</span>,
    },
    {
      title: '校验和',
      key: 'checksum',
      render: (_value: unknown, version) => (
        <span className="font-mono text-xs text-muted-foreground" title={version.checksum ?? undefined}>
          {version.checksum ? shortId(version.checksum) : '—'}
        </span>
      ),
    },
  ]

  const editorTrigger = useRef<HTMLElement | null>(null)
  const addButton = useRef<HTMLButtonElement>(null)
  const rebuildTrigger = useRef<HTMLElement | null>(null)
  const stopTrigger = useRef<HTMLElement | null>(null)
  const decommissionTrigger = useRef<HTMLElement | null>(null)
  const metadataState = dbxState(asset)
  const syncLabel = sync ? syncStatusLabel(sync.status) : loadingSync ? '核验中' : '未核验'

  return (
    <TooltipProvider delayDuration={300}>
      <section data-testid="integrated-asset-detail" className="flex h-full min-h-0 flex-col">
        {/* 弹窗头：DialogTitle 供 Radix Content 关联可达名 */}
        <DialogHeader icon={<Database size={18} />} className="mb-0 shrink-0 border-b border-border py-3">
          <div className="flex min-w-0 items-center gap-2">
            <DialogTitle className="truncate" title={asset.name}>{asset.name}</DialogTitle>
            <Hint content={canWrite
              ? '写入会直接修改源表，每次写入都受权限和并发检查保护。'
              : '当前账号只能查看，不能修改源表数据。'}>
              <span className="shrink-0" tabIndex={0}>
                <Badge variant={canWrite ? 'outline' : 'secondary'}>{canWrite ? '可写' : '只读'}</Badge>
              </span>
            </Hint>
          </div>
          <DialogDescription className="mt-0.5 truncate font-mono text-xs leading-4 text-muted-foreground">
            {resource} · 连接 {connectionLabel}
          </DialogDescription>
        </DialogHeader>

        {/* 状态条：固定高度，轮询只改文字不改结构，下方内容永不位移 */}
        <div
          data-testid="integrated-status-strip"
          className="flex h-14 shrink-0 items-center border-b border-border bg-[var(--color-bg-elevated)] px-5 max-xl:h-auto max-xl:flex-col max-xl:items-stretch max-xl:py-1.5"
        >
          <StatusItem
            testId="integrated-replica-state"
            label="数据可用性"
            view={availability}
            help="查询读取平台保存的本地副本，不直连源库。本地数据持续跟随源表变化；「确认于」是最近一次核对源表位置的时间，不是最后修改时间。"
            action={availability.action && (
              availability.action === 'preflight'
                ? <Button type="button" variant="outline" size="sm" disabled={preflighting} onClick={() => void runPreflight()}>检查源连接</Button>
                : <Button type="button" variant="outline" size="sm" disabled={rebuilding || replicaInitializing} onClick={event => openRebuild(event.currentTarget)}>重新同步</Button>
            )}
          />
          <span aria-hidden className="mx-5 h-6 w-px shrink-0 bg-border max-xl:hidden" />
          <StatusItem
            testId="integrated-cdc-state"
            label="变更捕获"
            view={capture}
            help="发布引用此表的本体映射后自动开启，用于把增量变更推送给映射；不回补历史数据，没有手动开关。"
          />
          <span aria-hidden className="mx-5 h-6 w-px shrink-0 bg-border max-xl:hidden" />
          <StatusItem
            testId="integrated-dbx-state"
            label="元数据服务"
            view={metadata}
            help="登记新表和同步表结构时，由元数据服务核验源库结构；它不影响已有数据的查询。"
          />
        </div>

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-5">
          {preflight && (
            <Alert variant={preflight.ok ? 'success' : 'warning'} role="status" data-testid="integrated-replica-preflight" className="text-sm">
              {preflight.ok
                ? '源连接正常，具备同步所需的能力。'
                : /^[A-Z0-9_]+$/.test(preflight.code ?? '')
                  ? replicaErrorCodeHint(preflight.code)
                  : `源连接检查未通过：${preflight.code ?? '未知原因'}`}
              {preflight.code && /^[A-Z0-9_]+$/.test(preflight.code) && (
                <p className="mt-0.5 font-mono text-xs opacity-80">{preflight.code}</p>
              )}
            </Alert>
          )}
          {sourceWritePendingSync && (
            <Alert variant="info" role="status" data-testid="integrated-replica-pending-sync" className="text-sm">
              <span className="inline-flex items-center gap-2"><Loader2 size={14} className="animate-spin" />{REPLICA_PENDING_SYNC_NOTE}</span>
            </Alert>
          )}
          {error && <Alert variant="danger" role="alert" data-testid="integrated-detail-error">{error}</Alert>}

          {/* 数据预览：可读时自动加载 */}
          <section aria-labelledby="integrated-preview-title" className="space-y-3">
            <div className="flex items-center gap-2">
              <SectionTitle id="integrated-preview-title" meta={rows ? `${rows.rows.length} 行` : undefined}>数据预览</SectionTitle>
              <div className="ml-auto flex shrink-0 items-center gap-2">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => void loadRows()}
                  loading={loadingRows}
                  disabled={!asset.capabilities.read || !replicaCanRead}
                >
                  {!loadingRows && <RotateCw size={14} />}刷新数据
                </Button>
                <Hint
                  wrap={!canWrite || !!writeLocked}
                  content={!canWrite
                    ? '当前账号只能查看，不能修改源表数据。'
                    : pk.length === 0 ? '源表没有声明主键，不能新增记录。' : '请先核查上一次写入的结果。'}
                >
                  <Button
                    ref={addButton}
                    type="button"
                    size="sm"
                    disabled={!canWrite || writeLocked}
                    onClick={event => beginEdit('insert', undefined, event.currentTarget)}
                  >
                    <Plus size={16} />新增记录
                  </Button>
                </Hint>
              </div>
            </div>
            <div data-testid="integrated-source-rows">
              {rowsError && (
                <Alert variant="danger" role="alert" className="mb-3 text-sm">
                  {rowsError}
                  <Button type="button" variant="link" size="sm" className="ml-2 h-auto px-1" onClick={() => void loadRows()}>重试</Button>
                </Alert>
              )}
              {!asset.capabilities.read ? (
                <EmptyState title="没有查看数据的权限" description="如需查看，请联系管理员为该账号开放读取权限。" />
              ) : unavailable && !rows ? (
                <EmptyState
                  title={unavailable.title}
                  description={unavailable.description}
                  action={availability.action && (
                    availability.action === 'preflight'
                      ? <Button type="button" variant="outline" size="sm" disabled={preflighting} onClick={() => void runPreflight()}>检查源连接</Button>
                      : <Button type="button" size="sm" disabled={rebuilding} onClick={event => openRebuild(event.currentTarget)}>重新同步</Button>
                  )}
                />
              ) : !rows && loadingRows ? (
                <div role="status" aria-label="正在加载数据" className="overflow-hidden rounded-lg border border-border">
                  <div className="flex h-10 items-center gap-6 border-b border-border bg-[var(--color-bg-elevated)] px-3">
                    {columns.slice(0, 6).map(column => (
                      <span key={column.name} className="w-24 truncate text-xs font-medium text-muted-foreground">{integratedColumnLabel(column)}</span>
                    ))}
                  </div>
                  {[0, 1, 2, 3, 4].map(line => (
                    <div key={line} className="flex h-10 items-center gap-6 border-b border-border px-3 last:border-b-0">
                      {columns.slice(0, 6).map((column, cell) => (
                        <span key={column.name} className="h-3 animate-pulse rounded-sm bg-muted" style={{ width: `${56 + ((cell * 37 + line * 53) % 72)}px` }} />
                      ))}
                    </div>
                  ))}
                </div>
              ) : rows && rows.rows.length === 0 ? (
                <EmptyState title="暂无数据" description="源表为空，或尚未同步。" />
              ) : rows ? (
                <div className="overflow-hidden rounded-lg border border-border">
                  <ConfigProvider theme={TABLE_THEME}>
                    <Table<IntegratedRow>
                      size="small"
                      pagination={false}
                      rowKey={(_row, index) => String(index)}
                      dataSource={rows.rows}
                      scroll={{ x: 'max-content' }}
                      columns={previewColumns}
                    />
                  </ConfigProvider>
                </div>
              ) : null}
            </div>
            {rows && !rowsError && <p className="text-xs text-muted-foreground">{previewFootnote(rows)}</p>}
          </section>

          {outcomeUnconfirmed && (
            <Alert variant="warning" role="alert" data-testid="integrated-mutation-locked" className="text-sm">
              没能确认上一次写入的结果，已暂停新的写入。请核查回执，或请管理员按下面的请求标识与源表记录核对。
              <span className="mt-1 block break-all font-mono text-xs">
                {intent?.idempotencyKey ?? receipt?.id ?? '请求标识见回执'}
              </span>
            </Alert>
          )}
          {receipt && !intent && (
            <p className="text-xs text-muted-foreground">
              最近写入：{receiptStatusLabel(receipt.status)} · 影响 {receipt.affected_rows} 行
              {receipt.replayed ? ' · 重复请求已忽略' : ''}
              <Button type="button" variant="link" size="sm" className="ml-2 h-auto px-1" disabled={saving} onClick={() => void refreshReceipt()}>核查回执</Button>
            </p>
          )}

          {/* 字段结构 */}
          <section aria-labelledby="integrated-fields-title" className="space-y-3">
            <div className="flex min-h-9 items-center gap-2">
              <button
                type="button"
                aria-expanded={fieldsOpen}
                aria-controls="integrated-fields-body"
                onClick={() => setFieldsOpen(open => !open)}
                className="flex items-center gap-1.5 rounded text-sm font-semibold text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <ChevronDown size={14} className={cn('text-muted-foreground transition-transform', !fieldsOpen && '-rotate-90')} />
                <span id="integrated-fields-title">字段结构</span>
              </button>
              <span className="min-w-0 truncate text-xs text-muted-foreground">
                {columns.length} 个字段 · {pk.length ? `主键 ${pk.join(', ')}` : '未声明主键（不能编辑或删除记录）'}
              </span>
              <Hint
                wrap={!canRefresh}
                content={!canRefresh ? '只有管理员和编辑者可以同步表结构。' : '重新读取源表结构；结构变化后会自动排队重新同步本地数据。'}
              >
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="ml-auto"
                  disabled={!canRefresh || refreshingSchema}
                  onClick={() => void refreshSchema()}
                >
                  {refreshingSchema ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}同步表结构
                </Button>
              </Hint>
            </div>
            {fieldsOpen && (
              <div id="integrated-fields-body" className="overflow-hidden rounded-lg border border-border">
                <ConfigProvider theme={TABLE_THEME}>
                  <Table<IntegratedColumn>
                    size="small"
                    pagination={false}
                    rowKey="name"
                    dataSource={columns}
                    scroll={{ x: 'max-content' }}
                    columns={fieldColumns}
                  />
                </ConfigProvider>
              </div>
            )}
          </section>

          {/* 版本与同步 */}
          <Disclosure
            testId="integrated-advanced-operations"
            title="版本与同步"
            meta={`${verifiedVersions} 个离线快照${activeSync ? ' · 同步进行中' : ''}`}
            open={advancedOpen}
            onToggle={setAdvancedOpen}
          >
            <div className="flex flex-wrap items-center gap-2">
              <Hint wrap={!canSync || !!syncError} content={!canSync
                ? '当前账号没有创建离线快照的权限。'
                : syncError ? '请先点击右侧刷新核验任务状态。' : '把整张源表封存为只读版本，仅用于对账。'}>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => void startSync()}
                  disabled={loadingSync || syncing || activeSync || !!syncError || !canSync || !asset.capabilities.sync}
                >
                  {syncing ? <Loader2 size={14} className="animate-spin" /> : <Archive size={14} />}创建离线快照
                </Button>
              </Hint>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-label="刷新版本状态"
                className="text-muted-foreground hover:text-foreground"
                disabled={loadingVersions}
                onClick={() => void loadVersions()}
              >
                <RefreshCw size={14} />
              </Button>
              <span className="text-xs text-muted-foreground">离线快照把整张源表封存为只读版本，仅用于对账，不影响日常查询。</span>
            </div>
            {syncing && !activeSync && <p role="status" className="text-xs text-muted-foreground">正在创建，尚未确认完成，请勿重复提交。</p>}
            {sync && (
              <dl data-testid="integrated-sync-state" className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
                <dt className="text-muted-foreground">最近一次同步</dt>
                <dd className="text-foreground">{syncLabel}</dd>
                {syncFacts(sync).map(fact => (
                  <Fragment key={fact.label}>
                    <dt className="text-muted-foreground">{fact.label}</dt>
                    <dd className="tabular-nums text-foreground">{fact.value}</dd>
                  </Fragment>
                ))}
              </dl>
            )}
            {sync?.status === 'failed' && (
              <p className="text-xs text-muted-foreground">这次同步失败了。重试会创建新的任务，不会续传旧任务。</p>
            )}
            {sync?.error && <p role="alert" className="text-xs text-[var(--color-danger)]">{sync.error}</p>}
            {syncError && (
              <Alert variant="danger" role="alert" className="text-sm">
                {syncError}
                <Button type="button" variant="link" size="sm" className="ml-2 h-auto px-1" disabled={loadingSync} onClick={() => void recoverSync()}>刷新任务状态</Button>
              </Alert>
            )}
            <div className="space-y-2">
              <h4 className="text-xs font-medium text-muted-foreground">离线快照</h4>
              {versionsError && (
                <Alert variant="danger" role="alert" className="text-sm">{versionsError}</Alert>
              )}
              {!versionsError && versions.length === 0 && (
                <p className="text-xs text-muted-foreground">{loadingVersions ? '正在加载…' : '还没有离线快照'}</p>
              )}
              {versions.length > 0 && (
                <div className="overflow-hidden rounded-lg border border-border">
                  <ConfigProvider theme={TABLE_THEME}>
                    <Table<IntegratedVersion>
                      size="small"
                      pagination={false}
                      rowKey="id"
                      dataSource={versions}
                      scroll={{ x: 'max-content' }}
                      columns={versionColumns}
                    />
                  </ConfigProvider>
                </div>
              )}
            </div>
          </Disclosure>

          {/* 更多信息：标识符可选中复制 */}
          <Disclosure title="更多信息">
            <dl className="grid grid-cols-[6rem_1fr] gap-x-4 gap-y-2 text-xs">
              <InfoRow label="资产 ID">{asset.id}</InfoRow>
              <InfoRow label="数据集 ID">{asset.dataset_id}</InfoRow>
              <InfoRow label="连接 ID">{asset.connection_id}</InfoRow>
              <InfoRow label="结构指纹">{schema.schema_fingerprint || asset.schema_fingerprint || '—'}</InfoRow>
              <InfoRow label="执行后端">
                {asset.backends ? `快照 ${asset.backends.snapshot} · 写入 ${asset.backends.mutation} · 元数据 ${asset.backends.metadata}` : '内置'}
              </InfoRow>
              <InfoRow label="元数据服务">
                {[metadataState.version ? `v${metadataState.version}` : '', metadataState.commit ? `commit ${metadataState.commit}` : '']
                  .filter(Boolean).join(' · ') || '—'}
              </InfoRow>
              <InfoRow label="创建时间">{formatTimestamp(asset.created_at)}</InfoRow>
              <InfoRow label="更新时间">{formatTimestamp(asset.updated_at)}</InfoRow>
            </dl>
          </Disclosure>
        </div>

        {/* 页脚：低频与危险操作集中在这里，主内容区只留日常动作 */}
        <div data-testid="integrated-detail-footer" className="flex shrink-0 items-center gap-1 border-t border-border px-3 py-2.5">
          {canSync && replicaRegistered && (
            <FooterAction
              icon={<RefreshCcw size={14} />}
              disabled={rebuilding || replicaInitializing}
              onClick={event => openRebuild(event.currentTarget)}
              wrap={rebuilding || replicaInitializing}
              hint={replicaInitializing ? '正在初始化，请等待完成后再操作。' : '重新导入整张源表并替换当前本地数据；源表不受影响。'}
            >重新同步数据</FooterAction>
          )}
          {canSync && (
            <FooterAction
              icon={<PlugZap size={14} />}
              disabled={preflighting}
              onClick={() => void runPreflight()}
              wrap={preflighting}
              hint="检查源库是否具备同步所需的权限和配置。"
            >检查源连接</FooterAction>
          )}
          {canStop && replicaRegistered && !replicaStopped && (
            <FooterAction
              icon={<CircleStop size={14} />}
              disabled={stopping}
              onClick={event => { stopTrigger.current = event.currentTarget; setStopOpen(true) }}
              wrap={stopping}
              hint="暂停后本地数据不可查询，源表不受影响。"
            >暂停同步</FooterAction>
          )}
          <FooterAction
            danger
            icon={<Unlink size={14} />}
            disabled={!canManage || activeSync}
            onClick={event => { decommissionTrigger.current = event.currentTarget; setDecommissionError(''); setDecommissionOpen(true) }}
            wrap={!canManage || activeSync}
            hint={!canManage
              ? '只有管理员和编辑者可以注销数据集。'
              : activeSync ? '同步进行中不能注销，请等待运行结束。' : '解除本平台的登记引用；历史记录和源表都不会被删除。'}
          >注销数据集</FooterAction>
          <span className="ml-auto hidden text-xs text-muted-foreground xl:inline">数据来自平台本地副本 · 时间均为本地时区</span>
          <Button type="button" variant="outline" size="sm" className="ml-2" onClick={onClose}>关闭</Button>
        </div>

        {intent && (
          <IntegratedRecordEditor
            key={editorSession}
            intent={intent}
            columns={columns}
            primaryKey={pk}
            resource={resource}
            values={values}
            saving={saving}
            locked={attempted && receipt?.status !== 'failed'}
            outcomeUnconfirmed={outcomeUnconfirmed}
            receipt={receipt}
            mutationError={mutationError}
            onValuesChange={setValues}
            onWrite={write}
            onRetryWrite={writeAfterFailure}
            onClose={closeEditor}
            onRetry={retryFailed}
            onReloadAfterConflict={reloadAfterConflict}
            onRefreshReceipt={refreshReceipt}
            onCloseAutoFocus={event => refocus(event, editorTrigger.current, addButton.current)}
          />
        )}

        <ConfirmDialog
          open={rebuildOpen}
          title="重新同步本地数据"
          description={
            <>
              <p>将重新导入「{asset.name}」的整张源表，并替换当前保存的本地数据。进行中的初始化会被新的任务取代。</p>
              <p className="mt-1 text-xs text-[var(--color-text-secondary)]">源表不受影响；完成后本地数据恢复可查询。</p>
              {rebuildError && <p className="mt-2 font-medium" role="alert">{rebuildError}</p>}
            </>
          }
          confirmText={rebuilding ? '同步中...' : '重新同步'}
          variant="warning"
          loading={rebuilding}
          layer="nested"
          onConfirm={rebuildReplica}
          onClose={() => { if (!rebuilding) setRebuildOpen(false) }}
          onCloseAutoFocus={event => refocus(event, rebuildTrigger.current)}
        />

        <ConfirmDialog
          open={stopOpen}
          title="暂停同步"
          description={
            <>
              <p>
                确认暂停「{asset.name}」的本地数据同步？暂停会取消进行中的任务，本地数据立即不可查询，
                且不会改为直接读取源库。
              </p>
              <p className="mt-1 text-xs text-[var(--color-text-secondary)]">
                源表与已封存的离线快照不受影响；需要恢复时请重新同步。
              </p>
            </>
          }
          confirmText={stopping ? '暂停中...' : '确认暂停'}
          variant="danger"
          loading={stopping}
          layer="nested"
          onConfirm={stopReplica}
          onClose={() => { if (!stopping) setStopOpen(false) }}
          onCloseAutoFocus={event => refocus(event, stopTrigger.current)}
        />

        <ConfirmDialog
          open={decommissionOpen}
          title="注销数据集"
          description={
            <>
              <p>
                确认注销「{asset.name}」（{resource}）？
                注销要求没有任何历史记录（数据集版本、同步任务、写入回执）、本体映射绑定、流水线与数据任务；
                存在任一依赖时会被拒绝并列出明细。
              </p>
              <p className="mt-1 text-xs text-[var(--color-text-secondary)]">
                历史与审计记录不会被删除；源表本身不受影响，只是解除本平台的登记引用。
              </p>
              {decommissionError && <p className="mt-2 font-medium" role="alert">{decommissionError}</p>}
            </>
          }
          confirmText={decommissioning ? '注销中...' : '确认注销'}
          variant="danger"
          loading={decommissioning}
          layer="nested"
          onConfirm={decommission}
          onClose={() => { if (!decommissioning) setDecommissionOpen(false) }}
          onCloseAutoFocus={event => refocus(event, decommissionTrigger.current)}
        />
      </section>
    </TooltipProvider>
  )
}
