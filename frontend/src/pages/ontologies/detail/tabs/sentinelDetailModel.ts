// 哨兵详情面板的纯展示模型：从 StructureSentinel 推导“执行逻辑”各分区的
// 人读文案（来源 / 触发 / 事件模式 / 动作摘要），供 SentinelDetailSheet 渲染。
// 纯函数、无 React 依赖，展示口径与后端 skill_export 模板保持一致。
import type {
  PublishedWorkspace,
  StructureSentinel,
  StructureSentinelPattern,
} from './structureGraphModel'

export function sentinelOriginLabel(sentinel: StructureSentinel): string {
  return sentinel.origin === 'assistant_dynamic' ? '动态哨兵' : '公共哨兵'
}

const TRIGGER_MODE_LABELS: Record<string, string> = {
  on_enter: '仅当实例新进入匹配集时触发',
  on_enter_leave: '实例进入或离开匹配集时触发',
  run_on_all: '每轮对全部命中实例执行',
  on_pattern: '按事件模式（CEP）触发',
}

export function sentinelTriggerModeLabel(triggerMode?: string): string {
  return TRIGGER_MODE_LABELS[triggerMode || 'on_enter'] || triggerMode || '—'
}

export function sentinelTriggerSummary(sentinel: StructureSentinel): string[] {
  const labels: string[] = []
  if (sentinel.pattern || sentinel.triggerMode === 'on_pattern') labels.push('事件模式')
  if (sentinel.onChange) labels.push('数据变更触发')
  if (sentinel.onSchedule) {
    labels.push(`每 ${sentinel.scanIntervalSeconds || 300} 秒定时扫描`)
  }
  return labels.length ? labels : ['仅手动运行']
}

const COMPARISON_LABELS: Record<string, string> = {
  gte: '≥', gt: '>', lte: '≤', lt: '<',
}

/** CEP 事件模式的单行摘要，格式对齐 DynamicSentinelDrawer 的既有口径。 */
export function sentinelPatternSummary(pattern: StructureSentinelPattern): string {
  const pieces: string[] = [
    (pattern.stages || []).map(stage => stage.alias).join(' → ') || '—',
    `窗口 ${pattern.within ?? 3600} 秒`,
  ]
  if (pattern.absence?.enabled) pieces.push('含缺失分支')
  if (pattern.aggregate) {
    const { aggregate } = pattern
    const comparison = COMPARISON_LABELS[aggregate.comparison || 'gte'] || '≥'
    pieces.push(
      `${aggregate.function}(${aggregate.property}) ${comparison} ${aggregate.threshold} / ${aggregate.window} 秒`,
    )
  }
  if (pattern.condition) pieces.push(`模式条件 ${pattern.condition}`)
  return pieces.join(' · ')
}

export interface SentinelActionSummary {
  id: string
  label: string
  technicalName: string
  requiresApproval: boolean
  /** 当前发布快照中是否存在该动作定义；缺失时如实保留引用（fail-closed）。 */
  available: boolean
  parameterNames: string[]
}

export function sentinelActionSummaries(
  workspace: PublishedWorkspace,
  sentinel: StructureSentinel,
): SentinelActionSummary[] {
  return (sentinel.actionIds || []).map(id => {
    const action = workspace.actions.find(item => item.id === id)
    const bindings = (sentinel.actionParameters || {})[id]
    return {
      id,
      label: action ? action.displayName || action.name : id,
      technicalName: action ? action.name : id,
      requiresApproval: Boolean(action?.requiresApproval),
      available: Boolean(action),
      parameterNames: bindings && typeof bindings === 'object'
        ? Object.keys(bindings)
        : [],
    }
  })
}
