import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  domainErrorMessage,
  domainNameValidationError,
  isDuplicateNameError,
  shortenDeleteDetail,
  successNotice,
} from '../../../pages/settings/domainUxHelpers.ts'

describe('successNotice', () => {
  it('标题写动作结果，说明携带领域名（P1-2：杜绝「领域设置删除成功」式拼接歧义）', () => {
    assert.deepEqual(successNotice('create', '制造'), {
      title: '已创建领域',
      description: '领域「制造」',
    })
    assert.deepEqual(successNotice('update', '制造'), {
      title: '已更新领域',
      description: '领域「制造」',
    })
    assert.deepEqual(successNotice('delete', '制造'), {
      title: '已删除领域',
      description: '领域「制造」',
    })
  })

  it('领域名缺失或全空白时只给标题，不输出空说明', () => {
    assert.deepEqual(successNotice('create'), { title: '已创建领域' })
    assert.deepEqual(successNotice('delete', '   '), { title: '已删除领域' })
  })

  it('领域名首尾空白会被裁掉', () => {
    assert.equal(successNotice('create', ' 制造 ').description, '领域「制造」')
  })
})

describe('isDuplicateNameError', () => {
  it('识别 409 detail 契约「领域「xx」已存在」', () => {
    assert.equal(isDuplicateNameError({ detail: '领域「制造」已存在' }), true)
  })

  it('非字符串 detail、其他文案与空错误都不是重名', () => {
    assert.equal(isDuplicateNameError({ detail: [{ msg: 'name required' }] }), false)
    assert.equal(isDuplicateNameError({ detail: '领域不存在' }), false)
    assert.equal(isDuplicateNameError(undefined), false)
  })
})

describe('domainErrorMessage', () => {
  it('依次识别 detail 字符串 / 422 数组 msg / detail.message / error.message', () => {
    assert.equal(domainErrorMessage({ detail: '领域「制造」已存在' }, '创建失败'), '领域「制造」已存在')
    assert.equal(domainErrorMessage({ detail: [{ msg: 'name required' }] }, '创建失败'), 'name required')
    assert.equal(domainErrorMessage({ detail: { message: '内部错误' } }, '创建失败'), '内部错误')
    assert.equal(domainErrorMessage({ message: '网络错误' }, '创建失败'), '网络错误')
  })

  it('无法识别时回落 fallback 文案', () => {
    assert.equal(domainErrorMessage({}, '创建失败'), '创建失败')
    assert.equal(domainErrorMessage(null, '更新失败'), '更新失败')
  })
})

describe('shortenDeleteDetail', () => {
  it('detail 以被删领域名开头时改称「该领域」，保留引用次数与处理办法', () => {
    assert.equal(
      shortenDeleteDetail('领域「制造」已被 3 个本体使用，请先调整这些本体的所属领域', '制造'),
      '该领域已被 3 个本体使用，请先调整这些本体的所属领域',
    )
  })

  it('detail 与被删领域名不匹配时原样保留', () => {
    const detail = '领域「供应链」已被 1 个本体使用'
    assert.equal(shortenDeleteDetail(detail, '制造'), detail)
  })

  it('领域名缺失或为空串时不做替换', () => {
    const detail = '领域「制造」已被 2 个本体使用'
    assert.equal(shortenDeleteDetail(detail, undefined), detail)
    assert.equal(shortenDeleteDetail(detail, ''), detail)
  })
})

describe('domainNameValidationError', () => {
  it('空串与纯空白名称返回固定行内错误文案', () => {
    assert.equal(domainNameValidationError(''), '请输入领域名称')
    assert.equal(domainNameValidationError('   '), '请输入领域名称')
  })

  it('有效名称（含首尾空白）返回空串表示通过', () => {
    assert.equal(domainNameValidationError('制造'), '')
    assert.equal(domainNameValidationError(' 制造 '), '')
  })
})
