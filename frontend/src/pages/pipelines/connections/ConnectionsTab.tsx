import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { Plus, Database, FileUp, Globe, X, Loader2, RefreshCw, Table2 } from 'lucide-react'
import { apiClientV2 } from '@/api/client'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'

interface Connection {
  id: string
  name: string
  kind: string
  status: string
}

const KIND_META: Record<string, { icon: React.ReactNode; label: string }> = {
  file:     { icon: <FileUp size={14} />,   label: '文件上传' },
  mysql:    { icon: <Database size={14} />, label: 'MySQL' },
  postgres: { icon: <Database size={14} />, label: 'PostgreSQL' },
  mongo:    { icon: <Database size={14} />, label: 'MongoDB' },
  rest:     { icon: <Globe size={14} />,    label: 'REST API' },
}

const STATUS_STYLE: Record<string, string> = {
  active:   'text-[var(--color-success)] bg-[var(--color-success-bg)] border-[color-mix(in_srgb,var(--color-success)_35%,transparent)]',
  inactive: 'text-[var(--color-text-tertiary)] bg-muted border-border',
  error:    'text-[var(--color-danger)] bg-[var(--color-danger-bg)] border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)]',
}

const STATUS_LABEL: Record<string, string> = {
  active: '活跃', inactive: '未激活', error: '错误',
}

// 新建连接仅支持外部系统；文件类数据统一在「数据资产湖 → 原始数据集」上传与维护
const CREATABLE_KINDS = ['mysql', 'postgres', 'mongo', 'rest'] as const

const KIND_CONFIG_FIELDS: Record<string, { key: string; label: string; placeholder: string; type?: string }[]> = {
  mysql:    [
    { key: 'host', label: '主机', placeholder: 'localhost' },
    { key: 'port', label: '端口', placeholder: '3306' },
    { key: 'database', label: '数据库名', placeholder: 'mydb' },
    { key: 'user', label: '用户名', placeholder: 'root' },
    { key: 'password', label: '密码', placeholder: '••••••', type: 'password' },
  ],
  postgres: [
    { key: 'host', label: '主机', placeholder: 'localhost' },
    { key: 'port', label: '端口', placeholder: '5432' },
    { key: 'database', label: '数据库名', placeholder: 'mydb' },
    { key: 'user', label: '用户名', placeholder: 'postgres' },
    { key: 'password', label: '密码', placeholder: '••••••', type: 'password' },
  ],
  mongo:    [
    { key: 'uri', label: '连接字符串', placeholder: 'mongodb://localhost:27017/mydb' },
  ],
  rest:     [
    { key: 'url', label: 'API URL', placeholder: 'https://api.example.com/data' },
    { key: 'headers', label: '请求头 (JSON)', placeholder: '{"Authorization": "Bearer token"}' },
  ],
  file: [],
}

// 拦截器 reject 的是已解包的响应体；HTTPException 的 detail 可能是字符串，
// 也可能是结构化对象（如连接删除 409 的 {message, datasets}）
function errorDetail(e: unknown, fallback: string): string {
  const err = e as { detail?: string | { message?: string }; message?: string }
  const payload = err?.detail
  if (typeof payload === 'string' && payload) return payload
  const message = typeof payload === 'object' && payload ? payload.message : ''
  return message || err?.message || fallback
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

  const [formName, setFormName] = useState('')
  const [formKind, setFormKind] = useState('mysql')
  const [formConfig, setFormConfig] = useState<Record<string, string>>({})
  const [formSyncMode, setFormSyncMode] = useState<'snapshot' | 'append'>('snapshot')

  const loadConnections = () => {
    setLoading(true)
    apiClientV2.get('/connections')
      .then((res: unknown) => setConnections(Array.isArray(res) ? res : ((res as { data?: Connection[] })?.data ?? [])))
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
      await apiClientV2.post('/connections', {
        name: formName, kind: formKind,
        config: { ...formConfig, sync_mode: formSyncMode },
      })
      setShowForm(false)
      resetForm()
      loadConnections()
    } catch (e: unknown) {
      setFormError(errorDetail(e, '保存失败'))
    } finally {
      setSaving(false)
    }
  }

  const handleTest = async (id: string) => {
    setTesting(id)
    setTestResult(null)
    try {
      const res = await apiClientV2.post<{ success: boolean; detail?: string }>(`/connections/${id}/test`, {})
      setTestResult({ id, ok: !!res.success, detail: res.detail })
      loadConnections()
    } catch (e: unknown) {
      const err = e as { detail?: string }
      setTestResult({ id, ok: false, detail: err?.detail || '测试失败' })
    } finally {
      setTesting(null)
    }
  }

  const handleSync = async (id: string) => {
    setSyncing(id)
    setSyncResult(null)
    try {
      const result = await apiClientV2.post<{ status?: string; rows?: number }>(
        `/connections/${id}/sync`,
        {},
      )
      setSyncResult({
        id,
        ok: true,
        detail: result.status === 'sync_triggered'
          ? '同步任务已提交，完成后请刷新查看连接状态'
          : `同步成功，共 ${result.rows ?? 0} 行`,
      })
      loadConnections()
    } catch (e: unknown) {
      setSyncResult({ id, ok: false, detail: errorDetail(e, '同步失败') })
    } finally {
      setSyncing(null)
    }
  }

  const handleDelete = async () => {
    if (!deleteTarget) return
    setDeleting(true)
    setDeleteError('')
    try {
      await apiClientV2.delete(`/connections/${deleteTarget.id}`)
      setDeleteTarget(null)
      loadConnections()
    } catch (e: unknown) {
      const err = e as { detail?: string | { message?: string; datasets?: { name?: string }[] } }
      const payload = err?.detail
      const names = (typeof payload === 'object' ? payload?.datasets : [])
        ?.map(d => d?.name).filter(Boolean) ?? []
      const message = errorDetail(e, '删除失败，请稍后重试')
      setDeleteError(
        names.length ? `${message}（${names.join('、')}）` : message)
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
            <label className="text-xs text-muted-foreground mb-2 block">连接类型</label>
            <div className="flex gap-2 flex-wrap">
              {CREATABLE_KINDS.map(k => {
                const m = KIND_META[k]
                return (
                  <button
                    key={k}
                    type="button"
                    onClick={() => { setFormKind(k); setFormConfig({}) }}
                    className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs border transition-colors
                      ${formKind === k ? 'bg-[var(--color-bg-overlay)] text-[var(--color-text-inverse)] border-border' : 'border-border text-muted-foreground hover:bg-muted'}`}
                  >
                    {m.icon} {m.label}
                  </button>
                )
              })}
            </div>
          </div>

          <div className="space-y-3">
            {KIND_CONFIG_FIELDS[formKind]?.map(f => (
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
            <label className="text-xs text-muted-foreground mb-2 block">同步模式</label>
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
            const meta = KIND_META[c.kind] ?? KIND_META.file
            const statusStyle = STATUS_STYLE[c.status] ?? STATUS_STYLE.inactive
            const statusLabel = STATUS_LABEL[c.status] ?? c.status
            return (
              <div key={c.id} className="p-4">
                <div className="flex items-center gap-3">
                  <div className="w-8 h-8 bg-muted rounded-lg flex items-center justify-center text-muted-foreground">
                    {meta.icon}
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="font-medium text-sm truncate">{c.name}</p>
                    <p className="text-xs text-[var(--color-text-tertiary)]">{meta.label}</p>
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
                  <button
                    onClick={() => { setDeleteTarget(c); setDeleteError('') }}
                    className="text-[var(--color-text-tertiary)] hover:text-[var(--color-danger)] text-xs px-1 transition-colors"
                  >
                    删除
                  </button>
                </div>
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
