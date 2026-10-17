/**
 * 模板列表派生筛选（任务实例弹窗，YIYA-44/45）。
 *
 * 数据源是 listTemplates('') 的完整非分页列表（重新激活按 revision 匹配
 * 也依赖这份完整数据），筛选在弹窗内派生，不改独立页 name OR description
 * 的服务端搜索契约。来源依据 is_preset（API 缺省视为用户自建）。
 */

/** 模板来源单选：严格两项，不允许取消到无选中 */
export type TemplateSource = 'preset' | 'personal'

export interface TemplateFilterInput {
  /** 仅展示 is_preset === true 的模板 */
  is_preset?: boolean
  name: string
}

export function isPresetTemplate(
  template: { is_preset?: boolean },
): boolean {
  return template.is_preset === true
}

export interface TemplateFilterState {
  keyword: string
  source: TemplateSource
}

/**
 * 关键词（仅名称，忽略首尾空格、不区分大小写的子串匹配）× 来源（预置/个人）
 * 取交集；清空关键词等价于仅按来源筛选。不匹配描述，不做模糊排序/纠错。
 */
export function filterTemplates<T extends TemplateFilterInput>(
  templates: readonly T[],
  { keyword, source }: TemplateFilterState,
): T[] {
  const needle = keyword.trim().toLowerCase()
  return templates.filter(template => {
    if (isPresetTemplate(template) !== (source === 'preset')) return false
    if (!needle) return true
    return template.name.toLowerCase().includes(needle)
  })
}

export type TemplateEmptyKind =
  | 'none'       // 平台还没有任何模板（全局空态）
  | 'category'   // 当前来源分类下没有模板（个人空态）
  | 'nomatch'    // 分类下有模板，但关键词无匹配
  | null         // 有结果

/**
 * 空态分类：三态必须可区分，不能把「关键词无匹配」说成「从未创建」。
 * 优先级：全局无模板 > 分类无模板 > 关键词无匹配。
 */
export function templateEmptyKind(
  templates: readonly TemplateFilterInput[],
  { keyword, source }: TemplateFilterState,
): TemplateEmptyKind {
  if (!templates.length) return 'none'
  const ofSource = templates.filter(
    template => isPresetTemplate(template) === (source === 'preset'))
  if (!ofSource.length) return 'category'
  return filterTemplates(templates, { keyword, source }).length ? null : 'nomatch'
}
