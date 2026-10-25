import { useCallback, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Database, Plus, RefreshCw } from 'lucide-react'
import { integratedDatasetsApi, type IntegratedAsset } from '@/api/v2/integrated-datasets'
import { useAuthStore } from '@/stores/authStore'
import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { EmptyState, LoadingState } from '@/components/ui/LoadingState'
import IntegratedRegisterDialog from './IntegratedRegisterDialog'
import IntegratedAssetDetail from './IntegratedAssetDetail'
import IntegratedQueryPanel from './IntegratedQueryPanel'
import { backendsLabel, dbxState, dbxStateDetail, dbxStateLabel, integratedError } from './integratedModel'

function CapabilityBadge({ label, enabled }: { label: string; enabled: boolean }) {
  return <Badge variant={enabled ? 'success' : 'secondary'}>{label} {enabled ? '允许' : '未启用'}</Badge>
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

  const dbx = assets[0] ? dbxState(assets[0]) : null
  const dbxUnavailable = !!dbx && (!dbx.configured || !dbx.available)

  return (
    <div data-testid="integrated-datasets-view" className="h-full space-y-4 overflow-y-auto pb-5">
      <section className="rounded-xl border border-border bg-card p-4">
        <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="flex items-center gap-2 text-base font-semibold"><Database size={17} />集成数据集</h1>
            <p className="mt-1 max-w-2xl text-xs text-muted-foreground">
              登记 MySQL / PostgreSQL 源表，直读、完整版本同步与受控源表写入。登记与结构刷新必须由真实 dbx 元数据服务核验；
              源数据读取与写入走 native 能力，二者不可互相冒充。
            </p>
          </div>
          <div className="flex gap-2">
            <Button type="button" variant="outline" size="sm" onClick={refresh} disabled={loading}>
              <RefreshCw size={13} />刷新资产
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
          <Alert variant="warning" role="status" data-testid="integrated-dbx-unavailable" className="mb-3">
            DBX 元数据服务{dbxStateLabel(assets[0])}：真实源表发现、登记与结构刷新会失败（503），本页不会用 native 反射冒充发现结果。
            <span className="mt-1 block text-xs opacity-80">{dbxStateDetail(assets[0])}</span>
          </Alert>
        )}

        {loading && <div role="status"><LoadingState message="加载集成资产中…" /></div>}
        {listError && (
          <Alert variant="danger" role="alert" className="mb-3">
            {listError}
            <Button type="button" variant="link" size="sm" onClick={refresh} className="ml-2 h-auto px-1">重试</Button>
          </Alert>
        )}
        {!loading && !listError && assets.length === 0 && (
          <EmptyState
            title="尚未登记集成数据集"
            description="先选择已有 MySQL / PostgreSQL 连接，由 dbx 核验源表后再登记。未配置 dbx 时登记会明确失败，不会生成半套资产。"
          />
        )}

        {assets.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-border text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 font-medium">资产名称</th>
                  <th className="px-3 py-2 font-medium">源资源</th>
                  <th className="px-3 py-2 font-medium">主键</th>
                  <th className="px-3 py-2 font-medium">访问能力</th>
                  <th className="px-3 py-2 font-medium">DBX 元数据 / 执行后端</th>
                  <th className="px-3 py-2 font-medium">数据版本</th>
                  <th className="px-3 py-2 font-medium">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {assets.map(asset => (
                  <tr key={asset.id} className={selected?.id === asset.id ? 'bg-brand-soft' : 'hover:bg-muted'}>
                    <td className="px-3 py-3 font-medium">
                      {asset.name}
                      <span className="mt-1 block break-all font-mono text-[10px] text-muted-foreground">{asset.id}</span>
                    </td>
                    <td className="px-3 py-3">
                      {asset.schema_name}.{asset.table_name}
                      <span className="mt-1 block text-[10px] text-muted-foreground">连接 {asset.connection_id}</span>
                    </td>
                    <td className="px-3 py-3">{asset.primary_key.join(', ') || '未声明（禁止更新/删除）'}</td>
                    <td className="px-3 py-3">
                      <div className="flex flex-wrap gap-1">
                        <CapabilityBadge label="读取" enabled={asset.capabilities.read} />
                        <CapabilityBadge label="写入" enabled={canManage && asset.writable && asset.capabilities.write} />
                        <CapabilityBadge label="同步" enabled={canManage && asset.capabilities.sync} />
                      </div>
                    </td>
                    <td className="px-3 py-3">
                      <Badge variant={dbxState(asset).configured && dbxState(asset).available ? 'success' : 'warning'}>{dbxStateLabel(asset)}</Badge>
                      <span className="mt-1 block text-[10px] text-muted-foreground">{backendsLabel(asset)}</span>
                    </td>
                    <td className="px-3 py-3">
                      {asset.latest_version_id
                        ? <span className="font-mono text-[11px]">最新版本 {asset.latest_version_id}（封存状态未核验）</span>
                        : '尚无完整版本'}
                    </td>
                    <td className="px-3 py-3">
                      <Button type="button" variant="outline" size="sm" aria-label={`查看 ${asset.name}`} onClick={() => select(asset.id)}>查看</Button>
                    </td>
                  </tr>
                ))}
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
        />
      )}
      <IntegratedQueryPanel key={`query-${selected?.id || 'unselected'}`} assets={assets} selectedId={selected?.id} />
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
