import type { SuperMessage, ToolStep } from '@/api/superAssistant'

/**
 * 部分模型会把整段答复包进 ```markdown 围栏。
 * react-markdown 会把它显示成源码；当围栏内容占答复主体时剥掉外层。
 */
export function normalizeAssistantMarkdown(value: string) {
  const normalized = value.replace(/\r\n?/g, '\n')
  const lines = normalized.split('\n')
  const blocks: Array<{ start: number; end: number; contentLength: number }> = []

  for (let index = 0; index < lines.length; index += 1) {
    const opening = /^\s*(`{3,}|~{3,})[ \t]*(?:markdown|md)[ \t]*$/i.exec(lines[index])
    if (!opening) continue
    const marker = opening[1][0]
    const minimumLength = opening[1].length

    for (let end = index + 1; end < lines.length; end += 1) {
      const closing = /^\s*(`+|~+)\s*$/.exec(lines[end])
      if (!closing || closing[1][0] !== marker || closing[1].length < minimumLength) continue
      blocks.push({
        start: index,
        end,
        contentLength: lines.slice(index + 1, end).join('\n').trim().length,
      })
      index = end
      break
    }
  }

  if (blocks.length > 0) {
    const dominant = blocks.reduce((best, block) => block.contentLength > best.contentLength ? block : best)
    const outside = [...lines.slice(0, dominant.start), ...lines.slice(dominant.end + 1)].join('\n').trim()
    const totalLength = normalized.trim().length || 1
    if (dominant.contentLength / totalLength >= 0.45 || outside.length <= 240) {
      return [
        ...lines.slice(0, dominant.start),
        ...lines.slice(dominant.start + 1, dominant.end),
        ...lines.slice(dominant.end + 1),
      ].join('\n').trim()
    }
  }

  const unfinishedOpening = lines.findIndex(line => /^\s*(`{3,}|~{3,})[ \t]*(?:markdown|md)[ \t]*$/i.test(line))
  if (blocks.length === 0 && unfinishedOpening >= 0 && lines.slice(0, unfinishedOpening).join('\n').trim().length <= 240) {
    return [...lines.slice(0, unfinishedOpening), ...lines.slice(unfinishedOpening + 1)].join('\n').trim()
  }

  return normalized
}

export function processSummary(steps: ToolStep[]) {
  if (steps.length === 1) return '1 个工具'
  if (steps.length > 1) return `${steps.length} 个工具`
  return '已思考'
}

/**
 * 多轮 Agent 会把边做边说的过程写进同一段 content，真正的答复通常从第一个标题开始。
 * 围栏内的 # 不算标题，以免把代码/引用切碎。切不出两端则原样返回。
 */
export function splitProcessAndAnswer(content: string) {
  const lines = content.replace(/\r\n?/g, '\n').split('\n')
  let inFence = false
  let fenceMarker = ''
  let fenceLength = 0

  for (let index = 0; index < lines.length; index += 1) {
    const opening = /^(\s*)(`{3,}|~{3,})/.exec(lines[index])
    if (opening) {
      const marker = opening[2][0]
      const length = opening[2].length
      if (!inFence) {
        inFence = true
        fenceMarker = marker
        fenceLength = length
      } else if (marker === fenceMarker && length >= fenceLength) {
        inFence = false
        fenceMarker = ''
        fenceLength = 0
      }
      continue
    }
    if (inFence || !/^#{1,3}[ \t]+\S/.test(lines[index])) continue
    if (index === 0) return { process: '', answer: content }
    const process = lines.slice(0, index).join('\n').trim()
    const answer = lines.slice(index).join('\n').trim()
    if (!process || !answer) return { process: '', answer: content }
    return { process, answer }
  }

  return { process: '', answer: content }
}

/** 常用内置工具的中文友好名：仅覆盖平台自带工具（与后端注册名一一对应），
 *  MCP/外部工具一律直出原名；原名等宽保留在旁边，用户引用与审计对得上 */
export const BUILTIN_TOOL_FRIENDLY_NAMES: Record<string, string> = {
  web_fetch: '抓取网页',
  web_search: '搜索互联网',
  think: '记录思考',
  subagent: '委派子代理',
  todo_write: '写入步骤清单',
  todo_read: '读取步骤清单',
  memory_search: '检索长期记忆',
  memory_save: '保存记忆',
  memory_delete: '删除记忆',
  memory_distill: '扫描近重复记忆',
  palace_zones: '列出记忆分区',
  palace_read_zone: '读取记忆分区',
  palace_recall: '回忆记忆宫殿',
  palace_graph_search: '检索知识图谱',
  palace_graph_files: '列出文档库',
  use_skill: '读取 Skill 指令',
  read_skill_file: '读取 Skill 文件',
  propose_skill: '提炼 Skill 候选',
  list_session_files: '列出会话附件',
  read_session_file: '读取会话附件',
  browser_open: '打开浏览器',
  browser_navigate: '打开网页',
  browser_click_element: '点击元素',
  browser_click_text: '点击文本',
  browser_type: '输入文字',
  browser_scroll: '滚动页面',
  browser_state: '读取页面状态',
  browser_network_requests: '查看网络请求',
  browser_page_resources: '查看页面资源',
  browser_save_resource: '保存资源',
}

export function toolStatusLabel(status: string) {
  if (status === 'running') return '进行中'
  if (status === 'awaiting_confirmation') return '待确认'
  if (status === 'success' || status === 'complete') return '完成'
  if (status === 'cancelled' || status === 'denied' || status === 'expired') return '已取消'
  if (status === 'error' || status === 'failed') return '失败'
  return status
}

export interface ToolStepGroup {
  toolName: string
  steps: ToolStep[]
  /** 组内第一步在原 steps 中的下标，作 React key 用 */
  startIndex: number
}

/** 连续同名工具折叠为一组（同名不连续不合并，保留时间线语义）：
 *  Agent 轮询类工具常连续出现多条，逐条平铺会把有信息量的步骤挤出视口 */
export function groupConsecutiveSteps(steps: ToolStep[]): ToolStepGroup[] {
  const groups: ToolStepGroup[] = []
  steps.forEach((step, index) => {
    const last = groups[groups.length - 1]
    if (last && last.toolName === step.toolName) last.steps.push(step)
    else groups.push({ toolName: step.toolName, steps: [step], startIndex: index })
  })
  return groups
}

/** 组内聚合状态：待确认 > 进行中 > 失败 > 已取消 > 完成 */
export function toolGroupStatus(steps: ToolStep[]) {
  const statuses = steps.map(step => step.status)
  if (statuses.includes('awaiting_confirmation')) return 'awaiting_confirmation'
  if (statuses.includes('running')) return 'running'
  if (statuses.some(status => status === 'error' || status === 'failed')) return 'error'
  if (statuses.some(status => status === 'cancelled' || status === 'denied' || status === 'expired')) return 'cancelled'
  return 'success'
}

/**
 * 全局搜索摘要的展示层净化：去掉 markdown 语法符号（围栏、标题符、强调星号、行内码），
 * 再截取关键词前后各约 radius 字的窗口；关键词未命中时截前 fallbackLimit 字。
 * 只做语法剥离，不渲染 markdown——命中哪句话必须一眼可读。
 * 单下划线强调不处理：会破坏 snake_case 工具名。
 */
export function plainSnippet(text: string, keyword: string, radius = 30, fallbackLimit = 64) {
  const plain = text
    .replace(/```[\w-]*\n?/g, '')
    .replace(/~~~[\w-]*\n?/g, '')
    .replace(/`([^`\n]*)`/g, '$1')
    .replace(/^\s{0,3}#{1,6}[ \t]+/gm, '')
    .replace(/\*\*([^*\n]+)\*\*/g, '$1')
    .replace(/__([^_\n]+)__/g, '$1')
    .replace(/\*([^*\n]+)\*/g, '$1')
    .replace(/\s+/g, ' ')
    .trim()
  const key = keyword.trim().toLowerCase()
  const index = key ? plain.toLowerCase().indexOf(key) : -1
  if (index < 0) {
    return plain.length > fallbackLimit ? `${plain.slice(0, fallbackLimit)}…` : plain
  }
  const start = Math.max(0, index - radius)
  const end = Math.min(plain.length, index + key.length + radius)
  return `${start > 0 ? '…' : ''}${plain.slice(start, end)}${end < plain.length ? '…' : ''}`
}

export function appendToolStart(steps: ToolStep[], payload: {
  toolName: string
  arguments?: Record<string, unknown>
  toolRunId?: string
}): ToolStep[] {
  return [...steps, {
    toolName: payload.toolName,
    status: 'running',
    arguments: payload.arguments,
    toolRunId: payload.toolRunId,
  }]
}

export function patchToolStep(steps: ToolStep[], payload: {
  toolRunId?: string
  status: string
  preview?: string
}): ToolStep[] {
  if (steps.length === 0) return steps
  let index = -1
  if (payload.toolRunId) index = steps.findIndex(step => step.toolRunId === payload.toolRunId)
  if (index < 0) {
    for (let cursor = steps.length - 1; cursor >= 0; cursor -= 1) {
      if (steps[cursor].status === 'running' || steps[cursor].status === 'awaiting_confirmation') {
        index = cursor
        break
      }
    }
  }
  if (index < 0) index = steps.length - 1
  return steps.map((step, current) => current === index
    ? {
        ...step,
        status: payload.status,
        preview: payload.preview !== undefined ? payload.preview : step.preview,
        toolRunId: payload.toolRunId || step.toolRunId,
      }
    : step)
}

export function enqueueMessage(queue: string[], message: string) {
  const trimmed = message.trim()
  if (!trimmed) return queue
  return [...queue, trimmed]
}

export function shiftQueue(queue: string[]) {
  if (queue.length === 0) return { next: null as string | null, rest: queue }
  const [next, ...rest] = queue
  return { next, rest }
}

/** 本页流式生成中的增量缓冲（SuperAssistantPage.streamsRef 的最小视图） */
export interface StreamOverlay {
  messageId: string
  content: string
  steps: ToolStep[]
  status: SuperMessage['status']
  tokenUsage: Record<string, number>
  thinkingRound: number | null
}

/**
 * 多端同步的服务端消息合并。服务端列表是持久化事实源：
 * 本页未在生成（无 overlay）时直接采用，其它端的新消息/生成占位由此到达本端；
 * 本页正在生成时把 overlay 叠加到已落库的 streaming 占位上（并回绑真实消息 id），
 * 增量渲染始终以本地缓冲为准。占位尚未落库（发送后的竞态窗口）返回 null，
 * 调用方保留本地乐观视图等下一周期。
 */
export function mergeServerMessages(
  server: SuperMessage[],
  overlay: StreamOverlay | undefined,
): { messages: SuperMessage[]; boundMessageId: string | null } | null {
  if (!overlay) return { messages: server, boundMessageId: null }
  const placeholder = [...server].reverse().find(
    item => item.role === 'assistant' && item.status === 'streaming',
  )
  if (!placeholder) return null
  return {
    messages: server.map(item => item.id === placeholder.id
      ? {
          ...item,
          content: overlay.content,
          steps: overlay.steps,
          status: overlay.status,
          token_usage: overlay.tokenUsage,
          thinking_round: overlay.thinkingRound,
        }
      : item),
    boundMessageId: placeholder.id,
  }
}

/** 轮询降噪：逐项等价时复用旧引用，避免整棵消息树每周期重渲染 */
export function sameMessageList(current: SuperMessage[], next: SuperMessage[]) {
  if (current.length !== next.length) return false
  return current.every((item, index) => {
    const other = next[index]
    return item.id === other.id
      && item.role === other.role
      && item.status === other.status
      && item.content === other.content
      && item.created_at === other.created_at
      && item.thinking_round === other.thinking_round
      && JSON.stringify(item.steps) === JSON.stringify(other.steps)
      && JSON.stringify(item.token_usage) === JSON.stringify(other.token_usage)
  })
}

/** 会话列表轮询降噪：对端新建会话/改标题/归档时才有真实变化 */
export function sameConversationList(
  current: Array<{ id: string; title: string; status: string; updated_at: string }>,
  next: Array<{ id: string; title: string; status: string; updated_at: string }>,
) {
  if (current.length !== next.length) return false
  return current.every((item, index) => {
    const other = next[index]
    return item.id === other.id
      && item.title === other.title
      && item.status === other.status
      && item.updated_at === other.updated_at
  })
}
