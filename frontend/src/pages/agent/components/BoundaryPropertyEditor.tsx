/**
 * 属性授权编辑器：与 BoundaryDrawer 的对象/链接/动作白名单同屏使用。
 * 三态语义完全由 boundaryProperties.ts 的纯函数表达，避免 UI 自行发明
 * 「未列出类型 = 全部」等与后端相反的解释。
 */
import { useState } from 'react'
import { LockKeyhole } from 'lucide-react'
import {
  declaredPropertyEntries,
  declaredPropertyNames,
  hiddenExplicitPropertyNames,
  ruleForMode,
  ruleMode,
  seedPropertyRules,
  toggleExplicitProperty,
  withPropertyRule,
  type PropertyRuleMode,
  type PropertyRules,
} from './boundaryProperties'

export interface BoundaryPropertyType {
  id: string
  name: string
  displayName: string
  properties?: unknown
}

const MODE_OPTIONS: { value: PropertyRuleMode; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'explicit', label: '指定' },
  { value: 'none', label: '无' },
]

const modeButtonClass = (active: boolean) => `cursor-pointer rounded-md px-2 py-0.5 text-[11px] transition-colors ${
  active
    ? 'bg-[var(--color-primary-light)] font-semibold text-[var(--color-primary)]'
    : 'text-[var(--color-text-tertiary)] hover:bg-[var(--color-bg-hover)]'
}`

export function BoundaryPropertyEditor({
  types,
  value,
  onChange,
  loading = false,
}: {
  types: BoundaryPropertyType[]
  value: PropertyRules | null
  onChange: (value: PropertyRules | null) => void
  loading?: boolean
}) {
  const allPropertiesAllowed = value === null
  // [] 与“未列出”语义相同（无属性），但管理员点「指定」时仍应展开勾选面板。
  const [explicitTypes, setExplicitTypes] = useState<Set<string>>(() => new Set())
  const visibleTypeIds = types.map(type => type.id)
  const hiddenTypeCount = value === null
    ? 0
    : Object.keys(value).filter(typeId => !visibleTypeIds.includes(typeId)).length

  return (
    <section data-testid="agent-property-authorization" className="space-y-2">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h4 className="flex items-center gap-1.5 text-xs font-semibold text-[var(--color-text-primary)]">
            <LockKeyhole size={13} className="text-[var(--color-primary)]" />
            属性授权
          </h4>
          <p className="mt-1 text-[11px] leading-relaxed text-[var(--color-text-tertiary)]">
            默认为全部属性；按类型收紧后，未列出的类型无业务属性。
          </p>
        </div>
        <label className="flex shrink-0 cursor-pointer items-center gap-1.5 text-xs text-[var(--color-text-secondary)]">
          <input
            type="checkbox"
            data-testid="agent-property-all-types"
            checked={allPropertiesAllowed}
            disabled={loading}
            onChange={event => {
              setExplicitTypes(new Set())
              onChange(event.target.checked ? null : seedPropertyRules(visibleTypeIds, value))
            }}
          />
          全部对象类型的全部属性
        </label>
      </div>

      {loading && (
        <p className="text-[11px] text-[var(--color-text-tertiary)]">正在加载本体属性…</p>
      )}

      {!allPropertiesAllowed && (
        <div className="space-y-2" data-testid="agent-property-type-list">
          {!loading && types.length === 0 && (
            <p className="text-[11px] text-[var(--color-text-tertiary)]">（本体中还没有对象类型）</p>
          )}
          {types.map(type => {
            const rule = value?.[type.id]
            const storedMode = ruleMode(rule)
            const explicitOpen = explicitTypes.has(type.id)
            const mode: PropertyRuleMode = storedMode === 'none' && explicitOpen ? 'explicit' : storedMode
            const declared = declaredPropertyEntries(type.properties)
            const declaredNames = declaredPropertyNames(type.properties)
            const hiddenNames = hiddenExplicitPropertyNames(rule, declaredNames)
            const chooseMode = (nextMode: PropertyRuleMode) => {
              setExplicitTypes(previous => {
                const next = new Set(previous)
                if (nextMode === 'explicit') next.add(type.id)
                else next.delete(type.id)
                return next
              })
              onChange(withPropertyRule(
                value ?? {},
                type.id,
                ruleForMode(rule, nextMode, declaredNames),
              ))
            }
            return (
              <div
                key={type.id}
                data-testid={`agent-property-type-${type.id}`}
                className="rounded-md border border-[var(--color-border)] bg-[var(--color-bg-base)] px-2.5 py-2"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="min-w-0 truncate text-[11px] font-medium text-[var(--color-text-primary)]">
                    {type.displayName}
                    {type.name && type.name !== type.displayName && (
                      <code className="ml-1.5 text-[10px] font-normal text-[var(--color-text-tertiary)]">{type.name}</code>
                    )}
                  </span>
                  <div
                    role="radiogroup"
                    aria-label={`${type.displayName}属性授权`}
                    className="flex shrink-0 items-center gap-0.5 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-0.5"
                  >
                    {MODE_OPTIONS.map(option => (
                      <label key={option.value} className={modeButtonClass(mode === option.value)}>
                        <input
                          type="radio"
                          className="sr-only"
                          name={`agent-property-mode-${type.id}`}
                          checked={mode === option.value}
                          onChange={() => chooseMode(option.value)}
                        />
                        {option.label}
                      </label>
                    ))}
                  </div>
                </div>

                {mode === 'explicit' && (
                  <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1">
                    {declared.map(property => (
                      <label
                        key={property.name}
                        className="flex min-w-0 cursor-pointer items-center gap-1.5 text-[11px] text-[var(--color-text-secondary)]"
                      >
                        <input
                          type="checkbox"
                          checked={(Array.isArray(rule) ? rule : []).includes(property.name)}
                          onChange={() => onChange(withPropertyRule(
                            value ?? {},
                            type.id,
                            toggleExplicitProperty(rule, property.name),
                          ))}
                        />
                        <span className="truncate">{property.label}</span>
                        {property.label !== property.name && (
                          <code className="shrink-0 text-[10px] text-[var(--color-text-tertiary)]">{property.name}</code>
                        )}
                      </label>
                    ))}
                    {declaredNames.length === 0 && (
                      <p className="col-span-2 text-[10px] text-[var(--color-text-tertiary)]">（该类型还没有声明属性）</p>
                    )}
                  </div>
                )}

                {hiddenNames.length > 0 && (
                  <p className="mt-1.5 text-[10px] leading-relaxed text-[var(--color-warning)]">
                    另有 {hiddenNames.length} 个已授权属性不在当前模型中（{hiddenNames.join('、')}），保存时原样保留。
                  </p>
                )}
              </div>
            )
          })}
          {hiddenTypeCount > 0 && (
            <p className="text-[10px] leading-relaxed text-[var(--color-text-tertiary)]">
              另有 {hiddenTypeCount} 个未在当前界面展示的对象类型配置将原样保留。
            </p>
          )}
        </div>
      )}
    </section>
  )
}
