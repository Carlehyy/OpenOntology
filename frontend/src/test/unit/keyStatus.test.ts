import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import { keyStatus } from '../../components/profile/keyStatus.ts'

describe('keyStatus', () => {
  it('已吊销优先于一切（吊销时间存在即 revoked，无论是否过期）', () => {
    assert.equal(keyStatus({ revoked_at: '2026-01-01T00:00:00', expires_at: '2000-01-01T00:00:00' }), 'revoked')
  })

  it('expires_at 为 naive UTC 串：按 UTC 判定过期，不按本地时区（A8 边界分裂回归锁）', () => {
    // 后端无 Z 串语义是 UTC。此断言与时区无关：过去/未来的 instant 用
    // 相对时间构造，任何 TZ 下结论一致。
    const past = new Date(Date.now() - 60_000)
    const future = new Date(Date.now() + 60_000)
    const naive = (d: Date) => d.toISOString().slice(0, 19) // 去 Z，模拟后端串
    assert.equal(keyStatus({ revoked_at: null, expires_at: naive(past) }), 'expired')
    assert.equal(keyStatus({ revoked_at: null, expires_at: naive(future) }), 'active')
  })

  it('expires_at 为空表示永久，只要未吊销即 active；显式 Z 串同样正确解析', () => {
    assert.equal(keyStatus({ revoked_at: null, expires_at: null }), 'active')
    assert.equal(keyStatus({ revoked_at: null, expires_at: new Date(Date.now() - 60_000).toISOString() }), 'expired')
  })
})
