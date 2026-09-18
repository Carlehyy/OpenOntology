import type { ToolStep } from '@/api/superAssistant'

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

export function toolStatusLabel(status: string) {
  if (status === 'running') return '进行中'
  if (status === 'awaiting_confirmation') return '待确认'
  if (status === 'success' || status === 'complete') return '完成'
  if (status === 'cancelled' || status === 'denied' || status === 'expired') return '已取消'
  if (status === 'error' || status === 'failed') return '失败'
  return status
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
