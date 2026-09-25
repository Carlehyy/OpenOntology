import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import { classifySaveError, saveStatusLabel } from '../../../pages/ontologies/detail/tabs/saveStatus.ts'

describe('saveStatusLabel', () => {
  it('pending 状态展示剩余秒数倒计时', () => {
    assert.equal(saveStatusLabel('pending', 3), '3 秒后自动保存')
    assert.equal(saveStatusLabel('pending', 2), '2 秒后自动保存')
    assert.equal(saveStatusLabel('pending', 1), '1 秒后自动保存')
  })

  it('倒计时数值钳位在 1..3 秒', () => {
    assert.equal(saveStatusLabel('pending', 0), '1 秒后自动保存')
    assert.equal(saveStatusLabel('pending', -2), '1 秒后自动保存')
    assert.equal(saveStatusLabel('pending', 5), '3 秒后自动保存')
    assert.equal(saveStatusLabel('pending', 2.6), '3 秒后自动保存')
  })

  it('其余状态使用固定文案', () => {
    assert.equal(saveStatusLabel('saving', 3), '正在保存布局')
    assert.equal(saveStatusLabel('saved', 3), '布局已保存')
    assert.equal(saveStatusLabel('error', 3), '保存失败')
    assert.equal(saveStatusLabel('idle', 3), '拖动后自动保存布局')
  })
})

describe('classifySaveError', () => {
  it('400/403/404/422 等 4xx（除 408/409/429）为永久性错误', () => {
    for (const status of [400, 401, 403, 404, 422]) {
      const result = classifySaveError({ status, detail: 'bad request' })
      assert.equal(result.permanent, true, `status=${status}`)
      assert.equal(result.status, status)
    }
  })

  it('408/409/429、5xx 与无响应网络错误为瞬时错误', () => {
    for (const status of [408, 409, 429, 500, 502, 503]) {
      assert.equal(classifySaveError({ status, detail: 'err' }).permanent, false, `status=${status}`)
    }
    // 网络错误：拦截器原样透出 AxiosError（只有 message，无 status）
    const network = classifySaveError({ message: 'Network Error' })
    assert.equal(network.permanent, false)
    assert.equal(network.status, undefined)
  })

  it('detail 为字符串时直接作为原因，message 附状态码', () => {
    const result = classifySaveError({ status: 404, detail: 'Version not found' })
    assert.equal(result.reason, 'Version not found')
    assert.equal(result.message, '保存失败（404）：Version not found')
    assert.equal(result.code, undefined)
  })

  it('detail 为 {code, message} 对象时提取 message 与 code', () => {
    const result = classifySaveError({
      status: 422,
      detail: { code: 'invalid_canvas_layout', message: '节点 l1:x 不属于该版本' },
    })
    assert.equal(result.reason, '节点 l1:x 不属于该版本')
    assert.equal(result.code, 'invalid_canvas_layout')
    assert.equal(result.message, '保存失败（422）：节点 l1:x 不属于该版本')
    assert.equal(result.permanent, true)
  })

  it('detail 为 FastAPI 校验数组时取第一条 msg', () => {
    const result = classifySaveError({
      status: 422,
      detail: [{ loc: ['body', 'positions'], msg: 'Input should be a valid dictionary', type: 'dict_type' }],
    })
    assert.equal(result.reason, 'Input should be a valid dictionary')
  })

  it('无 detail 时回落到 message，再回落默认文案；无状态码时不带括号', () => {
    assert.equal(classifySaveError({ message: 'Network Error' }).reason, 'Network Error')
    assert.equal(classifySaveError({ message: 'Network Error' }).message, '保存失败：Network Error')
    assert.equal(classifySaveError({ status: 500 }).reason, '布局保存失败')
    assert.equal(classifySaveError(null).reason, '布局保存失败')
    assert.equal(classifySaveError(undefined).message, '保存失败：布局保存失败')
  })
})
