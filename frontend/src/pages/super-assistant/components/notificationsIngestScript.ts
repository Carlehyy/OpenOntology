/**
 * 消息入口调用脚本的纯文本生成（端点与密钥均由调用方注入，保持本模块无 DOM/API 依赖、可单测）。
 * 脚本形态与 api-hub 公开接口示例同源：curl + X-API-Key 头 + JSON 体。
 */

/** 未注入密钥时的占位符：复制后需替换为密钥明文（明文仅在签发响应中一次性返回） */
export const INGEST_KEY_PLACEHOLDER = '<你的密钥>'

/** 消息中转站对外投递端点路径（绝对地址由运行环境拼接） */
export const INGEST_ENDPOINT_PATH = '/api/v2/notifications/ingest'

export function ingestCurlScript(endpoint: string, apiKey?: string): string {
  const key = apiKey?.trim() || INGEST_KEY_PLACEHOLDER
  return [
    `curl -X POST ${endpoint} \\`,
    `  -H "X-API-Key: ${key}" \\`,
    '  -H "Content-Type: application/json" \\',
    `  -d '{"eventId":"order-123","title":"订单异常","body":"# 详情\\n金额超限","priority":"high"}'`,
  ].join('\n')
}
