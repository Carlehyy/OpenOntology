/** Python 脚本页的工具函数：列类型推断与 traceback 行号解析（纯函数，便于单测） */

/** 与平台类型词表（入湖 columns_typed / 字段契约）一致 */
export type InferredType = 'string' | 'integer' | 'float' | 'boolean' | 'timestamp' | 'null'

export const TYPE_LABELS: Record<InferredType, string> = {
  string: '文本',
  integer: '整数',
  float: '小数',
  boolean: '布尔',
  timestamp: '时间',
  null: '空',
}

// 与后端 SchemaInferenceStep._infer_type 同一判别口径
const DATE_RE = /^\d{4}[-/]\d{1,2}[-/]\d{1,2}|^\d{1,2}[-/]\d{1,2}[-/]\d{4}|^\d{4}\d{2}\d{2}$|^\d{4}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}/
const INT_RE = /^[+-]?\d+$/
const FLOAT_RE = /^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$/

/** 推断单个值的类型；空值返回 null 类型（投票时跳过） */
export function inferValueType(value: unknown): InferredType {
  if (value === null || value === undefined) return 'null'
  if (typeof value === 'boolean') return 'boolean'
  if (typeof value === 'number') return Number.isInteger(value) ? 'integer' : 'float'
  const text = String(value).trim()
  if (!text || ['none', 'null', 'nan'].includes(text.toLowerCase())) return 'null'
  if (DATE_RE.test(text)) return 'timestamp'
  if (['true', 'false', 'yes', 'no', '1', '0'].includes(text.toLowerCase())) return 'boolean'
  const normalized = text.replace(/,/g, '')
  if (INT_RE.test(normalized)) return 'integer'
  if (FLOAT_RE.test(normalized)) return 'float'
  return 'string'
}

const PRIORITY: InferredType[] = ['timestamp', 'integer', 'float', 'boolean', 'string', 'null']

/** 对每列取前若干行样本投票，返回更准确的列类型（与后端多样本投票一致） */
export function inferColumnTypes(
  rows: Array<Record<string, unknown>>,
  columns: string[],
  sampleSize = 10,
): Record<string, InferredType> {
  const sample = rows.slice(0, sampleSize)
  const result: Record<string, InferredType> = {}
  for (const col of columns) {
    const votes = new Map<InferredType, number>()
    for (const row of sample) {
      const type = inferValueType(row[col])
      if (type === 'null') continue
      votes.set(type, (votes.get(type) ?? 0) + 1)
    }
    // 列级类型拓宽：同一列只要出现小数票，整数值就不可能把列拉回整数
    // （JS 中 200.0 === 200 会投整数票，与 100.5 的浮点票平票时曾让
    // PRIORITY 错误裁决为整数——与后端字符串口径「int("100.5") 失败→
    // float」对齐，float 票吸收 integer 票后再参与平票裁决）。
    if (votes.has('float') && votes.has('integer')) {
      votes.set('float', (votes.get('float') ?? 0) + (votes.get('integer') ?? 0))
      votes.delete('integer')
    }
    let best: InferredType = 'string'
    let bestScore: [number, number] = [0, 0]
    for (const [type, count] of votes) {
      // 票数优先，平票时 PRIORITY 中更具体的类型在前
      const score: [number, number] = [count, -PRIORITY.indexOf(type)]
      if (score[0] > bestScore[0] || (score[0] === bestScore[0] && score[1] > bestScore[1])) {
        best = type
        bestScore = score
      }
    }
    result[col] = votes.size === 0 ? 'string' : best
  }
  return result
}

const TRACEBACK_LINE_RE = /File "<string>", line (\d+)/g

/** 从 Jupyter traceback 中提取用户脚本的行号（去重、升序），供点击跳转编辑器 */
export function parseTracebackLines(traceback: string): number[] {
  const lines = new Set<number>()
  for (const match of traceback.matchAll(TRACEBACK_LINE_RE)) {
    const line = Number(match[1])
    if (Number.isInteger(line) && line > 0) lines.add(line)
  }
  return [...lines].sort((a, b) => a - b)
}

/**
 * 轻量 Python 源码整理（不做 AST 级重排，那需要 black；缩进由编辑器
 * indentSelection 完成）：
 *   Tab → 4 空格；去行尾空白；3+ 连续空行收敛为 2 行；保证单个文末换行。
 */
export function tidyPythonSource(source: string): string {
  const lines = source.replace(/\t/g, '    ').split('\n')
  const out: string[] = []
  let blankRun = 0
  for (const line of lines) {
    const trimmedEnd = line.replace(/[ \u00a0]+$/g, '')
    if (trimmedEnd === '') {
      blankRun += 1
      if (blankRun > 2) continue
      // 空行不保留任何空白字符
      out.push('')
    } else {
      blankRun = 0
      out.push(trimmedEnd)
    }
  }
  // 去掉开头多余空行，结尾只留一个换行
  while (out.length > 0 && out[0] === '') out.shift()
  while (out.length > 0 && out[out.length - 1] === '') out.pop()
  return out.join('\n') + '\n'
}
