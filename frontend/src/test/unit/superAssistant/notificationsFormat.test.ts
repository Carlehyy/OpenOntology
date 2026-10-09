import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  formatFileSize,
  isPlatformMediaSrc,
  mediaKind,
  priorityStyle,
  sourceTypeLabel,
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
