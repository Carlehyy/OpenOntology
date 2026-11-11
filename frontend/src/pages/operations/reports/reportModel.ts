/**
 * 运营报告册文章的正文模型（纯函数，Node 直跑单测）。
 *
 * 文章正文是 Markdown，特殊能力全部落在 fenced code block 上：
 *
 * ```sql <query-name>      命名 SQL：阅读态自动执行，图表/表格按名引用
 * ```chart                 图表：有限枚举 bar/line/pie/table/kpi，不开放任意 option
 * ```filter                筛选：首版 text/number/date 标量，手动刷新执行
 *
 * 解析是 fence-aware 的（CommonMark 围栏规则：同种字符、闭合长度不小于
 * 开栏长度），代码块内部的 `#` 行不会进入目录；图表/筛选配置用「key: value」
 * 行式语法，解析失败收集为 issues 就地展示，绝不静默丢弃。
 */

/** 图表类型有限枚举（不开放任意 ECharts option / HTML / JS）。 */
export type ReportChartKind = 'bar' | 'line' | 'pie' | 'table' | 'kpi'

export const REPORT_CHART_KINDS: readonly ReportChartKind[] = ['bar', 'line', 'pie', 'table', 'kpi']

/** 筛选标量类型有限枚举。 */
export type ReportFilterType = 'text' | 'number' | 'date'

export const REPORT_FILTER_TYPES: readonly ReportFilterType[] = ['text', 'number', 'date']

/**
 * 阅读态一次批量执行（execute {queries}）的查询数上限。
 * 超限显式报错，不静默截断成「前 20 条」。
 */
export const REPORT_EXECUTE_MAX_QUERIES = 20

export interface ReportChartConfig {
  /** 引用的命名 SQL（必填）。 */
  query: string
  type: ReportChartKind
  title?: string
  /** 类目/名称列（bar/line/pie 必填）。 */
  x?: string
  /** 数值列，逗号分隔多列（pie/kpi 取单列）。 */
  y: string[]
  /** kpi 展示标签。 */
  label?: string
}

export interface ReportFilterDefinition {
  name: string
  label: string
  type: ReportFilterType
  default: string
}

/** 解析期问题（配置缺失/非法/引用断链），阅读与编辑态就地展示。 */
export interface ReportBlockIssue {
  message: string
}

export type ReportBlock =
  | { kind: 'prose'; markdown: string }
  | { kind: 'sql'; name: string; sql: string; issues: ReportBlockIssue[] }
  | { kind: 'chart'; config: ReportChartConfig | null; issues: ReportBlockIssue[] }
  | { kind: 'filter'; filter: ReportFilterDefinition | null; issues: ReportBlockIssue[] }
  | { kind: 'code'; info: string; code: string }

// CommonMark fence：最多 3 个前导空格 + 至少 3 个同种字符（` 或 ~），
// 之后是 info 字符串（闭栏行其后只允许空白）。
const FENCE_LINE = /^ {0,3}(`{3,}|~{3,})(.*)$/
const HEADING_LINE = /^(#{1,3})\s+(.+?)\s*#*\s*$/

interface FenceState {
  char: string
  length: number
}

function fenceMatches(state: FenceState | null, marker: string, info: string): boolean {
  if (!state) return false
  return state.char === marker[0] && marker.length >= state.length && info.trim() === ''
}

/**
 * 「key: value」行式配置解析（chart/filter fence 体内）。
 *
 * 正则刻意不使用惰性量词配尾随 `\s*$` 的组合（如 `(.*?)\s*$`）：该形态对
 * 「值后接长空白」的行会产生二次方回退扫描，恶意/超长配置行会把解析拖成
 * O(n²)。这里用贪婪 `(.*)$`（零回退）+ 显式 trimEnd 保持原语义，整行线性。
 */
export function parseConfigLines(source: string): Map<string, string> {
  const map = new Map<string, string>()
  for (const line of source.split(/\r?\n/)) {
    const match = line.match(/^\s*([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*)$/)
    if (match) map.set(match[1], match[2].trimEnd())
  }
  return map
}

function chartIssuesFor(config: ReportChartConfig): ReportBlockIssue[] {
  const issues: ReportBlockIssue[] = []
  if (!config.query) issues.push({ message: '图表缺少 query（要引用的命名 SQL）' })
  if (config.type === 'bar' || config.type === 'line' || config.type === 'pie') {
    if (!config.x) issues.push({ message: `${config.type} 图需要 x（类目/名称列）` })
  }
  if (config.type === 'pie' && config.y.length !== 1) {
    issues.push({ message: '饼图需要恰好一个 y 数值列' })
  }
  if ((config.type === 'bar' || config.type === 'line') && config.y.length === 0) {
    issues.push({ message: `${config.type} 图至少需要一个 y 数值列` })
  }
  if (config.type === 'kpi' && config.y.length !== 1) {
    issues.push({ message: 'KPI 需要恰好一个 value/y 数值列' })
  }
  return issues
}

export function parseChartBlock(body: string): {
  config: ReportChartConfig | null
  issues: ReportBlockIssue[]
} {
  const configMap = parseConfigLines(body)
  // type 缺省按 table；显式给出但不合法时返回 null 配置就地报错，
  // 不做「按 table 渲染」的静默降级（与后端 validate_markdown 同口径）。
  const typeText = (configMap.get('type') ?? 'table').trim()
  if (!REPORT_CHART_KINDS.includes(typeText as ReportChartKind)) {
    return {
      config: null,
      issues: [{ message: `未知图表类型「${typeText}」，图表已阻止渲染（可选：${REPORT_CHART_KINDS.join('/')}）` }],
    }
  }
  const type = typeText as ReportChartKind
  const valueColumn = configMap.get('value') ?? ''
  const yText = configMap.get('y') ?? valueColumn
  const config: ReportChartConfig = {
    query: (configMap.get('query') ?? '').trim(),
    type,
    title: configMap.get('title')?.trim() || undefined,
    x: configMap.get('x')?.trim() || undefined,
    y: yText ? yText.split(',').map(part => part.trim()).filter(Boolean) : [],
    label: configMap.get('label')?.trim() || undefined,
  }
  return { config, issues: chartIssuesFor(config) }
}

export function parseFilterBlock(body: string): {
  filter: ReportFilterDefinition | null
  issues: ReportBlockIssue[]
} {
  const configMap = parseConfigLines(body)
  const issues: ReportBlockIssue[] = []
  const name = (configMap.get('name') ?? '').trim()
  // type 缺省按 text；显式给出但不合法时整个筛选不可用（不静默按 text 处理），
  // 引用它的 SQL 参数会因未声明而在执行期被后端显式拒绝。
  const typeText = (configMap.get('type') ?? 'text').trim()
  if (!REPORT_FILTER_TYPES.includes(typeText as ReportFilterType)) {
    return {
      filter: null,
      issues: [{ message: `未知筛选类型「${typeText}」，筛选不可用（可选：${REPORT_FILTER_TYPES.join('/')}）` }],
    }
  }
  const type = typeText as ReportFilterType
  if (!name) issues.push({ message: '筛选缺少 name（SQL 中的参数名）' })
  else if (/\s/.test(name)) issues.push({ message: '筛选 name 不能包含空白字符' })
  const filter: ReportFilterDefinition = {
    name,
    label: configMap.get('label')?.trim() || name,
    type,
    default: configMap.get('default') ?? '',
  }
  return { filter: name ? filter : null, issues }
}

/** SQL fence 的 info 形如 `sql name` 或 `sql name=...`；name 缺失或含空白记为 issue。 */
function sqlNameFromInfo(info: string): { name: string; issues: ReportBlockIssue[] } {
  const parts = info.trim().split(/\s+/)
  const rawName = parts[1] || ''
  const name = rawName.startsWith('name=') ? rawName.slice(5) : rawName
  const issues: ReportBlockIssue[] = []
  if (!name) issues.push({ message: '命名 SQL 缺少查询名（```sql 后跟名称）' })
  else if (parts.length > 2) issues.push({ message: `查询名「${name}」包含空白字符` })
  return { name, issues }
}

export interface ReportHeading {
  id: string
  level: number
  text: string
}

export interface ParsedReportMarkdown {
  blocks: ReportBlock[]
  /** fence 外的 1-3 级标题（代码块内标题不进目录）。 */
  headings: ReportHeading[]
}

/**
 * 把文章 Markdown 解析为 prose / sql / chart / filter / code 块序列。
 * 围栏规则与 CommonMark 一致；不完整的围栏（未闭合）按普通行回退为 prose。
 */
export function parseReportMarkdown(text: string): ParsedReportMarkdown {
  const lines = String(text ?? '').split(/\r?\n/)
  const blocks: ReportBlock[] = []
  const headings: ReportHeading[] = []

  let prose: string[] = []
  let fence: FenceState | null = null
  let fenceLines: string[] = []
  let fenceInfo = ''

  const flushProse = () => {
    if (prose.some(line => line.trim())) blocks.push({ kind: 'prose', markdown: prose.join('\n').trim() })
    prose = []
  }
  const flushFence = () => {
    const info = fenceInfo.trim()
    const lang = info.split(/\s+/)[0]?.toLowerCase() ?? ''
    const body = fenceLines.join('\n').replace(/^\n+|\n+$/g, '')
    if (lang === 'sql') {
      const { name, issues } = sqlNameFromInfo(info)
      blocks.push({ kind: 'sql', name, sql: body, issues })
    } else if (lang === 'chart') {
      const parsed = parseChartBlock(body)
      blocks.push({ kind: 'chart', config: parsed.config, issues: parsed.issues })
    } else if (lang === 'filter') {
      const parsed = parseFilterBlock(body)
      blocks.push({ kind: 'filter', filter: parsed.filter, issues: parsed.issues })
    } else if (lang) {
      blocks.push({ kind: 'code', info: lang, code: body })
    } else {
      // 无语言的围栏按普通代码块处理
      blocks.push({ kind: 'code', info: '', code: body })
    }
    fence = null
    fenceLines = []
    fenceInfo = ''
  }

  for (const line of lines) {
    const fenceMatch = line.match(FENCE_LINE)
    if (fenceMatch) {
      const marker = fenceMatch[1]
      const info = fenceMatch[2] ?? ''
      if (!fence) {
        flushProse()
        fence = { char: marker[0], length: marker.length }
        fenceInfo = info
        fenceLines = []
        continue
      }
      if (fenceMatches(fence, marker, info)) {
        flushFence()
        continue
      }
    }
    if (fence) {
      fenceLines.push(line)
      continue
    }
    const heading = line.match(HEADING_LINE)
    if (heading) {
      prose.push(line)
      headings.push({
        id: `report-heading-${headings.length}`,
        level: heading[1].length,
        text: heading[2].trim(),
      })
      continue
    }
    prose.push(line)
  }
  if (fence) {
    // 未闭合围栏：整段按原文回退 prose，避免静默丢内容
    prose.push(...([fenceInfo ? `\`\`\`${fenceInfo}` : '```', ...fenceLines] as string[]))
  }
  flushProse()

  return { blocks, headings }
}

/** 册内全部命名 SQL（按文档顺序，供执行与引用校验）。 */
export function sqlBlocksOf(blocks: ReportBlock[]): Extract<ReportBlock, { kind: 'sql' }>[] {
  return blocks.filter((block): block is Extract<ReportBlock, { kind: 'sql' }> => block.kind === 'sql')
}

/** 重复查询名检测（同名后写覆盖前写，阅读态必须显式报错）。 */
export function duplicateQueryNames(blocks: ReportBlock[]): string[] {
  const seen = new Set<string>()
  const duplicates = new Set<string>()
  for (const block of sqlBlocksOf(blocks)) {
    if (!block.name) continue
    if (seen.has(block.name)) duplicates.add(block.name)
    seen.add(block.name)
  }
  return [...duplicates]
}

/** 图表/表格引用的 query 名是否都能在命名 SQL 中找到（断链检测）。 */
export function chartQueryRefs(blocks: ReportBlock[]): { query: string; defined: boolean }[] {
  const defined = new Set(sqlBlocksOf(blocks).map(block => block.name).filter(Boolean))
  const refs: { query: string; defined: boolean }[] = []
  for (const block of blocks) {
    if (block.kind !== 'chart' || !block.config) continue
    refs.push({ query: block.config.query, defined: defined.has(block.config.query) })
  }
  return refs
}

/** filter 值 → execute params；number 解析失败收集为错误（不静默传字符串）。 */
export function buildExecuteParams(
  filters: ReportFilterDefinition[],
  values: Record<string, string>,
): { params: Record<string, string | number>; errors: string[] } {
  const params: Record<string, string | number> = {}
  const errors: string[] = []
  for (const filter of filters) {
    const raw = (values[filter.name] ?? '').trim()
    if (!raw) continue
    if (filter.type === 'number') {
      const num = Number(raw)
      if (!Number.isFinite(num)) {
        errors.push(`筛选「${filter.label}」需要数字`)
        continue
      }
      params[filter.name] = num
    } else {
      params[filter.name] = raw
    }
  }
  return { params, errors }
}

/** filter 默认值表（进入阅读态的初始值）。 */
export function defaultFilterValues(filters: ReportFilterDefinition[]): Record<string, string> {
  const values: Record<string, string> = {}
  for (const filter of filters) values[filter.name] = filter.default
  return values
}

/**
 * 把编辑后的 SQL 写回正文里同名围栏的体内（围栏行与缩进保持原样）。
 *
 * 与 parseReportMarkdown 同一套 fence 状态机：进入任意围栏后，只有
 * 「同种字符且长度不小于开栏、其后仅空白」的行才是闭栏；正文/文档示例
 * 围栏体内出现的 `sql <name>` 文本不会被误认为替换目标，嵌套代码示例
 * 因此安全。未找到同名围栏时原文返回（调用方用 toast 提示）。
 */
export function replaceSqlBlock(markdown: string, name: string, nextSql: string): string {
  const lines = markdown.split(/\r?\n/)
  let fence: FenceState | null = null
  let targetBodyStart = -1
  for (let i = 0; i < lines.length; i++) {
    const match = lines[i].match(FENCE_LINE)
    if (!match) continue
    const marker = match[1]
    const info = (match[2] ?? '').trim()
    if (!fence) {
      // 顶层开栏：任何围栏都进入跟踪；仅「sql <name>」精确命中才标记为目标。
      const parts = info.split(/\s+/)
      const candidate = parts[1]?.startsWith('name=') ? parts[1].slice(5) : parts[1]
      if (parts[0]?.toLowerCase() === 'sql' && candidate === name && parts.length === 2) {
        targetBodyStart = i + 1
      } else {
        targetBodyStart = -1
      }
      fence = { char: marker[0], length: marker.length }
      continue
    }
    if (fenceMatches(fence, marker, info)) {
      if (targetBodyStart >= 0) {
        // 命中目标围栏的闭栏：替换 [targetBodyStart, i) 区间
        const replaced = nextSql.length ? nextSql.replace(/\n+$/, '').split(/\r?\n/) : []
        return [...lines.slice(0, targetBodyStart), ...replaced, ...lines.slice(i)].join('\n')
      }
      fence = null
    }
  }
  return markdown
}

/** 顺序数组重排（文章上移/下移）；越界移动原文返回。 */
export function moveItem<T>(items: T[], from: number, to: number): T[] {
  if (from === to) return items
  if (from < 0 || from >= items.length || to < 0 || to >= items.length) return items
  const next = [...items]
  const [moved] = next.splice(from, 1)
  next.splice(to, 0, moved)
  return next
}

/** 查询结果单元格的展示文本（null/undefined → —，对象 JSON 化）。 */
export function formatQueryCell(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}
