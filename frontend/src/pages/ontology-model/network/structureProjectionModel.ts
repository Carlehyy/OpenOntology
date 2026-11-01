/**
 * 结构投影状态的纯展示派生：状态徽章文案 / 语义色调 / 漂移与错误说明。
 *
 * 事实源是 GET /api/v2/ontology-network/{oid}/structure-projection 的
 * StructureProjectionState；本模块只做文案与色调映射，不发起请求。
 * 纪律：failed/stale/未知状态一律如实展示，绝不文案化为「就绪/成功」；
 * 未知枚举值保守原文透出，不做猜测性 fallback。
 *
 * 被 Node --experimental-strip-types 单测直接执行（相对路径 import），
 * 禁止引入任何运行时依赖。
 */
import type { StructureProjectionState } from '../../../api/ontologyNetwork.ts'

export type StructureProjectionTone = 'success' | 'warning' | 'danger' | 'muted' | 'info'

/** 状态 → 用户可读文案；未知枚举保守原文，不误标成功 */
export function structureProjectionStatusLabel(status: string): string {
  switch (status) {
    case 'empty': return '未构建'
    case 'building': return '构建中'
    case 'ready': return '已就绪'
    case 'stale': return '已漂移'
    case 'failed': return '构建失败'
    case 'unavailable': return '暂不可用'
    default: return status
  }
}

/** 状态 → 语义色调（与 Badge 语义对齐；failed=danger、stale=warning） */
export function structureProjectionTone(status: string): StructureProjectionTone {
  switch (status) {
    case 'ready': return 'success'
    case 'building': return 'info'
    case 'stale': return 'warning'
    case 'failed': return 'danger'
    case 'unavailable': return 'warning'
    case 'empty': return 'muted'
    default: return 'muted'
  }
}

/**
 * 状态行说明（徽章后的灰色补充）：release 版本 + 计数 + 漂移/失败原因。
 * 只陈述服务端下发的事实；字段缺失就省略该段，不编造。
 */
export function structureProjectionSummary(state: StructureProjectionState): string {
  const parts: string[] = []
  if (state.version) parts.push(`发布 ${state.version}`)
  if (Number.isFinite(state.nodeCount) && Number.isFinite(state.edgeCount)) {
    parts.push(`结构 ${state.nodeCount} 节点 / ${state.edgeCount} 边`)
  }
  if (state.drift) parts.push('与 SQL 当前定义已漂移，建议重建')
  if (state.error) parts.push(state.error)
  return parts.join(' · ')
}
