// 实例值展示统一转译为用户语言:数字加千分位;date/datetime 列与严格 ISO
// 字符串转本地可读时间。实例时间遵循全站 UTC 规则(utils/datetime.ts):
// 后端序列化的 ISO 串没有 Z 后缀但语义是 UTC,解析前必须补 Z,否则上海
// 时区下比真实时间慢 8 小时,与事实历史的 recordedAt(带 Z)出现两个钟点。
// date 纯日期列只渲染日期部分——纯日期没有时刻,按本地时区补时刻会捏造
// 出“08:00:00”之类的幽灵时间(UTC 午夜平移),误导业务判断。仅命中带时刻
// 与可选时区的完整 ISO 形式,避免误伤 '2026-08'、编号等普通文本。

import { formatDateTime, parseServerTime } from '../../../../utils/datetime.ts'
import { formatProtocolFactSource } from './governanceFormat.ts'
export type InstanceValueDisplay =
  | { kind: 'empty' }
  | { kind: 'array'; text: string }
  | { kind: 'object'; text: string }
  | { kind: 'number'; text: string }
  | { kind: 'date'; text: string; raw: string }
  | { kind: 'datetime'; text: string; raw: string }
  | { kind: 'text'; text: string }

const DATE_ONLY_TYPE_RE = /^date$/i
const MOMENT_TYPE_RE = /^(datetime|timestamp|timestamptz|time)$/i
const ISO_MOMENT_RE = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$/
const ISO_DATE_PREFIX_RE = /^(\d{4})-(\d{2})-(\d{2})/

export function formatInstanceDateTime(value: string): string {
  const date = parseServerTime(value)
  if (!date) return value
  return formatDateTime(date, { seconds: true })
}

export function resolveInstanceValueDisplay(value: unknown, type?: string): InstanceValueDisplay {
  if (value === null || value === undefined || value === '') return { kind: 'empty' }
  if (Array.isArray(value)) return { kind: 'array', text: JSON.stringify(value) }
  if (typeof value === 'object') return { kind: 'object', text: JSON.stringify(value, null, 2) }
  if (typeof value === 'number') return { kind: 'number', text: value.toLocaleString('zh-CN') }

  const text = String(value)
  const columnType = type ? type.trim().replace(/\(.*\)/, '') : ''
  if (columnType && DATE_ONLY_TYPE_RE.test(columnType)) {
    const match = ISO_DATE_PREFIX_RE.exec(text)
    if (match) return { kind: 'date', text: `${match[1]}/${match[2]}/${match[3]}`, raw: text }
    return { kind: 'text', text }
  }
  const isMomentColumn = columnType ? MOMENT_TYPE_RE.test(columnType) : false
  if ((isMomentColumn || ISO_MOMENT_RE.test(text)) && parseServerTime(text) !== null) {
    return { kind: 'datetime', text: formatInstanceDateTime(text), raw: text }
  }
  return { kind: 'text', text }
}

// 实例来源(value.source)统一转译,与总览页“管道灌入”口径一致。
export function instanceSourceLabel(source?: string | null): string {
  const normalized = (source ?? '').trim()
  switch (normalized) {
    case 'pipeline': return '管道灌入'
    case 'collector': return '采集器'
    case 'action': return '动作执行'
    case 'import': return '数据导入'
    case 'manual': return '手工录入'
    case '': return '来源未知'
    default: return normalized
  }
}

// 事实记录的 kind 转译。
export function instanceFactKindLabel(kind?: string | null): string {
  switch ((kind ?? '').trim()) {
    case 'property': return '属性'
    case 'derived': return '派生'
    case 'decision': return '决策'
    case 'link': return '关系'
    case 'object': return '存在性'
    case '': return '事件'
    default: return kind!
  }
}

// 事实来源转译:协议式 URI 与治理区共用同一翻译表(发布快照/动作/函数/人工),
// 避免同一 URI 在两个页面出现两套名字;其余沿用实例来源口径(管道灌入等),
// 未知来源保留原文。
export function instanceFactSourceLabel(source?: string | null): string {
  const normalized = (source ?? '').trim()
  return formatProtocolFactSource(normalized) ?? instanceSourceLabel(normalized)
}

// 列的中文展示名查表:事实属性行与过滤 chip 共用;查不到的列(如历史
// 字段、运行时列)保留英文字段名。
export function columnDisplayLabel(
  propertyName: string,
  columns: Array<{ name: string; label?: string }>,
): string {
  const column = columns.find(item => item.name === propertyName)
  return column?.label || propertyName
}

// 存在性事实的人话文案;返回 null 表示不是存在性事实,按属性行渲染。
export function instanceFactBodyText(fact: { kind?: string | null; present?: boolean }): string | null {
  if ((fact.kind ?? '').trim() !== 'object') return null
  return fact.present === false ? '实例已删除' : '实例创建'
}
