/* 本体预览投影的纯展示逻辑：分组 / 计数 / disposition 映射。
   与 React 解耦（无 React 依赖），便于 node:test 单测。 */
import type {
  DraftSemanticIssue,
  OntologyPreview,
  OntologyPreviewDisposition,
  OntologyPreviewItem,
} from '@/api/exploration'

/** 五类集合的展示顺序与中文名（与后端 projected 键一一对应）。 */
export const PREVIEW_COLLECTIONS = [
  { key: 'objectTypes', label: '对象类型' },
  { key: 'linkTypes', label: '链接类型' },
  { key: 'actions', label: '动作' },
  { key: 'functions', label: '函数' },
  { key: 'sentinels', label: '哨兵' },
] as const

export type PreviewCollectionKey = (typeof PREVIEW_COLLECTIONS)[number]['key']

export const DISPOSITION_LABELS: Record<OntologyPreviewDisposition, string> = {
  add: '将新增',
  exists: '已存在·跳过',
  conflict: '同名冲突',
}

export interface PreviewCollectionView {
  key: PreviewCollectionKey
  label: string
  total: number
  counts: Record<OntologyPreviewDisposition, number>
  /** 按 add → conflict → exists 排序的明细（用户最关心新增与冲突）。 */
  items: OntologyPreviewItem[]
}

export interface OntologyPreviewView {
  collections: PreviewCollectionView[]
  totals: Record<OntologyPreviewDisposition, number> & { total: number }
  blockingIssues: DraftSemanticIssue[]
  unsupportedIssues: DraftSemanticIssue[]
}

const DISPOSITION_ORDER: Record<OntologyPreviewDisposition, number> = {
  add: 0,
  conflict: 1,
  exists: 2,
}

function emptyCounts(): Record<OntologyPreviewDisposition, number> {
  return { add: 0, exists: 0, conflict: 0 }
}

export function summarizeOntologyPreview(preview: OntologyPreview): OntologyPreviewView {
  const totals = { ...emptyCounts(), total: 0 }
  const collections = PREVIEW_COLLECTIONS.map(({ key, label }) => {
    const items = [...(preview.projected?.[key] || [])]
      .sort((a, b) =>
        DISPOSITION_ORDER[a.disposition] - DISPOSITION_ORDER[b.disposition]
        || a.name.localeCompare(b.name))
    const counts = emptyCounts()
    for (const item of items) counts[item.disposition] += 1
    for (const disposition of Object.keys(counts) as OntologyPreviewDisposition[]) {
      totals[disposition] += counts[disposition]
    }
    totals.total += items.length
    return { key, label, total: items.length, counts, items }
  })
  const issues = preview.semanticIssues || []
  return {
    collections,
    totals,
    blockingIssues: issues.filter(issue => issue.severity === 'blocking'),
    unsupportedIssues: issues.filter(issue => issue.severity === 'unsupported'),
  }
}
