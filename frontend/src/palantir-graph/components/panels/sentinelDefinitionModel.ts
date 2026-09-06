import type {
  SentinelLink,
  SentinelPatternAggregate,
} from '../../../api/sentinelApi'

export type DefinitionLoadState = 'idle' | 'loading' | 'ready' | 'error'

export interface SentinelConditionRow {
  leftAlias: string
  leftProp: string
  op: string
  rightKind: 'property' | 'value'
  rightAlias?: string
  rightProp?: string
  rightValue?: string
}

export interface SentinelPatternStageDraft {
  alias: string
  objectTypeId: string
  filter?: string | null
  // null = 使用 pattern 级缺省窗口。
  within?: number | null
}

export interface SentinelPatternDraft {
  stages: SentinelPatternStageDraft[]
  within: number
  absenceEnabled: boolean
  aggregate: SentinelPatternAggregate | null
  condition: string
}

export interface SentinelDraft {
  id?: string
  displayName: string
  description?: string
  bindings: { alias: string; objectTypeId: string; filter?: string | null }[]
  // 关系会改变命中集合和动作对象，必须由用户明确选择；[] 表示全组合。
  links: SentinelLink[]
  primaryAlias: string
  condRows: SentinelConditionRow[]
  condLogic: 'and' | 'or'
  advanced: boolean
  conditionRaw: string
  // CEP 事件模式（triggerMode='on_pattern'）；stages 与 bindings 镜像，
  // 由 syncPatternStages 保持同步（绑定即阶段，filter 复用 binding.filter）。
  pattern: SentinelPatternDraft
  actionIds: string[]
  actionParameters: Record<string, Record<string, unknown>>
  onChange: boolean
  onSchedule: boolean
  scanIntervalSeconds: number
  triggerMode: 'on_enter' | 'on_enter_leave' | 'run_on_all' | 'on_pattern'
  muted: boolean
  enabled: boolean
}

export type SentinelParameterMode =
  | 'default'
  | 'property'
  | 'constant'
  | 'primary_id'
  | 'event'
  | 'template'
  | 'advanced'

export const SENTINEL_OPERATOR_LABELS: Record<string, string> = {
  '==': '等于',
  '!=': '不等于',
  '>': '大于',
  '>=': '大于等于',
  '<': '小于',
  '<=': '小于等于',
  contains: '包含',
}

export const SENTINEL_EVENT_PARAMETER_PROPERTIES = [
  ['edge', '触发边沿（enter/leave）'],
  ['matchKey', '命中键'],
  ['occurredAt', '触发时间'],
  ['sentinelId', '哨兵 ID'],
  ['sentinelName', '哨兵名称'],
] as const

const aliasOf = (index: number) => String.fromCharCode(97 + index)

/** 生成首个未占用的代号——删掉中间绑定再添加时不能撞车（后端按 alias 作键）。 */
export function nextSentinelAlias(bindings: { alias: string }[]) {
  const used = new Set(bindings.map(binding => binding.alias))
  for (let index = 0; index < 26; index += 1) {
    const alias = aliasOf(index)
    if (!used.has(alias)) return alias
  }
  return `x${bindings.length}`
}

export const createEmptySentinelConditionRow = (
  alias: string,
): SentinelConditionRow => ({
  leftAlias: alias,
  leftProp: '',
  op: '>=',
  rightKind: 'value',
  rightValue: '',
})

export const createEmptySentinelDraft = (): SentinelDraft => ({
  displayName: '',
  description: '',
  bindings: [{ alias: 'a', objectTypeId: '' }],
  links: [],
  primaryAlias: 'a',
  condRows: [],
  condLogic: 'and',
  advanced: false,
  conditionRaw: '',
  pattern: {
    stages: [{ alias: 'a', objectTypeId: '', filter: null, within: null }],
    within: 3600,
    absenceEnabled: false,
    aggregate: null,
    condition: '',
  },
  actionIds: [],
  actionParameters: {},
  onChange: true,
  onSchedule: false,
  scanIntervalSeconds: 300,
  triggerMode: 'on_enter',
  muted: false,
  enabled: true,
})

/**
 * 把 pattern.stages 与 bindings 重新镜像（发布门禁的硬约束）。
 * 绑定即阶段：alias/objectTypeId 跟随 bindings，within 按别名保留；
 * filter 保留 stage 自身的值（助手往返时 stage.filter 是权威），
 * 仅当编辑器显式修改某别名的过滤时通过 filterPatch 覆写。
 */
export function syncPatternStages(
  bindings: SentinelDraft['bindings'],
  pattern: SentinelPatternDraft,
  filterPatch?: { alias: string; filter: string | null },
): SentinelPatternDraft {
  const stageByAlias = new Map(
    pattern.stages.map(stage => [stage.alias, stage]),
  )
  return {
    ...pattern,
    stages: bindings.map(binding => {
      const stage = stageByAlias.get(binding.alias)
      const filter = filterPatch && filterPatch.alias === binding.alias
        ? filterPatch.filter
        : stage?.filter ?? binding.filter ?? null
      return {
        alias: binding.alias,
        objectTypeId: binding.objectTypeId,
        filter,
        within: stage?.within ?? null,
      }
    }),
  }
}
