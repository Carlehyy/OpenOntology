import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  channelFailureHint,
  formatFileSize,
  forwardButtonLabel,
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

  it('未闭合围栏/反引号（服务端 200 码点截断可造成）补齐成对后整体折叠', () => {
    // 截断砍掉闭合围栏：代码内容不漏进单行预览，也不残留孤立反引号
    assert.equal(stripMarkdownPreview('```\n' + 'x'.repeat(60)), '')
    // 孤立行内反引号补齐后按空处理
    assert.equal(stripMarkdownPreview('错误码 `ETIMEDOUT'), '错误码 ETIMEDOUT')
  })

  it('截断锯齿的行首语法残留（结尾孤立 # / - 等）被收尾清理', () => {
    assert.equal(stripMarkdownPreview('x'.repeat(198) + '\n#'), 'x'.repeat(198))
    assert.equal(stripMarkdownPreview('y'.repeat(199) + '\n-'), 'y'.repeat(199))
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

describe('channelFailureHint', () => {
  it('去掉与状态行重复的「渠道投递失败」包装后按内因归类', () => {
    assert.equal(
      channelFailureHint('渠道投递失败（SMTP 授权码错误 535）'),
      '邮箱鉴权失败，请检查发件邮箱账号与授权码',
    )
    assert.equal(channelFailureHint('webhook 返回 401 Unauthorized'), '渠道鉴权失败，请检查渠道密钥配置')
    assert.equal(channelFailureHint('连接超时'), '连接超时，请检查网络或渠道地址')
    assert.equal(channelFailureHint('渠道投递失败（对方服务 502）'), '渠道服务暂不可用，可稍后重试或直发测试')
    assert.equal(channelFailureHint('422 收件人地址无效'), '参数被渠道拒绝，请检查收件人与配置格式')
  })

  it('超时先于 smtp 字样判定：SMTP 主机名里的 smtp 不再把超时误导成邮箱鉴权', () => {
    assert.equal(
      channelFailureHint('A timeout occurred while connecting to smtp.qq.com:465'),
      '连接超时，请检查网络或渠道地址',
    )
    assert.equal(channelFailureHint('SMTP 连接超时'), '连接超时，请检查网络或渠道地址')
  })

  it('永久性收件人错误先于 5xx 瞬时档：550 不再指引「稍后重试」', () => {
    assert.equal(
      channelFailureHint('550 5.1.1 Recipient address rejected: user not found'),
      '参数被渠道拒绝，请检查收件人与配置格式',
    )
  })

  it('邮箱鉴权要求邮件特征共现：webhook 的 username/登录 不误入邮箱档', () => {
    assert.equal(channelFailureHint('参数 invalid: username'), '参数被渠道拒绝，请检查收件人与配置格式')
  })

  it('auth 词边界容忍下划线邻接（invalid_auth 等错误码走渠道鉴权档）', () => {
    assert.equal(channelFailureHint('invalid_auth'), '渠道鉴权失败，请检查渠道密钥配置')
    assert.equal(channelFailureHint('authentication_failed'), '渠道鉴权失败，请检查渠道密钥配置')
  })

  it('限频单独成档；最高频的 notify 拒收给定向排查提示', () => {
    assert.equal(channelFailureHint('webhook 返回 429 Too Many Requests'), '触发渠道限频，请稍后重试或降低发送频率')
    assert.equal(
      channelFailureHint('渠道投递失败（notify 返回失败）'),
      '渠道端拒收，可直发测试核对（常见：关键词过滤或机器人权限）',
    )
  })

  it('空原因给复现引导；无法归类的原文兜底到悬浮提示', () => {
    assert.equal(channelFailureHint(''), '原因未知，可直发测试消息复现')
    assert.equal(channelFailureHint('   '), '原因未知，可直发测试消息复现')
    assert.equal(channelFailureHint('apprise URL 无法解析'), '原因详见悬浮提示')
  })
})

describe('forwardButtonLabel', () => {
  it('未选渠道明示「先选择」，避免「转发（0）」被误读为 0 条成功', () => {
    assert.equal(forwardButtonLabel(0), '选择渠道后转发')
    assert.equal(forwardButtonLabel(2), '转发（2 个渠道）')
  })
})
