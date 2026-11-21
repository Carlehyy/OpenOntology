import type { AutoLoopRuleCreate } from '../../../api/autoloop.ts'

export type RuleFormField = 'name' | 'position' | 'sourceKind' | 'ingestKeyId' | 'sourceSystem' | 'channel' | 'titleContains' | 'bodyContains' | 'fieldEquals' | 'titleTemplate' | 'descriptionTemplate' | 'assignee' | 'pattern' | 'targetIssue' | 'sample'
export type RuleFormErrors = Partial<Record<RuleFormField, string>>
export type RuleFieldEqualsRow = { field: string; value: string }

/** 试渲染样例的空白起点：编辑器初始值与兜底路径共用，保证口径一致。 */
export const DEFAULT_SAMPLE = '{"title":"示例标题","body":"示例正文"}'

/** 事件预填的白名单视图：与 AutoLoopTraceDetail 结构兼容，但不引入其完整契约。 */
export interface RuleTraceSource {
  title?: string | null
  source?: { kind?: string | null; system?: string | null; ref?: string | null } | null
  untrustedOriginal?: unknown
}

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

/** 结构化字段等值行版本：行校验给出行输入文案，其余字段边界复用文本校验（负载仍为数组）。 */
export function validateRuleFormRows(form: AutoLoopRuleCreate, rows: readonly RuleFieldEqualsRow[], position: string, editing: boolean): { errors: RuleFormErrors; value?: AutoLoopRuleCreate } {
  if (rows.length > 50) return { errors: { fieldEquals: '字段等值最多填写 50 项' } }
  for (const [index, row] of rows.entries()) {
    const label = `字段等值第 ${index + 1} 项`
    if (!row || typeof row.field !== 'string' || !row.field.trim() || typeof row.value !== 'string') {
      return { errors: { fieldEquals: `${label}的 field 不能为空，value 须为字符串` } }
    }
    if (Array.from(row.field).length > 255 || Array.from(row.value).length > 16384) {
      return { errors: { fieldEquals: `${label}的 field 最多 255 个字符，value 最多 16384 个字符` } }
    }
  }
  // 行已全部合法，序列化为规范文本后走同一校验出口（该路径不会再产生 fieldEquals 错误）。
  return validateRuleForm({ ...form, match: { ...form.match, fieldEquals: [...rows] } }, JSON.stringify(rows), position, editing)
}

export function parsePreviewSample(text: string): Record<string, unknown> {
  const value = parseRuleJson(text, '样例事件')
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('样例事件 JSON 须为对象')
  return value as Record<string, unknown>
}

const SAMPLE_CHAR_LIMIT = 20000
const SAMPLE_STRING_LIMIT = 500

/** 事件样例：优先取原始载荷（限长、顶层超长字符串浅截断），回退只用事件标题，不携带外部正文。 */
export function sampleFromTrace(trace: RuleTraceSource): string {
  const original = trace.untrustedOriginal
  if (original && typeof original === 'object') {
    const text = JSON.stringify(original, null, 2) ?? ''
    if (text.length <= SAMPLE_CHAR_LIMIT) return text
    if (!Array.isArray(original)) {
      const trimmed: Record<string, unknown> = {}
      for (const [key, value] of Object.entries(original)) {
        trimmed[key] = typeof value === 'string' && value.length > SAMPLE_STRING_LIMIT ? `${value.slice(0, SAMPLE_STRING_LIMIT)}…` : value
      }
      const trimmedText = JSON.stringify(trimmed, null, 2) ?? ''
      if (trimmedText.length <= SAMPLE_CHAR_LIMIT) return trimmedText
    }
  }
  const title = typeof trace.title === 'string' && trace.title.trim() ? trace.title.trim().slice(0, SAMPLE_STRING_LIMIT) : '示例标题'
  return JSON.stringify({ title, body: '' }, null, 2)
}

const boundText = (value: string, max: number) => Array.from(value).slice(0, max).join('')

/**
 * 事件预填草稿：只复制来源身份类白名单字段（sourceKind / sourceSystem）；
 * 标题、正文等外部内容不进入匹配条件与模板。名称留空、默认停用，
 * 保存前必须经用户检查（start/imported 路径本身也不会自动保存）。
 */
export function draftFromTrace(trace: RuleTraceSource, position: number): AutoLoopRuleCreate {
  const match: AutoLoopRuleCreate['match'] = {}
  if (trace.source?.kind) match.sourceKind = boundText(trace.source.kind, 40)
  if (trace.source?.system) match.sourceSystem = boundText(trace.source.system, 255)
  // 模板与优先级默认值与 autoloopLogic.newRule 保持一致；enabled 固定 false（预填不直接生效）。
  return { name: '', enabled: false, position, match, titleTemplate: '{{ title }}', descriptionTemplate: '{{ body }}', priority: 'medium', assignee: '', binding: null }
}
