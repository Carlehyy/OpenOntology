import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import { createSummaryScheduler } from '../../../pages/super-assistant/summaryScheduler.ts'

const wait = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

describe('createSummaryScheduler', () => {
  it('延迟到期后调用 generate，失败静默不抛未处理拒绝', async () => {
    const calls: string[] = []
    const scheduler = createSummaryScheduler({
      generate: async id => {
        calls.push(id)
        throw new Error('network down')
      },
      delayMs: 5,
    })
    scheduler.notify('c-1')
    assert.equal(calls.length, 0)
    await wait(30)
    assert.deepEqual(calls, ['c-1'])
    scheduler.dispose()
  })

  it('延迟窗口内的密集触发合并为一次生成（定时器重置）', async () => {
    const calls: string[] = []
    const scheduler = createSummaryScheduler({
      generate: async id => { calls.push(id) },
      delayMs: 20,
    })
    scheduler.notify('c-1')
    await wait(10)
    scheduler.notify('c-1')
    await wait(10)
    scheduler.notify('c-1')
    await wait(60)
    assert.deepEqual(calls, ['c-1'])
    scheduler.dispose()
  })

  it('生成中到达的触发不丢弃：完成后用最新会话补跑一次', async () => {
    let release: () => void = () => {}
    const gate = new Promise<void>(resolve => { release = resolve })
    const calls: string[] = []
    const settled: string[] = []
    const scheduler = createSummaryScheduler({
      generate: async id => {
        calls.push(id)
        if (calls.length === 1) await gate
      },
      delayMs: 5,
      onSettled: id => { settled.push(id) },
    })
    scheduler.notify('c-1')     // 第一次生成（挂起等 release）
    await wait(15)
    scheduler.notify('c-2')     // 生成中到达 → pending
    await wait(15)
    assert.deepEqual(calls, ['c-1'])
    release()
    await wait(40)
    assert.deepEqual(calls, ['c-1', 'c-2'])
    assert.deepEqual(settled, ['c-1', 'c-2'])
    scheduler.dispose()
  })

  it('dispose 停掉未到期的定时器且后续 notify 不再生效', async () => {
    const calls: string[] = []
    const scheduler = createSummaryScheduler({
      generate: async id => { calls.push(id) },
      delayMs: 10,
    })
    scheduler.notify('c-1')
    scheduler.dispose()
    scheduler.notify('c-2')
    await wait(40)
    assert.deepEqual(calls, [])
  })
})
