import type { Sentinel, SentinelPattern } from '../../../api/sentinelApi'
import { compileSentinelCondition } from './sentinelDefinitionCompiler.ts'
import {
  syncPatternStages,
  type SentinelConditionRow,
  type SentinelDraft,
} from './sentinelDefinitionModel.ts'

function patternToDraft(
  sentinel: Sentinel,
  bindings: SentinelDraft['bindings'],
): SentinelDraft['pattern'] {
  const raw = sentinel.pattern
  const fallback = {
    stages: bindings.map(binding => ({
      alias: binding.alias,
      objectTypeId: binding.objectTypeId,
      filter: binding.filter ?? null,
      within: null,
    })),
    within: 3600,
    absenceEnabled: false,
    aggregate: null,
    condition: '',
  }
  if (!raw || !Array.isArray(raw.stages) || raw.stages.length === 0) {
    return fallback
  }
  return syncPatternStages(bindings, {
    stages: raw.stages.map(stage => ({
      alias: String(stage.alias || ''),
      objectTypeId: String(stage.objectTypeId || ''),
      filter: stage.filter ?? null,
      within: stage.within ?? null,
    })),
    within: Number(raw.within) || 3600,
    absenceEnabled: !!(raw.absence && (raw.absence as any).enabled),
    aggregate: raw.aggregate ? { ...raw.aggregate } : null,
    condition: raw.condition || '',
  })
}

export function sentinelToDraft(sentinel: Sentinel): SentinelDraft {
  const bindings = (sentinel.bindings || []).map(binding => ({
    alias: binding.alias,
    objectTypeId: binding.objectTypeId,
    filter: binding.filter,
  }))
  return {
    id: sentinel.id,
    displayName: sentinel.displayName,
    description: sentinel.description,
    bindings,
    links: (sentinel.links || []).map(link => ({ ...link })),
    primaryAlias: sentinel.primaryAlias || sentinel.bindings?.[0]?.alias || 'a',
    condRows: (sentinel.conditionRows || []) as SentinelConditionRow[],
    condLogic: (sentinel.conditionLogic || 'and') as 'and' | 'or',
    advanced: !(sentinel.conditionRows?.length) && !!sentinel.condition,
    conditionRaw: sentinel.condition || '',
    pattern: patternToDraft(sentinel, bindings),
    actionIds: sentinel.actionIds || [],
    actionParameters: Object.fromEntries(
      Object.entries(sentinel.actionParameters || {}).map(
        ([actionId, params]) => [actionId, { ...(params || {}) }],
      ),
    ),
    onChange: sentinel.onChange,
    onSchedule: sentinel.onSchedule,
    scanIntervalSeconds: sentinel.scanIntervalSeconds,
    triggerMode: sentinel.triggerMode || 'on_enter',
    muted: !!sentinel.muted,
    enabled: sentinel.enabled,
  }
}

export function sentinelDraftBody(draft: SentinelDraft) {
  const patternMode = draft.triggerMode === 'on_pattern'
  const stages = syncPatternStages(draft.bindings, draft.pattern).stages
  const pattern: SentinelPattern | null = patternMode ? {
    stages: stages.map(stage => ({
      alias: stage.alias,
      objectTypeId: stage.objectTypeId,
      filter: stage.filter ?? null,
      within: stage.within ?? undefined,
    })),
    within: draft.pattern.within,
    absence: { enabled: draft.pattern.absenceEnabled },
    ...(draft.pattern.aggregate ? {
      aggregate: {
        ...draft.pattern.aggregate,
        comparison: draft.pattern.aggregate.comparison || 'gte',
      },
    } : {}),
    ...(draft.pattern.condition ? {
      condition: draft.pattern.condition,
    } : {}),
  } : null
  return {
    name: draft.displayName,
    displayName: draft.displayName,
    description: draft.description,
    bindings: draft.bindings.map(binding => ({
      alias: binding.alias,
      objectTypeId: binding.objectTypeId,
      filter: binding.filter ?? null,
    })),
    links: draft.links,
    condition: patternMode
      ? null
      : draft.advanced
        ? draft.conditionRaw
        : compileSentinelCondition(draft.condRows, draft.condLogic),
    pattern,
    conditionRows: patternMode || draft.advanced ? [] : draft.condRows,
    conditionLogic: draft.condLogic,
    primaryAlias: draft.primaryAlias || draft.bindings[0]?.alias,
    actionIds: draft.actionIds,
    actionParameters: draft.actionParameters,
    // 模式哨兵必须同时开启变化触发与定时扫描（后端门禁同样强制）。
    onChange: patternMode ? true : draft.onChange,
    onSchedule: patternMode ? true : draft.onSchedule,
    scanIntervalSeconds: draft.scanIntervalSeconds,
    triggerMode: draft.triggerMode,
    muted: draft.muted,
    enabled: draft.enabled,
    status: 'published',
  }
}
