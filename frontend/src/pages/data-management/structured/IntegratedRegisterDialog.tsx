import { useEffect, useState } from 'react'
import { Database, Loader2 } from 'lucide-react'
import { integratedDatasetsApi, type DiscoveredResource, type IntegratedAsset, type SourceConnection } from '@/api/v2/integrated-datasets'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { integratedError, validateResourceName } from './integratedModel'

const inputClass = 'mt-1 w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring'

export default function IntegratedRegisterDialog({ onClose, onRegistered }: {
  onClose: () => void
  onRegistered: (asset: IntegratedAsset) => void
}) {
  const [connections, setConnections] = useState<SourceConnection[]>([])
  const [name, setName] = useState('')
  const [connection, setConnection] = useState('')
  const [schema, setSchema] = useState('')
  const [table, setTable] = useState('')
  const [writable, setWritable] = useState(false)
  const [saving, setSaving] = useState(false)
  const [loading, setLoading] = useState(true)
  const [discovering, setDiscovering] = useState(false)
  const [discovered, setDiscovered] = useState(false)
  const [resources, setResources] = useState<DiscoveredResource[]>([])
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    integratedDatasetsApi.connections()
      .then(items => { if (alive) setConnections(items.filter(item => item.kind === 'mysql' || item.kind === 'postgres')) })
      .catch(error => { if (alive) setError(`加载数据连接失败：${integratedError(error)}`) })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [])

  const discover = async () => {
    if (!connection || discovering) return
    setDiscovering(true)
    setError('')
    setResources([])
    setDiscovered(false)
    try {
      const response = await integratedDatasetsApi.resources(connection)
      setResources(response.items)
      setDiscovered(true)
    } catch (failure) {
      // A 503 here is authoritative: never invent a local table list or fall
      // back to native reflection.
      setError(`dbx 源表发现失败：${integratedError(failure)}`)
    } finally {
      setDiscovering(false)
    }
  }

  const register = async () => {
    if (saving) return
    if (!name.trim()) { setError('请填写资产名称'); return }
    if (!connection) { setError('请选择 MySQL / PostgreSQL 数据连接'); return }
    const resourceError = validateResourceName(schema, table)
    if (resourceError) { setError(resourceError); return }
    setSaving(true)
    setError('')
    try {
      const asset = await integratedDatasetsApi.register({
        name: name.trim(),
        connection_id: connection,
        schema_name: schema,
        table_name: table,
        writable,
      })
      onRegistered(asset)
    } catch (failure) {
      setError(`登记失败（未创建半套资产）：${integratedError(failure)}`)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open onOpenChange={open => { if (!open && !saving) onClose() }}>
      <DialogContent dismissible={!saving} className="max-h-[85vh] overflow-y-auto">
        <DialogHeader icon={<Database size={18} />}>
          <DialogTitle>登记集成数据集</DialogTitle>
          <DialogDescription>
            只登记源资产引用，不复制密码或凭据。源表清单与登记元数据必须由后端真实 dbx 核验；dbx 不可用时明确失败，不使用本地推测。
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={event => { event.preventDefault(); register() }} className="space-y-4">
          <label className="block text-xs font-medium">
            资产名称
            <input aria-label="资产名称" value={name} onChange={event => setName(event.target.value)} className={inputClass} autoFocus autoComplete="off" />
          </label>
          <div>
            <label className="mb-1 block text-xs font-medium" id="integrated-connection-label">数据连接</label>
            <Select value={connection} onValueChange={value => { setConnection(value); setResources([]); setDiscovered(false); setSchema(''); setTable('') }}>
              <SelectTrigger aria-labelledby="integrated-connection-label" disabled={loading}>
                <SelectValue placeholder={loading ? '加载连接中…' : '选择 MySQL / PostgreSQL 连接'} />
              </SelectTrigger>
              <SelectContent>
                {connections.map(item => <SelectItem key={item.id} value={item.id}>{item.name} · {item.kind}</SelectItem>)}
              </SelectContent>
            </Select>
            {!loading && connections.length === 0 && (
              <p className="mt-1 text-xs text-muted-foreground">没有可用的 MySQL / PostgreSQL 连接，请先在数据连接中配置。</p>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button type="button" variant="outline" size="sm" disabled={!connection || discovering} onClick={discover}>
              {discovering ? '读取中…' : '发现源表'}
            </Button>
            <span className="text-xs text-muted-foreground">发现结果只来自真实 dbx；失败不会回退到 native 元数据。</span>
          </div>
          {resources.length > 0 && (
            <div data-testid="integrated-discovered-resources" aria-label="发现的源表" className="max-h-32 overflow-y-auto rounded-lg border border-border p-2">
              {resources.map(item => (
                <button
                  key={`${item.schema_name}.${item.table_name}`}
                  type="button"
                  onClick={() => { setSchema(item.schema_name); setTable(item.table_name) }}
                  className="block w-full rounded px-2 py-1 text-left text-xs hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  {item.schema_name}.{item.table_name}
                </button>
              ))}
            </div>
          )}
          {discovered && resources.length === 0 && <p className="text-xs text-muted-foreground">dbx 未返回可登记的表；不会生成推测表清单。</p>}
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="block text-xs font-medium">
              数据库 Schema
              <input aria-label="数据库 Schema" value={schema} onChange={event => setSchema(event.target.value)} placeholder="例如 public（保持源名称大小写）" className={inputClass} autoComplete="off" />
            </label>
            <label className="block text-xs font-medium">
              源表名称
              <input aria-label="源表名称" value={table} onChange={event => setTable(event.target.value)} className={inputClass} autoComplete="off" />
            </label>
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={writable} onChange={event => setWritable(event.target.checked)} />
            允许受控写入
          </label>
          <p className="text-xs text-muted-foreground">
            默认只读。写入还要求源主键、行令牌与服务端写权限；登记成功不代表当前连通或已有完整版本。
          </p>
          {error && <Alert variant="danger" role="alert">{error}</Alert>}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose} disabled={saving}>取消</Button>
            <Button type="submit" disabled={saving || loading}>
              {saving && <Loader2 size={14} className="animate-spin" />}确认登记
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
