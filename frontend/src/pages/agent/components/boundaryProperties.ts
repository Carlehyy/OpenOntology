/**
 * 属性级授权配置的三态模型（与后端 boundary.allowed_properties 语义一致）：
 * - 整列 null：所有对象类型的全部已声明属性均允许；
 * - map 中某类型为 null：该类型全部属性允许；
 * - map 中某类型为 []：该类型无业务属性；
 * - map 中某类型为名单：仅名单内属性允许；
 * - map 非空但未列出某类型：该类型无业务属性。
 *
 * 这里只做纯数据整理，不渲染 UI、不发请求；未在当前界面展示的类型键
 * （隐藏类型/已删除类型）必须原样保留，保存时不能因界面未渲染而丢失。
 */

export type PropertyRule = string[] | null
export type PropertyRules = Record<string, PropertyRule>
export type PropertyRuleMode = 'all' | 'explicit' | 'none'

export interface DeclaredProperty {
  name?: string
  displayName?: string
  display_name?: string
}

function uniqueNames(names: readonly string[]): string[] {
  return [...new Set(names.map(name => name.trim()).filter(Boolean))]
}

/** 读取 profile.allowedProperties；后端契约外形状按“无属性”安全收紧。 */
export function readPropertyRules(value: unknown): PropertyRules | null {
  if (value === null || value === undefined) return null
  if (typeof value !== 'object' || Array.isArray(value)) return {}
  const rules: PropertyRules = {}
  for (const [typeId, entry] of Object.entries(value as Record<string, unknown>)) {
    if (entry === null) {
      rules[typeId] = null
    } else if (Array.isArray(entry)) {
      rules[typeId] = uniqueNames(entry.filter((item): item is string => typeof item === 'string'))
    } else {
      rules[typeId] = []
    }
  }
  return rules
}

export function ruleMode(rule: PropertyRule | undefined): PropertyRuleMode {
  if (rule === null) return 'all'
  if (!rule || rule.length === 0) return 'none'
  return 'explicit'
}

/** 依据三态按钮给出下一份规则；null(全部) → 指定时保留当前全部勾选。 */
export function ruleForMode(
  rule: PropertyRule | undefined,
  mode: PropertyRuleMode,
  declaredNames: readonly string[],
): PropertyRule {
  if (mode === 'all') return null
  if (mode === 'none') return []
  if (rule === null) return uniqueNames(declaredNames)
  return Array.isArray(rule) ? [...rule] : []
}

export function withPropertyRule(
  rules: PropertyRules,
  typeId: string,
  rule: PropertyRule,
): PropertyRules {
  return {
    ...rules,
    [typeId]: rule === null ? null : uniqueNames(rule),
  }
}

/** 从“全局全部”切到按类型配置时，先用 null 填满当前可见类型（不收紧现有权限）。 */
export function seedPropertyRules(
  typeIds: readonly string[],
  current: PropertyRules | null,
): PropertyRules {
  const next: PropertyRules = { ...(current ?? {}) }
  for (const typeId of typeIds) {
    if (!(typeId in next)) next[typeId] = null
  }
  return next
}

export function toggleExplicitProperty(
  rule: PropertyRule | undefined,
  name: string,
): string[] {
  const current = Array.isArray(rule) ? rule : []
  return current.includes(name)
    ? current.filter(item => item !== name)
    : [...current, name]
}

export interface DeclaredPropertyEntry {
  name: string
  label: string
}

export function declaredPropertyEntries(properties: unknown): DeclaredPropertyEntry[] {
  if (!Array.isArray(properties)) return []
  const seen = new Set<string>()
  const entries: DeclaredPropertyEntry[] = []
  for (const item of properties) {
    if (!item || typeof item !== 'object') continue
    const property = item as DeclaredProperty
    const name = typeof property.name === 'string' ? property.name.trim() : ''
    if (!name || seen.has(name)) continue
    seen.add(name)
    const label = typeof property.displayName === 'string' && property.displayName.trim()
      ? property.displayName.trim()
      : typeof property.display_name === 'string' && property.display_name.trim()
        ? property.display_name.trim()
        : name
    entries.push({ name, label })
  }
  return entries
}

export function declaredPropertyNames(properties: unknown): string[] {
  return declaredPropertyEntries(properties).map(entry => entry.name)
}

/** 已配置但当前模型未声明（或未展示）的属性名，保存时必须保留。 */
export function hiddenExplicitPropertyNames(
  rule: PropertyRule | undefined,
  declaredNames: readonly string[],
): string[] {
  if (!Array.isArray(rule)) return []
  return rule.filter(name => !declaredNames.includes(name))
}

/** 保存前深拷贝并去重；null 必须继续经 resetToAll 表达，而不是写 null 字段。 */
export function serializePropertyRules(rules: PropertyRules): PropertyRules {
  const serialized: PropertyRules = {}
  for (const [typeId, rule] of Object.entries(rules)) {
    serialized[typeId] = rule === null ? null : uniqueNames(rule)
  }
  return serialized
}
