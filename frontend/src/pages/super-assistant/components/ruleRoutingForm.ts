import type { AutoLoopRuleCreate } from '../../../api/autoloop.ts'

export type RuleFormField = 'name' | 'position' | 'sourceKind' | 'ingestKeyId' | 'sourceSystem' | 'channel' | 'titleContains' | 'bodyContains' | 'fieldEquals' | 'titleTemplate' | 'descriptionTemplate' | 'assignee' | 'pattern' | 'targetIssue' | 'sample'
export type RuleFormErrors = Partial<Record<RuleFormField, string>>

/** 不向表单暴露 JS 引擎原文；定位以 JSON 解析器报告的位置为准。 */
export function parseRuleJson(text: string, label: string): unknown {
  try { return JSON.parse(text) }
  catch (error) {
    const message = error instanceof Error ? error.message : ''
    const location = /line (\d+) column (\d+)/.exec(message)
    const position = /position (\d+)/.exec(message)
    const before = text.slice(0, position ? Number(position[1]) : text.length)
    const line = location ? Number(location[1]) : before.split('\n').length
    const column = location ? Number(location[2]) : before.length - before.lastIndexOf('\n')
    throw new Error(`${label} JSON 格式不正确：第 ${line} 行第 ${column} 列`, { cause: error })
  }
}

/** 对齐 RuleCreate 的字段边界；服务端仍负责模板语法、关联模式等最终校验。 */
export function validateRuleForm(form: AutoLoopRuleCreate, fieldEquals: string, position: string, editing: boolean): { errors: RuleFormErrors; value?: AutoLoopRuleCreate } {
  const errors: RuleFormErrors = {}
  const length = (text: string) => Array.from(text).length
  const limit = (field: RuleFormField, text: string | null | undefined, max: number, label: string) => {
    if (text != null && length(text) > max) errors[field] = `${label}不能超过 ${max} 个字符`
  }
  if (!form.name.trim()) errors.name = '请输入规则名称'
  else limit('name', form.name, 255, '规则名称')
  if (!position.trim() || !Number.isSafeInteger(Number(position)) || Number(position) < 0) errors.position = '匹配顺序须为非负整数'
  for (const [key, label, max] of [
    ['sourceKind', '来源类型', 40], ['ingestKeyId', '来源 Key 标识', 36], ['sourceSystem', '来源系统', 255],
    ['channel', '渠道', 255], ['titleContains', '标题包含条件', 16384], ['bodyContains', '正文包含条件', 16384],
  ] as const) limit(key, form.match[key], max, label)
  let fields: unknown
  try {
    fields = parseRuleJson(fieldEquals, '字段等值')
    if (!Array.isArray(fields)) errors.fieldEquals = '字段等值 JSON 须为数组，例如 [{"field":"severity","value":"high"}]'
    else if (fields.length > 50) errors.fieldEquals = '字段等值最多填写 50 项'
    else {
      for (const [index, entry] of fields.entries()) {
        const label = `字段等值第 ${index + 1} 项`
        if (!entry || typeof entry !== 'object' || Array.isArray(entry)
          || Object.keys(entry).some(key => key !== 'field' && key !== 'value')
          || typeof entry.field !== 'string' || !entry.field.trim() || typeof entry.value !== 'string') {
          errors.fieldEquals = `${label}须为对象，只包含非空字符串 field 和字符串 value`; break
        }
        if (length(entry.field) > 255 || length(entry.value) > 16384) {
          errors.fieldEquals = `${label}的 field 最多 255 个字符，value 最多 16384 个字符`; break
        }
      }
    }
  } catch (error) { errors.fieldEquals = (error as Error).message }
  if (!form.titleTemplate.length) errors.titleTemplate = '请输入标题模板'
  else limit('titleTemplate', form.titleTemplate, 16384, '标题模板')
  limit('descriptionTemplate', form.descriptionTemplate, 16384, '描述模板')
  limit('assignee', form.assignee, 255, '指派对象')
  limit('pattern', form.binding?.pattern, 256, '关联提取模式')
  limit('targetIssue', form.binding?.targetIssue, 255, '关联目标 Issue')
  if (Object.keys(errors).length) return { errors }
  return { errors, value: {
    ...form, name: form.name.trim(), position: Number(position), match: { ...form.match, fieldEquals: fields as AutoLoopRuleCreate['match']['fieldEquals'] },
    // PATCH 未提供字段会保留旧值；新建时仍沿用省略空指派的既有行为。
    assignee: form.assignee?.trim() || (editing ? null : undefined),
  } }
}

export function parsePreviewSample(text: string): Record<string, unknown> {
  const value = parseRuleJson(text, '样例事件')
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('样例事件 JSON 须为对象')
  return value as Record<string, unknown>
}
