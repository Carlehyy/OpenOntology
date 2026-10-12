/**
 * 非安全上下文的 HTTPS 入口自动升级（纯函数部分用 node:test 直接覆盖，不依赖 DOM）。
 *
 * 平台以裸 IP + 自签证书提供 HTTPS 入口（默认 8443，见 docker-compose.prod.yml
 * 的 PUBLIC_HTTPS_PORT 与 frontend/nginx/ssl-443.conf），浏览器麦克风等安全
 * 上下文能力只有从该入口进入才可用。CI 构建时把公网主机名与入口端口烧进
 * 产物（VITE_HTTPS_UPGRADE_HOST / VITE_HTTPS_UPGRADE_PORT，见
 * deploy-nano-ontoprompt.yml 的 Frontend production build 步骤）：
 *
 * - 应用入口守卫：SPA 在「同主机的普通 HTTP 页面」启动时自动跳转到 HTTPS
 *   入口（保留 pathname/search/hash 深链），语音按钮随即出现。主机名精确
 *   匹配才生效：localhost / 局域网 IP / 其它地址一律不动，开发与 E2E 环境
 *   不受影响；直调 8088 API 的机器客户端（n8n 回传、远程助手邀请 curl、
 *   文件夹同步脚本）不经过 SPA，行为不变。
 * - 「语音」tab 降级提示：只要烧入了入口，无论当前主机名是否匹配，都给出
 *   一键前往链接（局域网 IP 访问时公网 HTTPS 入口仍可能可达）。
 *
 * 烧入值做严格格式校验：非法 host/port（尾随空格、误填 scheme、端口越界
 * 等）一律视为未烧入，宁可静默不升级也不能拼出坏 URL 让 HTTP 入口白屏。
 */

export interface HttpsUpgradeTarget {
  host: string
  port: string
}

export interface HttpsUpgradeLocationLike {
  protocol: string
  hostname: string
  pathname: string
  search: string
  hash: string
}

/** 无条件拼出入口深链（含 URL 登录态注入的 query 与 HashRouter 深链）。 */
export function buildHttpsEntranceHref(
  target: HttpsUpgradeTarget,
  location: HttpsUpgradeLocationLike,
): string {
  return `https://${target.host}:${target.port}${location.pathname}${location.search}${location.hash}`
}

/**
 * 应用入口守卫的升级判定：仅「普通 HTTP 且主机名与烧入的公网主机名精确
 * 相同」时返回跳转地址；https、localhost、局域网 IP、未烧入均为 null。
 */
export function resolveHttpsUpgradeHref(
  location: HttpsUpgradeLocationLike,
  target: HttpsUpgradeTarget | null,
): string | null {
  if (!target || !target.host || !target.port) return null
  if (location.protocol !== 'http:') return null
  if (location.hostname !== target.host) return null
  return buildHttpsEntranceHref(target, location)
}

/** 本机/回环主机名（这类地址上从不自动升级，诊断日志也跳过）。 */
export function isLocalHostname(hostname: string): boolean {
  const host = hostname.toLowerCase().replace(/^\[|\]$/g, '')
  if (host === 'localhost' || host.endsWith('.localhost')) return true
  if (host === '::1' || host === '0.0.0.0') return true
  // 127.0.0.0/8 整段均为回环
  return /^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(host)
}

export interface HttpsUpgradeBuildEnv {
  VITE_HTTPS_UPGRADE_HOST?: unknown
  VITE_HTTPS_UPGRADE_PORT?: unknown
}

/**
 * 读取构建期烧入值（默认取 import.meta.env，测试可注入）；任何字段缺失或
 * 格式非法（host 非 DNS 名/IP、port 非数字或越界）都返回 null。host 统一
 * 转小写——浏览器 location.hostname 恒为小写，变量带大写时若不归一会静默失配。
 */
export function httpsUpgradeTargetFromBuild(env: HttpsUpgradeBuildEnv = import.meta.env as never): HttpsUpgradeTarget | null {
  const rawHost = env?.VITE_HTTPS_UPGRADE_HOST
  const rawPort = env?.VITE_HTTPS_UPGRADE_PORT
  if (typeof rawHost !== 'string' || typeof rawPort !== 'string') return null
  const host = rawHost.trim().toLowerCase()
  const port = rawPort.trim()
  // host 只允许 DNS 名/IP 字符（阻断 "https://…" 误填与空格等杂字符）
  if (!/^[a-z0-9.-]+$/.test(host) || !host) return null
  if (!/^\d{1,5}$/.test(port)) return null
  const portNum = Number(port)
  if (portNum < 1 || portNum > 65535) return null
  return { host, port: String(portNum) }
}
