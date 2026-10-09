import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  CONVERSATIONS_SNAPSHOT_LIMIT,
  conversationsSnapshotKey,
  readConversationsSnapshot,
  writeConversationsSnapshot,
  type SnapshotStorage,
} from '../../pages/super-assistant/conversationCache.ts'

/** 与 SuperConversation（@/api/superAssistant）同构的最小条目工厂 */
function item(id: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    title: `会话-${id}`,
    model_config_id: null,
    status: 'active',
    created_at: '2026-10-09T00:00:00',
    updated_at: '2026-10-09T01:00:00',
    ...overrides,
  }
}

/** 内存假存储：可选注入读写故障，模拟配额满/私有模式 */
function memoryStorage(overrides: Partial<SnapshotStorage> = {}): SnapshotStorage & { store: Map<string, string> } {
  const store = new Map<string, string>()
  return {
    store,
    getItem: key => store.get(key) ?? null,
    setItem: (key, value) => { store.set(key, value) },
    removeItem: key => { store.delete(key) },
    ...overrides,
  }
}

describe('conversationsSnapshotKey', () => {
  it('按用户隔离并携带版本前缀', () => {
    assert.equal(conversationsSnapshotKey('u1'), 'ob:super-conversations:v1:u1')
    assert.notEqual(conversationsSnapshotKey('u1'), conversationsSnapshotKey('u2'))
  })
})

describe('readConversationsSnapshot', () => {
  it('无用户标识返回 null', () => {
    const storage = memoryStorage()
    assert.equal(readConversationsSnapshot(storage, null), null)
    assert.equal(readConversationsSnapshot(storage, undefined), null)
  })

  it('键不存在返回 null', () => {
    assert.equal(readConversationsSnapshot(memoryStorage(), 'u1'), null)
  })

  it('写入后可完整读回（含空列表）', () => {
    const storage = memoryStorage()
    const list = [item('a'), item('b', { status: 'archived' })]
    writeConversationsSnapshot(storage, 'u1', list)
    assert.deepEqual(readConversationsSnapshot(storage, 'u1'), list)

    writeConversationsSnapshot(storage, 'u1', [])
    assert.deepEqual(readConversationsSnapshot(storage, 'u1'), [])
  })

  it('不同用户互不可见（键隔离）', () => {
    const storage = memoryStorage()
    writeConversationsSnapshot(storage, 'u1', [item('a')])
    assert.equal(readConversationsSnapshot(storage, 'u2'), null)
  })

  it('损坏 JSON：返回 null 并清除键', () => {
    const storage = memoryStorage()
    storage.setItem(conversationsSnapshotKey('u1'), '{not-json')
    assert.equal(readConversationsSnapshot(storage, 'u1'), null)
    assert.equal(storage.getItem(conversationsSnapshotKey('u1')), null)
  })

  it('根不是数组：返回 null 并清除键', () => {
    const storage = memoryStorage()
    storage.setItem(conversationsSnapshotKey('u1'), JSON.stringify({ id: 'x' }))
    assert.equal(readConversationsSnapshot(storage, 'u1'), null)
    assert.equal(storage.getItem(conversationsSnapshotKey('u1')), null)
  })

  it('任一条目缺核心字段或类型错误：整体废弃并清除键', () => {
    for (const bad of [
      { ...item('a'), title: 123 },
      { ...item('a'), id: null },
      { ...item('a'), status: undefined },
      { title: '缺 id' },
      'not-an-object',
    ]) {
      const storage = memoryStorage()
      storage.setItem(conversationsSnapshotKey('u1'), JSON.stringify([item('ok'), bad]))
      assert.equal(readConversationsSnapshot(storage, 'u1'), null, `条目 ${JSON.stringify(bad)} 应触发整体废弃`)
      assert.equal(storage.getItem(conversationsSnapshotKey('u1')), null)
    }
  })

  it('条目含未知扩展字段或缺可选 browser_source_id：仍接受（v1 前向兼容）', () => {
    const storage = memoryStorage()
    const list = [
      item('a', { extra_future_field: true }),
      item('b', { browser_source_id: 'src-1' }),
      { ...item('c') },
    ]
    delete (list[2] as Record<string, unknown>).browser_source_id
    storage.setItem(conversationsSnapshotKey('u1'), JSON.stringify(list))
    assert.deepEqual(readConversationsSnapshot(storage, 'u1'), list)
  })

  it('读取截断到快照上限，避免历史超长快照拖慢水合', () => {
    const storage = memoryStorage()
    const oversized = Array.from({ length: CONVERSATIONS_SNAPSHOT_LIMIT + 50 }, (_, i) => item(`c${i}`))
    writeConversationsSnapshot(storage, 'u1', oversized)
    const restored = readConversationsSnapshot(storage, 'u1')
    assert.equal(restored?.length, CONVERSATIONS_SNAPSHOT_LIMIT)
    assert.equal(restored?.[0].id, 'c0')
  })

  it('getItem 抛异常（存储禁用）不向上传播', () => {
    const storage = memoryStorage({ getItem: () => { throw new Error('blocked') } })
    assert.equal(readConversationsSnapshot(storage, 'u1'), null)
  })
})

describe('writeConversationsSnapshot', () => {
  it('无用户标识时不产生任何写入', () => {
    const storage = memoryStorage()
    writeConversationsSnapshot(storage, null, [item('a')])
    assert.equal(storage.store.size, 0)
  })

  it('超过上限的列表写入前截断', () => {
    const storage = memoryStorage()
    const oversized = Array.from({ length: CONVERSATIONS_SNAPSHOT_LIMIT + 10 }, (_, i) => item(`c${i}`))
    writeConversationsSnapshot(storage, 'u1', oversized)
    const stored = JSON.parse(storage.getItem(conversationsSnapshotKey('u1')) as string)
    assert.equal(stored.length, CONVERSATIONS_SNAPSHOT_LIMIT)
  })

  it('配额满（setItem 抛异常）：不向上传播且清理残留键', () => {
    let removed = false
    const storage = memoryStorage({
      setItem: () => { throw new Error('QuotaExceededError') },
      removeItem: key => {
        removed = key === conversationsSnapshotKey('u1')
      },
    })
    assert.doesNotThrow(() => writeConversationsSnapshot(storage, 'u1', [item('a')]))
    assert.equal(removed, true)
  })
})
