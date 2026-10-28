import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Database, KeyRound, Loader2 } from 'lucide-react'
import {
  integratedDatasetsApi,
  type DiscoveredResource,
  type IntegratedAsset,
  type IntegratedSourceKind,
  type SourceConnection,
  type SourceReference,
} from '@/api/v2/integrated-datasets'
import { Alert } from '@/components/ui/Alert'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { integratedError } from './integratedModel'
import {
  DISPLAY_NAME_MAX,
  FIELD_NAME_MAX,
  SOURCE_DEFAULT_PORTS,
  SOURCE_KIND_LABELS,
  beginDiscover,
  beginSchemaVerify,
  beginSourceTest,
  buildRegisterPayload,
  connectionStatusLabel,
  duplicateAliases,
  emptySourceForm,
  initialRegisterEvidence,
  newSourceReference,
  portForKindChange,
  prefillFieldConfig,
  probeFailureLabel,
  registerReady,
  resourceKey,
  sourceReferenceKey,
  validateFieldConfig,
  validateNewSourceForm,
  type FieldConfigDraft,
  type NewSourceFormValues,
  type RegisterEvidence,
} from './integratedSourceForm'

const fieldInputClass = 'h-8 w-full min-w-32 rounded-md border border-border bg-card px-2 text-sm outline-none transition focus-visible:ring-2 focus-visible:ring-ring'
const sectionTitleClass = 'text-xs font-semibold text-foreground'
const sectionHintClass = 'mt-0.5 text-[11px] text-[var(--color-text-tertiary)]'

type SourceMode = 'new' | 'existing'
type Stage = 'test' | 'resources' | 'schema'

const CONNECTION_STATUS_BADGE: Record<string, 'success' | 'secondary' | 'danger' | 'warning'> = {
  active: 'success',
  inactive: 'secondary',
  error: 'danger',
}

/**
 * 登记集成数据集：弹窗内完成新源配置/复用连接、连通测试、dbx 源表发现、
 * 结构核验与字段配置，不跳转数据连接页。
 * 阶段请求走 epoch + AbortController：源/表/参数变化即作废下游证据；
 * 每个阶段发起时先丢弃自身旧证据，失败重试后不得沿用上一次结果登记，
 * 迟到响应与被抢占阶段的中间态一律丢弃。
 */
export default function IntegratedRegisterDialog({ onClose, onRegistered }: {
  onClose: () => void
  onRegistered: (asset: IntegratedAsset) => void
}) {
  const navigate = useNavigate()
  const [connections, setConnections] = useState<SourceConnection[]>([])
  const [connectionsLoading, setConnectionsLoading] = useState(true)
  const [connectionsError, setConnectionsError] = useState('')

  const [mode, setMode] = useState<SourceMode>('new')
  const [assetName, setAssetName] = useState('')
  const [writable, setWritable] = useState(false)
  const [form, setForm] = useState<NewSourceFormValues>(() => emptySourceForm())
  const [connectionId, setConnectionId] = useState('')

  const [evidence, setEvidence] = useState<RegisterEvidence>(initialRegisterEvidence)
  const [testing, setTesting] = useState(false)
  const [testMessage, setTestMessage] = useState('')
  const [loadingResources, setLoadingResources] = useState(false)
  const [resourcesError, setResourcesError] = useState('')
  const [schemaLoading, setSchemaLoading] = useState(false)
  const [schemaError, setSchemaError] = useState('')

  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  const aliveRef = useRef(true)
  const epochRef = useRef(0)
  const abortRef = useRef<AbortController | null>(null)
  const connectionsEpochRef = useRef(0)

  useEffect(() => {
    aliveRef.current = true
    return () => {
      aliveRef.current = false
      abortRef.current?.abort()
    }
  }, [])

  const isLive = (epoch: number) => aliveRef.current && epoch === epochRef.current
  const beginStageRequest = (stage: Stage) => {
    epochRef.current += 1
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    setTesting(stage === 'test')
    setLoadingResources(stage === 'resources')
    setSchemaLoading(stage === 'schema')
    return { epoch: epochRef.current, signal: controller.signal }
  }
  /** Deprecate every in-flight stage request without starting a new one. */
  const deprecateStages = () => {
    epochRef.current += 1
    abortRef.current?.abort()
    setTesting(false)
    setLoadingResources(false)
    setSchemaLoading(false)
  }

  const loadConnections = useCallback(() => {
    connectionsEpochRef.current += 1
    const epoch = connectionsEpochRef.current
    setConnectionsLoading(true)
    setConnectionsError('')
    integratedDatasetsApi.connections()
      .then(items => {
        if (aliveRef.current && connectionsEpochRef.current === epoch) {
          setConnections(items.filter(item => item.kind === 'mysql' || item.kind === 'postgres'))
        }
      })
      .catch(failure => {
        if (aliveRef.current && connectionsEpochRef.current === epoch) {
          setConnectionsError(integratedError(failure))
        }
      })
      .finally(() => {
        if (aliveRef.current && connectionsEpochRef.current === epoch) setConnectionsLoading(false)
      })
  }, [])

  useEffect(() => { loadConnections() }, [loadConnections])

  const formError = mode === 'new' ? validateNewSourceForm(form) : ''
  const sourceCandidate: SourceReference | null = useMemo(() => {
    if (mode === 'existing') return connectionId ? { connection_id: connectionId } : null
    return formError === '' ? newSourceReference(form) : null
  }, [mode, connectionId, formError, form])
  const sourceKey = sourceCandidate ? sourceReferenceKey(sourceCandidate) : ''

  // Any change of source identity invalidates the whole downstream evidence chain.
  useEffect(() => {
    deprecateStages()
    setTestMessage('')
    setResourcesError('')
    setSchemaError('')
    setEvidence(initialRegisterEvidence)
  }, [sourceKey])

  const testSource = async () => {
    if (!sourceCandidate) return
    const { epoch, signal } = beginStageRequest('test')
    setTestMessage('')
    setEvidence(beginSourceTest)
    try {
      const response = await integratedDatasetsApi.testSource(sourceCandidate, signal)
      if (!isLive(epoch)) return
      if (response.success) {
        setEvidence(current => ({ ...current, testedOk: true }))
        setTestMessage('连通测试通过，可发现源表。')
      } else {
        setTestMessage(`连通测试失败：${probeFailureLabel(response)}`)
      }
    } catch (failure) {
      if (!isLive(epoch) || signal.aborted) return
      setTestMessage(`连通测试失败：${integratedError(failure)}`)
    } finally {
      if (isLive(epoch)) setTesting(false)
    }
  }

  const discover = async () => {
    if (!sourceCandidate || !evidence.testedOk) return
    const { epoch, signal } = beginStageRequest('resources')
    setResourcesError('')
    setSchemaError('')
    setEvidence(beginDiscover)
    try {
      const response = await integratedDatasetsApi.sourceResources(sourceCandidate, signal)
      if (!isLive(epoch)) return
      setEvidence(current => ({ ...current, resources: response.items }))
    } catch (failure) {
      if (!isLive(epoch) || signal.aborted) return
      setResourcesError(`dbx 源表发现失败：${integratedError(failure)}`)
    } finally {
      if (isLive(epoch)) setLoadingResources(false)
    }
  }

  const loadSchema = async () => {
    if (!sourceCandidate || !evidence.selectedResource) return
    const resource = evidence.selectedResource
    const { epoch, signal } = beginStageRequest('schema')
    setSchemaError('')
    setEvidence(beginSchemaVerify)
    try {
      const response = await integratedDatasetsApi.sourceSchema(sourceCandidate, resource, signal)
      if (!isLive(epoch)) return
      setEvidence(current => ({
        ...current,
        schema: response,
        fields: prefillFieldConfig(response.columns, response.primary_key),
      }))
    } catch (failure) {
      if (!isLive(epoch) || signal.aborted) return
      setSchemaError(`源表结构核验失败：${integratedError(failure)}`)
    } finally {
      if (isLive(epoch)) setSchemaLoading(false)
    }
  }

  // Table-selection invalidation is explicit, not an effect on tableKey: stage
  // restarts clear selectedResource themselves, and an effect watching it would
  // abort the request that just cleared it. Selecting the same table is a no-op.
  const selectResource = (item: DiscoveredResource) => {
    const changed = !evidence.selectedResource
      || resourceKey(evidence.selectedResource) !== resourceKey(item)
    if (!changed) return
    deprecateStages()
    setSchemaError('')
    setEvidence(current => ({ ...current, selectedResource: item, schema: null, fields: [] }))
  }

  const setField = (id: string, patch: Partial<FieldConfigDraft>) => {
    setEvidence(current => ({
      ...current,
      fields: current.fields.map(field => field.id === id ? { ...field, ...patch } : field),
    }))
    setError('')
  }

  const fieldMessage = evidence.schema ? validateFieldConfig(evidence.fields) : ''
  const duplicates = useMemo(() => duplicateAliases(evidence.fields), [evidence.fields])
  const canRegister = registerReady(evidence, {
    assetName,
    sourceReady: sourceCandidate !== null,
    fieldMessage,
    saving,
  })

  const register = async () => {
    if (!sourceCandidate || !evidence.selectedResource || saving) return
    if (!assetName.trim()) { setError('请填写资产名称'); return }
    const invalidFields = validateFieldConfig(evidence.fields)
    if (invalidFields) { setError(invalidFields); return }
    setSaving(true)
    setError('')
    try {
      const asset = await integratedDatasetsApi.register(buildRegisterPayload({
        name: assetName,
        writable,
        schema_name: evidence.selectedResource.schema_name,
        table_name: evidence.selectedResource.table_name,
        source: sourceCandidate,
        fields: evidence.fields,
      }))
      onRegistered(asset)
    } catch (failure) {
      if (aliveRef.current) setError(`登记结果未确认：${integratedError(failure)}；响应丢失时资产可能已创建，请先核对资产列表再决定是否重试。`)
    } finally {
      if (aliveRef.current) setSaving(false)
    }
  }

  const selectedConnection = connections.find(item => item.id === connectionId)
  const modeButtonClass = (active: boolean) =>
    `h-7 rounded-md px-3 text-xs font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${active
      ? 'bg-card text-foreground shadow-[var(--shadow-sm)]'
      : 'text-muted-foreground hover:text-foreground'}`

  return (
    <Dialog open onOpenChange={open => { if (!open && !saving) onClose() }}>
      <DialogContent dismissible={!saving} className="flex max-h-[88vh] w-[min(96vw,1080px)] flex-col p-0">
        <DialogHeader className="mb-0 shrink-0 border-b border-border px-5 pb-3 pt-4" icon={<Database size={18} />}>
          <DialogTitle>登记集成数据集</DialogTitle>
          <DialogDescription>
            在本弹窗内直接配置新的 MySQL / PostgreSQL 源或复用已有连接：先连通测试，再由 dbx
            发现并核验源表结构，配置字段后登记；不跳转数据连接页，测试与发现不保存任何凭据。
          </DialogDescription>
        </DialogHeader>

        <main className="min-h-0 flex-1 overflow-y-auto">
          <section className="border-b border-border px-5 py-4">
            <h4 className={sectionTitleClass}>资产</h4>
            <div className="mt-2 grid gap-3 sm:grid-cols-2">
              <label className="block text-xs font-medium">
                资产名称
                <input
                  aria-label="资产名称"
                  value={assetName}
                  onChange={event => setAssetName(event.target.value)}
                  className={`${fieldInputClass} mt-1 w-full`}
                  autoFocus
                  autoComplete="off"
                />
              </label>
              <div className="flex items-end pb-1.5">
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={writable}
                    onChange={event => setWritable(event.target.checked)}
                    disabled={saving}
                  />
                  允许受控写入
                </label>
              </div>
            </div>
            <p className={sectionHintClass}>
              默认只读。写入还要求源主键、行令牌与服务端写权限；登记成功不代表当前连通或已有完整版本。
            </p>
          </section>

          <section className="border-b border-border px-5 py-4" data-testid="integrated-source-section">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <div>
                <h4 className={sectionTitleClass}>数据源</h4>
                <p className={sectionHintClass}>测试、发现与结构核验只针对本次登记，不会创建连接或保存密码。</p>
              </div>
              <div className="inline-flex rounded-lg border border-border bg-muted p-0.5" role="group" aria-label="数据源方式">
                <button type="button" aria-pressed={mode === 'new'} className={modeButtonClass(mode === 'new')} onClick={() => setMode('new')}>
                  新建数据源
                </button>
                <button type="button" aria-pressed={mode === 'existing'} className={modeButtonClass(mode === 'existing')} onClick={() => setMode('existing')} disabled={connectionsLoading}>
                  使用已有连接
                </button>
              </div>
            </div>

            {mode === 'new' ? (
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <label className="block text-xs font-medium">
                  源名称
                  <input aria-label="源名称" value={form.name} onChange={event => setForm(current => ({ ...current, name: event.target.value }))} className={`${fieldInputClass} mt-1 w-full`} autoComplete="off" disabled={saving} />
                </label>
                <div>
                  <span id="integrated-source-kind-label" className="block text-xs font-medium">数据库类型</span>
                  <Select
                    value={form.kind}
                    onValueChange={value => {
                      const kind = value as IntegratedSourceKind
                      setForm(current => ({ ...current, kind, port: portForKindChange(current.port, kind) }))
                    }}
                    disabled={saving}
                  >
                    <SelectTrigger className="mt-1 h-8 rounded-md text-sm" aria-labelledby="integrated-source-kind-label">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {(Object.keys(SOURCE_KIND_LABELS) as IntegratedSourceKind[]).map(kind => (
                        <SelectItem key={kind} value={kind}>{SOURCE_KIND_LABELS[kind]}（默认端口 {SOURCE_DEFAULT_PORTS[kind]}）</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <label className="block text-xs font-medium">
                  主机地址
                  <input aria-label="主机地址" value={form.host} onChange={event => setForm(current => ({ ...current, host: event.target.value }))} placeholder="localhost" className={`${fieldInputClass} mt-1 w-full`} autoComplete="off" disabled={saving} />
                </label>
                <label className="block text-xs font-medium">
                  端口
                  <input aria-label="端口" value={form.port} onChange={event => setForm(current => ({ ...current, port: event.target.value }))} inputMode="numeric" placeholder={String(SOURCE_DEFAULT_PORTS[form.kind])} className={`${fieldInputClass} mt-1 w-full font-mono`} autoComplete="off" disabled={saving} />
                </label>
                <label className="block text-xs font-medium">
                  数据库名
                  <input aria-label="数据库名" value={form.database} onChange={event => setForm(current => ({ ...current, database: event.target.value }))} className={`${fieldInputClass} mt-1 w-full`} autoComplete="off" disabled={saving} />
                </label>
                <label className="block text-xs font-medium">
                  用户名
                  <input aria-label="用户名" value={form.user} onChange={event => setForm(current => ({ ...current, user: event.target.value }))} className={`${fieldInputClass} mt-1 w-full`} autoComplete="off" disabled={saving} />
                </label>
                <label className="block text-xs font-medium">
                  密码
                  <input aria-label="密码" type="password" value={form.password} onChange={event => setForm(current => ({ ...current, password: event.target.value }))} className={`${fieldInputClass} mt-1 w-full`} autoComplete="new-password" disabled={saving} />
                </label>
                <details className="sm:col-span-2 lg:col-span-4">
                  <summary className="cursor-pointer text-xs font-medium text-muted-foreground">高级：dbx 服务访问地址（可选）</summary>
                  <div className="mt-2 grid gap-3 sm:grid-cols-2">
                    <label className="block text-xs font-medium">
                      dbx 访问源主机
                      <input aria-label="dbx 访问源主机" value={form.dbxHost} onChange={event => setForm(current => ({ ...current, dbxHost: event.target.value }))} placeholder="留空不覆盖" className={`${fieldInputClass} mt-1 w-full`} autoComplete="off" disabled={saving} />
                    </label>
                    <label className="block text-xs font-medium">
                      dbx 访问源端口
                      <input aria-label="dbx 访问源端口" value={form.dbxPort} onChange={event => setForm(current => ({ ...current, dbxPort: event.target.value }))} inputMode="numeric" placeholder="留空不覆盖" className={`${fieldInputClass} mt-1 w-full font-mono`} autoComplete="off" disabled={saving} />
                    </label>
                  </div>
                  <p className={sectionHintClass}>当 dbx 元数据服务经不同网络路径访问该源（例如容器内经 host.docker.internal 访问宿主机数据库）时填写；两项必须成对填写，留空则复用上方主机与端口。</p>
                </details>
              </div>
            ) : (
              <div className="space-y-2">
                <div className="grid gap-3 sm:grid-cols-2">
                  <div>
                    <span id="integrated-connection-label" className="block text-xs font-medium">已有连接</span>
                    <Select
                      value={connectionId}
                      onValueChange={setConnectionId}
                      disabled={connectionsLoading || connections.length === 0 || saving}
                    >
                      <SelectTrigger className="mt-1" aria-labelledby="integrated-connection-label">
                        <SelectValue placeholder={connectionsLoading ? '加载连接中…' : connections.length === 0 ? '暂无 MySQL / PostgreSQL 连接' : '选择 MySQL / PostgreSQL 连接'} />
                      </SelectTrigger>
                      <SelectContent>
                        {connections.map(item => (
                          <SelectItem key={item.id} value={item.id}>
                            {item.name} · {SOURCE_KIND_LABELS[item.kind as IntegratedSourceKind] ?? item.kind} · {connectionStatusLabel(item.status)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  {selectedConnection && (
                    <div className="flex items-end pb-1.5" data-testid="integrated-connection-status">
                      <Badge variant={CONNECTION_STATUS_BADGE[selectedConnection.status] ?? 'warning'}>
                        {connectionStatusLabel(selectedConnection.status)}
                      </Badge>
                    </div>
                  )}
                </div>
                {connectionsError && (
                  <Alert variant="warning" role="alert" data-testid="integrated-connections-error">
                    加载已有连接失败：{connectionsError}
                    <Button type="button" variant="link" size="sm" className="ml-2 h-auto px-1" onClick={loadConnections} disabled={connectionsLoading}>重试</Button>
                    <span className="mt-1 block text-xs opacity-80">仍可切回「新建数据源」直接完成登记。</span>
                  </Alert>
                )}
                {!connectionsLoading && !connectionsError && connections.length === 0 && (
                  <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground" data-testid="integrated-connection-empty-hint">
                    <span>没有可复用的 MySQL / PostgreSQL 连接；请切回「新建数据源」在下方直接填写源参数。</span>
                    <Button
                      type="button"
                      variant="link"
                      size="sm"
                      className="h-auto px-1 py-0"
                      onClick={() => {
                        onClose()
                        navigate('/data/pipelines/connections')
                      }}
                    >
                      管理数据连接
                    </Button>
                  </div>
                )}
              </div>
            )}

            <div className="mt-3 flex flex-wrap items-center gap-2">
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={testSource}
                disabled={!sourceCandidate || testing || saving}
                data-testid="integrated-test-source"
              >
                {testing ? <Loader2 size={12} className="animate-spin" /> : null}
                {mode === 'existing' ? '测试连通（重测）' : '测试连通'}
              </Button>
              {mode === 'new' && formError && !testing && (
                <span className="text-xs text-muted-foreground">补全源参数后可测试：{formError}</span>
              )}
              {mode === 'existing' && !connectionId && !testing && (
                <span className="text-xs text-muted-foreground">选择连接后可测试连通。</span>
              )}
              {testMessage && (
                <span
                  className={`text-xs ${evidence.testedOk ? 'text-[var(--color-success)]' : 'text-[var(--color-danger)]'}`}
                  role="status"
                  data-testid="integrated-test-source-result"
                >
                  {testMessage}
                </span>
              )}
            </div>
          </section>

          <section className="border-b border-border px-5 py-4">
            <div className="mb-2">
              <h4 className={sectionTitleClass}>源表发现</h4>
              <p className={sectionHintClass}>发现结果只来自真实 dbx；失败不会回退到 native 元数据，也不会生成推测表清单。</p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={discover}
                disabled={!sourceCandidate || !evidence.testedOk || loadingResources || saving}
                data-testid="integrated-discover-resources"
              >
                {loadingResources ? <Loader2 size={12} className="animate-spin" /> : null}发现源表
              </Button>
              {!evidence.testedOk && (
                <span className="text-xs text-muted-foreground">需先通过连通测试。</span>
              )}
              {evidence.selectedResource && (
                <span className="font-mono text-xs text-muted-foreground" data-testid="integrated-selected-resource">
                  已选源表：{resourceKey(evidence.selectedResource)}
                </span>
              )}
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={loadSchema}
                disabled={!sourceCandidate || !evidence.testedOk || !evidence.selectedResource || schemaLoading || saving}
                data-testid="integrated-load-schema"
              >
                {schemaLoading ? <Loader2 size={12} className="animate-spin" /> : null}
                {evidence.schema ? '重新核验结构' : '读取并核验字段'}
              </Button>
            </div>
            {resourcesError && (
              <Alert variant="danger" role="alert" className="mt-2">{resourcesError}</Alert>
            )}
            {evidence.resources.length > 0 && (
              <div data-testid="integrated-discovered-resources" aria-label="发现的源表" className="mt-2 max-h-40 overflow-y-auto rounded-lg border border-border p-1.5">
                {evidence.resources.map(item => {
                  const active = evidence.selectedResource
                    ? resourceKey(evidence.selectedResource) === resourceKey(item)
                    : false
                  return (
                    <button
                      key={resourceKey(item)}
                      type="button"
                      onClick={() => selectResource(item)}
                      className={`block w-full rounded px-2 py-1 text-left font-mono text-xs transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${active ? 'bg-brand-soft text-brand-ink' : 'hover:bg-muted'}`}
                      aria-pressed={active}
                    >
                      {resourceKey(item)}
                    </button>
                  )
                })}
              </div>
            )}
            {!loadingResources && evidence.testedOk && !resourcesError && evidence.resources.length === 0 && !evidence.selectedResource && (
              <p className="mt-2 text-xs text-muted-foreground">尚未发现源表；点击「发现源表」读取 dbx 真实清单。</p>
            )}
            {schemaError && (
              <Alert variant="danger" role="alert" className="mt-2">{schemaError}</Alert>
            )}
          </section>

          {evidence.schema && evidence.fields.length > 0 && (
            <section className="px-5 py-4" data-testid="integrated-field-config">
              <div className="mb-2 flex flex-wrap items-end justify-between gap-2">
                <div>
                  <h4 className={sectionTitleClass}>字段配置</h4>
                  <p className={sectionHintClass}>
                    字段标识是平台内使用的稳定逻辑别名（小写字母开头，仅小写字母、数字、下划线，最长 {FIELD_NAME_MAX} 字符）；显示名用于界面展示（最长 {DISPLAY_NAME_MAX} 字符）。登记不会在外部源表执行任何 DDL，源字段、类型、可空与主键以核验到的结构为准，只读不可改。
                  </p>
                </div>
                <span className="break-all font-mono text-[10px] text-[var(--color-text-tertiary)]">结构指纹 {evidence.schema.schema_fingerprint}</span>
              </div>
              <div className="overflow-x-auto rounded-xl border border-border">
                <table className="w-full min-w-[860px] text-xs">
                  <thead className="border-b border-border bg-muted text-muted-foreground">
                    <tr>
                      <th className="w-16 px-3 py-2 text-center font-medium">纳入</th>
                      <th className="px-3 py-2 text-left font-medium">源字段</th>
                      <th className="px-3 py-2 text-left font-medium">显示名</th>
                      <th className="px-3 py-2 text-left font-medium">字段标识</th>
                      <th className="w-44 px-3 py-2 text-left font-medium">源类型</th>
                      <th className="w-16 px-3 py-2 text-center font-medium">非空</th>
                      <th className="w-16 px-3 py-2 text-center font-medium"><span className="inline-flex items-center gap-1"><KeyRound size={10} className="text-[var(--color-warning)]" />主键</span></th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {evidence.fields.map(field => {
                      const duplicated = duplicates.has(field.name.trim())
                      return (
                        <tr key={field.id} className="hover:bg-muted">
                          <td className="p-1.5 text-center">
                            <input
                              type="checkbox"
                              checked={field.included || field.primary_key}
                              disabled={field.primary_key || saving}
                              onChange={event => setField(field.id, { included: event.target.checked })}
                              aria-label={`纳入字段 ${field.source_key}`}
                            />
                          </td>
                          <td className="px-3 py-2 font-mono text-muted-foreground">{field.source_key}</td>
                          <td className="p-1.5">
                            <input
                              value={field.display_name}
                              onChange={event => setField(field.id, { display_name: event.target.value })}
                              className={fieldInputClass}
                              aria-label={`${field.source_key} 显示名`}
                              autoComplete="off"
                              disabled={saving}
                            />
                          </td>
                          <td className="p-1.5">
                            <div>
                              <input
                                value={field.name}
                                onChange={event => setField(field.id, { name: event.target.value })}
                                autoCapitalize="none"
                                autoCorrect="off"
                                spellCheck={false}
                                title="字段标识必须唯一；以小写字母开头，仅允许小写字母、数字和下划线"
                                aria-invalid={duplicated || undefined}
                                aria-describedby={duplicated ? `integrated-alias-error-${field.id}` : undefined}
                                className={`${fieldInputClass} font-mono ${duplicated ? 'border-[var(--color-danger)] bg-[var(--color-danger-bg)]' : ''}`}
                                aria-label={`${field.source_key} 字段标识`}
                                disabled={saving}
                              />
                              {duplicated && (
                                <p id={`integrated-alias-error-${field.id}`} className="mt-1 text-[10px] text-[var(--color-danger)]" role="alert">
                                  字段标识重复，请为每一列使用唯一标识
                                </p>
                              )}
                            </div>
                          </td>
                          <td className="px-3 py-2 font-mono text-muted-foreground">{field.source_type}</td>
                          <td className="px-3 py-2 text-center text-muted-foreground">{field.nullable ? '—' : '是'}</td>
                          <td className="px-3 py-2 text-center">
                            <input type="checkbox" checked={field.primary_key} disabled aria-label={`${field.source_key} 主键`} />
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
              {fieldMessage && (
                <p className="mt-2 text-xs text-[var(--color-danger)]" role="alert">{fieldMessage}</p>
              )}
            </section>
          )}
        </main>

        {error && (
          <div className="mx-5 mb-3 shrink-0">
            <Alert variant="danger" role="alert" data-testid="integrated-register-error">{error}</Alert>
          </div>
        )}

        <footer className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-t border-border bg-muted px-5 py-3">
          <p className="text-xs text-muted-foreground">确认登记前必须完成连通测试与源表结构核验；主键字段始终纳入。</p>
          <div className="flex gap-2">
            <Button type="button" variant="outline" size="sm" onClick={onClose} disabled={saving}>取消</Button>
            <Button
              type="button"
              size="sm"
              onClick={register}
              disabled={!canRegister}
              data-testid="integrated-register-submit"
            >
              {saving ? <Loader2 size={12} className="animate-spin" /> : null}确认登记
            </Button>
          </div>
        </footer>
      </DialogContent>
    </Dialog>
  )
}
