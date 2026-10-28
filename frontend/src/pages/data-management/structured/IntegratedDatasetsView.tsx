import { useCallback, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { formatDateTime } from '@/utils/datetime'
import { AlertTriangle, CheckCircle, Database, Eye, Loader2, Plus, RefreshCw, Table2 } from 'lucide-react'
import { integratedDatasetsApi, type IntegratedAsset } from '@/api/v2/integrated-datasets'
import { useAuthStore } from '@/stores/authStore'
import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import IntegratedRegisterDialog from './IntegratedRegisterDialog'
import IntegratedAssetDetail from './IntegratedAssetDetail'
import { backendsLabel, dbxState, dbxStateDetail, dbxStateLabel, integratedError } from './integratedModel'

function CapabilityBadge({ label, enabled }: { label: string; enabled: boolean }) {
  return <Badge variant={enabled ? 'success' : 'secondary'}>{label} {enabled ? '允许' : '未启用'}</Badge>
}

function formatUpdatedAt(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return formatDateTime(date)
}

export default function IntegratedDatasetsView({ focusDatasetId }: { focusDatasetId?: string | null }) {
  const [, setSearchParams] = useSearchParams()
  const role = useAuthStore(state => state.user?.role)
  const canManage = role === 'admin' || role === 'editor'
  const [assets, setAssets] = useState<IntegratedAsset[]>([])
  const [selected, setSelected] = useState<IntegratedAsset | null>(null)
  const [registering, setRegistering] = useState(false)
  const [loading, setLoading] = useState(true)
  const [listError, setListError] = useState('')
  const [focusError, setFocusError] = useState('')
  const [retry, setRetry] = useState(0)
  const refresh = useCallback(() => setRetry(value => value + 1), [])

  useEffect(() => {
    let alive = true
    setLoading(true)
    setListError('')
    integratedDatasetsApi.list()
      .then(response => { if (alive) setAssets(response.items) })
      .catch(error => { if (alive) { setAssets([]); setListError(integratedError(error)) } })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [retry])

  useEffect(() => {
    if (!focusDatasetId) { setSelected(null); setFocusError(''); return }
    const existing = assets.find(asset => asset.id === focusDatasetId)
    if (existing) { setSelected(existing); setFocusError(''); return }
    let alive = true
    setSelected(null)
    setFocusError('')
    integratedDatasetsApi.get(focusDatasetId)
      .then(asset => { if (alive) setSelected(asset) })
      .catch(error => { if (alive) setFocusError(`深链资产 ${focusDatasetId} 无法核验：${integratedError(error)}`) })
    return () => { alive = false }
  }, [focusDatasetId, assets])

  const select = (id: string) => setSearchParams(previous => {
    const next = new URLSearchParams(previous)
    next.set('tab', 'integrated')
    next.set('dataset', id)
    return next
  }, { replace: true })

  const clearSelection = () => setSearchParams(previous => {
    const next = new URLSearchParams(previous)
    next.set('tab', 'integrated')
    next.delete('dataset')
    return next
  }, { replace: true })

  const dbx = assets[0] ? dbxState(assets[0]) : null
  const dbxUnavailable = !!dbx && (!dbx.configured || !dbx.available)

  return (
    <div data-testid="integrated-datasets-view" className="h-full space-y-4 overflow-y-auto pb-5">
      {/* 资产列表卡片 —— 表格语言与成品数据集页签一致（同一张卡、同一套表头/操作列规范） */}
      <section className="flex flex-col rounded-xl border border-border bg-card shadow-sm/50">
        <div className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-border px-5 pt-4 pb-3">
          <div className="min-w-0">
            <h1 className="flex items-center gap-2 text-sm font-semibold"><Database size={15} />集成数据集</h1>
            <p className="mt-1 max-w-2xl text-xs text-muted-foreground">
              登记 MySQL / PostgreSQL 源表，直读、完整版本同步与受控源表写入。登记与结构刷新必须由真实 dbx 元数据服务核验；
              源数据读取与写入走 native 能力，二者不可互相冒充。
            </p>
          </div>
          <div className="ml-auto flex shrink-0 items-center gap-2">
            <Button type="button" variant="outline" size="sm" onClick={refresh} disabled={loading}>
              <RefreshCw size={13} className={loading ? 'animate-spin' : undefined} />刷新资产
            </Button>
            <Button
              type="button"
              size="sm"
              onClick={() => setRegistering(true)}
              disabled={!canManage}
              title={!canManage ? '只有管理员和编辑者可登记资产' : undefined}
            >
              <Plus size={13} />登记集成数据集
            </Button>
          </div>
        </div>

        {dbxUnavailable && (
          <div className="px-5 pt-3">
            <Alert variant="warning" role="status" data-testid="integrated-dbx-unavailable">
              DBX 元数据服务{dbxStateLabel(assets[0])}：真实源表发现、登记与结构刷新会失败（503），本页不会用 native 反射冒充发现结果。
              <span className="mt-1 block text-xs opacity-80">{dbxStateDetail(assets[0])}</span>
            </Alert>
          </div>
        )}

        {listError && (
          <div className="px-5 pt-3">
            <Alert variant="danger" role="alert">
              {listError}
              <Button type="button" variant="link" size="sm" onClick={refresh} className="ml-2 h-auto px-1">重试</Button>
            </Alert>
          </div>
        )}

        {loading && (
          <div role="status" className="flex items-center justify-center gap-2 p-12 text-sm text-[var(--color-text-tertiary)]">
            <Loader2 size={16} className="animate-spin" /> 加载集成资产中…
          </div>
        )}

        {!loading && !listError && assets.length === 0 && (
          <div className="m-5 border-2 border-dashed rounded-xl p-12 text-center text-[var(--color-text-tertiary)] space-y-2">
            <Table2 size={32} className="mx-auto opacity-30" />
            <p className="text-sm font-medium">尚未登记集成数据集</p>
          </div>
        )}

        {assets.length > 0 && (
          <div className="overflow-x-auto">
            {/* border-separate：sticky 列在 collapsed 边框模型下横滚时边框不随单元格移动（同目录先例 CuratedView） */}
            <table className="w-full min-w-[1020px] border-separate border-spacing-0 text-sm">
              <thead className="bg-card">
                <tr>
                  <th className="border-b border-border px-4 py-2.5 text-left font-medium text-muted-foreground text-xs">资产名称</th>
                  <th className="border-b border-border px-4 py-2.5 text-left font-medium text-muted-foreground text-xs">源资源</th>
                  <th className="border-b border-border px-4 py-2.5 text-left font-medium text-muted-foreground text-xs">主键</th>
                  <th className="border-b border-border px-4 py-2.5 text-center font-medium text-muted-foreground text-xs">访问能力</th>
                  <th className="border-b border-border px-4 py-2.5 text-center font-medium text-muted-foreground text-xs">DBX 元数据 / 执行后端</th>
                  <th className="border-b border-border px-4 py-2.5 text-left font-medium text-muted-foreground text-xs">数据版本</th>
                  <th className="whitespace-nowrap border-b border-border px-4 py-2.5 text-center font-medium text-muted-foreground text-xs">最近更新时间</th>
                  <th className="sticky right-0 z-10 border-b border-l border-border bg-card px-4 py-2.5 text-center font-medium text-muted-foreground text-xs">操作</th>
                </tr>
              </thead>
              {/* 分离边框模型下行（tr）边框不渲染，行间分隔改挂在单元格上（等价 divide-y） */}
              <tbody className="[&>tr:not(:last-child)>td]:border-b [&>tr:not(:last-child)>td]:border-border">
                {assets.map(asset => {
                  const assetDbx = dbxState(asset)
                  const dbxOk = assetDbx.configured && assetDbx.available
                  return (
                    <tr key={asset.id} className={`group transition-colors hover:bg-muted ${selected?.id === asset.id ? 'bg-brand-soft' : ''}`}>
                      <td className="max-w-[240px] px-4 py-3 text-left font-medium text-foreground">
                        <span className="block truncate" title={asset.name}>{asset.name}</span>
                        <span className="mt-0.5 block break-all font-mono text-[10px] text-muted-foreground">{asset.id}</span>
                      </td>
                      <td className="max-w-[200px] px-4 py-3 text-left text-xs">
                        <span className="block truncate font-mono" title={`${asset.schema_name}.${asset.table_name}`}>{asset.schema_name}.{asset.table_name}</span>
                        <span className="mt-0.5 block truncate text-[10px] text-muted-foreground" title={asset.connection_id}>连接 {asset.connection_id}</span>
                      </td>
                      <td className="max-w-[160px] px-4 py-3 text-left text-xs text-muted-foreground">
                        <span className="block truncate" title={asset.primary_key.join(', ')}>
                          {asset.primary_key.join(', ') || '未声明（禁止更新/删除）'}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex flex-wrap items-center justify-center gap-1">
                          <CapabilityBadge label="读取" enabled={asset.capabilities.read} />
                          <CapabilityBadge label="写入" enabled={canManage && asset.writable && asset.capabilities.write} />
                          <CapabilityBadge label="同步" enabled={canManage && asset.capabilities.sync} />
                        </div>
                      </td>
                      <td className="px-4 py-3 text-center">
                        <span className={`inline-flex items-center gap-1 whitespace-nowrap rounded border px-1.5 py-0.5 text-xs ${
                          dbxOk
                            ? 'bg-[var(--color-success-bg)] text-[var(--color-success)] border-[color-mix(in_srgb,var(--color-success)_35%,transparent)]'
                            : 'bg-[var(--color-warning-bg)] text-[var(--color-warning)] border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)]'
                        }`}>
                          {dbxOk ? <CheckCircle size={12} /> : <AlertTriangle size={12} />}
                          {dbxStateLabel(asset)}
                        </span>
                        <span className="mt-1 block text-[10px] text-muted-foreground">{backendsLabel(asset)}</span>
                      </td>
                      <td className="max-w-[200px] px-4 py-3 text-left text-xs text-muted-foreground">
                        {asset.latest_version_id
                          ? <span className="block truncate font-mono text-[11px]" title={asset.latest_version_id}>最新版本 {asset.latest_version_id}（封存状态未核验）</span>
                          : '尚无完整版本'}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3 text-center text-xs tabular-nums text-muted-foreground" title={asset.updated_at ?? asset.created_at}>
                        {formatUpdatedAt(asset.updated_at ?? asset.created_at)}
                      </td>
                      <td className="sticky right-0 z-10 border-l border-border bg-card px-4 py-3 text-center transition-colors group-hover:bg-muted">
                        <button
                          type="button"
                          aria-label={`查看 ${asset.name}`}
                          onClick={() => select(asset.id)}
                          className="inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-lg border border-border bg-card px-2.5 py-1.5 text-xs font-medium text-muted-foreground transition hover:border-brand-line hover:bg-brand-soft hover:text-brand-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        >
                          <Eye size={12} /> 查看
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {focusError && <Alert variant="danger" role="alert">{focusError}</Alert>}
      {selected && (
        <IntegratedAssetDetail
          key={`detail-${selected.id}`}
          asset={selected}
          canManage={canManage}
          onChanged={refresh}
          onDecommissioned={() => {
            clearSelection()
            window.dispatchEvent(new Event('ontoprompt:data-assets-changed'))
          }}
        />
      )}
      {registering && (
        <IntegratedRegisterDialog
          onClose={() => setRegistering(false)}
          onRegistered={asset => {
            setRegistering(false)
            setAssets(current => [...current, asset])
            select(asset.id)
            refresh()
            window.dispatchEvent(new Event('ontoprompt:data-assets-changed'))
          }}
        />
      )}
    </div>
  )
}
