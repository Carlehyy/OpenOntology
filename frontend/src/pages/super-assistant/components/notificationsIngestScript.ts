/**
 * 消息入口调用脚本的纯文本生成（端点与密钥均由调用方注入，保持本模块无 DOM/API 依赖、可单测）。
 * 脚本形态与 api-hub 公开接口示例同源：curl + X-API-Key 头 + JSON 体。
 * 渲染与复制共用同一分段结构（ingestCurlLines）：所见即所复制，避免两态漂移。
 */

/** 未注入密钥时的占位符：复制后需替换为密钥明文（明文仅在签发响应中一次性返回） */
export const INGEST_KEY_PLACEHOLDER = '<你的密钥>'

/** 消息中转站对外投递端点路径（绝对地址由运行环境拼接） */
export const INGEST_ENDPOINT_PATH = '/api/v2/notifications/ingest'

/** 语法分段的语义种类（组件层据此着色，本模块只负责切分） */
export type CurlSegmentKind = 'cmd' | 'url' | 'flag' | 'header-value' | 'json-key' | 'placeholder' | 'plain'

export interface CurlSegment {
  text: string
  kind: CurlSegmentKind
}

/** 脚本示例 JSON 体（key 顺序即展示顺序；value 内 \n 为字面转义，两处换行都不破坏 JSON） */
const SAMPLE_BODY: Array<[key: string, value: string]> = [
  ['eventId', 'order-123'],
  ['title', '订单异常'],
  ['body', '# 详情\\n金额超限'],
  ['priority', 'high'],
]

export function ingestCurlLines(endpoint: string, apiKey?: string): CurlSegment[][] {
  const key = apiKey?.trim() || INGEST_KEY_PLACEHOLDER
  const keySegments: CurlSegment[] = key === INGEST_KEY_PLACEHOLDER
    ? [{ text: key, kind: 'placeholder' }]
    : [{ text: key, kind: 'plain' }]
  const bodyLines: CurlSegment[][] = SAMPLE_BODY.map(([k, v], index) => {
    const line: CurlSegment[] = [
      { text: `    "${k}"`, kind: 'json-key' },
      { text: `: "${v}"`, kind: 'plain' },
    ]
    if (index < SAMPLE_BODY.length - 1) line.push({ text: ',', kind: 'plain' })
    return line
  })
  return [
    [
      { text: 'curl -X POST ', kind: 'cmd' },
      { text: endpoint, kind: 'url' },
      { text: ' \\', kind: 'plain' },
    ],
    [
      { text: '  -H ', kind: 'flag' },
      { text: '"X-API-Key: ', kind: 'plain' },
      ...keySegments,
      { text: '" \\', kind: 'plain' },
    ],
    [
      { text: '  -H ', kind: 'flag' },
      { text: '"Content-Type: application/json" \\', kind: 'header-value' },
    ],
    [{ text: "  -d '{", kind: 'flag' }],
    ...bodyLines,
    [
      { text: "  }'", kind: 'flag' },
    ],
  ]
}

export function ingestCurlScript(endpoint: string, apiKey?: string): string {
  return ingestCurlLines(endpoint, apiKey)
    .map(line => line.map(seg => seg.text).join(''))
    .join('\n')
}
