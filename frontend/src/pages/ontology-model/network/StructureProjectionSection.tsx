/**
 * 结构投影面板（本体网络页右栏新区块）。
 *
 * 职责：逐个选中本体展示结构投影状态（release 版本 / 计数 / 漂移 / 失败原因），
 * 提供管理员重建入口（服务端 admin+write 是最终裁决；前端按钮可见性只是
 * 展示优化，服务端错误一律如实透出，失败不宣称成功）；unavailable（图不可读）
 * 不展开旧图，仅允许重建；单选且 ready 时提供只读结构图预览（折叠，
 * 结构层不含业务实例）。
 *
 * 纪律：本面板状态来自 structure-projection 端点，与本体详情页实例投影
 * projection_status 是两套事实，禁止互相冒充。
 */
import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import ReactECharts from 'echarts-for-react'
import { Loader2, RefreshCw } from 'lucide-react'
import {
  ontologyNetworkApi,
  type NetworkOntologySection,
  type StructureProjectionState,
} from '@/api/ontologyNetwork'
import { useAuthStore } from '@/stores/authStore'
import {
  structureProjectionStatusLabel,
  structureProjectionSummary,
  structureProjectionTone,
} from './structureProjectionModel.ts'
import { buildStructureGraphOption } from './structureGraphOption.ts'

const TONE_CLASS: Record<string, string> = {
  success: 'bg-brand-soft text-brand-ink',
  info: 'bg-muted text-muted-foreground',
  warning: 'bg-[var(--color-warning-bg)] text-[var(--color-warning)]',
  danger: 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]',
  muted: 'bg-muted text-muted-foreground',
}

const requestErrorText = (error: unknown, fallback: string): string => {
  const detail = (error as { detail?: unknown; message?: string })?.detail
    ?? (error as { message?: string })?.message
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object') {
    const code = (detail as { code?: string }).code
    const message = (detail as { message?: string }).message
    return [code ? `[${code}]` : '', message || fallback].filter(Boolean).join(' ')
  }
  return fallback
}

/** 单个本体的状态行：状态徽章 + 说明 + 重建按钮（admin 可见） */
function StructureProjectionRow({ ontology }: { ontology: NetworkOntologySection }) {
  const queryClient = useQueryClient()
  const role = useAuthStore(state => state.user?.role)
  const canRebuild = role === 'admin'
  const [rebuilding, setRebuilding] = useState(false)
  const [rebuildError, setRebuildError] = useState('')
  const [graphOpen, setGraphOpen] = useState(false)

  const statusQuery = useQuery({
    queryKey: ['ontology-structure-projection', ontology.id],
    queryFn: () => ontologyNetworkApi.structureProjection(ontology.id),
    // 状态为读侧对账结果：重建/手动刷新时再拉，不做后台轮询。
    refetchOnWindowFocus: false,
    staleTime: 15_000,
    retry: false,
  })

  const state: StructureProjectionState | null = statusQuery.data ?? null
  const readyForGraph = !!state && state.status === 'ready' && !state.drift

  // 缓存键携带 release+hash：重建/切 release 后 key 变化，旧图不会被复用
  const graphKey = ['ontology-structure-graph', ontology.id, state?.releaseId ?? '', state?.snapshotHash ?? '']
  const graphQuery = useQuery({
    queryKey: graphKey,
    queryFn: () => ontologyNetworkApi.structureGraph(ontology.id),
    enabled: graphOpen && readyForGraph,
    refetchOnWindowFocus: false,
    staleTime: 15_000,
    retry: false,
  })

  const rebuild = async () => {
    if (rebuilding) return
    setRebuilding(true)
    setRebuildError('')
    try {
      // 同步返回重建后的权威状态；直接写入缓存让徽章即时如实更新，
      // 并清掉该本体全部旧图缓存（旧 release/hash 的子图不得复用）。
      const next = await ontologyNetworkApi.rebuildStructureProjection(ontology.id)
      queryClient.setQueryData(['ontology-structure-projection', ontology.id], next)
      queryClient.removeQueries({ queryKey: ['ontology-structure-graph', ontology.id] })
      if (next.status !== 'ready') {
        setRebuildError(structureProjectionSummary(next) || '重建未完成，请勿视为成功')
      }
    } catch (error) {
      setRebuildError(requestErrorText(error, '重建失败，请稍后重试'))
      // 重建失败时服务端已持久 failed 状态：主动 refetch 权威状态，
      // 避免「本地仍显示 ready、实际投影已失败」的误导。
      await queryClient.refetchQueries({
        queryKey: ['ontology-structure-projection', ontology.id],
      }).catch(() => undefined)
    } finally {
      setRebuilding(false)
    }
  }

  return (
    <div className="rounded-md border border-[var(--color-border)] px-2 py-1.5" data-testid={`structure-projection-row-${ontology.id}`}>
      <div className="flex items-center gap-1.5">
        <span className="min-w-0 flex-1 truncate text-[11.5px] font-medium text-foreground">{ontology.name}</span>
        {statusQuery.isLoading ? (
          <span className="flex items-center gap-1 text-[10px] text-muted-foreground">
            <Loader2 size={11} className="animate-spin" />读取状态…
          </span>
        ) : statusQuery.error ? (
          <span className="text-[10px] text-[var(--color-warning)]" role="status">
            状态不可用（{requestErrorText(statusQuery.error, '读取失败')}）
          </span>
        ) : state ? (
          <span
            className={`shrink-0 rounded px-1.5 py-0.5 text-[9.5px] font-medium ${TONE_CLASS[structureProjectionTone(state.status)] ?? TONE_CLASS.muted}`}
            data-testid={`structure-projection-status-${ontology.id}`}
          >
            {structureProjectionStatusLabel(state.status)}
          </span>
        ) : null}
        {canRebuild && (
          <button
            type="button"
            onClick={rebuild}
            disabled={rebuilding}
            title='重建结构投影（服务端要求管理员与源本体写权限）'
            aria-label={`重建 ${ontology.name} 结构投影`}
            className="flex h-6 w-6 shrink-0 items-center justify-center rounded text-muted-foreground hover:bg-muted disabled:opacity-50"
          >
            {rebuilding ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
          </button>
        )}
      </div>
      {state && (
        <p className="mt-1 break-all text-[10px] leading-relaxed text-[var(--color-text-tertiary)]">
          {structureProjectionSummary(state) || '尚无构建记录'}
        </p>
      )}
      {rebuildError && (
        <p role="alert" className="mt-1 text-[10px] font-medium text-[var(--color-danger)]">{rebuildError}</p>
      )}
      {readyForGraph && (
        <details className="mt-1" onToggle={event => setGraphOpen((event.target as HTMLDetailsElement).open)}>
          <summary className="cursor-pointer text-[10px] font-medium text-muted-foreground hover:text-foreground">
            结构图预览（只读 · 结构层）
          </summary>
          {graphQuery.error ? (
            <p role="alert" className="mt-1 text-[10px] text-[var(--color-warning)]">
              结构图不可读（{requestErrorText(graphQuery.error, '读取失败')}）
            </p>
          ) : graphQuery.data ? (
            <div data-testid="structure-graph-host" className="mt-1">
              <ReactECharts
                option={buildStructureGraphOption(graphQuery.data)}
                opts={{ renderer: 'svg' }}
                style={{ height: 220, width: '100%' }}
                notMerge
              />
            </div>
          ) : (
            <p className="mt-1 flex items-center gap-1 text-[10px] text-muted-foreground">
              <Loader2 size={11} className="animate-spin" />加载结构图…
            </p>
          )}
        </details>
      )}
    </div>
  )
}

export default function StructureProjectionSection({ ontologies }: {
  /** 当前选中的本体清单（保持页面勾选顺序） */
  ontologies: NetworkOntologySection[]
}) {
  return (
    <section className="rounded-lg border border-[var(--color-border)] bg-card" data-testid="network-structure-projection">
      <div className="border-b border-[var(--color-border)] px-3 py-2">
        <p className="text-xs font-semibold text-foreground">结构投影（SQL→图 · 只读展示）</p>
      </div>
      <div className="space-y-1.5 px-2 py-2">
        {ontologies.length === 0 && (
          <p className="px-1 py-1 text-[11px] text-[var(--color-text-tertiary)]">选择本体后展示其结构投影状态。</p>
        )}
        {ontologies.map(ontology => (
          <StructureProjectionRow key={ontology.id} ontology={ontology} />
        ))}
      </div>
    </section>
  )
}
