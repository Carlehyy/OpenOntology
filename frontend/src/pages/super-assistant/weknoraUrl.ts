/**
 * 解析后端下发的 WeKnora 原生 Web 入口地址。
 *
 * 只接受 http(s) 绝对地址：后端返回异常协议时不做跳转，避免把用户带到非
 * Web 目标。回环主机名（localhost、127.0.0.0/8、[::1]、0.0.0.0）只在本机
 * 可达——部署未显式配置 WEKNORA_WEB_URL 时后端会把开发默认值原样下发，
 * 远程浏览器点击即落到访客自己机器上；此时改用浏览器当前访问平台的主机名，
 * 协议与端口保留（WeKnora 网关与平台同机发布，HTTP 8089 / HTTPS 8444），
 * 与本体等同源跟随当前访问地址。非回环地址（公网 IP、自定义域名）视为
 * 部署方显式配置，原样使用。
 */
export function resolveWeKnoraWebUrl(
  raw: string | null | undefined,
  currentHostname: string,
): string | null {
  const trimmed = raw?.trim()
  if (!trimmed) return null
  try {
    const target = new URL(trimmed)
    if (target.protocol !== 'https:' && target.protocol !== 'http:') return null
    if (isLoopbackHostname(target.hostname) && currentHostname) {
      target.hostname = currentHostname
    }
    // 后端 origin() 契约保证无路径：返回纯 origin，避免 toString 补 "/"
    return target.origin
  } catch {
    return null
  }
}

function isLoopbackHostname(hostname: string): boolean {
  const host = hostname.toLowerCase().replace(/^\[|\]$/g, '')
  if (host === 'localhost' || host === '::1' || host === '0.0.0.0') return true
  return /^127(?:\.\d{1,3}){3}$/.test(host)
}
