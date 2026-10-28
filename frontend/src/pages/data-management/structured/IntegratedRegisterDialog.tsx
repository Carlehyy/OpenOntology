import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Database, Loader2, Plus, Trash2 } from 'lucide-react'
import {
  connectionsApi,
  connectionErrorDetail,
  CONNECTION_CONFIG_FIELDS,
  CONNECTION_KIND_LABELS,
  rotationConfigFromInput,
  type Connection,
} from '@/api/v2/connections'
import { integratedDatasetsApi, type DiscoveredResource, type IntegratedAsset } from '@/api/v2/integrated-datasets'
import { Alert } from '@/components/ui/Alert'
import { Button } from '@/components/ui/Button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { integratedError, validateResourceName } from './integratedModel'

const inputClass = 'mt-1 w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring'

/** 集成登记只支持关系型源；连接创建复用数据连接页同一端点与字段契约。 */
const INTEGRATED_SOURCE_KINDS = ['mysql', 'postgres'] as const
type IntegratedSourceKind = (typeof INTEGRATED_SOURCE_KINDS)[number]

export default function IntegratedRegisterDialog({ onClose, onRegistered }: {
  onClose: () => void
  onRegistered: (asset: IntegratedAsset) => void
}) {
  const navigate = useNavigate()
  const [connections, setConnections] = useState<Connection[]>([])
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

  // 页内新建连接：创建/测试/凭据修正都走与数据连接页相同的端点
  const [createOpen, setCreateOpen] = useState(false)
  const [newName, setNewName] = useState('')
  const [newKind, setNewKind] = useState<IntegratedSourceKind>('mysql')
  const [newConfig, setNewConfig] = useState<Record<string, string>>({})
  const [createdConnection, setCreatedConnection] = useState<Connection | null>(null)
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState('')
  const [createNote, setCreateNote] = useState('')

  const loadConnections = () => {
    connectionsApi.list()
      .then(items => setConnections(items.filter(item => item.kind === 'mysql' || item.kind === 'postgres')))
      .catch(failure => setError(`加载数据连接失败：${integratedError(failure)}`))
      .finally(() => setLoading(false))
  }

  useEffect(() => { loadConnections() }, [])

  const activeConnections = connections.filter(item => item.status === 'active')
  const emptyConnectionHint = connections.length === 0
    ? '没有可用的 MySQL / PostgreSQL 连接；可在下方直接新建并测试，或先在数据连接中配置。'
    : `已有 ${connections.length} 个 MySQL / PostgreSQL 连接，但均未通过连通测试；可在下方新建连接，或到数据连接中重新测试。`

  const resetCreateForm = () => {
    setNewName('')
    setNewKind('mysql')
    setNewConfig({})
    setCreatedConnection(null)
    setCreateError('')
    setCreateNote('')
  }

  // 创建 → 立即测试：只有通过真实连通测试（active）的连接才进入选择器。
  // 创建/轮换成功后立即清空表单中的凭据：客户端不留存任何明文凭据，
  // 后续修正只提交重新填写的键（留空 = 保留服务端现值）。
  const createAndTest = async () => {
    if (creating) return
    if (!createdConnection && !newName.trim()) { setCreateError('请填写连接名称'); return }
    setCreating(true)
    setCreateError('')
    setCreateNote('')
    try {
      let target = createdConnection
      if (!target) {
        target = await connectionsApi.create({
          name: newName.trim(),
          kind: newKind,
          config: { ...newConfig },
        })
        setCreatedConnection(target)
        setNewConfig({})
      } else {
        const patch = rotationConfigFromInput(newConfig)
        if (Object.keys(patch).length > 0) {
          target = await connectionsApi.rotateConfig(target.id, patch)
          setNewConfig({})
        }
      }
      const tested = await connectionsApi.test(target.id)
      if (tested.success) {
        const active: Connection = { ...target, status: 'active' }
        setConnections(current => [active, ...current.filter(item => item.id !== active.id)])
        setConnection(active.id)
        setCreatedConnection(null)
        setCreateOpen(false)
        resetCreateForm()
        setCreateNote('')
        setResources([])
        setDiscovered(false)
        setSchema('')
        setTable('')
      } else {
        setCreateNote(
          `连通测试失败${tested.detail ? `：${tested.detail}` : ''}；连接已保存为未激活状态，`
          + '凭据不再回显，修正需要更新的字段后重测（留空 = 保留已保存值），或删除后重建。')
      }
    } catch (failure) {
      setCreateError(connectionErrorDetail(failure, '连接创建失败'))
    } finally {
      setCreating(false)
    }
  }

  const discardCreated = async () => {
    if (!createdConnection || creating) return
    setCreating(true)
    setCreateError('')
    try {
      await connectionsApi.remove(createdConnection.id)
      resetCreateForm()
    } catch (failure) {
      setCreateError(`删除未激活连接失败：${connectionErrorDetail(failure, '请稍后重试')}`)
    } finally {
      setCreating(false)
    }
  }

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

  const newFields = CONNECTION_CONFIG_FIELDS[newKind] ?? []

  return (
    <Dialog open onOpenChange={open => { if (!open && !saving) onClose() }}>
      <DialogContent dismissible={!saving} className="max-h-[85vh] overflow-y-auto">
        <DialogHeader icon={<Database size={18} />}>
          <DialogTitle>登记集成数据集</DialogTitle>
          <DialogDescription>
            只登记源资产引用，不复制密码或凭据。源表清单与登记元数据必须由后端真实 dbx 核验；dbx 不可用时明确失败，不使用本地推测。
            没有现成连接时可在本弹窗内直接创建并测试 MySQL / PostgreSQL 连接，无需跳转数据连接页。
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={event => { event.preventDefault(); register() }} className="space-y-4">
          <label className="block text-xs font-medium">
            资产名称
            <input aria-label="资产名称" value={name} onChange={event => setName(event.target.value)} className={inputClass} autoFocus autoComplete="off" />
          </label>
          <div>
            <label className="mb-1 block text-xs font-medium" id="integrated-connection-label">数据连接</label>
            <Select value={connection} onValueChange={value => {
              if (!value) return
              setConnection(value); setResources([]); setDiscovered(false); setSchema(''); setTable('')
            }}>
              <SelectTrigger aria-labelledby="integrated-connection-label" disabled={loading || activeConnections.length === 0}>
                <SelectValue placeholder={loading ? '加载连接中…' : activeConnections.length === 0 ? '暂无可用连接' : '选择 MySQL / PostgreSQL 连接'} />
              </SelectTrigger>
              <SelectContent>
                {activeConnections.map(item => <SelectItem key={item.id} value={item.id}>{item.name} · {item.kind}</SelectItem>)}
              </SelectContent>
            </Select>
            {!loading && !error && activeConnections.length === 0 && (
              <p className="mt-1 text-xs text-muted-foreground" data-testid="integrated-connection-empty-hint">
                {emptyConnectionHint}
                <Button
                  type="button"
                  variant="link"
                  size="sm"
                  className="ml-1 h-auto px-1 py-0"
                  onClick={() => navigate('/data/pipelines/connections')}
                >
                  前往数据连接
                </Button>
              </p>
            )}
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="mt-2"
              disabled={creating}
              onClick={() => { setCreateOpen(open => !open); setCreateError(''); setCreateNote('') }}
            >
              <Plus size={13} />{createOpen ? '收起新建连接' : '新建连接'}
            </Button>
          </div>

          {createOpen && (
            <div className="rounded-lg border border-border bg-muted/40 p-3 space-y-3" data-testid="integrated-create-connection">
              <p className="text-xs text-muted-foreground">
                {createdConnection
                  ? `连接「${createdConnection.name}」已保存但未通过连通测试；修正下方字段后重测（留空 = 保留已保存值），或删除后重建。凭据不会被回显。`
                  : '在本页创建 MySQL / PostgreSQL 连接（与数据连接页同一逻辑）：保存后立即做真实连通测试，通过后自动进入上方选择器。配置加密存储，永不回显。'}
              </p>
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="block text-xs font-medium">
                  连接名称
                  <input
                    aria-label="连接名称"
                    value={createdConnection ? createdConnection.name : newName}
                    onChange={event => setNewName(event.target.value)}
                    disabled={!!createdConnection}
                    placeholder="例：ERP 订单数据库"
                    className={`${inputClass} disabled:opacity-70`}
                    autoComplete="off"
                  />
                </label>
                <div>
                  <label className="mb-1 block text-xs font-medium" id="integrated-new-kind-label">连接类型</label>
                  <Select value={newKind} onValueChange={(value: IntegratedSourceKind) => { setNewKind(value); setNewConfig({}) }}>
                    <SelectTrigger aria-labelledby="integrated-new-kind-label" disabled={!!createdConnection}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {INTEGRATED_SOURCE_KINDS.map(kind => (
                        <SelectItem key={kind} value={kind}>{CONNECTION_KIND_LABELS[kind]}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                {newFields.map(field => (
                  <label key={field.key} className="block text-xs font-medium">
                    {field.label}
                    <input
                      type={field.type || 'text'}
                      aria-label={createdConnection ? `修正 ${field.label}` : field.label}
                      value={newConfig[field.key] || ''}
                      onChange={event => setNewConfig(current => ({ ...current, [field.key]: event.target.value }))}
                      placeholder={field.key === 'password' && createdConnection ? '输入新密码（留空保持原值）' : field.placeholder}
                      className={inputClass}
                      autoComplete="new-password"
                    />
                  </label>
                ))}
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Button type="button" size="sm" onClick={createAndTest} disabled={creating}>
                  {creating && <Loader2 size={13} className="animate-spin" />}
                  {createdConnection ? '更新配置并重测' : '创建并测试'}
                </Button>
                {createdConnection && (
                  <Button type="button" variant="outline" size="sm" onClick={discardCreated} disabled={creating}>
                    <Trash2 size={13} />删除并重建
                  </Button>
                )}
                {createNote && <span className="text-xs text-[var(--color-warning)]" role="status">{createNote}</span>}
              </div>
              {createError && <Alert variant="danger" role="alert">{createError}</Alert>}
            </div>
          )}

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
