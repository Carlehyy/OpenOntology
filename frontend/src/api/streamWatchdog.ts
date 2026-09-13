/**
 * SSE 流式连接的静默看门狗（D-014）。
 *
 * 后端同步生成器卡在工具调用里不 yield、不关流时，fetch 的 ReadableStream
 * 会永久挂起——调用方 await 的 Promise 永不 settle，聊天输入的 busy 态
 * 永久锁死（整页刷新才能恢复）。看门狗在「无任何字节到达」持续超过
 * idleTimeout 时主动 abort，把死流转化为可呈现的错误。
 *
 * 任何字节（含 heartbeat/文本增量）都算活性；真正的长工具执行只要
 * 心跳不断即不会误伤。
 */

export interface StreamWatchdog {
  /** 记录一次活性（收到响应头或任意 chunk 时调用） */
  activity: () => void
  /** 是否已因静默超时触发 abort */
  timedOut: () => boolean
  /** 传入 fetch 的 signal（同时串联调用方自己的 AbortSignal） */
  signal: AbortSignal
  /** 流结束后清理定时器（finally 中调用，幂等） */
  dispose: () => void
}

export function createStreamWatchdog(
  idleTimeoutMs: number,
  parentSignal?: AbortSignal,
): StreamWatchdog {
  const controller = new AbortController()
  let lastActivityAt = Date.now()
  let disposed = false
  let fired = false

  const onParentAbort = () => controller.abort()
  if (parentSignal) {
    if (parentSignal.aborted) controller.abort()
    else parentSignal.addEventListener('abort', onParentAbort, { once: true })
  }

  // 轮询而非 setTimeout 重排：活性频繁到达时避免每次都销毁重建定时器。
  const timer = setInterval(() => {
    if (disposed || fired) return
    if (Date.now() - lastActivityAt >= idleTimeoutMs) {
      fired = true
      controller.abort()
    }
  }, Math.min(1000, Math.max(50, Math.floor(idleTimeoutMs / 20))))

  return {
    activity: () => { lastActivityAt = Date.now() },
    timedOut: () => fired,
    signal: controller.signal,
    dispose: () => {
      if (disposed) return
      disposed = true
      clearInterval(timer)
      if (parentSignal) {
        parentSignal.removeEventListener('abort', onParentAbort)
      }
    },
  }
}

/** 流静默超时的专用错误，调用方据此与用户主动取消（AbortError）区分呈现 */
export class StreamIdleTimeoutError extends Error {
  readonly idleTimeoutMs: number

  constructor(idleTimeoutMs: number) {
    super(`回复流超过 ${Math.round(idleTimeoutMs / 1000)} 秒没有任何响应，已自动断开；未完成的内容不会写入会话，请重新发送`)
    this.name = 'StreamIdleTimeoutError'
    this.idleTimeoutMs = idleTimeoutMs
  }
}
