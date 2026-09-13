import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  createStreamWatchdog,
  StreamIdleTimeoutError,
} from '../../../api/streamWatchdog.ts'

const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

describe('createStreamWatchdog', () => {
  it('静默超过阈值后 abort 且 timedOut 为真', async () => {
    const watchdog = createStreamWatchdog(60)
    try {
      await sleep(200)
      assert.equal(watchdog.timedOut(), true)
      assert.equal(watchdog.signal.aborted, true)
    } finally {
      watchdog.dispose()
    }
  })

  it('任意活性会重置计时：持续 activity 不误伤', async () => {
    const watchdog = createStreamWatchdog(80)
    try {
      for (let i = 0; i < 5; i++) {
        await sleep(40)
        watchdog.activity()
      }
      assert.equal(watchdog.timedOut(), false)
      assert.equal(watchdog.signal.aborted, false)
    } finally {
      watchdog.dispose()
    }
  })

  it('停止活性后仍会在阈值后触发', async () => {
    const watchdog = createStreamWatchdog(70)
    try {
      watchdog.activity()
      await sleep(40)
      watchdog.activity()
      assert.equal(watchdog.timedOut(), false)
      await sleep(200)
      assert.equal(watchdog.timedOut(), true)
    } finally {
      watchdog.dispose()
    }
  })

  it('dispose 后不再触发；父 signal 取消会级联 abort 但不算超时', async () => {
    const disposed = createStreamWatchdog(60)
    disposed.dispose()
    await sleep(150)
    assert.equal(disposed.timedOut(), false)
    assert.equal(disposed.signal.aborted, false)

    const parent = new AbortController()
    const child = createStreamWatchdog(1000, parent.signal)
    try {
      parent.abort()
      assert.equal(child.signal.aborted, true)
      assert.equal(child.timedOut(), false)
    } finally {
      child.dispose()
    }
  })

  it('父 signal 已取消时创建即级联', () => {
    const parent = new AbortController()
    parent.abort()
    const watchdog = createStreamWatchdog(1000, parent.signal)
    watchdog.dispose()
    assert.equal(watchdog.signal.aborted, true)
  })
})

describe('StreamIdleTimeoutError', () => {
  it('携带可读中文信息与超时毫秒数', () => {
    const error = new StreamIdleTimeoutError(180_000)
    assert.equal(error.name, 'StreamIdleTimeoutError')
    assert.equal(error.idleTimeoutMs, 180_000)
    assert.match(error.message, /180 秒/)
    assert.match(error.message, /请重新发送/)
  })
})
