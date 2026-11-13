import { useCallback, useEffect, useRef, useState } from 'react'
import { Table } from 'antd'
import { CircleStop, DatabaseZap, Loader2, Pencil, Plus, RefreshCw, ShieldCheck, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { v4 as uuid } from 'uuid'
import {
  cdcCardLabel, cdcFactsDetail, cdcSubscriptionSummary, integratedDatasetsApi, isIntegratedCdcStatus,
  type IntegratedAsset, type IntegratedCdcStatus, type IntegratedReplica, type IntegratedReplicaPreflightResult,
  type IntegratedRow, type IntegratedRows, type IntegratedSchema,
  type IntegratedSyncRun, type IntegratedVersion, type MutationReceipt,
} from '@/api/v2/integrated-datasets'
import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import {
  REPLICA_PENDING_SYNC_NOTE, backendsLabel, dbxStateDetail, dbxStateLabel, integratedCell,
  integratedColumnLabel, integratedError, isReplicaStatusPayload, latestVersionStateLabel,
  parseObject, primaryKeyOf,
  receiptNeedsReconciliation, receiptStatusLabel, replicaCoversWrite, replicaFactsDetail,
  replicaReadable, replicaNotRegistered, replicaStale, replicaStatusLabel, replicaSyncBaseline,
  sourceMutationPayload, sourceReadDetail, sourceReadLabel, syncIsActive, syncIsSealed,
  syncStatusLabel, versionStateLabel,
} from './integratedModel'

type EditIntent = { mode: 'insert' | 'update' | 'delete'; row?: IntegratedRow; idempotencyKey: string }
type FailurePayload = {
  status?: number
  detail?: { code?: string; message?: string; run?: IntegratedSyncRun; mutation?: MutationReceipt }
  message?: string
}

function terminalReceiptGuidance(status: MutationReceipt['status']): string {
  if (status === 'failed') return '写入已明确失败；当前幂等键已结算，请使用新幂等键重试。'
  if (status === 'conflict') return '源记录已发生冲突；当前幂等键已结算，请取消并重新加载副本数据。'
  return '写入未确认成功，请先核查回执，不要重复提交。'
}

const DEFAULT_SCHEMA: IntegratedSchema = { columns: [], primary_key: [], schema_fingerprint: '' }

/** Automatic CDC status refresh cadence while the detail dialog is mounted. */
const CDC_POLL_INTERVAL_MS = 5000

/** CDC status is a read-only server fact; failed or malformed fetches never degrade to the last healthy state. */
type CdcPollState = { phase: 'checking' } | { phase: 'ok'; status: IntegratedCdcStatus } | { phase: 'unavailable' }

export default function IntegratedAssetDetail({ asset, canManage, onChanged, onDecommissioned }: {
  asset: IntegratedAsset
  canManage: boolean
  onChanged: () => void
  onDecommissioned?: () => void
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
  // 受控注销：确认弹窗 + 结构化 409 明细（历史/映射/流水线/任务）
  const [decommissioning, setDecommissioning] = useState(false)
  const [decommissionOpen, setDecommissionOpen] = useState(false)
  const [decommissionError, setDecommissionError] = useState('')
  // 离线快照折叠区默认收起（整表同步退出常规路径）；进行中的同步运行必须可见，
  // 因此 activeSync 时自动展开——用户仍可手动收起（不强行抢回）。
  const [advancedOpen, setAdvancedOpen] = useState(false)
  // 本地副本控制面：状态轮询 + 重建/预检动作。状态是服务端事实，失败或
  // 形态不符一律显示不可核验，绝不沿用上次健康态。
  const [replica, setReplica] = useState<IntegratedReplica | null>(null)
  const [replicaUnverified, setReplicaUnverified] = useState(false)
  const [rebuilding, setRebuilding] = useState(false)
  const [preflighting, setPreflighting] = useState(false)
  const [preflight, setPreflight] = useState<IntegratedReplicaPreflightResult | null>(null)
  const [stopping, setStopping] = useState(false)
  const [stopOpen, setStopOpen] = useState(false)
  const [sourceWritePendingSync, setSourceWritePendingSync] = useState(false)
  // 写入成功后的副本追平基线：轮询看到 applied_at 前进才撤下"等待同步"说明，
  // 不能用固定超时假装副本已包含该写入。
  const pendingSync = useRef(false)
  const pendingSyncBaseline = useRef<string | null>(null)
  const [cdcPoll, setCdcPoll] = useState<CdcPollState>({ phase: 'checking' })
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

  // 自动 CDC 订阅状态：仅在本详情挂载期间轮询（无手动开关）；订阅由已发布
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

  // 本地副本状态轮询：与资产详情同读授权、无源端 IO；固定间隔即可（就绪
  // 与初始化推进都可见）。失败、形态不符或不属于当前资产一律不可核验，
  // 绝不沿用上次健康态。
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
        setReplica({
          status: payload.status, complete: payload.complete,
          stale: payload.stale ?? (payload.complete && payload.status !== 'ready'),
          row_count: payload.row_count, applied_at: payload.applied_at,
          coverage_confirmed_at: payload.coverage_confirmed_at,
          last_error_code: payload.last_error_code,
          generation: payload.generation, run_id: payload.run_id,
        })
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

  const loadRows = async () => {
    setLoadingRows(true)
    setRowsError('')
    setRows(null)
    try {
      const response = await integratedDatasetsApi.rows(asset.id, 100)
      if (alive.current) setRows(response)
    } catch (failure) {
      if (alive.current) setRowsError(`本地副本读取失败：${integratedError(failure)}`)
    } finally {
      if (alive.current) setLoadingRows(false)
    }
  }

  const rebuildReplica = async () => {
    setRebuilding(true)
    setError('')
    try {
      await integratedDatasetsApi.rebuildReplica(asset.id)
      if (!alive.current) return
      toast.success('已请求重建本地副本；初始化完成后可读')
      const payload = await integratedDatasetsApi.replica(asset.id)
      if (alive.current && isReplicaStatusPayload(payload, asset.id)) {
        setReplica({
          status: payload.status, complete: payload.complete,
          stale: payload.stale ?? (payload.complete && payload.status !== 'ready'),
          row_count: payload.row_count, applied_at: payload.applied_at,
          coverage_confirmed_at: payload.coverage_confirmed_at,
          last_error_code: payload.last_error_code,
          generation: payload.generation, run_id: payload.run_id,
        })
      }
      onChanged()
    } catch (failure) {
      if (alive.current) setError(`副本重建请求失败：${integratedError(failure)}`)
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
      toast.success('已停止本地副本复制；重建前本地查询不可用')
      const payload = await integratedDatasetsApi.replica(asset.id)
      if (alive.current && isReplicaStatusPayload(payload, asset.id)) {
        setReplica({
          status: payload.status, complete: payload.complete,
          stale: payload.stale ?? (payload.complete && payload.status !== 'ready'),
          row_count: payload.row_count, applied_at: payload.applied_at,
          coverage_confirmed_at: payload.coverage_confirmed_at,
          last_error_code: payload.last_error_code,
          generation: payload.generation, run_id: payload.run_id,
        })
      }
      onChanged()
    } catch (failure) {
      if (alive.current) setError(`停止本地副本失败：${integratedError(failure)}`)
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
    await loadRows()
  }

  // A settled conflict key cannot be replayed, so closing the editor after a
  // conflict must take the cancel-and-reload path; otherwise the stale row
  // token stays and every write button remains locked with no way out.
  const closeEditor = () => {
    if (receipt?.status === 'conflict') { void reloadAfterConflict(); return }
    if (!saving) setIntent(null)
  }

  const write = async () => {
    if (!intent || saving || outcomeUnconfirmed || attempted || receipt?.status === 'conflict') return
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
        setAttempted(true)
        setOutcomeUnconfirmed(false)
        setMutationError(terminalReceiptGuidance(response.status))
      } else {
        // Conflicts and unknown/pending outcomes must not be replayed with the
        // same key. Conflicts require a fresh source read; unknown/pending
        // outcomes require receipt reconciliation.
        setOutcomeUnconfirmed(true)
        setMutationError(terminalReceiptGuidance(response.status))
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
        setOutcomeUnconfirmed(next.status !== 'failed')
        setMutationError(terminalReceiptGuidance(next.status))
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
  const cdcCardStateLabel = cdcPoll.phase === 'ok' ? cdcCardLabel(cdcPoll.status) : cdcPoll.phase === 'checking' ? '核验中' : '不可核验'
  const cdcCardDetailLine = cdcPoll.phase === 'ok'
    ? cdcPoll.status.enabled
      ? `${cdcSubscriptionSummary(cdcPoll.status.subscriptions)} · ${cdcFactsDetail(cdcPoll.status)}`
      : '尚无已发布的远程对象映射订阅本表；远程映射发布后自动开启订阅。'
    : cdcPoll.phase === 'checking'
    ? '正在核验服务端订阅状态。'
    : '订阅状态查询失败，不代表订阅正常；详情打开期间会自动重试。'
  const replicaCardLabel = replicaUnverified ? '不可核验' : replicaStatusLabel(replica?.status)
  const replicaCanRead = !replicaUnverified && replicaReadable(replica)
  const replicaCardDetail = replicaUnverified
    ? '副本状态查询失败，不代表副本正常；详情打开期间会自动重试。'
    : replicaFactsDetail(replica)
  const replicaInitializing = !replicaUnverified
    && !!replica && ['pending', 'initializing', 'catching_up'].includes(replica.status)
  const replicaStopped = !replicaUnverified && replica?.status === 'stopped'
  const replicaBlocked = !replicaUnverified
    && !!replica && ['rebuild_required', 'blocked'].includes(replica.status)
  const replicaIsStale = !replicaUnverified && replicaStale(replica)
  const replicaHint = replicaUnverified
    ? '状态查询失败期间不宣称副本可读；详情打开期间会自动重试，重建与停止仍以服务端裁决为准。'
    : replicaCanRead && replicaIsStale
      ? `查询读取平台本地副本，但副本未完全就绪（${replicaCardLabel}）：覆盖落后于复制推进，覆盖确认时点不代表最后变更时间；不直连源库，源网不可达不阻断本地读取。`
      : replicaCanRead
        ? '查询与预览读取平台本地副本（覆盖确认时点，不是最后变更时间），不直连源库；源网不可达不阻断本地读取。'
        : replicaStopped
          ? '副本已停止：本地查询被拒绝，源表不受影响；重建后可恢复读取。'
          : replicaBlocked
            ? '副本不可读：需先重建（或先做源复制预检）；不会回退为远端源直读。'
            : '初始化未完成前本地查询不可用；可重建副本或先做源复制能力预检。'

  return (
    <section data-testid="integrated-asset-detail" className="flex h-full min-h-0 flex-col">
      {/* 弹窗头：DialogTitle 供 Radix Content 关联可达名；pr-14 给右上角关闭按钮让位 */}
      <div className="flex shrink-0 flex-wrap items-start justify-between gap-3 border-b border-border px-5 py-4 pr-14">
        <div className="min-w-0">
          <DialogTitle className="text-base font-semibold">{asset.name}</DialogTitle>
          <p className="mt-1 break-all text-xs text-muted-foreground">
            {asset.schema_name}.{asset.table_name} · 连接 {asset.connection_id} · 资产 {asset.id}
          </p>
        </div>
        <Badge variant={canWrite ? 'success' : 'secondary'}>{canWrite ? '受控可写' : '只读'}</Badge>
      </div>

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-5">
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
          快照版本：<strong>{latestVersionStateLabel(asset, versions)}</strong>
          <p className="mt-1 text-muted-foreground">「完整同步到版本」会把整张源表封存成离线快照，仅用于精确对账；日常查询读本地副本，不依赖该快照。</p>
        </div>
        <div
          className="rounded-lg bg-muted p-3"
          role="status"
          aria-label={`CDC 变更订阅状态：${cdcCardStateLabel}`}
          data-testid="integrated-cdc-state"
        >
          CDC 变更订阅：<strong>{cdcCardStateLabel}</strong>
          <p className="mt-1 break-words text-muted-foreground">{cdcCardDetailLine}</p>
          <p className="mt-1 text-muted-foreground">订阅随已发布的远程映射自动开启与停止，无手动开关；仅同步开启后的增量变更，不回补历史数据。</p>
        </div>
        <div
          className="rounded-lg bg-muted p-3"
          role="status"
          aria-label={`本地副本状态：${replicaCardLabel}`}
          data-testid="integrated-replica-state"
        >
          本地副本：<strong>{replicaCardLabel}</strong>
          <p className="mt-1 break-words text-muted-foreground">{replicaCardDetail}</p>
          <p className="mt-1 text-muted-foreground">{replicaHint}</p>
          {canSync && (
            <div className="mt-2 flex flex-wrap gap-2">
              <Button
                type="button" variant="outline" size="sm"
                disabled={rebuilding || replicaInitializing}
                title={replicaInitializing ? '初始化进行中；如需强制换代仍可稍后重试' : '重建会整体替换当前副本（围栏运行，不生成数据集版本）'}
                onClick={rebuildReplica}
              >
                {rebuilding ? <Loader2 size={13} className="animate-spin" /> : <DatabaseZap size={13} />}重建本地副本
              </Button>
              <Button
                type="button" variant="outline" size="sm"
                disabled={preflighting}
                title="对源做复制能力预检（binlog/WAL/权限），只返回稳定错误码"
                onClick={runPreflight}
              >
                {preflighting && <Loader2 size={13} className="animate-spin" />}
                <ShieldCheck size={13} />源复制预检
              </Button>
              {canStop && (
                <Button
                  type="button" variant="outline" size="sm"
                  disabled={stopping || replicaStopped}
                  title={replicaStopped
                    ? '副本已停止；重建后可恢复本地查询'
                    : '停止复制并取消在飞运行；当前副本立即不可读，源表不受影响'}
                  onClick={() => setStopOpen(true)}
                >
                  {stopping ? <Loader2 size={13} className="animate-spin" /> : <CircleStop size={13} />}停止本地副本
                </Button>
              )}
            </div>
          )}
          {preflight && (
            <p className="mt-1 break-words text-muted-foreground" role="status">
              {preflight.ok
                ? '预检通过：源具备复制所需能力。'
                : `预检未通过：${preflight.code ?? '未知错误码'}；请按错误码修正源配置后重试。`}
            </p>
          )}
        </div>
      </div>

      {sourceWritePendingSync && (
        <p className="text-xs text-muted-foreground" role="status" data-testid="integrated-replica-pending-sync">
          {REPLICA_PENDING_SYNC_NOTE}
        </p>
      )}

      <div className="flex flex-wrap gap-2">
        <Button type="button" variant="outline" size="sm" onClick={loadRows} disabled={loadingRows || !asset.capabilities.read}>
          {loadingRows ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}加载副本数据
        </Button>
        <Button
          type="button" variant="outline" size="sm"
          disabled={!canRefresh || refreshingSchema}
          title={!canRefresh ? '只有管理员和编辑者可刷新源结构' : '服务端仍要求资产 owner 或管理员'}
          onClick={refreshSchema}
        >
          {refreshingSchema && <Loader2 size={13} className="animate-spin" />}刷新源结构
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
        <Button
          type="button" variant="outline" size="sm"
          disabled={!canManage || activeSync}
          title={!canManage
            ? '只有管理员和编辑者可注销资产'
            : activeSync
              ? '同步进行中不能注销；请等待运行结束'
              : '受控注销：零历史/零映射绑定/零任务才允许，服务端仍要求资产 owner 或管理员'}
          onClick={() => { setDecommissionOpen(true); setDecommissionError('') }}
        >
          <Trash2 size={13} />注销资产
        </Button>
      </div>

      {!canWrite && (
        <p className="text-xs text-muted-foreground">
          写入未启用或当前角色无该资产写权限；本地副本读取仍可用，完整同步在下方「离线快照与高级操作」折叠区。
          写权限由服务端按资产 owner/管理员与资产配置逐动作裁决。
        </p>
      )}
      {error && <Alert variant="danger" role="alert">{error}</Alert>}

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
            本地副本预览：最多 {rows.limit ?? 100} 行，不是总行数。覆盖确认时点 {rows.source_state.coverage_confirmed_at ?? rows.source_state.started_at}（非最后变更时间） · 读取模式 {rows.source_state.kind}
            {rows.stale || rows.source_state.stale ? ' · 副本可能滞后（标注读取）' : ''}。
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

      {/* 整表同步已退出日常路径：完整快照作为受控离线能力折叠保留，
          历史（同步运行/封存版本）与 API 全部兼容保留；进行中运行会自动展开。 */}
      <details
        open={advancedOpen}
        onToggle={event => setAdvancedOpen((event.target as HTMLDetailsElement).open)}
        data-testid="integrated-advanced-operations"
      >
        <summary className="cursor-pointer text-sm font-medium">离线快照与高级操作</summary>
        <div className="mt-2 space-y-3">
          <p className="text-xs text-muted-foreground">
            日常查询读取平台本地副本（按最近确认覆盖时点，副本滞后如实标注）；受控写入仍直写源库并等待副本追平。
            完整同步把源表整体快照为封存版本，仅用于精确离线对账，不参与日常查询路径。
          </p>
          <div className="flex flex-wrap gap-2">
            <Button
              type="button" variant="outline" size="sm"
              onClick={startSync}
              disabled={loadingSync || syncing || activeSync || !!syncError || !canSync || !asset.capabilities.sync}
              title={!canSync ? '服务端按资产配置与 RBAC 裁决同步权限' : undefined}
            >
              {syncing && <Loader2 size={13} className="animate-spin" />}完整同步到版本
            </Button>
          </div>
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
        </div>
      </details>

      {outcomeUnconfirmed && receipt?.status !== 'conflict' && (
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

      </div>

      {/* 嵌套弹窗必须升层：外层详情弹窗占据 base 层 */}
      <Dialog open={!!intent} onOpenChange={open => { if (!open) closeEditor() }}>
        <DialogContent dismissible={!saving} layer="nested" className="max-h-[85vh] overflow-y-auto">
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
                disabled={saving || (attempted && receipt?.status !== 'failed')}
                value={values}
                onChange={event => setValues(event.target.value)}
                rows={10}
                className="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </label>
          )}
          <p className="mt-2 text-xs text-muted-foreground">
            主键变更由服务端拒绝；更新/删除必须携带最近一次本地副本读取得到的行令牌，并在源库当前行上做乐观并发检查（CAS）。
            副本滞后期间源行可能已变化，此时会以 ROW_CONFLICT 拒绝；请取消并重新加载副本数据后再编辑。
            写入成功后立即作用于源库，本地副本追平前预览仍可能显示旧值。
          </p>
          {mutationError && (
            <Alert variant="danger" role="alert" className="mt-3">
              {mutationError}
              <p className="mt-1 text-xs">
                {receipt?.status === 'failed'
                  ? '写入失败不是未知结果；请使用新幂等键重试，当前编辑内容会保留。'
                  : receipt?.status === 'conflict'
                    ? '源记录已变化；请取消并重新加载副本数据，不能重放当前幂等键。'
                    : '冲突或未确认结果都不是成功；结果未知时不要重复提交或换新幂等键，请先核查回执。'}
              </p>
              {/* 结算后的恢复动作必须放在弹窗内：模态层会挡住页面上的任何按钮。 */}
              {receipt?.status === 'failed' && (
                <Button type="button" variant="link" size="sm" className="ml-2 h-auto px-1" disabled={saving} onClick={retryFailed}>
                  <RefreshCw size={13} />使用新幂等键重试
                </Button>
              )}
              {receipt?.status === 'conflict' && (
                <Button type="button" variant="link" size="sm" className="ml-2 h-auto px-1" disabled={saving} onClick={() => void reloadAfterConflict()}>
                  <RefreshCw size={13} />取消并重新加载副本数据
                </Button>
              )}
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
            <Button type="button" variant="outline" disabled={saving} onClick={closeEditor}>取消</Button>
            <Button
              type="button"
              variant={intent?.mode === 'delete' ? 'danger' : 'default'}
              onClick={write}
              disabled={saving || outcomeUnconfirmed || attempted || receipt?.status === 'conflict'}
            >
              {saving && <Loader2 size={14} className="animate-spin" />}确认写入源表
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={stopOpen}
        title="停止本地副本"
        description={
          <>
            <p>
              确认停止「{asset.name}」的本地副本复制？停止会取消在飞运行并立即让当前副本不可读，
              本地查询将被服务端拒绝，且不会回退为远端源直读。
            </p>
            <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">
              源表与已封存版本不受影响；需要恢复本地查询时请重建副本。
            </p>
          </>
        }
        confirmText={stopping ? '停止中...' : '确认停止'}
        variant="danger"
        loading={stopping}
        layer="nested"
        onConfirm={stopReplica}
        onClose={() => { if (!stopping) setStopOpen(false) }}
      />

      <ConfirmDialog
        open={decommissionOpen}
        title="注销集成资产"
        description={
          <>
            <p>
              确认注销「{asset.name}」（{asset.schema_name}.{asset.table_name}）？
              受控注销要求零历史（数据集版本 / 同步运行 / 写入回执，任何状态）、零本体映射绑定、零流水线与数据任务；
              存在任一依赖时服务端会以 409 拒绝并列出明细。
            </p>
            <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">
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
      />
    </section>
  )
}
