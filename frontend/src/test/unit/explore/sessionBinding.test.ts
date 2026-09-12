import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  EXPLORE_VIEWS,
  bindingFailureBannerText,
  parseExploreView,
  parsePendingNewSession,
  parseSessionBinding,
  resolveBoundSession,
  sessionBindingKey,
  shouldAutoSelectLatestSession,
} from '../../../pages/explore/sessionBinding.ts'


const params = (entries: Record<string, string>) => ({
  get: (key: string) => entries[key] ?? null,
})

const session = (id: string, ontologyId: string | null = null, ontologyVersionId: string | null = null) => ({
  id,
  ontologyId,
  ontologyVersionId,
})

describe('parseSessionBinding', () => {
  it('ontologyId 与 versionId 齐备时解析为绑定锚点', () => {
    assert.deepEqual(
      parseSessionBinding(params({ ontologyId: 'ont-1', versionId: 'ver-2' })),
      { ontologyId: 'ont-1', versionId: 'ver-2' },
    )
  })

  it('缺任一参数按非绑定态处理', () => {
    assert.equal(parseSessionBinding(params({ ontologyId: 'ont-1' })), null)
    assert.equal(parseSessionBinding(params({ versionId: 'ver-2' })), null)
    assert.equal(parseSessionBinding(params({})), null)
  })

  it('参数去空白；全空白视同缺失', () => {
    assert.deepEqual(
      parseSessionBinding(params({ ontologyId: ' ont-1 ', versionId: ' ver-2 ' })),
      { ontologyId: 'ont-1', versionId: 'ver-2' },
    )
    assert.equal(parseSessionBinding(params({ ontologyId: '  ', versionId: 'ver-2' })), null)
  })

  it('sessionBindingKey 以 本体:版本 作为代际标识', () => {
    assert.equal(sessionBindingKey({ ontologyId: 'ont-1', versionId: 'ver-2' }), 'ont-1:ver-2')
  })
})

describe('resolveBoundSession', () => {
  const binding = { ontologyId: 'ont-1', versionId: 'ver-2' }

  it('当前会话已是目标绑定 → 不再动作', () => {
    const sessions = [session('s1', 'ont-1', 'ver-2')]
    assert.deepEqual(resolveBoundSession(sessions, binding, 's1'), { action: 'none' })
  })

  it('列表中存在同绑定会话 → 选中它（即使当前选中了别的会话）', () => {
    const sessions = [
      session('s1'),
      session('s2', 'ont-1', 'ver-2'),
      session('s3', 'ont-1', 'ver-9'),
    ]
    assert.deepEqual(resolveBoundSession(sessions, binding, 's1'), { action: 'select', sessionId: 's2' })
    assert.deepEqual(resolveBoundSession(sessions, binding, ''), { action: 'select', sessionId: 's2' })
  })

  it('无匹配（含绑定到其他版本的会话）→ 创建绑定会话', () => {
    const sessions = [session('s1'), session('s3', 'ont-1', 'ver-9'), session('s4', 'ont-2', 'ver-2')]
    assert.deepEqual(resolveBoundSession(sessions, binding, 's1'), { action: 'create' })
    assert.deepEqual(resolveBoundSession([], binding, ''), { action: 'create' })
  })

  it('历史会话缺绑定字段（旧数据）不参与匹配', () => {
    const legacy = [{ id: 's1' }]
    assert.deepEqual(resolveBoundSession(legacy, binding, 's1'), { action: 'create' })
  })
})

describe('parsePendingNewSession', () => {
  it('session=new 识别为待建新会话意图', () => {
    assert.equal(parsePendingNewSession(params({ session: 'new' })), true)
    assert.equal(parsePendingNewSession(params({ session: ' new ' })), true)
  })

  it('无参数或取值不符按普通进入处理', () => {
    assert.equal(parsePendingNewSession(params({})), false)
    assert.equal(parsePendingNewSession(params({ session: '' })), false)
    assert.equal(parsePendingNewSession(params({ session: 'old' })), false)
    assert.equal(parsePendingNewSession(params({ ontologyId: 'ont-1' })), false)
  })
})

describe('shouldAutoSelectLatestSession', () => {
  it('普通进入且未选中会话 → 自动恢复最近会话', () => {
    assert.equal(shouldAutoSelectLatestSession(false, false), true)
  })

  it('已选中会话时不自动切换', () => {
    assert.equal(shouldAutoSelectLatestSession(false, true), false)
  })

  it('待建新会话态保持空白，不恢复最近会话', () => {
    assert.equal(shouldAutoSelectLatestSession(true, false), false)
    assert.equal(shouldAutoSelectLatestSession(true, true), false)
  })
})

describe('parseExploreView', () => {
  it('缺省与非法值回落到业务场景视图', () => {
    assert.equal(parseExploreView(params({})), 'canvas')
    assert.equal(parseExploreView(params({ view: '' })), 'canvas')
    assert.equal(parseExploreView(params({ view: 'unknown' })), 'canvas')
  })

  it('四个合法视图原样解析', () => {
    for (const view of EXPLORE_VIEWS) {
      assert.equal(parseExploreView(params({ view: view.id })), view.id)
    }
  })

  it('与绑定参数共存时互不影响', () => {
    const merged = params({ ontologyId: 'ont-1', versionId: 'ver-2', view: 'mapping' })
    assert.equal(parseExploreView(merged), 'mapping')
    assert.deepEqual(parseSessionBinding(merged), { ontologyId: 'ont-1', versionId: 'ver-2' })
  })
})

describe('bindingFailureBannerText', () => {
  it('HTTP detail 字符串透出为失败原因', () => {
    assert.equal(
      bindingFailureBannerText({ detail: '目标本体不存在' }),
      '绑定本体版本失败：目标本体不存在',
    )
  })

  it('detail 对象的 message 字段透出为失败原因', () => {
    assert.equal(
      bindingFailureBannerText({ detail: { code: 'version_missing', message: '绑定版本已发布' } }),
      '绑定本体版本失败：绑定版本已发布',
    )
  })

  it('无 detail 时回落到 Error.message；完全无法识别时给通用兜底', () => {
    assert.equal(bindingFailureBannerText(new Error('network down')), '绑定本体版本失败：network down')
    assert.equal(bindingFailureBannerText(null), '绑定本体版本失败，请检查绑定参数后重试')
    assert.equal(bindingFailureBannerText({}), '绑定本体版本失败，请检查绑定参数后重试')
    assert.equal(bindingFailureBannerText({ detail: '  ' }), '绑定本体版本失败，请检查绑定参数后重试')
  })
})
