import { useMemo } from 'react'
import { PlusIcon } from '@heroicons/react/24/outline'
import type {
  Action,
  LinkType,
  ObjectType,
  Property,
} from '../../types/ontology'
import type { SentinelLink } from '../../../api/sentinelApi'
import {
  compileSentinelCondition,
  isSentinelNumericType,
  sentinelOperatorsForType,
} from './sentinelDefinitionCompiler'
import {
  createEmptySentinelConditionRow,
  nextSentinelAlias,
  SENTINEL_OPERATOR_LABELS,
  syncPatternStages,
  type SentinelConditionRow,
  type SentinelDraft,
} from './sentinelDefinitionModel'
import { SentinelActionBindingsEditor } from './SentinelActionBindingsEditor'

interface SentinelDefinitionEditorProps {
  draft: SentinelDraft
  busy: boolean
  objectTypes: ObjectType[]
  linkTypes: LinkType[]
  actions: Action[]
  objectTypeName: (id: string) => string
  propertiesOf: (objectTypeId: string) => Property[]
  onChange: (draft: SentinelDraft) => void
  onSave: () => Promise<void>
  onCancel: () => void
}

const AGGREGATE_FUNCTION_LABELS: Record<string, string> = {
  count: '次数',
  avg: '平均值',
  sum: '求和',
  min: '最小值',
  max: '最大值',
}

const AGGREGATE_COMPARISON_LABELS: Record<string, string> = {
  gte: '≥ 达到',
  gt: '> 严格超过',
  lte: '≤ 降至',
  lt: '< 低于',
}

export function SentinelDefinitionEditor({
  draft,
  busy,
  objectTypes,
  linkTypes,
  actions,
  objectTypeName,
  propertiesOf,
  onChange,
  onSave,
  onCancel,
}: SentinelDefinitionEditorProps) {
  const patternMode = draft.triggerMode === 'on_pattern'

  // 条件里对象的可读称谓：用对象类型中文名；同类型多个时附代号区分。
  const subjectLabel = (alias: string) => {
    const binding = draft.bindings.find(item => item.alias === alias)
    if (!binding) return alias
    const name = objectTypeName(binding.objectTypeId)
    if (name === '未选择') return `对象 ${alias}`
    const duplicateCount = draft.bindings.filter(
      item => item.objectTypeId === binding.objectTypeId,
    ).length
    return duplicateCount > 1 ? `${name}(${alias})` : name
  }

  const propertyType = (alias: string, propertyName?: string) => {
    const binding = draft.bindings.find(item => item.alias === alias)
    return propertiesOf(binding?.objectTypeId || '')
      .find(property => property.name === propertyName)
      ?.type as string | undefined
  }

  const directedLinkChoices = (
    left: SentinelDraft['bindings'][number],
    right: SentinelDraft['bindings'][number],
  ) => linkTypes.flatMap(linkType => {
    const choices: Array<{
      link: SentinelLink
      displayName: string
    }> = []
    if (
      linkType.sourceObjectTypeId === left.objectTypeId
      && linkType.targetObjectTypeId === right.objectTypeId
    ) {
      choices.push({
        link: {
          from: left.alias,
          linkTypeId: linkType.id,
          to: right.alias,
        },
        displayName: linkType.displayName,
      })
    }
    if (
      linkType.sourceObjectTypeId === right.objectTypeId
      && linkType.targetObjectTypeId === left.objectTypeId
    ) {
      choices.push({
        link: {
          from: right.alias,
          linkTypeId: linkType.id,
          to: left.alias,
        },
        displayName: linkType.displayName,
      })
    }
    return choices
  })

  const setPairLink = (
    leftAlias: string,
    rightAlias: string,
    encoded: string,
  ) => {
    const remaining = draft.links.filter(link => !(
      (link.from === leftAlias && link.to === rightAlias)
      || (link.from === rightAlias && link.to === leftAlias)
    ))
    if (!encoded) {
      onChange({ ...draft, links: remaining })
      return
    }
    const selected = JSON.parse(encoded) as SentinelLink
    onChange({ ...draft, links: [...remaining, selected] })
  }

  // 推断关系的可读描述/歧义提示。
  const relationHint = useMemo(() => {
    if (draft.bindings.length < 2) return null
    const results: { text: string; ambiguous: boolean }[] = []
    for (let leftIndex = 0; leftIndex < draft.bindings.length; leftIndex += 1) {
      for (
        let rightIndex = leftIndex + 1;
        rightIndex < draft.bindings.length;
        rightIndex += 1
      ) {
        const left = draft.bindings[leftIndex]
        const right = draft.bindings[rightIndex]
        if (!left.objectTypeId || !right.objectTypeId) continue
        const configured = draft.links.filter(link => (
          (link.from === left.alias && link.to === right.alias)
          || (link.from === right.alias && link.to === left.alias)
        ))
        if (configured.length === 0) {
          const choices = directedLinkChoices(left, right)
          if (choices.length > 0) {
            results.push({
              text: `${objectTypeName(left.objectTypeId)} 与 ${objectTypeName(right.objectTypeId)} 当前不使用关系（按全组合匹配，可在下方明确选择）`,
              ambiguous: true,
            })
          } else {
            results.push({
              text: `${objectTypeName(left.objectTypeId)} 与 ${objectTypeName(right.objectTypeId)} 之间没有可用关系（按全组合匹配）`,
              ambiguous: false,
            })
          }
        } else {
          configured.forEach(link => {
            const linkType = linkTypes.find(
              item => item.id === link.linkTypeId,
            )
            results.push({
              text: `当前约束：${link.from} —${linkType?.displayName || link.linkTypeId}→ ${link.to}`,
              ambiguous: false,
            })
          })
        }
      }
    }
    return results
  }, [draft.bindings, draft.links, linkTypes])

  const setBinding = (
    index: number,
    patch: Partial<SentinelDraft['bindings'][number]>,
  ) => {
    const previous = draft.bindings[index]
    const bindings = draft.bindings.map((binding, bindingIndex) => (
      bindingIndex === index ? { ...binding, ...patch } : binding
    ))
    const typeChanged = (
      patch.objectTypeId !== undefined
      && patch.objectTypeId !== previous?.objectTypeId
    )
    onChange({
      ...draft,
      bindings,
      // 旧关系的端点类型契约已经失效，必须让用户按新类型重新选择。
      links: typeChanged && previous
        ? draft.links.filter(link => (
            link.from !== previous.alias && link.to !== previous.alias
          ))
        : draft.links,
      // 模式哨兵：stages 与 bindings 镜像（发布门禁硬约束）；
      // filter 显式修改时覆写对应 stage。
      pattern: patternMode
        ? syncPatternStages(
            bindings,
            draft.pattern,
            patch.filter !== undefined && previous
              ? { alias: previous.alias, filter: patch.filter ?? null }
              : undefined,
          )
        : draft.pattern,
    })
  }

  const removeBinding = (alias: string) => {
    const bindings = draft.bindings.filter(
      binding => binding.alias !== alias,
    )
    const links = draft.links.filter(
      link => link.from !== alias && link.to !== alias,
    )
    onChange({
      ...draft,
      bindings,
      links,
      primaryAlias: draft.primaryAlias === alias
        ? (bindings[0]?.alias || '')
        : draft.primaryAlias,
      pattern: patternMode ? syncPatternStages(bindings, draft.pattern) : draft.pattern,
    })
  }

  const addPatternBinding = () => {
    const bindings = [
      ...draft.bindings,
      {
        alias: nextSentinelAlias(draft.bindings),
        objectTypeId: '',
        filter: null,
      },
    ]
    onChange({
      ...draft,
      bindings,
      pattern: syncPatternStages(bindings, draft.pattern),
    })
  }

  const setStageWithin = (alias: string, raw: string) => {
    const parsed = raw === '' ? null : Number(raw)
    onChange({
      ...draft,
      pattern: {
        ...draft.pattern,
        stages: draft.pattern.stages.map(stage => (
          stage.alias === alias
            ? { ...stage, within: parsed }
            : stage
        )),
      },
    })
  }

  const setConditionRow = (
    index: number,
    patch: Partial<SentinelConditionRow>,
  ) => {
    const rows = draft.condRows.map((row, rowIndex) => (
      rowIndex === index ? { ...row, ...patch } : row
    ))
    onChange({ ...draft, condRows: rows })
  }

  return (
    <div className="space-y-4 text-xs">
      {/* 名称 */}
      <div>
        <label className="block text-surface-300 mb-1">哨兵名称</label>
        <input className="inp" value={draft.displayName} placeholder="如：大额订单超信用额度"
          onChange={event => onChange({ ...draft, displayName: event.target.value })} />
      </div>

      {/* 1. 监听对象 — 清晰结构 */}
      <div className="rounded-lg border border-surface-700 p-3 space-y-2">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-surface-200">监听对象</div>
            <div className="text-[10px] text-surface-500">给每个对象类型起个代号，下方条件里用代号引用它的属性</div>
          </div>
          <button className="text-rose-400 whitespace-nowrap"
            onClick={() => (patternMode ? addPatternBinding() : onChange({
              ...draft,
              bindings: [
                ...draft.bindings,
                {
                  alias: nextSentinelAlias(draft.bindings),
                  objectTypeId: '',
                  filter: null,
                },
              ],
            }))}>+ 加阶段对象</button>
        </div>
        {draft.bindings.map((binding, index) => (
          <div key={index} className="flex items-center gap-2">
            <span className="flex items-center gap-1 text-surface-400">
              代号
              <span className="w-6 h-6 rounded bg-rose-500/15 text-rose-300 flex items-center justify-center font-mono font-semibold">{binding.alias}</span>
              =
            </span>
            <select className="inp flex-1" value={binding.objectTypeId}
              onChange={event => setBinding(index, { objectTypeId: event.target.value })}>
              <option value="">选择对象类型…</option>
              {objectTypes.map(objectType => (
                <option key={objectType.id} value={objectType.id}>
                  {objectType.displayName}
                </option>
              ))}
            </select>
            {draft.bindings.length > 1 && (
              <button className="text-red-400 px-1" onClick={() => removeBinding(binding.alias)}>×</button>
            )}
            {patternMode && (
              <div className="flex items-center gap-2 w-full pl-8">
                <span className="text-[10px] text-surface-500 whitespace-nowrap">
                  阶段 {index + 1} 事件条件
                </span>
                <input className="inp flex-1 font-mono"
                  placeholder={index === 0
                    ? "如 a.status == 'submitted'（空=该对象任意变更）"
                    : "如 b.status == 'approved'（空=该对象任意变更）"}
                  value={binding.filter || ''}
                  onChange={event => setBinding(index, { filter: event.target.value || null })} />
                {index > 0 && (
                  <span className="flex items-center gap-1 text-[10px] text-surface-500 whitespace-nowrap">
                    限时
                    <input type="number" className="inp w-20"
                      placeholder={`${draft.pattern.within}`}
                      value={draft.pattern.stages.find(
                        stage => stage.alias === binding.alias,
                      )?.within ?? ''}
                      onChange={event => setStageWithin(binding.alias, event.target.value)} />
                    秒
                  </span>
                )}
              </div>
            )}
          </div>
        ))}
        <div className="flex items-center gap-2 border-t border-surface-700/60 pt-2">
          <span className="text-surface-400 whitespace-nowrap">动作目标对象</span>
          <select className="inp" value={draft.primaryAlias}
            onChange={event => onChange({ ...draft, primaryAlias: event.target.value })}>
            {draft.bindings.map(binding => (
              <option key={binding.alias} value={binding.alias}>
                {subjectLabel(binding.alias)}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* 2. 关系约束 — 关系会改变命中集合，必须显式选择 */}
      {relationHint && relationHint.length > 0 && (
        <div className="rounded-lg border border-surface-700 p-3 space-y-1">
          <div className="text-surface-200">对象关联<span className="text-[10px] text-surface-500 ml-1">（不自动猜测；不选即按全组合匹配）</span></div>
          {relationHint.map((hint, index) => (
            <div key={index} className={`text-[11px] flex items-start gap-1 ${hint.ambiguous ? 'text-amber-300' : 'text-surface-400'}`}>
              <span>{hint.ambiguous ? '⚠' : '↳'}</span><span>{hint.text}</span>
            </div>
          ))}
          {draft.bindings.flatMap((left, leftIndex) =>
            draft.bindings.slice(leftIndex + 1).map(right => {
              const choices = directedLinkChoices(left, right)
              if (choices.length === 0) return null
              const configured = draft.links.filter(link => (
                (link.from === left.alias && link.to === right.alias)
                || (link.from === right.alias && link.to === left.alias)
              ))
              const value = configured.length === 1
                ? JSON.stringify(configured[0])
                : configured.length > 1 ? '__multiple__' : ''
              return (
                <div key={`${left.alias}:${right.alias}`}
                  className="mt-2 grid grid-cols-[minmax(0,1fr)_minmax(180px,1.2fr)] items-center gap-2">
                  <span className="text-[10px] text-surface-400">
                    {subjectLabel(left.alias)} ↔ {subjectLabel(right.alias)}
                  </span>
                  <select className="inp" value={value}
                    onChange={event => setPairLink(
                      left.alias,
                      right.alias,
                      event.target.value,
                    )}>
                    {configured.length > 1 && (
                      <option value="__multiple__" disabled>
                        当前保留 {configured.length} 条关系约束
                      </option>
                    )}
                    <option value="">不使用关系（按全组合匹配）</option>
                    {choices.map(choice => {
                      const link = choice.link
                      return (
                        <option key={JSON.stringify(link)} value={JSON.stringify(link)}>
                          {link.from} —{choice.displayName}→ {link.to}
                        </option>
                      )
                    })}
                  </select>
                </div>
              )
            }),
          )}
        </div>
      )}

      {/* 3. 触发条件 — 句子式逐行，AND/OR，属性比常量/属性（模式模式隐藏） */}
      {patternMode ? (
        <div className="rounded-lg border border-rose-500/40 p-3 space-y-3">
          <div>
            <div className="text-surface-200">事件模式 <span className="text-[10px] text-surface-500 ml-1">按阶段顺序匹配事件序列；上方每个阶段对象即一个阶段（第 1 个为起点）</span></div>
            <div className="mt-1.5 flex flex-wrap items-center gap-3 text-[11px] text-surface-400">
              <span>缺省限时</span>
              <span className="flex items-center gap-1">
                <input type="number" className="inp w-20" value={draft.pattern.within}
                  onChange={event => onChange({
                    ...draft,
                    pattern: { ...draft.pattern, within: Number(event.target.value) || 3600 },
                  })} /> 秒
              </span>
              <label className="flex items-center gap-1">
                <input type="checkbox" checked={draft.pattern.absenceEnabled}
                  onChange={event => onChange({
                    ...draft,
                    pattern: { ...draft.pattern, absenceEnabled: event.target.checked },
                  })} />
                缺失分支（限时内下一阶段未发生也触发，事件边沿为 absence）
              </label>
            </div>
          </div>
          <div className="border-t border-surface-700/60 pt-2 space-y-2">
            <div className="text-surface-300">窗口聚合 <span className="text-[10px] text-surface-500 ml-1">可选；启用后仅允许上方第 1 个阶段对象，按窗口统计事件</span></div>
            {draft.pattern.aggregate ? (
              <div className="flex flex-wrap items-center gap-2">
                <select className="inp-inline" value={draft.pattern.aggregate.function}
                  onChange={event => onChange({
                    ...draft,
                    pattern: {
                      ...draft.pattern,
                      aggregate: {
                        ...draft.pattern.aggregate!,
                        function: event.target.value as any,
                      },
                    },
                  })}>
                  {Object.entries(AGGREGATE_FUNCTION_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>{label}</option>
                  ))}
                </select>
                <select className="inp-inline" value={draft.pattern.aggregate.comparison || 'gte'}
                  onChange={event => onChange({
                    ...draft,
                    pattern: {
                      ...draft.pattern,
                      aggregate: {
                        ...draft.pattern.aggregate!,
                        comparison: event.target.value as any,
                      },
                    },
                  })}>
                  {Object.entries(AGGREGATE_COMPARISON_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>{label}</option>
                  ))}
                </select>
                <input type="number" className="inp-inline w-20" value={draft.pattern.aggregate.threshold}
                  onChange={event => onChange({
                    ...draft,
                    pattern: {
                      ...draft.pattern,
                      aggregate: {
                        ...draft.pattern.aggregate!,
                        threshold: Number(event.target.value),
                      },
                    },
                  })} />
                <span className="text-surface-500">时触发；属性</span>
                <select className="inp-inline"
                  value={draft.pattern.aggregate.property}
                  onChange={event => onChange({
                    ...draft,
                    pattern: {
                      ...draft.pattern,
                      aggregate: {
                        ...draft.pattern.aggregate!,
                        property: event.target.value,
                      },
                    },
                  })}>
                  <option value="">选择属性</option>
                  {propertiesOf(draft.bindings[0]?.objectTypeId || '').map(property => (
                    <option key={property.id} value={property.name}>{property.displayName}</option>
                  ))}
                </select>
                <span className="flex items-center gap-1 text-surface-500">窗口
                  <input type="number" className="inp-inline w-20" value={draft.pattern.aggregate.window}
                    onChange={event => onChange({
                      ...draft,
                      pattern: {
                        ...draft.pattern,
                        aggregate: {
                          ...draft.pattern.aggregate!,
                          window: Number(event.target.value),
                        },
                      },
                    })} /> 秒
                </span>
                <button className="text-surface-500 hover:text-red-400 px-1"
                  onClick={() => onChange({
                    ...draft,
                    pattern: { ...draft.pattern, aggregate: null },
                  })}>× 移除聚合</button>
              </div>
            ) : (
              <button className="text-rose-400 text-[11px]"
                onClick={() => onChange({
                  ...draft,
                  pattern: {
                    ...draft.pattern,
                    aggregate: {
                      property: '',
                      function: 'count',
                      window: 300,
                      threshold: 3,
                      comparison: 'gte',
                    },
                  },
                })}>+ 启用窗口聚合</button>
            )}
          </div>
          <div className="border-t border-surface-700/60 pt-2">
            <div className="text-surface-300 mb-1">模式完成条件 <span className="text-[10px] text-surface-500 ml-1">可选；序列全部完成时对阶段快照求值，支持 changed_within('a.属性', 秒) / prev('a.属性') 时间算子</span></div>
            <textarea className="inp font-mono h-16 resize-none"
              placeholder="如 a.amount > 1000 and prev('a.status') == 'submitted'（留空=不附加条件）"
              value={draft.pattern.condition}
              onChange={event => onChange({
                ...draft,
                pattern: { ...draft.pattern, condition: event.target.value },
              })} />
          </div>
        </div>
      ) : (
      <div className="rounded-lg border border-surface-700 p-3 space-y-2">
        <div className="flex items-center justify-between">
          <div className="text-surface-200">触发条件 <span className="text-[10px] text-surface-500 ml-1">满足下列条件时触发</span></div>
          <label className="flex items-center gap-1 text-[10px] text-surface-400">
            <input type="checkbox" checked={draft.advanced} onChange={event => onChange({ ...draft, advanced: event.target.checked })} />
            高级模式
          </label>
        </div>

        {draft.advanced ? (
          <textarea className="inp font-mono h-20 resize-none" placeholder="如 a.amount > b.credit_limit and a.status == 'submitted'"
            value={draft.conditionRaw} onChange={event => onChange({ ...draft, conditionRaw: event.target.value })} />
        ) : (
          <>
            {draft.condRows.length > 1 && (
              <div className="flex items-center gap-2 text-[11px] text-surface-300">
                满足
                <div className="flex rounded overflow-hidden border border-surface-600">
                  {(['and', 'or'] as const).map(logic => (
                    <button key={logic} onClick={() => onChange({ ...draft, condLogic: logic })}
                      className={`px-2 py-0.5 ${draft.condLogic === logic ? 'bg-rose-500 text-white' : 'text-surface-300'}`}>
                      {logic === 'and' ? '以下全部' : '以下任一'}
                    </button>
                  ))}
                </div>
                条件
              </div>
            )}

            {draft.condRows.length === 0 && (
              <p className="text-[11px] text-surface-500 leading-relaxed">
                还没有条件。点下方「添加条件」，像填一句话一样设置，例如：<br />
                <span className="text-surface-400">当 订单 的 金额 大于 1000</span>。
              </p>
            )}

            {draft.condRows.map((row, index) => {
              const leftProperties = propertiesOf(
                draft.bindings.find(
                  binding => binding.alias === row.leftAlias,
                )?.objectTypeId || '',
              )
              const rightProperties = propertiesOf(
                draft.bindings.find(
                  binding => binding.alias === row.rightAlias,
                )?.objectTypeId || '',
              )
              const leftType = propertyType(row.leftAlias, row.leftProp)
              const operators = sentinelOperatorsForType(leftType)
              return (
                <div key={index} className="rounded border border-surface-700/60 bg-surface-800/40 p-2.5">
                  <div className="flex flex-nowrap items-center gap-1.5">
                    <span className="text-surface-500 shrink-0">当</span>
                    <select className="inp-inline" value={row.leftAlias}
                      onChange={event => setConditionRow(index, { leftAlias: event.target.value, leftProp: '' })}>
                      {draft.bindings.map(binding => (
                        <option key={binding.alias} value={binding.alias}>
                          {subjectLabel(binding.alias)}
                        </option>
                      ))}
                    </select>
                    <span className="text-surface-500">的</span>
                    <select className="inp-inline" value={row.leftProp}
                      onChange={event => setConditionRow(index, {
                        leftProp: event.target.value,
                        op: sentinelOperatorsForType(
                          propertyType(row.leftAlias, event.target.value),
                        )[0],
                      })}>
                      <option value="">选择属性</option>
                      {leftProperties.map(property => (
                        <option key={property.id} value={property.name}>
                          {property.displayName}
                        </option>
                      ))}
                    </select>
                    <select className="inp-inline font-medium text-rose-300" value={row.op}
                      onChange={event => setConditionRow(index, { op: event.target.value })}>
                      {operators.map(operator => (
                        <option key={operator} value={operator}>
                          {SENTINEL_OPERATOR_LABELS[operator]}
                        </option>
                      ))}
                    </select>
                    {row.rightKind === 'value' ? (
                      <input className="inp-inline min-w-[88px]"
                        placeholder={isSentinelNumericType(leftType) ? '如 1000' : '如 已提交'}
                        value={row.rightValue || ''} onChange={event => setConditionRow(index, { rightValue: event.target.value })} />
                    ) : (
                      <span className="inline-flex items-center gap-1.5">
                        <select className="inp-inline" value={row.rightAlias || ''}
                          onChange={event => setConditionRow(index, { rightAlias: event.target.value, rightProp: '' })}>
                          <option value="">对象</option>
                          {draft.bindings.map(binding => (
                            <option key={binding.alias} value={binding.alias}>
                              {subjectLabel(binding.alias)}
                            </option>
                          ))}
                        </select>
                        <span className="text-surface-500">的</span>
                        <select className="inp-inline" value={row.rightProp || ''}
                          onChange={event => setConditionRow(index, { rightProp: event.target.value })}>
                          <option value="">属性</option>
                          {rightProperties.map(property => (
                            <option key={property.id} value={property.name}>
                              {property.displayName}
                            </option>
                          ))}
                        </select>
                      </span>
                    )}
                    <button className="ml-auto text-surface-500 hover:text-red-400 px-1"
                      onClick={() => onChange({ ...draft, condRows: draft.condRows.filter((_, rowIndex) => rowIndex !== index) })}>×</button>
                  </div>
                  <div className="mt-1.5 pl-5">
                    <button className="text-[10px] text-surface-500 hover:text-rose-300"
                      onClick={() => setConditionRow(index, { rightKind: row.rightKind === 'value' ? 'property' : 'value' })}>
                      {row.rightKind === 'value' ? '↔ 改为对比另一个对象的属性' : '↔ 改为对比固定值'}
                    </button>
                  </div>
                </div>
              )
            })}

            <button className="flex items-center gap-1 text-rose-400 text-[11px]"
              onClick={() => onChange({
                ...draft,
                condRows: [
                  ...draft.condRows,
                  createEmptySentinelConditionRow(
                    draft.bindings[0]?.alias || 'a',
                  ),
                ],
              })}>
              <PlusIcon className="w-3.5 h-3.5" /> 添加条件
            </button>

            {draft.condRows.length > 0 && (
              <div className="text-[10px] text-surface-500 break-all border-t border-surface-700/50 pt-1.5">
                生成的规则：<code className="text-amber-300/80">{compileSentinelCondition(draft.condRows, draft.condLogic) || '（条件不完整）'}</code>
              </div>
            )}
          </>
        )}
      </div>
      )}

      {/* 命中后执行的动作 — 无内部滚轮 */}
      <SentinelActionBindingsEditor
        draft={draft}
        actions={actions}
        propertiesOf={propertiesOf}
        subjectLabel={subjectLabel}
        onChange={onChange}
      />

      {/* 触发时机 */}
      <div className="rounded-lg border border-surface-700 p-3 space-y-3">
        <div>
          <div className="text-surface-200 mb-2">触发时机</div>
          <div className="flex flex-wrap items-center gap-4">
            <label className="flex items-center gap-1.5"><input type="checkbox" disabled={patternMode} checked={patternMode || draft.onChange} onChange={event => onChange({ ...draft, onChange: event.target.checked })} /> 数据变化时{patternMode && <span className="text-[10px] text-surface-500">（模式必需）</span>}</label>
            <label className="flex items-center gap-1.5"><input type="checkbox" disabled={patternMode} checked={patternMode || draft.onSchedule} onChange={event => onChange({ ...draft, onSchedule: event.target.checked })} /> 定期扫描{patternMode && <span className="text-[10px] text-surface-500">（超时兜底必需）</span>}</label>
            {draft.onSchedule && (
              <span className="flex items-center gap-1">每
                <input type="number" className="inp w-16" value={draft.scanIntervalSeconds}
                  onChange={event => onChange({ ...draft, scanIntervalSeconds: Number(event.target.value) })} /> 秒
              </span>
            )}
          </div>
        </div>
        <div>
          <div className="text-surface-200 mb-1">触发方式 <span className="text-[10px] text-surface-500 ml-1">条件持续满足时是否重复触发</span></div>
          <select className="inp" value={draft.triggerMode}
            onChange={event => {
              const mode = event.target.value as SentinelDraft['triggerMode']
              onChange({
                ...draft,
                triggerMode: mode,
                // 模式哨兵依赖事件驱动推进 + 定时扫描兜底超时判定，
                // 后端门禁同样强制两者开启。
                onChange: mode === 'on_pattern' ? true : draft.onChange,
                onSchedule: mode === 'on_pattern' ? true : draft.onSchedule,
                pattern: mode === 'on_pattern'
                  ? syncPatternStages(draft.bindings, draft.pattern)
                  : draft.pattern,
              })
            }}>
            <option value="on_enter">仅在"刚满足"时触发一次（推荐，避免重复）</option>
            <option value="on_enter_leave">满足时触发 + 条件消除时也触发（用于收尾）</option>
            <option value="run_on_all">每次都对所有满足的对象执行（电平/批量）</option>
            <option value="on_pattern">事件模式（CEP）：按阶段序列/时间窗/聚合匹配事件</option>
          </select>
        </div>
        <label className="flex items-center gap-1.5 text-surface-300">
          <input type="checkbox" checked={draft.muted} onChange={event => onChange({ ...draft, muted: event.target.checked })} />
          静默（仍评估并记录命中，但不执行动作——可用于上线前观察）
        </label>
      </div>

      <div className="flex items-center gap-2 pt-1">
        <button onClick={() => void onSave()} disabled={busy || !draft.displayName}
          className="flex-1 py-2 rounded-lg bg-rose-500 hover:bg-rose-600 text-white disabled:opacity-50">{draft.id ? '保存' : '创建'}</button>
        <button onClick={onCancel} className="px-4 py-2 rounded-lg border border-surface-600 text-surface-300">取消</button>
      </div>
    </div>
  )
}
