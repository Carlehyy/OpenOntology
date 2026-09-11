import { formatDateTime } from '@/utils/datetime'
import { useState, useCallback, useEffect } from 'react'
import type { ModelConfig } from '@/types/ontology'
import { modelApi } from '@/api/ontologies'

export interface DailyStats {
  date: string
  callCount: number
  successCount: number
  errorCount: number
  avgLatency: number
}

export type RunStatus = 'normal' | 'degraded' | 'error' | 'disabled'

export interface HeatCell {
  color: string
  title: string
  status: 'success' | 'timeout' | 'error' | 'none' | 'disabled'
}

export interface ModelSummary {
  todayCalls: number
  availability: string
  avgLatency: number
  lastCall: string
  successRate: number
}

interface ModelStatsData {
  todayCalls: number
  availability: string | null
  avgLatency: number | null
  lastCall: string | null
  successRate: number | null
  heatCells: Array<{ color: string; title: string; status: string }>
}

const HEAT_EMPTY = '#eceef1'

function emptyHeatCells(model: ModelConfig | undefined, n: number): HeatCell[] {
  const disabled = model ? model.enabled === false : false
  return Array.from({ length: n }, () => ({
    color: HEAT_EMPTY,
    title: disabled ? '已停用（无调用）' : '暂无调用记录',
    status: (disabled ? 'disabled' : 'none') as HeatCell['status'],
  }))
}

function emptySummary(model: ModelConfig | undefined): ModelSummary {
  if (model?.enabled === false) {
    return { todayCalls: 0, availability: '—', avgLatency: 0, lastCall: '已停用', successRate: 0 }
  }
  return { todayCalls: 0, availability: '—', avgLatency: 0, lastCall: '—', successRate: 1 }
}

function formatLastCall(iso: string | null): string {
  // 展示为本地时区的绝对时间，精确到秒（UTC 解析规则由 parseServerTime 统一处理）
  return formatDateTime(iso, { seconds: true })
}

export function useMockModels() {
  const [models, setModels] = useState<ModelConfig[]>([])
  const [modelStats, setModelStats] = useState<Record<string, ModelStatsData>>({})
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const loadModels = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await modelApi.list()
      const list = Array.isArray(data) ? data : []
      setModels(list)
      // 并行加载所有模型的统计数据
      const statsMap: Record<string, ModelStatsData> = {}
      await Promise.all(list.map(async (m) => {
        try {
          statsMap[m.id] = await modelApi.stats(m.id)
        } catch {
          statsMap[m.id] = {
            todayCalls: 0, availability: null, avgLatency: null,
            lastCall: null, successRate: null, heatCells: [],
          }
        }
      }))
      setModelStats(statsMap)
    } catch (err) {
      setError(err instanceof Error ? err.message : '模型配置加载失败')
      setModels([])
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { loadModels() }, [loadModels])

  // 仅以后端显式默认状态为准，避免把列表第一项错误标成默认模型。
  const defaultModelId = models.find(m => m.is_default)?.id || ''

  const createModel = useCallback(async (data: Partial<ModelConfig> & { api_key?: string }) => {
    const created = await modelApi.create(data)
    setModels(prev => [created, ...prev.map(m => created.is_default ? { ...m, is_default: false } : m)])
    return created
  }, [])

  const updateModel = useCallback(async (id: string, data: Partial<ModelConfig> & { api_key?: string }) => {
    const updated = await modelApi.update(id, data)
    setModels(prev => prev.map(m => {
      if (m.id === id) return updated
      return updated.is_default ? { ...m, is_default: false } : m
    }))
    return updated
  }, [])

  const deleteModel = useCallback(async (id: string) => {
    await modelApi.delete(id)
    await loadModels()
  }, [loadModels])

  const importModels = useCallback(async (configs: Array<Partial<ModelConfig>>) => {
    const result = await modelApi.import(configs)
    setModels(prev => [...result.configs, ...prev])
    return result
  }, [])

  const setDefault = useCallback(async (id: string) => {
    const updated = await modelApi.setDefault(id)
    setModels(prev => prev.map(m => m.id === id ? updated : { ...m, is_default: false }))
    return updated
  }, [])

  const isEnabled = useCallback((id: string) => {
    return models.find(m => m.id === id)?.enabled !== false
  }, [models])

  const toggleEnabled = useCallback(async (id: string) => {
    const target = models.find(m => m.id === id)
    if (!target) return
    const updated = await modelApi.setEnabled(id, target.enabled === false)
    setModels(prev => prev.map(m => {
      if (m.id === id) return updated
      return updated.is_default ? { ...m, is_default: false } : m
    }))
  }, [models])

  const testConnection = useCallback(async (id: string): Promise<{ ok: boolean; message: string }> => {
    try {
      const result = await modelApi.test(id)
      // 测试后刷新该模型的统计
      try {
        const s = await modelApi.stats(id)
        setModelStats(prev => ({ ...prev, [id]: s }))
      } catch { /* ignore */ }
      return { ok: Boolean(result.ok), message: result.response || (result.ok ? '连接成功，响应正常' : '连接失败') }
    } catch (err: any) {
      const detail = err?.detail || err?.message || '连接失败'
      return { ok: false, message: String(detail) }
    }
  }, [])

  const getModelHeatCells = useCallback((modelId: string, n = 60): HeatCell[] => {
    const model = models.find(m => m.id === modelId)
    const s = modelStats[modelId]
    // 后端始终返回60格；仅 API 异常时才 fallback 到空占位
    if (!s || s.heatCells.length === 0) {
      return emptyHeatCells(model, n)
    }
    const cells = s.heatCells as HeatCell[]
    // 后端已补满60格，直接返回
    return cells.length === n ? cells : cells.slice(0, n)
  }, [models, modelStats])

  const getModelRunStatus = useCallback((modelId: string): RunStatus => {
    if (!isEnabled(modelId)) return 'disabled'
    const s = modelStats[modelId]
    if (!s || s.availability === null) return 'normal'
    const avail = parseFloat(s.availability)
    if (avail < 80) return 'error'
    if (avail < 95) return 'degraded'
    return 'normal'
  }, [isEnabled, modelStats])

  const getModelSummary = useCallback((modelId: string): ModelSummary => {
    const model = models.find(m => m.id === modelId)
    const s = modelStats[modelId]
    if (!s || (s.todayCalls === 0 && s.avgLatency === null && s.availability === null)) {
      return emptySummary(model)
    }
    return {
      todayCalls: s.todayCalls,
      availability: s.availability ?? '—',
      avgLatency: s.avgLatency ?? 0,
      lastCall: formatLastCall(s.lastCall),
      successRate: s.successRate ?? 0,
    }
  }, [models, modelStats])

  return {
    models,
    loading,
    error,
    defaultModelId,
    createModel,
    updateModel,
    deleteModel,
    importModels,
    setDefault,
    testConnection,
    getModelHeatCells,
    getModelRunStatus,
    getModelSummary,
    isEnabled,
    toggleEnabled,
    reload: loadModels,
  }
}
