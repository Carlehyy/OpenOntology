import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { Plus, Database, FileUp, Globe, X, Loader2, RefreshCw, Table2, KeyRound } from 'lucide-react'
import {
  connectionsApi,
  connectionErrorDetail,
  CONNECTION_CONFIG_FIELDS,
  CONNECTION_KIND_LABELS,
  CREATABLE_CONNECTION_KINDS,
  decommissionBlockerSummary,
  rotationConfigFromInput,
  type Connection,
  type ConnectionDecommissionBlockers,
} from '@/api/v2/connections'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'

const KIND_ICONS: Record<string, React.ReactNode> = {
  file: <FileUp size={14} />,
  mysql: <Database size={14} />,
  postgres: <Database size={14} />,
  mongo: <Database size={14} />,
  rest: <Globe size={14} />,
}

const STATUS_STYLE: Record<string, string> = {
  active:   'text-[var(--color-success)] bg-[var(--color-success-bg)] border-[color-mix(in_srgb,var(--color-success)_35%,transparent)]',
  inactive: 'text-[var(--color-text-tertiary)] bg-muted border-border',
  error:    'text-[var(--color-danger)] bg-[var(--color-danger-bg)] border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)]',
}

const STATUS_LABEL: Record<string, string> = {
  active: '活跃', inactive: '未激活', error: '错误',
}

export default function ConnectionsTab() {
  const navigate = useNavigate()
  const [connections, setConnections] = useState<Connection[]>([])
  const [loading, setLoading] = useState(true)
  const [showForm, setShowForm] = useState(false)
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState('')
  const [syncing, setSyncing] = useState<string | null>(null)
  const [syncResult, setSyncResult] = useState<{ id: string; ok: boolean; detail: string } | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<Connection | null>(null)
  const [deleteError, setDeleteError] = useState('')
  const [deleting, setDeleting] = useState(false)
  const [testResult, setTestResult] = useState<{ id: string; ok: boolean; detail?: string } | null>(null)
  const [testing, setTesting] = useState<string | null>(null)

  // 凭据轮换：只针对单个连接展开，字段全部留空 = 保留服务端现值
  const [rotatingId, setRotatingId] = useState<string | null>(null)
  const [rotateValues, setRotateValues] = useState<Record<string, string>>({})
  const [rotating, setRotating] = useState(false)
  const [rotateResult, setRotateResult] = useState<{ id: string; ok: boolean; detail: string } | null>(null)

  const [formName, setFormName] = useState('')
  const [formKind, setFormKind] = useState('mysql')
  const [formConfig, setFormConfig] = useState<Record<string, string>>({})
  const [formSyncMode, setFormSyncMode] = useState<'snapshot' | 'append'>('snapshot')

  const loadConnections = () => {
    setLoading(true)
    connectionsApi.list()
      .then(res => setConnections(Array.isArray(res) ? res : []))
      .catch(() => setConnections([]))
      .finally(() => setLoading(false))
  }

  useEffect(() => { loadConnections() }, [])

  const resetForm = () => {
    setFormName('')
    setFormKind('mysql')
    setFormConfig({})
    setFormSyncMode('snapshot')
    setFormError('')
  }

  const handleSave = async () => {
    if (!formName.trim()) { setFormError('请填写连接名称'); return }
    if (connections.some(c => c.name.trim() === formName.trim())) {
      setFormError(`已存在同名连接「${formName.trim()}」，请更换连接名称`)
      return
    }
    if (formKind === 'rest') {
      if (!formConfig.url?.trim()) { setFormError('请填写 REST API URL'); return }
      try {
        const headers = formConfig.headers?.trim() ? JSON.parse(formConfig.headers) : {}
        if (!headers || Array.isArray(headers) || typeof headers !== 'object') {
          setFormError('请求头必须是 JSON 对象')
          return
        }
      } catch {
        setFormError('请求头不是合法 JSON')
        return
      }
    }
    setSaving(true)
    setFormError('')
    try {
      await connectionsApi.create({
        name: formName, kind: formKind,
        config: { ...formConfig, sync_mode: formSyncMode },
      })
      setShowForm(false)
      resetForm()
      loadConnections()
    } catch (e: unknown) {
      setFormError(connectionErrorDetail(e, '保存失败'))
    } finally {
      setSaving(false)
    }
  }

  const handleTest = async (id: string) => {
    setTesting(id)
    setTestResult(null)
    try {
      const res = await connectionsApi.test(id)
      setTestResult({ id, ok: !!res.success, detail: res.detail })
      loadConnections()
    } catch (e: unknown) {
      setTestResult({ id, ok: false, detail: connectionErrorDetail(e, '测试失败') })
    } finally {
      setTesting(null)
    }
  }

  // 空字段不提交，保留服务端现值。
  const handleRotate = async (connection: Connection) => {
    const config = rotationConfigFromInput(rotateValues)
    if (Object.keys(config).length === 0) {
      setRotateResult({ id: connection.id, ok: false, detail: '请至少填写一个要更新的配置项；留空字段保持原值' })
      return
    }
    setRotating(true)
    setRotateResult(null)
    try {
      try {
        await connectionsApi.rotateConfig(connection.id, config)
      } catch (e: unknown) {
        setRotateResult({ id: connection.id, ok: false, detail: `配置更新失败：${connectionErrorDetail(e, '请稍后重试')}` })
        return
      }
      loadConnections()
      try {
        const tested = await connectionsApi.test(connection.id)
        setRotateResult({
          id: connection.id,
          ok: !!tested.success,
          detail: tested.success
            ? '已更新配置并通过连通测试'
            : `配置已更新，但连通测试失败${tested.detail ? `：${tested.detail}` : ''}`,
        })
        loadConnections()
      } catch (e: unknown) {
        setRotateResult({
          id: connection.id,
          ok: false,
          detail: `配置已更新，但未能取得连通测试结果（${connectionErrorDetail(e, '网络错误')}）；请刷新连接状态后重新测试连通。`,
        })
        loadConnections()
      }
    } finally {
      setRotating(false)
    }
  }

  const handleSync = async (id: string) => {
    setSyncing(id)
    setSyncResult(null)
    try {
      const result = await connectionsApi.triggerSync(id)
      setSyncResult({
        id,
        ok: true,
        detail: result.status === 'sync_triggered'
          ? '同步任务已提交，完成后请刷新查看连接状态'
          : `同步成功，共 ${result.rows ?? 0} 行`,
      })
      loadConnections()
    } catch (e: unknown) {
      setSyncResult({ id, ok: false, detail: connectionErrorDetail(e, '同步失败') })
    } finally {
      setSyncing(null)
    }
  }

  const handleDelete = async () => {
    if (!deleteTarget) return
    setDeleting(true)
    setDeleteError('')
    try {
      await connectionsApi.remove(deleteTarget.id)
      setDeleteTarget(null)
      loadConnections()
    } catch (e: unknown) {
      const err = e as { detail?: string | ConnectionDecommissionBlockers }
      const payload = err?.detail
      if (payload && typeof payload === 'object') {
        // 受控注销 409：结构化依赖明细（数据集/集成资产/历史/映射/任务）
        setDeleteError(decommissionBlockerSummary(payload))
      } else {
        setDeleteError(connectionErrorDetail(e, '删除失败，请稍后重试'))
      }
    } finally {
      setDeleting(false)
    }
  }

  if (loading) return <div className="text-[var(--color-text-tertiary)] text-sm p-4">加载中...</div>

  return (
    <div className="space-y-4">
      <div className="flex justify-between items-center">
        <p className="text-xs text-[var(--color-text-tertiary)]">管理外部数据源连接（数据库 / API），作为流水线连接器的数据来源</p>
        <button
          onClick={() => { resetForm(); setShowForm(true) }}
          className="flex items-center gap-2 bg-[var(--color-bg-overlay)] text-[var(--color-text-inverse)] px-4 py-2 rounded-lg text-sm"
        >
          <Plus size={14} /> 新建连接
        </button>
      </div>

      {/* 文件类数据引导 */}
      <div className="flex items-center gap-2 px-4 py-2.5 bg-[var(--color-info-bg)] border border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] rounded-lg text-xs text-[var(--color-info)]">
        <Table2 size={14} className="shrink-0" />
        <span className="flex-1">
          Excel / CSV / JSON / 文档等<b>文件类数据</b>无需创建连接：直接到资产湖上传为原始数据集，后续在同一数据集上追加新版本即可完成数据更新
        </span>
        <button
          onClick={() => navigate('/data/structured?tab=raw')}
          className="px-2.5 py-1 bg-card border border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] rounded-lg hover:bg-[var(--color-info-bg)] shrink-0"
        >
          去资产湖上传
        </button>
      </div>

      {showForm && (
        <div className="border rounded-xl p-5 bg-card space-y-4">
          <div className="flex justify-between items-center">
            <h3 className="font-medium text-sm">新建连接</h3>
            <button onClick={() => { setShowForm(false); resetForm() }} className="text-[var(--color-text-tertiary)] hover:text-foreground">
              <X size={16} />
            </button>
          </div>

          <div>
            <label className="text-xs text-muted-foreground mb-1 block">连接名称 *</label>
            <input
              value={formName}
              onChange={e => setFormName(e.target.value)}
              placeholder="例：ERP 订单数据库"
              className="w-full border rounded-lg px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </div>

          <div>
            <label className="text-xs text-muted-foreground mb-2">连接类型</label>
            <div className="flex gap-2 flex-wrap">
              {CREATABLE_CONNECTION_KINDS.map(k => (
                <button
                  key={k}
                  type="button"
                  onClick={() => { setFormKind(k); setFormConfig({}) }}
                  className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs border transition-colors
                    ${formKind === k ? 'bg-[var(--color-bg-overlay)] text-[var(--color-text-inverse)] border-border' : 'border-border text-muted-foreground hover:bg-muted'}`}
                >
                  {KIND_ICONS[k]} {CONNECTION_KIND_LABELS[k] ?? k}
                </button>
              ))}
            </div>
          </div>

          <div className="space-y-3">
            {CONNECTION_CONFIG_FIELDS[formKind]?.map(f => (
              <div key={f.key}>
                <label className="text-xs text-muted-foreground mb-1 block">{f.label}</label>
                <input
                  type={f.type || 'text'}
                  value={formConfig[f.key] || ''}
                  onChange={e => setFormConfig(p => ({ ...p, [f.key]: e.target.value }))}
                  placeholder={f.placeholder}
                  className="w-full border rounded-lg px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                />
              </div>
            ))}
          </div>

          <div>
            <label className="text-xs text-muted-foreground mb-2">同步模式</label>
            <div className="flex gap-4">
              {(['snapshot', 'append'] as const).map(m => (
                <label key={m} className="flex items-center gap-2 text-sm cursor-pointer">
                  <input
                    type="radio"
                    name="sync_mode"
                    value={m}
                    checked={formSyncMode === m}
                    onChange={() => setFormSyncMode(m)}
                    className="accent-black"
                  />
                  <span>{m === 'snapshot' ? 'SNAPSHOT（全量覆盖）' : 'APPEND（增量追加）'}</span>
                </label>
              ))}
            </div>
          </div>

          {formError && <p className="text-[var(--color-danger)] text-xs">{formError}</p>}

          <div className="flex gap-2 justify-end">
            <button onClick={() => { setShowForm(false); resetForm() }} className="px-4 py-2 text-sm border rounded-lg hover:bg-muted">
              取消
            </button>
            <button
              onClick={handleSave}
              disabled={saving}
              className="flex items-center gap-2 px-4 py-2 text-sm bg-[var(--color-bg-overlay)] text-[var(--color-text-inverse)] rounded-lg disabled:opacity-50"
            >
              {saving && <Loader2 size={13} className="animate-spin" />}
              {saving ? '保存中...' : '保存'}
            </button>
          </div>
        </div>
      )}

      {connections.length === 0 ? (
        <div className="border-2 border-dashed rounded-xl p-10 text-center text-[var(--color-text-tertiary)] space-y-2">
          <Database size={28} className="mx-auto opacity-30" />
          <p className="text-sm">暂无数据连接</p>
          <p className="text-xs">点击「新建连接」添加数据源</p>
        </div>
      ) : (
        <div className="border rounded-xl divide-y overflow-hidden">
          {connections.map(c => {
            const icon = KIND_ICONS[c.kind] ?? KIND_ICONS.file
            const statusStyle = STATUS_STYLE[c.status] ?? STATUS_STYLE.inactive
            const statusLabel = STATUS_LABEL[c.status] ?? c.status
            const kindLabel = CONNECTION_KIND_LABELS[c.kind] ?? c.kind
            const rotateFields = CONNECTION_CONFIG_FIELDS[c.kind] ?? []
            const rotatingThis = rotatingId === c.id
            return (
              <div key={c.id} className="p-4">
                <div className="flex items-center gap-3">
                  <div className="w-8 h-8 bg-muted rounded-lg flex items-center justify-center text-muted-foreground">
                    {icon}
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="font-medium text-sm truncate">{c.name}</p>
                    <p className="text-xs text-[var(--color-text-tertiary)]">{kindLabel}</p>
                  </div>
                  <span className={`text-xs font-medium px-2 py-0.5 rounded border ${statusStyle}`}>
                    {statusLabel}
                  </span>
                  <button
                    onClick={() => handleTest(c.id)}
                    disabled={testing === c.id}
                    className="flex items-center gap-1 text-xs px-2.5 py-1.5 border rounded-lg hover:bg-muted disabled:opacity-50 transition-colors"
                    title="测试连接是否可用"
                  >
                    {testing === c.id ? <Loader2 size={11} className="animate-spin" /> : null}
                    测试
                  </button>
                  <button
                    onClick={() => handleSync(c.id)}
                    disabled={syncing === c.id}
                    className="flex items-center gap-1 text-xs px-2.5 py-1.5 border rounded-lg hover:bg-muted disabled:opacity-50 transition-colors"
                    title="立即拉取一次数据到资产湖"
                  >
                    <RefreshCw size={11} className={syncing === c.id ? 'animate-spin' : ''} />
                    同步
                  </button>
                  {rotateFields.length > 0 && (
                    <button
                      onClick={() => {
                        setRotatingId(rotatingThis ? null : c.id)
                        setRotateValues({})
                        setRotateResult(null)
                      }}
                      className="flex items-center gap-1 text-xs px-2.5 py-1.5 border rounded-lg hover:bg-muted transition-colors"
                      title="轮换凭据 / 更新连接配置；配置不会被回显"
                    >
                      <KeyRound size={11} />
                      轮换凭据
                    </button>
                  )}
                  <button
                    onClick={() => { setDeleteTarget(c); setDeleteError('') }}
                    className="text-[var(--color-text-tertiary)] hover:text-[var(--color-danger)] text-xs px-1 transition-colors"
                  >
                    删除
                  </button>
                </div>

                {/* 凭据轮换面板：全部留空 = 保留现值；服务端不回填任何配置 */}
                {rotatingThis && (
                  <div className="mt-3 ml-11 rounded-lg border border-border bg-muted/50 p-3 space-y-2" data-testid="connection-rotate-panel">
                    <p className="text-xs text-muted-foreground">
                      轮换「{c.name}」的凭据/配置：只提交填写的字段，留空保持原值；保存后会立即重新测试连通。既有配置不会回显到表单。
                    </p>
                    <div className="grid gap-2 sm:grid-cols-2">
                      {rotateFields.map(f => (
                        <label key={f.key} className="block text-xs">
                          {f.label}
                          <input
                            type={f.type || 'text'}
                            aria-label={`轮换 ${c.name} ${f.label}`}
                            value={rotateValues[f.key] || ''}
                            onChange={e => setRotateValues(prev => ({ ...prev, [f.key]: e.target.value }))}
                            placeholder={f.key === 'password' ? '输入新密码' : f.placeholder}
                            autoComplete="new-password"
                            className="mt-1 w-full border rounded-lg px-3 py-2 text-sm bg-card focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          />
                        </label>
                      ))}
                    </div>
                    <div className="flex items-center gap-2">
                      <button
                        onClick={() => handleRotate(c)}
                        disabled={rotating}
                        className="flex items-center gap-1 px-3 py-1.5 text-xs bg-[var(--color-bg-overlay)] text-[var(--color-text-inverse)] rounded-lg disabled:opacity-50"
                      >
                        {rotating && <Loader2 size={11} className="animate-spin" />}
                        保存并测试
                      </button>
                      <button
                        onClick={() => { setRotatingId(null); setRotateValues({}); setRotateResult(null) }}
                        className="px-3 py-1.5 text-xs border rounded-lg hover:bg-muted"
                      >
                        取消
                      </button>
                    </div>
                    {rotateResult?.id === c.id && (
                      <p className={`text-xs ${rotateResult.ok ? 'text-[var(--color-success)]' : 'text-[var(--color-danger)]'}`}>
                        {rotateResult.ok ? '✓ ' : '✗ '}{rotateResult.detail}
                      </p>
                    )}
                  </div>
                )}

                {testResult?.id === c.id && (
                  <p className={`text-xs mt-2 ml-11 ${testResult.ok ? 'text-[var(--color-success)]' : 'text-[var(--color-danger)]'}`}>
                    {testResult.ok ? '✓ 连接可用' : `✗ 连接失败${testResult.detail ? `：${testResult.detail}` : ''}`}
                  </p>
                )}
                {syncResult?.id === c.id && (
                  <p className={`text-xs mt-2 ml-11 flex items-center gap-2 ${syncResult.ok ? 'text-[var(--color-success)]' : 'text-[var(--color-danger)]'}`}>
                    <span>{syncResult.ok ? `✓ ${syncResult.detail}` : `✗ ${syncResult.detail}`}</span>
                    {syncResult.ok && (
                      <button
                        onClick={() => navigate('/data/pipelines/datasets')}
                        className="underline underline-offset-2 hover:opacity-80"
                        title="连接同步数据集在「数据流水线 → 数据集」中查看"
                      >
                        查看数据集
                      </button>
                    )}
                  </p>
                )}
              </div>
            )
          })}
        </div>
      )}

      <ConfirmDialog
        open={!!deleteTarget}
        title="删除连接"
        description={
          <>
            <p>确认删除连接「{deleteTarget?.name}」？依赖该连接的同步任务将无法执行。</p>
            <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">
              受控注销要求：零数据集引用、零集成资产、零历史、零本体映射绑定、零数据任务；历史与审计记录不会被删除。
            </p>
            {deleteError && (
              <p className="mt-2 font-medium" role="alert">{deleteError}</p>
            )}
          </>
        }
        confirmText="确认删除"
        variant="danger"
        loading={deleting}
        onConfirm={handleDelete}
        onClose={() => { setDeleteTarget(null); setDeleteError('') }}
      />
    </div>
  )
}
