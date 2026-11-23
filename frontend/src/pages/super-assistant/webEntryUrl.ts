/**
 * 解析后端下发的外部 Web 入口地址（WeKnora / Kaneo 共用）。
 *
 * 只接受 http(s) 绝对地址：后端返回异常协议时不做跳转，避免把用户带到非
 * Web 目标。回环主机名（localhost、127.0.0.0/8、[::1]、0.0.0.0）只在本机
 * 可达——部署未显式配置对外地址时后端会把开发默认值原样下发，远程浏览器
 * 点击即落到访客自己机器上；此时改用浏览器当前访问平台的主机名，协议与
 * 端口保留（服务与平台同机发布），与本体等同源跟随当前访问地址。非回环
 * 地址（公网 IP、自定义域名）视为部署方显式配置，原样使用。
 */
export function resolveWebUrl(
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

/**
 * 解析不随当前访问主机改写的原生入口地址（Kaneo / Multica 平台默认入口用）。
 *
 * Kaneo 前端的 API/认证 origin 均固定注入自后端 KANEO_WEB_URL：若像 WeKnora
 * 那样把回环主机改写成当前访问主机，会与上游 trustedOrigins/cookie/API 不一致
 * （登录 403 或远程不可达）。因此严格保留后端 canonical origin，仅做 http(s)
 * 协议校验；本地 localhost 默认可用，远程访问须由部署方显式配置 KANEO_WEB_URL。
 *
 * Multica 平台默认 profile 的 web_url 同用此策略：MULTICA_DEFAULT_WEB_URL 由
 * 部署下发（bundled 栈为 multica-gateway 的推导 origin，见
 * docs/operations/configuration.md），前端不做任何推导，回环值也原样保留。
 * custom profile 恒无 web_url 字段，其「打开工作台」入口由
 * multicaCustomWebEntryUrl 从 base_url origin 推导（前提与局限见该函数注释）。
 */
export function resolveNativeWebUrl(raw: string | null | undefined): string | null {
  return resolveWebUrl(raw, '')
}
