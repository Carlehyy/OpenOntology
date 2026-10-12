import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  formatFileSize,
  isPlatformMediaSrc,
  listEmptyCopy,
  mediaKind,
  priorityStyle,
  sourceTypeLabel,
  stripMarkdownPreview,
} from '../../../pages/super-assistant/components/notificationsFormat.ts'

describe('priorityStyle', () => {
  it('四级优先级有中文标签与语义色令牌', () => {
    assert.equal(priorityStyle('urgent').label, '紧急')
    assert.equal(priorityStyle('high').label, '高优')
    assert.equal(priorityStyle('normal').label, '普通')
    assert.equal(priorityStyle('low').label, '低优')
    assert.match(priorityStyle('urgent').chipClass, /--color-danger/)
    assert.match(priorityStyle('high').chipClass, /--color-warning/)
  })

  it('未知取值回退 normal，不抛错', () => {
    assert.equal(priorityStyle('critical').label, '普通')
    assert.equal(priorityStyle('').label, '普通')
  })
})

describe('sourceTypeLabel', () => {
  it('三种来源映射中文；未知值原样透传', () => {
    assert.equal(sourceTypeLabel('manual'), '手动发送')
    assert.equal(sourceTypeLabel('internal'), '平台内部')
    assert.equal(sourceTypeLabel('ingest'), '外部投递')
    assert.equal(sourceTypeLabel('custom-x'), 'custom-x')
  })
})

describe('formatFileSize', () => {
  it('B / KB / MB 阶梯与人读化', () => {
    assert.equal(formatFileSize(0), '0 B')
    assert.equal(formatFileSize(999), '999 B')
    assert.equal(formatFileSize(1024), '1 KB')
    assert.equal(formatFileSize(1024 * 1024 - 1), '1024 KB')
    assert.equal(formatFileSize(3 * 1024 * 1024 + 150 * 1024), '3.1 MB')
  })

  it('非法输入返回占位符', () => {
    assert.equal(formatFileSize(Number.NaN), '-')
    assert.equal(formatFileSize(-5), '-')
  })
})

describe('mediaKind', () => {
  it('音频/视频扩展名（含大写与查询串）识别为原生控件', () => {
    assert.equal(mediaKind('/api/v2/notifications/m/a/voice.mp3?download=1'), 'audio')
    assert.equal(mediaKind('/api/v2/notifications/m/a/download?fmt=mp3'), 'image')  // 扩展名在查询串不算文件名后缀
    assert.equal(mediaKind('https://cdn.example.com/a/b/report.MP4?sig=1'), 'video')
    assert.equal(mediaKind('attachments/voice.wav'), 'audio')
    assert.equal(mediaKind('https://cdn.example.com/clip.webm'), 'video')
  })

  it('图片与无扩展名回退 image（交给 ZoomableImage）', () => {
    assert.equal(mediaKind('https://cdn.example.com/pic.png'), 'image')
    assert.equal(mediaKind('/api/v2/notifications/m/a/download'), 'image')
    assert.equal(mediaKind(undefined), 'image')
    assert.equal(mediaKind('no-extension-path'), 'image')
  })
})

describe('isPlatformMediaSrc', () => {
  it('仅消息附件下载端点需要鉴权拉取（其它 /api 路径一律拒绝）', () => {
    assert.equal(isPlatformMediaSrc('/api/v2/notifications/m1/attachments/a1/download'), true)
    assert.equal(isPlatformMediaSrc('/api/v2/notifications/m/attachments/a/download'), true)
    assert.equal(isPlatformMediaSrc('/api/v2/super-assistant/conversations/c/export'), false)
    assert.equal(isPlatformMediaSrc('/api/v2/notifications/m1'), false)
    assert.equal(isPlatformMediaSrc('https://cdn.example.com/a.png'), false)
    assert.equal(isPlatformMediaSrc('relative/path.png'), false)
    assert.equal(isPlatformMediaSrc(undefined), false)
  })
})

describe('stripMarkdownPreview', () => {
  it('生产实例：标题与加粗语法被剥离，正文语义保留', () => {
    assert.equal(stripMarkdownPreview('# 库存警报 **商品**: 缺货'), '库存警报 商品: 缺货')
  })

  it('链接取文字、图片移除、行内代码保留内容', () => {
    assert.equal(stripMarkdownPreview('见 [运维手册](https://ops.example.com) 第 3 节'), '见 运维手册 第 3 节')
    assert.equal(stripMarkdownPreview('![截图](https://cdn.example.com/a.png) 已上传'), '已上传')
    assert.equal(stripMarkdownPreview('错误码 `ETIMEDOUT` 已重试'), '错误码 ETIMEDOUT 已重试')
  })

  it('围栏代码块整体折叠，不把代码内容漏进单行预览', () => {
    assert.equal(stripMarkdownPreview('报告如下\n```json\n{"code": 1}\n```\n完毕'), '报告如下 完毕')
  })

  it('列表符号、引用符、表格竖线与换行都归一为空格分隔', () => {
    assert.equal(stripMarkdownPreview('- 第一项\n- 第二项'), '第一项 第二项')
    assert.equal(stripMarkdownPreview('> 引用内容\n正文'), '引用内容 正文')
    assert.equal(stripMarkdownPreview('| 任务 | 状态 |'), '任务 状态')
    assert.equal(stripMarkdownPreview('1. 步骤一\n2. 步骤二'), '步骤一 步骤二')
  })

  it('多级标题、斜体、删除线与空串边界', () => {
    assert.equal(stripMarkdownPreview('### 三级标题'), '三级标题')
    assert.equal(stripMarkdownPreview('*强调* 与 ~~废弃~~'), '强调 与 废弃')
    assert.equal(stripMarkdownPreview('   '), '')
    assert.equal(stripMarkdownPreview('![only](https://x/y.png)'), '')
  })

  it('词边界：蛇形命名与算式符号不当强调剥离', () => {
    assert.equal(stripMarkdownPreview('字段 user_name_field 缺失'), '字段 user_name_field 缺失')
    assert.equal(stripMarkdownPreview('配额 2 * 3 * 5 已用尽'), '配额 2 * 3 * 5 已用尽')
    assert.equal(stripMarkdownPreview('**加粗**仍应剥离'), '加粗仍应剥离')
  })
})

describe('listEmptyCopy', () => {
  it('未读空态：有历史消息时给「查看全部」引导，否则只说明', () => {
    const withHistory = listEmptyCopy('unread', 3)
    assert.equal(withHistory.title, '没有未读消息')
    assert.equal(withHistory.allCtaTotal, 3)
    const coldStart = listEmptyCopy('unread', 0)
    assert.equal(coldStart.allCtaTotal, undefined)
    assert.equal(coldStart.composeCta, undefined)
  })

  it('已标记/已归档空态：只讲语义，不带投递接入话术', () => {
    const starred = listEmptyCopy('starred', 5)
    assert.equal(starred.title, '还没有标记的消息')
    assert.equal(starred.allCtaTotal, undefined)
    assert.match(starred.description, /星标/)
    const archived = listEmptyCopy('archived', 5)
    assert.equal(archived.title, '没有已归档消息')
    assert.match(archived.description, /归档不会删除/)
  })

  it('全部空态：冷启动引导去发送；未知 tab 回退全部语义', () => {
    const all = listEmptyCopy('all', 0)
    assert.equal(all.composeCta, true)
    assert.equal(listEmptyCopy('whatever', 0).title, '还没有消息')
  })
})
