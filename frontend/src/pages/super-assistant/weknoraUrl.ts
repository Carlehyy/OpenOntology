/**
 * 解析后端下发的 WeKnora 原生 Web 入口地址。
 *
 * 校验与回环改写规则见 webEntryUrl.ts（与 Kaneo 等外链入口共用）；
 * WeKnora 网关与平台同机发布（HTTP 8089 / HTTPS 8444），未显式配置
 * WEKNORA_WEB_URL 时后端下发回环默认值，前端改用当前访问平台的主机名。
 */
import { resolveWebUrl } from './webEntryUrl.ts'

export function resolveWeKnoraWebUrl(
  raw: string | null | undefined,
  currentHostname: string,
): string | null {
  return resolveWebUrl(raw, currentHostname)
}

/**
 * 解析一键免登录跳转地址（POST /super-assistant/weknora/entry 下发）。
 *
 * redirect_url 与 web_url 同源，形如 {WEKNORA_WEB_URL}/sso/entry?ticket=…：
 * 套用与领域知识入口一致的回环主机改写（后端下发回环默认值时改用当前访问
 * 平台的主机名，协议/端口保留），避免远程浏览器把票据送到访客自己的机器；
 * 非回环地址视为部署方显式配置，原样使用。仅接受 http(s) 且路径恰为
 * /sso/entry 的地址，异常输入返回 null（调用方按打开失败回退原生直开）。
 */
export function resolveWeKnoraEntryUrl(
  raw: string | null | undefined,
  currentHostname: string,
): string | null {
  const trimmed = raw?.trim()
  if (!trimmed) return null
  try {
    const target = new URL(trimmed)
    const origin = resolveWebUrl(trimmed, currentHostname)
    if (!origin || target.pathname !== '/sso/entry') return null
    return `${origin}${target.pathname}${target.search}`
  } catch {
    return null
  }
}
