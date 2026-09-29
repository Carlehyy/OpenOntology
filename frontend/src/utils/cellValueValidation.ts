// 单元格值类型校验：与后端 lake_gate._cell_type_ok 同一把尺子（宽松 coercion，
// 只拦明显矛盾的值）。站内数据集编辑器（DatasetEditorModal）与外链维护页
// （PublicManualDatasetPage）共用，保证「前端放行的值后端一定收、后端接受的
// 词表前端不拦」——历史上外链单独一套规则，放行的「是/否」提交后必被后端
// 400，又拦住了后端接受的 yes/no 与带千分位的整数。
//
// 后端权威实现：backend/app/data_channel/datasets/lake_gate.py::_cell_type_ok；
// timestamp 模式与 SchemaInferenceStep._DATE_RE 保持一致。

/** boolean 词表与 SchemaInferenceStep._infer_type 一致；「是/否」后端不收。 */
const BOOLEAN_WORDS = ['true', 'false', 'yes', 'no', '1', '0']

/** 对齐后端 _DATE_RE：年月日在前、日在前、紧凑 8 位、ISO 日期时间。 */
const TIMESTAMP_PATTERNS = [
  /^\d{4}[-/]\d{1,2}[-/]\d{1,2}/, // 2024-01-15 | 2024/1/15（可带时间后缀）
  /^\d{1,2}[-/]\d{1,2}[-/]\d{4}/, // 15/01/2024
  /^\d{4}\d{2}\d{2}$/, // 20240115
  /^\d{4}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}/, // ISO datetime
]

export function cellValueMatchesType(type: string | undefined, value: string): boolean {
  const expected = (type || 'string').toLowerCase()
  const text = value.trim()
  // 与后端一致：空值放行，非空约束由主键/列契约负责，不是类型的职责
  if (expected === 'string' || !text) return true
  switch (expected) {
    case 'integer': {
      // 后端 int(s.replace(',', ''))：先去千分位逗号再按 Python int 解析。
      // 残渣（纯逗号、",,+"、空串）后端必拒，这里同样拒绝。
      const stripped = text.replaceAll(',', '')
      return stripped !== '' && /^[+-]?\d+$/.test(stripped)
    }
    case 'float': {
      // 后端 Python float：去逗号后按十进制形态解析；不接受 0x/0b 十六进制/
      // 二进制字面量（JS Number 会收），也不接受空残渣。
      const stripped = text.replaceAll(',', '')
      return stripped !== ''
        && /^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$/.test(stripped)
    }
    case 'boolean':
      return BOOLEAN_WORDS.includes(text.toLowerCase())
    case 'timestamp':
      return TIMESTAMP_PATTERNS.some(pattern => pattern.test(text))
    case 'json':
      try {
        JSON.parse(text)
        return true
      } catch {
        return false
      }
    default:
      return true
  }
}

const TYPE_HINTS: Record<string, string> = {
  integer: '整数（可用千分位逗号）',
  float: '数字',
  boolean: 'true/false、yes/no 或 1/0',
  timestamp: '日期或日期时间（如 2024-01-15、2024/1/15 10:30、20240115）',
  json: '合法 JSON',
}

/** 返回 null 表示通过；否则返回带列名的中文错误提示。 */
export function validateCellValue(
  columnLabel: string,
  type: string | undefined,
  value: string,
): string | null {
  const expected = (type || 'string').toLowerCase()
  if (cellValueMatchesType(expected, value)) return null
  return `「${columnLabel}」必须是${TYPE_HINTS[expected] ?? '有效值'}`
}
