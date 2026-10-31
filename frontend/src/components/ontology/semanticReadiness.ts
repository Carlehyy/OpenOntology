/* 版本业务语义层的纯逻辑：数据归一、计数行、文档新鲜度与三面一致性判定。
   与展示组件解耦（无 React 依赖），便于 node:test 单测。
   供 pages/ontologies（发布前检查）与 pages/explore（本体模型漂移角标）共用。 */
import type { OntologySemanticOverview } from '@/api/v2/ontology-versions'

export interface SemanticCountRow {
  key: string
  label: string
  count: number
}

/** 画布七类模型展示顺序与 CanvasPanel 分组一致。 */
const CANVAS_COUNT_LABELS: ReadonlyArray<[keyof OntologySemanticOverview['canvasCounts'], string]> = [
  ['objects', '对象'],
  ['actors', '主体'],
  ['behaviors', '行为'],
  ['events', '事件'],
  ['rules', '规则'],
  ['processes', '流程'],
  ['scenarios', '场景'],
]

/** 结构五类集合标签沿用版本域既有口径（对象实体/实体关系/执行动作/激活函数/哨兵）。 */
const STRUCTURE_COUNT_LABELS: ReadonlyArray<[keyof OntologySemanticOverview['structureCounts'], string]> = [
  ['objectTypes', '对象实体'],
  ['linkTypes', '实体关系'],
  ['actions', '执行动作'],
  ['functions', '激活函数'],
  ['sentinels', '哨兵'],
]

/** 语义一致性 issue code → 人读标签（与后端 semantic_gate 的 code 一一对应）。 */
const ISSUE_CODE_LABELS: Record<string, string> = {
  semantic_business_missing: '结构缺业务语义',
  semantic_structure_missing: '画布模型未落地',
  semantic_signature_mismatch: '签名不一致',
  semantic_document_missing: '需求文档缺失',
  semantic_document_stale: '文档/画布已变更',
}

export function semanticIssueCodeLabel(code: string): string {
  return ISSUE_CODE_LABELS[code] || code
}

const toCount = (value: unknown): number =>
  typeof value === 'number' && Number.isFinite(value) && value > 0 ? Math.floor(value) : 0

/**
 * 归一化 impact/semantic 端点返回的总览：旧后端缺字段时返回 null（调用方不渲染），
 * 字段部分缺失时计数按 0、一致性按空集合兜底，不把脏数据带进展示层。
 */
export function normalizeSemanticOverview(raw: unknown): OntologySemanticOverview | null {
  if (!raw || typeof raw !== 'object') return null
  const value = raw as Partial<OntologySemanticOverview> & Record<string, unknown>
  const canvas = (value.canvasCounts || {}) as Record<string, unknown>
  const structure = (value.structureCounts || {}) as Record<string, unknown>
  const consistency = (value.consistency || {}) as {
    issueCount?: unknown
    byCode?: Record<string, unknown>
  }
  const byCode: Record<string, number> = {}
  for (const [code, count] of Object.entries(consistency.byCode || {})) {
    const normalized = toCount(count)
    if (normalized > 0) byCode[code] = normalized
  }
  return {
    hasSemanticLayer: Boolean(value.hasSemanticLayer),
    documentTitle: typeof value.documentTitle === 'string' && value.documentTitle.trim()
      ? value.documentTitle
      : null,
    documentStale: Boolean(value.documentStale),
    canvasCounts: {
      objects: toCount(canvas.objects),
      actors: toCount(canvas.actors),
      behaviors: toCount(canvas.behaviors),
      events: toCount(canvas.events),
      rules: toCount(canvas.rules),
      scenarios: toCount(canvas.scenarios),
      processes: toCount(canvas.processes),
    },
    structureCounts: {
      objectTypes: toCount(structure.objectTypes),
      linkTypes: toCount(structure.linkTypes),
      actions: toCount(structure.actions),
      functions: toCount(structure.functions),
      sentinels: toCount(structure.sentinels),
    },
    consistency: {
      issueCount: toCount(consistency.issueCount),
      byCode,
    },
  }
}

export function canvasCountRows(overview: OntologySemanticOverview): SemanticCountRow[] {
  return CANVAS_COUNT_LABELS.map(([key, label]) => ({
    key,
    label,
    count: toCount(overview.canvasCounts?.[key]),
  }))
}

export function structureCountRows(overview: OntologySemanticOverview): SemanticCountRow[] {
  return STRUCTURE_COUNT_LABELS.map(([key, label]) => ({
    key,
    label,
    count: toCount(overview.structureCounts?.[key]),
  }))
}

export interface ConsistencyView {
  /** consistent = 业务画布/需求文档/本体结构三面一致 */
  tone: 'consistent' | 'diverged'
  text: string
  details: Array<{ code: string; label: string; count: number }>
}

export function consistencyView(overview: OntologySemanticOverview): ConsistencyView {
  const { issueCount, byCode } = overview.consistency
  if (issueCount === 0) return { tone: 'consistent', text: '三面一致', details: [] }
  const details = Object.entries(byCode)
    .map(([code, count]) => ({ code, label: semanticIssueCodeLabel(code), count }))
    .sort((a, b) => b.count - a.count || a.code.localeCompare(b.code))
  return { tone: 'diverged', text: `${issueCount} 项不一致`, details }
}

export interface DocumentStateView {
  tone: 'ok' | 'stale' | 'none'
  text: string
}

/** 文档标题与新鲜度：过期给警示态；无标题且未过期视为尚未生成。 */
export function documentStateView(overview: OntologySemanticOverview): DocumentStateView {
  const title = overview.documentTitle || '需求文档'
  if (overview.documentStale) return { tone: 'stale', text: `${title} · 已过期` }
  if (!overview.documentTitle) return { tone: 'none', text: '尚未生成需求文档' }
  return { tone: 'ok', text: `${title} · 最新` }
}
