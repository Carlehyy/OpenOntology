import { formatDateTime } from '../../../utils/datetime.ts'
import type { SlowRequestItem, TraceSpan } from '@/api/monitoring'

const LAYER_NAMES: Record<string, string> = {
  db: '数据库 SQL',
  llm: 'LLM 大模型调用',
  http: '下游 HTTP/WS 服务调用',
}

function percent(part: number, total: number): string {
  return ((part / Math.max(total, 1)) * 100).toFixed(1) + '%'
}

function formatTime(value: string | null): string {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return formatDateTime(date, { seconds: true }) + ' / ' + value
}

/**
 * 把一条慢请求（含调用链）整理成可直接交给其他 Agent 的完整分析提示词。
 */
export function buildAnalysisPrompt(request: SlowRequestItem): string {
  const spans: TraceSpan[] = request.spans ?? []
  const total = Math.max(request.duration_ms, 1)
  const attributed = spans.reduce((sum, span) => sum + (span.duration_ms || 0), 0)
  const unattributed = Math.max(0, request.duration_ms - attributed)

  const lines: string[] = []
  lines.push('# 平台慢接口调用链分析请求')
  lines.push('')
  lines.push('## 背景')
  lines.push(
    'OpenOntology 平台（本体即服务平台）的 API 性能监控捕获到一次慢请求。',
    '请以资深后端性能优化专家的视角，基于下面的调用链数据定位本次请求的耗时瓶颈，',
    '并给出具体、可落地的优化建议，用于指导平台性能优化。',
  )
  lines.push('')
  lines.push('## 技术栈上下文')
  lines.push('- 后端：Python / FastAPI + SQLAlchemy，生产数据库为 PostgreSQL；')
  lines.push('- LLM 调用经平台统一模型网关（OpenAI/Anthropic 兼容协议）发起；')
  lines.push('- Python 脚本 / 推演任务通过 Jupyter Kernel Gateway 执行（HTTP 创建内核 + WebSocket 传输执行）；')
  lines.push('- 其他下游依赖：n8n 工作流、MCP 工具服务、外部 HTTP 接口代理等；')
  lines.push('- 慢请求判定阈值：1000ms；明细与调用链保留 7 天。')
  lines.push('')
  lines.push('## 慢请求概要')
  lines.push('- 接口：' + request.method + ' ' + request.route)
  lines.push('- 总耗时：' + (request.duration_ms / 1000).toFixed(2) + 's（' + request.duration_ms + 'ms）')
  lines.push('- HTTP 状态码：' + request.status_code)
  lines.push('- 发生时间：' + formatTime(request.created_at))
  lines.push('- 请求 ID：' + request.request_id)
  if (request.username) lines.push('- 用户：' + request.username)
  if (request.source_ip) lines.push('- 来源 IP：' + request.source_ip)
  lines.push('')
  lines.push('## 分层耗时汇总')
  const layers: { key: 'db' | 'llm' | 'http'; label: string }[] = [
    { key: 'db', label: '数据库（DB）' },
    { key: 'llm', label: '大模型（LLM）' },
    { key: 'http', label: '下游服务（HTTP/WS）' },
  ]
  const present = layers.filter(layer => request.breakdown?.[layer.key])
  if (!present.length && !unattributed) {
    lines.push('（无分层汇总数据）')
  } else {
    for (const layer of present) {
      const entry = request.breakdown[layer.key]!
      lines.push(
        '- ' + layer.label + '：' + entry.count + ' 次 · ' + (entry.total_ms / 1000).toFixed(2) + 's（占 ' + percent(entry.total_ms, total) + '）',
      )
    }
    if (unattributed > 0) {
      lines.push(
        '- 未归因耗时：' + (unattributed / 1000).toFixed(2) + 's（占 ' + percent(unattributed, total) + '，Python 计算及其他未埋点环节）',
      )
    }
  }
  lines.push('')
  lines.push('## 调用链（按时间顺序）')
  if (!spans.length) {
    lines.push(
      '（该请求未采集到逐步调用链——为旧版本记录的慢请求，仅有分层汇总。请基于概要信息与分层汇总进行分析。）',
    )
  } else {
    lines.push('| # | 层级 | 操作 | 目标 | 开始偏移 | 耗时 | 占比 | 状态 |')
    lines.push('| --- | --- | --- | --- | --- | --- | --- | --- |')
    for (const span of spans) {
      const layerName = LAYER_NAMES[span.layer] ?? span.layer
      lines.push(
        '| ' + [
          span.seq,
          layerName,
          span.name || '-',
          span.target || '-',
          '+' + span.start_ms + 'ms',
          span.duration_ms + 'ms',
          percent(span.duration_ms, total),
          span.status || '-',
        ].join(' | ') + ' |',
      )
    }
    const details = spans.filter(span => span.detail)
    if (details.length) {
      lines.push('')
      lines.push('### 关键步骤明细（SQL 语句等）')
      for (const span of details) {
        const layerName = LAYER_NAMES[span.layer] ?? span.layer
        lines.push('- 步骤 ' + span.seq + '（' + layerName + ' ' + (span.name || '') + ' ' + (span.target || '') + '）：')
        lines.push('')
        lines.push('```sql')
        lines.push(span.detail)
        lines.push('```')
        lines.push('')
      }
    }
    if (request.spans_truncated) {
      lines.push('> 注意：调用链步骤过多，系统按耗时保留了最慢的步骤，存在截断。')
    }
  }
  lines.push('')
  lines.push('## 分析要求')
  lines.push('1. **瓶颈定位**：找出本次请求最耗时的步骤，结合操作类型与目标判断耗时是否合理；')
  lines.push('2. **优化建议**：针对每个显著耗时步骤给出具体建议，例如：')
  lines.push('   - SQL：补充索引、查询改写、减少往返（N+1）、避免大字段/全表扫描等；')
  lines.push('   - LLM：流式输出、控制输出上限、结果缓存、换用更小模型、并行化相互独立的调用等；')
  lines.push('   - 下游服务：超时与重试策略、连接复用、异步化、结果缓存等；')
  lines.push('   - 未归因耗时：结合接口业务逻辑推测可能来源（纯计算、任务调度等待等）并给出排查思路；')
  lines.push('3. **优先级排序**：按 高 / 中 / 低 排序，说明每项的预期收益、实施成本与风险；')
  lines.push('4. **信息补充**：若需要更多上下文（相关代码、表结构、配置、样本数据等），明确列出所需信息清单。')
  lines.push('')
  lines.push('## 输出格式要求')
  lines.push('请以结构化 Markdown 输出：')
  lines.push('1. 瓶颈定位（结论先行）；')
  lines.push('2. 优化建议（按优先级排序，含预期收益 / 风险 / 实施成本）；')
  lines.push('3. 未归因耗时分析；')
  lines.push('4. 需要补充的信息清单。')
  return lines.join('\n')
}
