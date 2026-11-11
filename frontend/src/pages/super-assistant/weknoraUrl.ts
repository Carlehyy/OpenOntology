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
