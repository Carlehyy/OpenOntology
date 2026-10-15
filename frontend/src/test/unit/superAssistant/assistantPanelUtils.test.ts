import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import { errorText } from '../../../pages/super-assistant/components/assistantPanelUtils.ts'

describe('errorText', () => {
  it('字符串 detail / message 正常透传，兜底文案最后', () => {
    assert.equal(errorText({ detail: '渠道已停用' }), '渠道已停用')
    assert.equal(errorText({ message: 'Network Error' }), 'Network Error')
    assert.equal(errorText({ detail: '', message: 'Network Error' }), 'Network Error')
    assert.equal(errorText({}), '操作失败')
    assert.equal(errorText(null, '自定义兜底'), '自定义兜底')
  })

  it('FastAPI 422 的数组 detail 折叠为可读字符串（防 toast 渲染非字符串导致整树白屏）', () => {
    const fastapi422 = {
      detail: [
        { type: 'too_long', loc: ['body', 'title'], msg: 'String should have at most 300 characters' },
        { type: 'missing', loc: ['body', 'channelIds', 0], msg: 'Field required' },
      ],
    }
    const text = errorText(fastapi422)
    assert.equal(typeof text, 'string')
    assert.match(text, /^参数校验失败：/)
    assert.match(text, /title: String should have at most 300 characters/)
    assert.match(text, /channelIds\.0: Field required/)
  })

  it('数组 detail 混入非标准项不抛错；空数组视为无信息落 message/兜底', () => {
    const messy = errorText({ detail: [{ msg: 'x' }, 'raw', 42, null] })
    assert.equal(typeof messy, 'string')
    assert.match(messy, /参数校验失败/)
    assert.equal(errorText({ detail: [], message: 'Network Error' }), 'Network Error')
    assert.equal(errorText({ detail: [] }), '操作失败')
  })
})
