import { useState, useCallback, useEffect, useRef } from 'react'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useForm } from 'react-hook-form'
import { useTranslation } from 'react-i18next'
import {
  Plus, Trash2, TestTube2, Pencil, X, Loader2, CheckCircle2, XCircle,
  Star, Search, Upload, Download, Settings2, FileClock, Cpu,
} from 'lucide-react'
import type { ModelConfig } from '@/types/ontology'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { toast } from 'sonner'
import ModelDetailDrawer from './components/ModelDetailDrawer'
import ModelHeatStrip from './components/ModelHeatStrip'
import { useMockModels, type RunStatus } from './hooks/useMockModels'

const CONFIG_TYPES = [
  { value: 'llm', label: 'LLM配置' },
  { value: 'ocr', label: 'OCR配置' },
  { value: 'other', label: '其他配置' },
]

const PROVIDERS: Record<string, Array<{ value: string; label: string }>> = {
  llm: [
    { value: 'openai', label: 'OpenAI' },
    { value: 'anthropic', label: 'Anthropic' },
    { value: 'compatible', label: 'OpenAI-Compatible' },
    { value: 'minimax', label: 'MiniMax' },
    { value: 'deepseek', label: 'DeepSeek' },
  ],
  ocr: [
    { value: 'easyocr', label: 'EasyOCR' },
    { value: 'paddleocr', label: 'PaddleOCR' },
    { value: 'tesseract', label: 'Tesseract' },
    { value: 'external_api', label: 'External OCR API' },
  ],
  other: [
    { value: 'custom', label: 'Custom' },
    { value: 'local_service', label: 'Local Service' },
    { value: 'http_api', label: 'HTTP API' },
  ],
}

// 每个提供商仅允许配置一个模型：单个模型名转成后端期望的数组结构
function modelList(text?: string) {
  const name = text?.trim()
  return name ? [name] : []
}

function parseOptions(text?: string) {
  if (!text?.trim()) return {}
  try {
    return JSON.parse(text)
  } catch {
    return {}
  }
}

function parseTokenLimit(value: any): number | undefined {
  if (value === undefined || value === null || String(value).trim() === '') return undefined
  const n = Number(value)
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : undefined
}

function buildPayload(data: any, mode: 'create' | 'update' = 'create') {
  const isLlm = (data.config_type || 'llm') === 'llm'
  const maxContext = isLlm ? parseTokenLimit(data.max_context_tokens) : undefined
  const maxOutput = isLlm ? parseTokenLimit(data.max_output_tokens) : undefined
  const options: Record<string, any> = {
    ...parseOptions(data.options_json),
    ...(data.config_type === 'ocr' ? {
      enabled: data.ocr_enabled === 'true',
      lang: data.ocr_lang || 'ch',
      device: data.ocr_device || 'cpu',
    } : {}),
  }
  if (maxContext !== undefined) options.max_context_tokens = maxContext
  else delete options.max_context_tokens
  if (maxOutput !== undefined) options.max_output_tokens = maxOutput
  else delete options.max_output_tokens
  const payload: any = {
    name: data.name,
    config_type: data.config_type || 'llm',
    provider: data.provider,
    api_base: data.api_base,
    models: modelList(data.models_str),
    options,
  }
  const apiKey = typeof data.api_key === 'string' ? data.api_key.trim() : data.api_key
  if (mode === 'create' || apiKey) payload.api_key = apiKey || ''
  return payload
}

function typeLabel(type?: string) {
  return CONFIG_TYPES.find(t => t.value === (type || 'llm'))?.label || 'LLM配置'
}

function providerColor(provider: string): string {
  const colors: Record<string, string> = {
    openai: 'bg-[var(--color-success-bg)] text-[var(--color-success)] border-[color-mix(in_srgb,var(--color-success)_35%,transparent)]',
    anthropic: 'bg-viz-violet-soft text-viz-violet border-viz-violet-soft',
    compatible: 'bg-[var(--color-info-bg)] text-[var(--color-info)] border-[color-mix(in_srgb,var(--color-info)_35%,transparent)]',
    minimax: 'bg-viz-rose-soft text-viz-rose border-viz-rose-soft',
    deepseek: 'bg-[var(--color-info-bg)] text-[var(--color-info)] border-[color-mix(in_srgb,var(--color-info)_35%,transparent)]',
    easyocr: 'bg-viz-orange-soft text-viz-orange border-viz-orange-soft',
    paddleocr: 'bg-viz-cyan-soft text-viz-cyan border-viz-cyan-soft',
    tesseract: 'bg-viz-fuchsia-soft text-viz-fuchsia border-viz-fuchsia-soft',
    external_api: 'bg-[var(--color-warning-bg)] text-[var(--color-warning)] border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)]',
    custom: 'bg-muted text-muted-foreground border-border',
    local_service: 'bg-brand-soft text-brand-ink border-brand-line',
    http_api: 'bg-viz-indigo-soft text-viz-indigo border-viz-indigo-soft',
  }
  return colors[provider] || 'bg-muted text-muted-foreground border-border'
}

// 运行 / 健康状态样式
const RUN_META: Record<RunStatus, { label: string; color: string; bg: string; dot: string }> = {
  normal:   { label: '正常', color: '#2d8a4e', bg: '#e8f5e9', dot: '#2d8a4e' },
  degraded: { label: '降级', color: '#c9861a', bg: '#fff8e1', dot: '#c9861a' },
  error:    { label: '异常', color: '#c23b3b', bg: '#fde8e8', dot: '#c23b3b' },
  disabled: { label: '已停用', color: '#8b8ba3', bg: '#f1f3f5', dot: '#c2c6d0' },
}

function availColor(av: string): string {
  if (av === '—') return '#b8b8c8'
  const n = parseFloat(av)
  return n >= 98 ? '#2d8a4e' : n >= 92 ? '#c9861a' : '#c23b3b'
}

function latColor(ms: number): string {
  if (!ms) return '#b8b8c8'
  return ms < 1500 ? '#2d8a4e' : ms < 3000 ? '#c9861a' : '#c23b3b'
}

export default function ModelsPage() {
  const { t } = useTranslation()
  const {
    models, loading, error, defaultModelId, setDefault, createModel, updateModel, deleteModel, importModels,
    testConnection,
    isEnabled, toggleEnabled, getModelRunStatus, getModelSummary, getModelHeatCells,
  } = useMockModels()

  // UI States
  const [searchQuery, setSearchQuery] = useState('')
  const [filterType, setFilterType] = useState<string>('all')
  const [enabledFilter, setEnabledFilter] = useState<string>('all')
  const filterTabsRef = useRef<HTMLDivElement>(null)
  const [indicatorPos, setIndicatorPos] = useState({ left: 0, width: 0 })
  useEffect(() => {
    const container = filterTabsRef.current
    if (!container) return
    const activeBtn = container.querySelector(`[data-tab-value="${filterType}"]`) as HTMLElement | null
    if (!activeBtn) return
    const containerRect = container.getBoundingClientRect()
    const btnRect = activeBtn.getBoundingClientRect()
    setIndicatorPos({
      left: btnRect.left - containerRect.left,
      width: btnRect.width,
    })
  }, [filterType, models.length])

  // Modal States
  const [showCreate, setShowCreate] = useState(false)
  const [editTarget, setEditTarget] = useState<ModelConfig | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<ModelConfig | null>(null)

  // Detail Drawer
  const [detailModel, setDetailModel] = useState<ModelConfig | null>(null)

  // Test States
  const [testStatus, setTestStatus] = useState<Record<string, 'idle' | 'testing' | 'success' | 'error'>>({})

  // Export/Import
  const handleExport = useCallback(() => {
    const exportData = models.map(m => ({
      name: m.name,
      config_type: m.config_type,
      provider: m.provider,
      api_base: m.api_base,
      models: m.models,
      options: m.options,
    }))
    const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `model-configs-${new Date().toISOString().split('T')[0]}.json`
    a.click()
    URL.revokeObjectURL(url)
    toast.success(`已导出 ${models.length} 个模型配置`)
  }, [models])

  const handleImport = useCallback((file: File) => {
    const reader = new FileReader()
    reader.onload = async (e) => {
      try {
        const data = JSON.parse(e.target?.result as string)
        const items = Array.isArray(data) ? data : data?.configs
        if (!Array.isArray(items) || items.length === 0) throw new Error('导入文件中没有模型配置')
        const result = await importModels(items)
        toast.success(`成功导入 ${result.imported} 个模型配置`)
        toast.warning(result.warning)
      } catch (error: any) {
        toast.error(String(error?.detail || error?.message || '导入模型配置失败'))
      }
    }
    reader.readAsText(file)
  }, [importModels])

  const fileInputRef = useRef<HTMLInputElement>(null)

  // Form
  const { register, handleSubmit, reset, watch, setValue: setCreateValue } = useForm<any>({
    defaultValues: { config_type: 'llm', provider: 'openai', ocr_enabled: 'false', ocr_lang: 'ch', ocr_device: 'cpu' },
  })
  const { register: regEdit, handleSubmit: handleEditSubmit, setValue, watch: watchEdit } = useForm<any>()

  // Filtered + sorted models: default first, then by created_at ascending
  const filteredModels = models.filter(m => {
    const matchSearch = !searchQuery ||
      m.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
      m.provider.toLowerCase().includes(searchQuery.toLowerCase())
    const matchType = filterType === 'all' || m.config_type === filterType
    const matchEnabled = enabledFilter === 'all' ||
      (enabledFilter === 'enabled' && m.enabled !== false) ||
      (enabledFilter === 'disabled' && m.enabled === false)
    return matchSearch && matchType && matchEnabled
  })

  const sortedModels = [...filteredModels].sort((a, b) => {
    if (a.is_default && !b.is_default) return -1
    if (!a.is_default && b.is_default) return 1
    return new Date(a.created_at).getTime() - new Date(b.created_at).getTime()
  })

  // Handlers
  const handleCreate = async (data: any) => {
    try {
      await createModel(buildPayload(data))
      setShowCreate(false)
      reset()
      toast.success(`模型 "${data.name}" 创建成功，请测试后启用`)
    } catch {
      toast.error(`模型 "${data.name}" 创建失败`)
    }
  }

  const handleUpdate = async (data: any) => {
    if (!editTarget) return
    try {
      await updateModel(editTarget.id, buildPayload(data, 'update'))
      setEditTarget(null)
      toast.success('模型更新成功')
    } catch {
      toast.error('模型更新失败')
    }
  }

  const handleDelete = async () => {
    if (!deleteTarget) return
    try {
      await deleteModel(deleteTarget.id)
      if (detailModel?.id === deleteTarget.id) setDetailModel(null)
      setDeleteTarget(null)
      toast.success('模型已删除')
    } catch {
      toast.error('模型删除失败')
    }
  }

  const handleTest = async (id: string) => {
    setTestStatus(prev => ({ ...prev, [id]: 'testing' }))
    const result = await testConnection(id)
    setTestStatus(prev => ({ ...prev, [id]: result.ok ? 'success' : 'error' }))
    if (result.ok) toast.success(result.message)
    else toast.error(result.message)
    setTimeout(() => setTestStatus(prev => ({ ...prev, [id]: 'idle' })), 3000)
  }

  const openEdit = (m: ModelConfig) => {
    const options = m.options || {}
    setEditTarget(m)
    setValue('name', m.name)
    setValue('config_type', m.config_type || 'llm')
    setValue('provider', m.provider)
    setValue('api_key', '')
    setValue('api_base', m.api_base || '')
    setValue('models_str', (m.models || [])[0] || '')
    setValue('ocr_enabled', options.enabled ? 'true' : 'false')
    setValue('ocr_lang', String(options.lang || 'ch'))
    setValue('ocr_device', String(options.device || 'cpu'))
    setValue('max_context_tokens', options.max_context_tokens != null ? String(options.max_context_tokens) : '')
    setValue('max_output_tokens', options.max_output_tokens != null ? String(options.max_output_tokens) : '')
    setValue('options_json', JSON.stringify(
      Object.fromEntries(Object.entries(options).filter(([k]) => !['lang', 'device', 'enabled', 'max_context_tokens', 'max_output_tokens'].includes(k))),
      null, 2,
    ))
  }

  const typeCount = (type: string) => models.filter(m => m.config_type === type).length

  return (
    <div className="min-h-full">

      {/* 搜索、筛选、操作按钮 */}
      <div className="flex items-center gap-3 bg-card rounded-xl border border-border px-4 py-3 flex-wrap mb-5 shadow-sm/50">
        <div className="relative w-full sm:w-64">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-[var(--color-text-tertiary)]" />
          <input
            value={searchQuery}
            onChange={e => setSearchQuery(e.target.value)}
            placeholder="搜索提供商..."
            className="w-full pl-8 pr-3 py-1.5 rounded-lg text-sm border border-border text-foreground placeholder:text-[var(--color-text-tertiary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring transition-all"
          />
        </div>

        <div ref={filterTabsRef} className="relative flex items-center gap-1 rounded-lg border border-border bg-muted p-0.5">
          <div
            className="absolute top-0.5 h-[calc(100%-4px)] rounded-md bg-brand shadow-sm transition-all duration-300 ease-out"
            style={{ left: `${indicatorPos.left}px`, width: `${indicatorPos.width}px` }}
          />
          {[
            { value: 'all', label: '全部', count: models.length },
            { value: 'llm', label: 'LLM', count: typeCount('llm') },
            { value: 'ocr', label: 'OCR', count: typeCount('ocr') },
            { value: 'other', label: '其他', count: typeCount('other') },
          ].map(tab => (
            <button
              key={tab.value}
              data-tab-value={tab.value}
              onClick={() => setFilterType(tab.value)}
              className={`relative px-3 py-1.5 rounded-md text-xs font-medium z-10 transition-colors duration-200 ${
                filterType === tab.value
                  ? 'text-[var(--color-text-inverse)]'
                  : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              {tab.label}
              <span className={`ml-1 ${filterType === tab.value ? 'text-[var(--color-text-tertiary)]' : 'text-[var(--color-text-tertiary)]'}`}>
                {tab.count}
              </span>
            </button>
          ))}
        </div>

        <Select value={enabledFilter} onValueChange={setEnabledFilter}>
          <SelectTrigger className="w-32 rounded-lg bg-card px-3 py-1.5 text-xs font-medium" aria-label="按启用状态筛选">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部状态</SelectItem>
            <SelectItem value="enabled">已启用</SelectItem>
            <SelectItem value="disabled">已禁用</SelectItem>
          </SelectContent>
        </Select>

        <div className="ml-auto flex items-center gap-2">
          <input
            ref={fileInputRef}
            type="file"
            accept=".json"
            className="hidden"
            onChange={e => { const f = e.target.files?.[0]; if (f) { handleImport(f); e.target.value = '' } }}
          />
          <button
            onClick={() => fileInputRef.current?.click()}
            className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg text-sm font-medium bg-card text-muted-foreground border border-border hover:bg-muted transition-colors"
            title="导入配置"
          >
            <Upload size={14} /> 导入
          </button>
          <button
            onClick={handleExport}
            className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg text-sm font-medium bg-card text-muted-foreground border border-border hover:bg-muted transition-colors"
            title="导出配置"
          >
            <Download size={14} /> 导出
          </button>
          <button
            onClick={() => { setShowCreate(true); reset({ config_type: 'llm', provider: 'openai', ocr_enabled: 'false', ocr_lang: 'ch', ocr_device: 'cpu' }) }}
            className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg text-sm font-medium text-[var(--color-text-inverse)] bg-[var(--color-nav-bg)] hover:opacity-90 transition-colors shadow-sm"
          >
            <Plus size={14} /> 添加模型
          </button>
        </div>
      </div>

      {/* Content */}
      {error ? (
        <div className="bg-card border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] rounded-xl p-8 text-center text-sm text-[var(--color-danger)]">
          {error}
        </div>
      ) : loading ? (
        <div className="bg-card border border-border rounded-xl p-8 text-center text-sm text-[var(--color-text-tertiary)]">
          加载中...
        </div>
      ) : sortedModels.length === 0 ? (
        <div className="bg-card border border-border rounded-xl p-12 text-center">
          <div className="w-16 h-16 rounded-full bg-muted flex items-center justify-center mx-auto mb-4">
            <Settings2 size={28} className="text-[var(--color-text-tertiary)]" />
          </div>
          <p className="text-muted-foreground text-sm font-medium">暂无提供商配置</p>
          <p className="text-[var(--color-text-tertiary)] text-xs mt-1">点击右上角按钮添加第一个提供商</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
          {sortedModels.map(m => {
            const status = testStatus[m.id] || 'idle'
            const isDefault = m.id === defaultModelId
            const enabled = isEnabled(m.id)
            const run = getModelRunStatus(m.id)
            const runMeta = RUN_META[run]
            const summary = getModelSummary(m.id)
            const cells = getModelHeatCells(m.id, 60)
            return (
              <div
                key={m.id}
                className={`group bg-card rounded-2xl border transition-all duration-200 hover:shadow-lg overflow-hidden ${
                  isDefault ? 'border-brand ring-1 ring-ring' : 'border-border'
                } ${enabled ? '' : 'opacity-95'}`}
              >
                <div className="p-4 pb-3.5">
                  {/* 名称 + 默认 + 启用开关 */}
                  <div className="flex items-start justify-between gap-2.5">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-1.5">
                        <h3
                          className={`font-semibold text-[14px] truncate cursor-pointer hover:text-brand-ink transition-colors ${enabled ? 'text-foreground' : 'text-[var(--color-text-tertiary)]'}`}
                          onClick={() => setDetailModel(m)}
                        >
                          {m.name}
                        </h3>
                        {isDefault && <Star size={13} className="shrink-0 text-[var(--color-warning)] fill-[var(--color-warning)]" />}
                      </div>
                      <div className="flex items-center gap-1.5 mt-1.5">
                        <span className="text-[11px] px-1.5 py-0.5 rounded-md bg-muted text-muted-foreground border border-border font-medium">
                          {typeLabel(m.config_type)}
                        </span>
                        <span className={`text-[11px] px-1.5 py-0.5 rounded-md border font-medium capitalize ${providerColor(m.provider)}`}>
                          {m.provider}
                        </span>
                      </div>
                    </div>
                    {/* 启用 / 停用开关 */}
                    <button
                      onClick={async () => {
                        try {
                          await toggleEnabled(m.id)
                          toast.info(`"${m.name}" 已${enabled ? '停用' : '启用'}`)
                        } catch {
                          toast.error(`"${m.name}" 状态更新失败`)
                        }
                      }}
                      title={enabled ? '点击停用' : '点击启用'}
                      className="relative shrink-0 w-[38px] h-[22px] rounded-full transition-colors"
                      style={{ background: enabled ? '#059669' : '#cbd2dc' }}
                    >
                      <span
                        className="absolute top-0.5 w-[18px] h-[18px] rounded-full bg-card shadow transition-all"
                        style={{ left: enabled ? 18 : 2 }}
                      />
                    </button>
                  </div>

                  {/* 运行状态 + 最近调用（精确到秒，与状态标识同行垂直居中；空间不足时整体折行、不截断） */}
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1 mt-3.5">
                    <span
                      className="inline-flex items-center gap-1.5 text-[11px] font-semibold px-2.5 py-1 rounded-full shrink-0"
                      style={{ background: runMeta.bg, color: runMeta.color }}
                    >
                      <span className="w-1.5 h-1.5 rounded-full" style={{ background: runMeta.dot }} />
                      {runMeta.label}
                    </span>
                    <span className="text-[11px] text-[var(--color-text-tertiary)] whitespace-nowrap">
                      最近调用 <span className="text-muted-foreground tabular-nums">{summary.lastCall}</span>
                    </span>
                  </div>

                  {/* 指标 */}
                  <div className="flex gap-2 mt-3.5">
                        <div className="flex-1 bg-muted rounded-lg px-2.5 py-2">
                          <div className="text-[10px] text-[var(--color-text-tertiary)] font-medium">今日调用</div>
                          <div className="text-[16px] font-bold text-foreground mt-0.5">{enabled ? summary.todayCalls : '—'}</div>
                        </div>
                        <div className="flex-1 bg-muted rounded-lg px-2.5 py-2">
                          <div className="text-[10px] text-[var(--color-text-tertiary)] font-medium">30天可用率</div>
                          <div className="text-[16px] font-bold mt-0.5" style={{ color: availColor(summary.availability) }}>
                            {summary.availability === '—' ? '—' : `${summary.availability}%`}
                          </div>
                        </div>
                        <div className="flex-1 bg-muted rounded-lg px-2.5 py-2">
                          <div className="text-[10px] text-[var(--color-text-tertiary)] font-medium">平均延迟</div>
                          <div className="text-[16px] font-bold mt-0.5" style={{ color: latColor(summary.avgLatency) }}>
                            {enabled && summary.avgLatency ? `${(summary.avgLatency / 1000).toFixed(1)}s` : '—'}
                          </div>
                        </div>
                      </div>

                      <div className="mt-3.5">
                        <div className="flex items-center justify-between mb-1.5">
                          <span className="text-[11px] font-semibold text-muted-foreground">近 60 次调用</span>
                          <span className="text-[10px] text-[var(--color-text-tertiary)]">← 早 · 近 →</span>
                        </div>
                        <ModelHeatStrip cells={cells} />
                      </div>
                </div>

                {/* Card Actions */}
                <div className="px-4 py-2.5 border-t border-border flex items-center gap-1.5 transition-opacity">
                  <button
                    onClick={() => handleTest(m.id)}
                    disabled={status === 'testing'}
                    className={`inline-flex shrink-0 whitespace-nowrap items-center gap-1 px-2.5 py-1.5 rounded-lg text-[11px] font-medium transition-all disabled:opacity-50 ${
                      status === 'testing' ? 'bg-[var(--color-info-bg)] text-[var(--color-info)]' :
                      status === 'success' ? 'bg-[var(--color-success-bg)] text-[var(--color-success)]' :
                      status === 'error' ? 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]' :
                      'bg-muted text-muted-foreground hover:bg-muted'
                    }`}
                  >
                    {status === 'testing' ? <Loader2 size={11} className="animate-spin" /> :
                     status === 'success' ? <CheckCircle2 size={11} /> :
                     status === 'error' ? <XCircle size={11} /> :
                     <TestTube2 size={11} />}
                    测试
                  </button>
                  <button
                    onClick={() => setDetailModel(m)}
                    className="inline-flex shrink-0 whitespace-nowrap items-center gap-1 px-2.5 py-1.5 rounded-lg text-[11px] font-medium bg-muted text-muted-foreground hover:bg-muted transition-colors"
                  >
                    <FileClock size={11} /> 日志
                  </button>
                  {!isDefault && (
                    <button
                      onClick={async () => {
                        try {
                          await setDefault(m.id)
                          toast.success(`"${m.name}" 已设为默认模型`)
                        } catch {
                          toast.error(`"${m.name}" 设置默认失败`)
                        }
                      }}
                      className="inline-flex shrink-0 whitespace-nowrap items-center gap-1 px-2.5 py-1.5 rounded-lg text-[11px] font-medium bg-muted text-foreground hover:bg-[var(--color-bg-active)] transition-colors"
                    >
                      <Star size={11} /> 默认
                    </button>
                  )}
                  <div className="ml-auto flex items-center gap-1">
                    <button
                      onClick={() => openEdit(m)}
                      className="p-1.5 rounded-lg text-[var(--color-text-tertiary)] hover:text-muted-foreground hover:bg-muted transition-colors"
                      title="编辑"
                    >
                      <Pencil size={13} />
                    </button>
                    <button
                      onClick={() => setDeleteTarget(m)}
                      className="p-1.5 rounded-lg text-[var(--color-text-tertiary)] hover:text-[var(--color-danger)] hover:bg-[var(--color-danger-bg)] transition-colors"
                      title="删除"
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                </div>

              </div>
            )
          })}
        </div>
      )}

      {/* 图例 */}
      {sortedModels.length > 0 && (
        <div className="flex items-center gap-4 flex-wrap mt-5 px-4 py-3 bg-card border border-border rounded-xl">
          <span className="text-[11px] font-semibold text-muted-foreground">调用热力条图例</span>
          <div className="flex items-center gap-1.5 text-[11px] text-[var(--color-text-tertiary)]">
            成功
            {['#216e39', '#2d8a4e', '#40c463', '#9be9a8'].map(c => (
              <span key={c} className="w-3 h-3 rounded-[2px]" style={{ background: c }} />
            ))}
            <span className="text-[var(--color-text-tertiary)]">快←→慢</span>
          </div>
          <div className="flex items-center gap-1.5 text-[11px] text-[var(--color-text-tertiary)]">
            <span className="w-3 h-3 rounded-[2px]" style={{ background: '#f0a020' }} /> 超时
          </div>
          <div className="flex items-center gap-1.5 text-[11px] text-[var(--color-text-tertiary)]">
            <span className="w-3 h-3 rounded-[2px]" style={{ background: '#e5484d' }} /> 异常
          </div>
          <div className="flex items-center gap-1.5 text-[11px] text-[var(--color-text-tertiary)]">
            <span className="w-3 h-3 rounded-[2px]" style={{ background: '#eceef1' }} /> 已停用
          </div>
        </div>
      )}

      {/* Create Modal */}
      {showCreate && (
        <ModelFormModal
          title="添加模型"
          onClose={() => setShowCreate(false)}
          onSubmit={handleCreate}
          register={register}
          handleSubmit={handleSubmit}
          configType={watch('config_type') || 'llm'}
          setValue={setCreateValue}
        />
      )}

      {/* Edit Modal */}
      {editTarget && (
        <div className="fixed inset-0 bg-[var(--color-bg-overlay)] backdrop-blur-sm flex items-center justify-center z-50 p-4" onClick={() => setEditTarget(null)}>
          <div className="bg-card rounded-xl shadow-2xl p-6 w-[560px] max-h-[88vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
            <div className="flex justify-between items-center mb-5">
              <h3 className="font-semibold text-foreground">编辑模型</h3>
              <button onClick={() => setEditTarget(null)} className="text-[var(--color-text-tertiary)] hover:text-muted-foreground p-1 rounded-lg hover:bg-muted transition-colors">
                <X size={16} />
              </button>
            </div>
            <form onSubmit={handleEditSubmit(handleUpdate)} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-foreground mb-1.5">名称 *</label>
                  <input {...regEdit('name', { required: true })} className="w-full border border-border rounded-lg px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring transition-all" />
                </div>
                <div>
                  <label className="block text-sm font-medium text-foreground mb-1.5">配置分类 *</label>
                  <select {...regEdit('config_type', { required: true, onChange: e => setValue('provider', PROVIDERS[e.target.value]?.[0]?.value || 'custom') })}
                    className="w-full border border-border rounded-lg px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring transition-all">
                    {CONFIG_TYPES.map(t => <option key={t.value} value={t.value}>{t.label}</option>)}
                  </select>
                </div>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-foreground mb-1.5">Provider *</label>
                  <select {...regEdit('provider', { required: true })}
                    className="w-full border border-border rounded-lg px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring transition-all">
                    {(PROVIDERS[watchEdit('config_type') || 'llm'] || PROVIDERS.llm).map(p => <option key={p.value} value={p.value}>{p.label}</option>)}
                  </select>
                </div>
                <div>
                  <label className="block text-sm font-medium text-foreground mb-1.5">API Key</label>
                  <input {...regEdit('api_key')} type="password" placeholder={editTarget.has_api_key ? '已保存，留空保留原密钥' : 'sk-...'}
                    className="w-full border border-border rounded-lg px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring transition-all" />
                </div>
              </div>
              <div>
                <label className="block text-sm font-medium text-foreground mb-1.5">API Base</label>
                <input {...regEdit('api_base')} placeholder="https://api.openai.com/v1"
                  className="w-full border border-border rounded-lg px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring transition-all" />
              </div>
              <div>
                <label className="block text-sm font-medium text-foreground mb-1.5">模型名</label>
                <input {...regEdit('models_str')} placeholder="gpt-4o"
                  className="w-full border border-border rounded-lg px-3 py-2 text-sm font-mono text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring transition-all" />
                <p className="text-[11px] text-[var(--color-text-tertiary)] mt-1">每个提供商仅支持配置一个模型</p>
              </div>
              {(watchEdit('config_type') || 'llm') === 'llm' && (
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-sm font-medium text-foreground mb-1.5">最大上下文（tokens）</label>
                    <input {...regEdit('max_context_tokens')} type="number" min={1} placeholder="如：128000"
                      className="w-full border border-border rounded-lg px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring transition-all" />
                    <p className="text-[11px] text-[var(--color-text-tertiary)] mt-1">模型可接受的最大输入上下文，留空则不限制</p>
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-foreground mb-1.5">最大输出（tokens）</label>
                    <input {...regEdit('max_output_tokens')} type="number" min={1} placeholder="如：4096"
                      className="w-full border border-border rounded-lg px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring transition-all" />
                    <p className="text-[11px] text-[var(--color-text-tertiary)] mt-1">单次调用最大生成 tokens，留空用默认值</p>
                  </div>
                </div>
              )}
              {(watchEdit('config_type') || 'llm') === 'ocr' && (
                <div className="grid grid-cols-3 gap-4">
                  <div>
                    <label className="block text-sm font-medium text-foreground mb-1.5">启用运行</label>
                    <select {...regEdit('ocr_enabled')}
                      className="w-full border border-border rounded-lg px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring transition-all">
                      <option value="false">关闭</option><option value="true">开启</option>
                    </select>
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-foreground mb-1.5">OCR语言</label>
                    <input {...regEdit('ocr_lang')} placeholder="ch"
                      className="w-full border border-border rounded-lg px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring transition-all" />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-foreground mb-1.5">设备</label>
                    <select {...regEdit('ocr_device')}
                      className="w-full border border-border rounded-lg px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring transition-all">
                      <option value="cpu">CPU</option><option value="gpu">GPU</option>
                    </select>
                  </div>
                </div>
              )}
              <div>
                <label className="block text-sm font-medium text-foreground mb-1.5">高级参数 JSON</label>
                <textarea {...regEdit('options_json')} rows={3} placeholder='{"timeout": 30}'
                  className="w-full border border-border rounded-lg px-3 py-2 text-sm font-mono text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring transition-all" />
              </div>
              <div className="flex justify-center gap-3 pt-2">
                <button type="button" onClick={() => setEditTarget(null)}
                  className="px-5 py-2 border border-border rounded-lg text-sm text-muted-foreground hover:bg-muted transition-colors">取消</button>
                <button type="submit"
                  className="flex items-center gap-1.5 px-5 py-2 bg-brand text-[var(--color-text-inverse)] rounded-lg text-sm hover:bg-brand-deep transition-colors">
                  保存
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Detail Drawer */}
      <ModelDetailDrawer
        model={detailModel}
        isOpen={!!detailModel}
        onClose={() => setDetailModel(null)}
      />

      {/* Delete Confirm */}
      <ConfirmDialog
        open={!!deleteTarget}
        title={t('model.confirm_delete')}
        description={t('model.confirm_delete_msg', { name: deleteTarget?.name })}
        confirmText="确认删除"
        variant="danger"
        onConfirm={handleDelete}
        onClose={() => setDeleteTarget(null)}
      />
    </div>
  )
}

/** Model Form Modal (Create) */
function ModelFormModal({ title, onClose, onSubmit, register, handleSubmit, configType, setValue }: any) {
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={title}
      className="fixed inset-0 bg-[var(--color-bg-overlay)] backdrop-blur-sm flex items-center justify-center z-50 p-4"
      onClick={() => onClose()}
    >
      <div className="bg-card rounded-xl shadow-2xl p-6 w-[min(92vw,32rem)] max-h-[88vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
        <div className="mb-5 flex items-center gap-3 pr-12">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-brand-soft text-brand-ink">
            <Cpu size={18} />
          </div>
          <h3 className="text-base font-semibold leading-6 text-foreground">{title}</h3>
        </div>
        <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-foreground mb-1.5">名称 *</label>
              <input {...register('name', { required: true })} placeholder="如：GPT-4o 生产环境"
                className="w-full border border-border rounded-lg px-3 py-2 text-sm text-foreground placeholder:text-[var(--color-text-tertiary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring transition-all" />
            </div>
            <div>
              <label className="block text-sm font-medium text-foreground mb-1.5">配置分类 *</label>
              <select {...register('config_type', { required: true, onChange: (e: any) => setValue('provider', PROVIDERS[e.target.value]?.[0]?.value || 'custom') })}
                className="w-full border border-border rounded-lg px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring transition-all">
                {CONFIG_TYPES.map(t => <option key={t.value} value={t.value}>{t.label}</option>)}
              </select>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-foreground mb-1.5">Provider *</label>
              <select {...register('provider', { required: true })}
                className="w-full border border-border rounded-lg px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring transition-all">
                {(PROVIDERS[configType] || PROVIDERS.llm).map(p => <option key={p.value} value={p.value}>{p.label}</option>)}
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-foreground mb-1.5">API Key</label>
              <input {...register('api_key')} type="password" placeholder="sk-..."
                className="w-full border border-border rounded-lg px-3 py-2 text-sm text-foreground placeholder:text-[var(--color-text-tertiary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring transition-all" />
            </div>
          </div>
          <div>
            <label className="block text-sm font-medium text-foreground mb-1.5">API Base</label>
            <input {...register('api_base')} placeholder="https://api.openai.com/v1"
              className="w-full border border-border rounded-lg px-3 py-2 text-sm text-foreground placeholder:text-[var(--color-text-tertiary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring transition-all" />
          </div>
          <div>
            <label className="block text-sm font-medium text-foreground mb-1.5">模型名</label>
            <input {...register('models_str')} placeholder="gpt-4o"
              className="w-full border border-border rounded-lg px-3 py-2 text-sm font-mono text-foreground placeholder:text-[var(--color-text-tertiary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring transition-all" />
            <p className="text-[11px] text-[var(--color-text-tertiary)] mt-1">每个提供商仅支持配置一个模型</p>
          </div>
          {configType === 'llm' && (
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-medium text-foreground mb-1.5">最大上下文（tokens）</label>
                <input {...register('max_context_tokens')} type="number" min={1} placeholder="如：128000"
                  className="w-full border border-border rounded-lg px-3 py-2 text-sm text-foreground placeholder:text-[var(--color-text-tertiary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring transition-all" />
                <p className="text-[11px] text-[var(--color-text-tertiary)] mt-1">模型可接受的最大输入上下文，留空则不限制</p>
              </div>
              <div>
                <label className="block text-sm font-medium text-foreground mb-1.5">最大输出（tokens）</label>
                <input {...register('max_output_tokens')} type="number" min={1} placeholder="如：4096"
                  className="w-full border border-border rounded-lg px-3 py-2 text-sm text-foreground placeholder:text-[var(--color-text-tertiary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring transition-all" />
                <p className="text-[11px] text-[var(--color-text-tertiary)] mt-1">单次调用最大生成 tokens，留空用默认值</p>
              </div>
            </div>
          )}
          {configType === 'ocr' && (
            <div className="grid grid-cols-3 gap-4">
              <div>
                <label className="block text-sm font-medium text-foreground mb-1.5">启用运行</label>
                <select {...register('ocr_enabled')}
                  className="w-full border border-border rounded-lg px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring transition-all">
                  <option value="false">关闭</option><option value="true">开启</option>
                </select>
              </div>
              <div>
                <label className="block text-sm font-medium text-foreground mb-1.5">OCR语言</label>
                <input {...register('ocr_lang')} placeholder="ch"
                  className="w-full border border-border rounded-lg px-3 py-2 text-sm text-foreground placeholder:text-[var(--color-text-tertiary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring transition-all" />
              </div>
              <div>
                <label className="block text-sm font-medium text-foreground mb-1.5">设备</label>
                <select {...register('ocr_device')}
                  className="w-full border border-border rounded-lg px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring transition-all">
                  <option value="cpu">CPU</option><option value="gpu">GPU</option>
                </select>
              </div>
            </div>
          )}
          <div>
            <label className="block text-sm font-medium text-foreground mb-1.5">高级参数 JSON</label>
            <textarea {...register('options_json')} rows={3} placeholder='{"timeout": 30}'
              className="w-full border border-border rounded-lg px-3 py-2 text-sm font-mono text-foreground placeholder:text-[var(--color-text-tertiary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring transition-all" />
          </div>
          <div className="flex justify-center gap-3 pt-2">
            <button type="button" onClick={onClose}
              className="px-5 py-2 border border-border rounded-lg text-sm text-muted-foreground hover:bg-muted transition-colors">取消</button>
            <button type="submit"
              className="flex items-center gap-1.5 px-5 py-2 rounded-lg text-sm text-[var(--color-text-inverse)] bg-brand hover:bg-brand-deep transition-colors">
              保存
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
