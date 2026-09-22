/**
 * 试运行输出展示的共享工具：单元格序列化 + n8n 骨架回显识别。
 *
 * 列表页试运行弹窗、编辑向导 SampleTable / DryRunPagedTable 三处渲染同一份
 * dry-run 数据，序列化必须走同一函数——此前向导侧用 String() 直接渲染对象，
 * 单元格出现 [object Object]，而试运行弹窗是 JSON 文本（UX 评审 §1.1）。
 */

/** 单元格值的统一序列化：对象转 JSON 文本，null/undefined 转空串，其余转字符串 */
export function displayCellValue(value: unknown): string {
  if (value == null) return ''
  if (typeof value === 'object') {
    try { return JSON.stringify(value) } catch { return String(value) }
  }
  return String(value)
}

/** n8n 骨架工作流 webhook 回显的默认列集合（未编排时输出即请求本身） */
const WEBHOOK_ECHO_COLUMNS = ['headers', 'params', 'query', 'body', 'webhookUrl', 'executionMode']

/**
 * 未编排的 n8n 骨架工作流试运行时，输出列恰好是 webhook 回显形状——
 * 此时"输出"是 n8n 收到的请求本身，不是业务数据。
 * 精确匹配整个列集合，避免误伤响应体恰好含 body/headers 等列名的真实流水线。
 */
export function isWebhookEchoColumns(columns: string[]): boolean {
  if (columns.length !== WEBHOOK_ECHO_COLUMNS.length) return false
  const present = new Set(columns)
  return WEBHOOK_ECHO_COLUMNS.every(column => present.has(column))
}
