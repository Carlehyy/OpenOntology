import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  PER_FILE_TOAST_LIMIT,
  summarizePalaceUpload,
} from '../../pages/super-assistant/components/palaceUploadSummary.ts'

describe('summarizePalaceUpload：多文件上传汇总口径', () => {
  it('空批次返回 null（无事可报）', () => {
    assert.equal(summarizePalaceUpload({ total: 0, failed: 0, failedNames: [], refreshFailed: false }), null)
  })

  it('全部成功：success 标题带份数，描述说明自动抽取', () => {
    const toast = summarizePalaceUpload({ total: 3, failed: 0, failedNames: [], refreshFailed: false })
    assert.equal(toast?.variant, 'success')
    assert.equal(toast?.title, '已上传 3 份文件')
    assert.match(toast?.description ?? '', /自动抽取实体与关系/)
    assert.equal(toast?.perFileToasts, false)
  })

  it('全部成功但刷新失败：描述追加手动刷新提示，避免「已上传」与树为空矛盾', () => {
    const toast = summarizePalaceUpload({ total: 1, failed: 0, failedNames: [], refreshFailed: true })
    assert.equal(toast?.variant, 'success')
    assert.match(toast?.description ?? '', /手动刷新/)
  })

  it('部分失败：error 汇总成败份数并列出失败文件名', () => {
    const toast = summarizePalaceUpload({
      total: 5, failed: 2, failedNames: ['a.md', 'b.pdf'], refreshFailed: false,
    })
    assert.equal(toast?.variant, 'error')
    assert.equal(toast?.title, '上传完成：成功 3 份，失败 2 份')
    assert.match(toast?.description ?? '', /a\.md、b\.pdf/)
    assert.match(toast?.description ?? '', /可重试/)
    // 失败数 ≤ 上限：逐份 toast 保留（原因可见性）
    assert.equal(toast?.perFileToasts, true)
  })

  it('失败超过 3 份：点名折叠为「等 N 份」，逐份 toast 关闭（防风暴）', () => {
    const names = ['a.md', 'b.md', 'c.md', 'd.md', 'e.md']
    const toast = summarizePalaceUpload({
      total: names.length, failed: names.length, failedNames: names, refreshFailed: false,
    })
    assert.match(toast?.description ?? '', /a\.md、b\.md、c\.md 等 5 份/)
    assert.equal(toast?.perFileToasts, false)
  })

  it('部分失败叠加刷新失败：两种提示都保留', () => {
    const toast = summarizePalaceUpload({
      total: 2, failed: 1, failedNames: ['x.docx'], refreshFailed: true,
    })
    assert.match(toast?.description ?? '', /x\.docx/)
    assert.match(toast?.description ?? '', /手动刷新/)
  })

  it('PER_FILE_TOAST_LIMIT 与汇总分支一致：恰好等于上限时仍逐份弹', () => {
    const toast = summarizePalaceUpload({
      total: PER_FILE_TOAST_LIMIT + 1,
      failed: PER_FILE_TOAST_LIMIT,
      failedNames: Array.from({ length: PER_FILE_TOAST_LIMIT }, (_, i) => `f${i}.md`),
      refreshFailed: false,
    })
    assert.equal(toast?.perFileToasts, true)
  })
})
