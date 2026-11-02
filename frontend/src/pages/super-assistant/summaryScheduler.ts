// 会话纪要自动生成调度器：页面在两个触点（用户消息实际发送后 / 超级助手
// 完全响应结束后）调用 notify，延迟 delayMs（默认 1s，等消息落库稳定）后
// 调 generate。单飞 + 补位：生成中到达的新触发不丢弃，记为 pending，当前
// 次完成后用最新会话 id 补跑一次；密集触发会合并重置定时器。所有失败静默
// （自动纪要是旁路能力，不打扰对话主流程）。独立成纯逻辑模块便于单测
// （Node test runner，无 DOM 依赖）。
export interface SummaryScheduler {
  /** 登记一次触发（conversationId：触发时刻的目标会话） */
  notify: (conversationId: string) => void
  /** 页面卸载时停掉定时器；进行中的 generate 任其自然结束（服务端幂等覆盖） */
  dispose: () => void
}

export function createSummaryScheduler(options: {
  generate: (conversationId: string) => Promise<unknown>
  /** 触发到实际生成的延迟，默认 1000ms */
  delayMs?: number
  /** 一次生成结束后回调（无论成败），页面据此刷新纪要弹窗 */
  onSettled?: (conversationId: string) => void
}): SummaryScheduler {
  const delayMs = options.delayMs ?? 1000
  let timer: ReturnType<typeof setTimeout> | null = null
  let running = false
  let pendingId: string | null = null
  let disposed = false

  const run = (conversationId: string) => {
    running = true
    Promise.resolve(options.generate(conversationId))
      .catch(() => { /* 自动纪要失败静默：弹窗展示上一次结果即可 */ })
      .finally(() => {
        running = false
        if (!disposed) options.onSettled?.(conversationId)
        if (disposed || pendingId === null) return
        const next = pendingId
        pendingId = null
        run(next)
      })
  }

  return {
    notify(conversationId: string) {
      if (disposed) return
      if (timer !== null) clearTimeout(timer)
      timer = setTimeout(() => {
        timer = null
        if (running) {
          pendingId = conversationId
          return
        }
        run(conversationId)
      }, delayMs)
    },
    dispose() {
      disposed = true
      if (timer !== null) {
        clearTimeout(timer)
        timer = null
      }
    },
  }
}
