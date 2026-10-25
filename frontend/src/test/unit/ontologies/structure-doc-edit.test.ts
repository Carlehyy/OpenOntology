import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  buildDocumentSaveBody,
  classifyDocumentSaveError,
  DOCUMENT_MAX_BYTES,
  documentByteLabel,
  isBlankDocument,
  isDocumentDirty,
  isDocumentTooLarge,
  utf8Bytes,
} from '../../../pages/ontologies/detail/tabs/structureDocEdit.ts'

describe('业务文档编辑纯函数', () => {
  it('utf8Bytes 按字节计数：中文每字符 3 字节', () => {
    assert.equal(utf8Bytes('abc'), 3)
    assert.equal(utf8Bytes('中文'), 6)
    assert.equal(utf8Bytes('a中b'), 5)
    assert.equal(utf8Bytes(''), 0)
  })

  it('空白正文判定覆盖空串、纯空白与换行', () => {
    assert.equal(isBlankDocument(''), true)
    assert.equal(isBlankDocument('   \n\t '), true)
    assert.equal(isBlankDocument('正文'), false)
    assert.equal(isBlankDocument(' 0 '), false)
  })

  it('超限判定按 UTF-8 字节而非字符数', () => {
    assert.equal(isDocumentTooLarge('a'.repeat(DOCUMENT_MAX_BYTES)), false)
    assert.equal(isDocumentTooLarge('a'.repeat(DOCUMENT_MAX_BYTES + 1)), true)
    // 233_334 个中文 = 700_002 字节，按字符数则远小于上限。
    assert.equal(isDocumentTooLarge('中'.repeat(233_334)), true)
  })

  it('脏态判定只做原文比较，不做归一化', () => {
    assert.equal(isDocumentDirty('a', 'a'), false)
    assert.equal(isDocumentDirty('a', 'a '), true)
    assert.equal(isDocumentDirty('a\n', 'a'), true)
  })

  it('documentByteLabel 输出当前字节与上限（KB 口径）', () => {
    assert.equal(documentByteLabel(''), '0.0 / 700K')
    assert.equal(documentByteLabel('a'.repeat(1500)), '1.5 / 700K')
    assert.equal(documentByteLabel('中'), '0.0 / 700K')
  })

  it('buildDocumentSaveBody 组装三基线 CAS 请求体', () => {
    assert.deepEqual(
      buildDocumentSaveBody('# 正文', {
        semanticRevision: 3,
        fingerprint: 'fp-3',
        expectedCurrentReleaseId: 'rel-1',
      }),
      {
        documentMd: '# 正文',
        baseSemanticRevision: 3,
        baseFingerprint: 'fp-3',
        expectedCurrentReleaseId: 'rel-1',
      })
  })
})

describe('classifyDocumentSaveError', () => {
  const httpError = (status: number, detail: unknown) => ({
    status,
    detail,
    message: 'Request failed',
  })

  it('409 conflict：携带服务端当前修订号与指纹', () => {
    const result = classifyDocumentSaveError(httpError(409, {
      code: 'conflict',
      message: '业务文档已被其他会话修改，请重新加载后再保存',
      currentSemanticRevision: 7,
      currentFingerprint: 'fp-7',
    }))
    assert.equal(result.kind, 'conflict')
    assert.equal(result.currentSemanticRevision, 7)
    assert.equal(result.currentFingerprint, 'fp-7')
    assert.ok(result.message.includes('其他会话'))
    assert.ok(result.detail.includes('409'))
  })

  it('409 release_context_changed 与 403 权限错误分别分类', () => {
    const changed = classifyDocumentSaveError(httpError(409, {
      code: 'release_context_changed',
      message: '当前发布版本已变化',
    }))
    assert.equal(changed.kind, 'release-context-changed')
    assert.ok(changed.message.includes('发布版本已更新'))

    const forbidden = classifyDocumentSaveError(
      httpError(403, 'This role is read-only'))
    assert.equal(forbidden.kind, 'forbidden')
    assert.ok(forbidden.message.includes('权限'))
  })

  it('422 空/超限给出明确文案，超限透传服务端 maxBytes', () => {
    const empty = classifyDocumentSaveError(httpError(422, {
      code: 'document_empty', message: '业务文档正文不能为空',
    }))
    assert.equal(empty.kind, 'document-empty')
    assert.ok(empty.message.includes('不能为空'))

    const tooLarge = classifyDocumentSaveError(httpError(422, {
      code: 'document_too_large',
      message: '业务文档正文超过 700000 字节上限',
      maxBytes: 700000,
    }))
    assert.equal(tooLarge.kind, 'document-too-large')
    assert.equal(tooLarge.maxBytes, 700000)
    assert.ok(tooLarge.message.includes('700000'))
  })

  it('其他 422 与网络错误可区分，网络错误不带状态码', () => {
    const invalid = classifyDocumentSaveError(httpError(422, [
      { loc: ['body', 'documentMd'], msg: 'str expected', type: 'type_error' },
    ]))
    assert.equal(invalid.kind, 'invalid-body')

    const network = classifyDocumentSaveError({ message: 'Network Error' })
    assert.equal(network.kind, 'network')
    assert.equal(network.status, undefined)
    assert.ok(network.message.includes('重试'))

    const server = classifyDocumentSaveError(httpError(500, 'boom'))
    assert.equal(server.kind, 'unknown')
    assert.ok(server.detail.includes('500'))
  })

  it('错误 detail 为字符串时直接作为原因', () => {
    const result = classifyDocumentSaveError(httpError(409, '版本冲突'))
    assert.equal(result.kind, 'unknown')
    assert.ok(result.detail.includes('版本冲突'))
  })
})
