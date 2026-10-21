import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  DRAFTS_ENTRY_LIMIT,
  DRAFT_VALUE_CHAR_LIMIT,
  draftsKey,
  readDrafts,
  readCachedDrafts,
  writeCachedDrafts,
  writeDrafts,
  type DraftStorage,
} from '../../../pages/super-assistant/draftCache.ts'

/** 内存假存储：可选注入读写故障，模拟配额满/私有模式 */
function memoryStorage(overrides: Partial<DraftStorage> = {}): DraftStorage & { store: Map<string, string> } {
  const store = new Map<string, string>()
  return {
    store,
    getItem: key => store.get(key) ?? null,
    setItem: (key, value) => { store.set(key, value) },
    removeItem: key => { store.delete(key) },
    ...overrides,
  }
}

describe('draftsKey', () => {
  it('按用户隔离并携带版本前缀', () => {
    assert.equal(draftsKey('u1'), 'ob:super-assistant-draft:v1:u1')
    assert.notEqual(draftsKey('u1'), draftsKey('u2'))
  })
})

describe('readDrafts', () => {
  it('无用户标识返回空 Map', () => {
    assert.equal(readDrafts(memoryStorage(), null).size, 0)
    assert.equal(readDrafts(memoryStorage(), undefined).size, 0)
  })

  it('键不存在返回空 Map', () => {
    assert.equal(readDrafts(memoryStorage(), 'u1').size, 0)
  })

  it('写入后可完整读回（含 __new__ 槽）', () => {
    const storage = memoryStorage()
    writeDrafts(storage, 'u1', new Map([['c-1', '草稿一'], ['__new__', '未落地草稿']]))
    assert.deepEqual(readDrafts(storage, 'u1'), new Map([['c-1', '草稿一'], ['__new__', '未落地草稿']]))
  })

  it('不同用户互不可见（键隔离）', () => {
    const storage = memoryStorage()
    writeDrafts(storage, 'u1', new Map([['c-1', '草稿一']]))
    assert.equal(readDrafts(storage, 'u2').size, 0)
  })

  it('损坏 JSON：返回空 Map 并清除键', () => {
    const storage = memoryStorage()
    storage.setItem(draftsKey('u1'), '{not-json')
    assert.equal(readDrafts(storage, 'u1').size, 0)
    assert.equal(storage.getItem(draftsKey('u1')), null)
  })

  it('根不是对象（数组/字符串/null）：返回空 Map 并清除键', () => {
    for (const bad of ['[]', '"text"', 'null']) {
      const storage = memoryStorage()
      storage.setItem(draftsKey('u1'), bad)
      assert.equal(readDrafts(storage, 'u1').size, 0)
      assert.equal(storage.getItem(draftsKey('u1')), null)
    }
  })

  it('条目值不是字符串：整体废弃并清除键', () => {
    const storage = memoryStorage()
    storage.setItem(draftsKey('u1'), JSON.stringify({ 'c-1': '草稿一', 'c-2': 123 }))
    assert.equal(readDrafts(storage, 'u1').size, 0)
    assert.equal(storage.getItem(draftsKey('u1')), null)
  })

  it('超条目上限的外部数据：截断接收前 N 条（防御，不视为损坏）', () => {
    const storage = memoryStorage()
    const overflow: Record<string, string> = {}
    for (let index = 0; index < DRAFTS_ENTRY_LIMIT + 5; index += 1) overflow[`c-${index}`] = `草稿${index}`
    storage.setItem(draftsKey('u1'), JSON.stringify(overflow))
    assert.equal(readDrafts(storage, 'u1').size, DRAFTS_ENTRY_LIMIT)
  })

  it('读端存储抛错：返回空 Map 不上抛', () => {
    const storage = memoryStorage({ getItem: () => { throw new Error('unavailable') } })
    assert.equal(readDrafts(storage, 'u1').size, 0)
  })
})

describe('writeDrafts', () => {
  it('跳过空草稿条目', () => {
    const storage = memoryStorage()
    writeDrafts(storage, 'u1', new Map([['c-1', '草稿一'], ['c-2', '']]))
    assert.deepEqual(readDrafts(storage, 'u1'), new Map([['c-1', '草稿一']]))
  })

  it('全部为空时移除键（不留空对象）', () => {
    const storage = memoryStorage()
    writeDrafts(storage, 'u1', new Map([['c-1', 'x']]))
    writeDrafts(storage, 'u1', new Map([['c-1', '']]))
    assert.equal(storage.getItem(draftsKey('u1')), null)
    assert.equal(readDrafts(storage, 'u1').size, 0)
  })

  it('超条目上限：按写入顺序保留最新条目', () => {
    const storage = memoryStorage()
    const drafts = new Map<string, string>()
    for (let index = 0; index < DRAFTS_ENTRY_LIMIT + 5; index += 1) drafts.set(`c-${index}`, `草稿${index}`)
    writeDrafts(storage, 'u1', drafts)
    const restored = readDrafts(storage, 'u1')
    assert.equal(restored.size, DRAFTS_ENTRY_LIMIT)
    assert.equal(restored.get('c-4'), undefined)
    assert.equal(restored.get('c-5'), `草稿5`)
    assert.equal(restored.get(`c-${DRAFTS_ENTRY_LIMIT + 4}`), `草稿${DRAFTS_ENTRY_LIMIT + 4}`)
  })

  it('超单条长度上限：截断到上限字符数', () => {
    const storage = memoryStorage()
    const long = '长'.repeat(DRAFT_VALUE_CHAR_LIMIT + 100)
    writeDrafts(storage, 'u1', new Map([['c-1', long]]))
    assert.equal(readDrafts(storage, 'u1').get('c-1')?.length, DRAFT_VALUE_CHAR_LIMIT)
  })

  it('无用户标识：不写键', () => {
    const storage = memoryStorage()
    writeDrafts(storage, null, new Map([['c-1', '草稿一']]))
    writeDrafts(storage, undefined, new Map([['c-1', '草稿一']]))
    assert.equal(storage.store.size, 0)
  })

  it('写端存储抛错（配额满/私有模式）：清键且不上抛', () => {
    const storage = memoryStorage({
      setItem: () => { throw new Error('QuotaExceededError') },
    })
    writeDrafts(storage, 'u1', new Map([['c-1', '草稿一']]))
    assert.equal(storage.getItem(draftsKey('u1')), null)
  })
})

describe('页面 localStorage 绑定', () => {
  it('node 环境无 window：读写均安全无操作', () => {
    assert.equal(readCachedDrafts('u1').size, 0)
    assert.doesNotThrow(() => writeCachedDrafts('u1', new Map([['c-1', '草稿一']])))
  })
})
