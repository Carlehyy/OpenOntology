import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import { buildConversationExportFilename } from '../../../pages/super-assistant/exportFilename.ts'

describe('buildConversationExportFilename', () => {
  it('常规标题：清洗后拼接 id 前 8 位与 .json 后缀', () => {
    assert.equal(
      buildConversationExportFilename('上周数据摸底', 'c-earlier-1234'),
      '上周数据摸底-c-earlie.json',
    )
  })

  it('Windows 非法字符与多空白折叠为单空格', () => {
    assert.equal(
      buildConversationExportFilename('a/b\\c:d*e?f"g<h>i|j  \t k', 'conv-0001'),
      'a b c d e f g h i j k-conv-000.json',
    )
  })

  it('C0 控制字符与换行被清洗', () => {
    assert.equal(
      buildConversationExportFilename('行一\r\n行二\u0000\u0008', 'conv-0002'),
      '行一 行二-conv-000.json',
    )
  })

  it('RTL/双向 Unicode 格式符被剥离（防扩展名视觉伪装）', () => {
    assert.equal(
      buildConversationExportFilename('正常\u202Efdp.json标题', 'conv-0003'),
      '正常 fdp.json标题-conv-000.json',
    )
  })

  it('空标题与纯空白标题回退「未命名会话」', () => {
    assert.equal(buildConversationExportFilename('', 'conv-0004'), '未命名会话-conv-000.json')
    assert.equal(buildConversationExportFilename('   ', 'conv-0004'), '未命名会话-conv-000.json')
  })

  it('全非法字符标题清洗后为空时同样回退（不产出前导连字符文件名）', () => {
    assert.equal(buildConversationExportFilename('???', 'conv-0005'), '未命名会话-conv-000.json')
  })

  it('超长标题截断到 80 字符', () => {
    const name = buildConversationExportFilename('长'.repeat(120), 'conv-0006')
    assert.equal(name, `${'长'.repeat(80)}-conv-000.json`)
  })
})
