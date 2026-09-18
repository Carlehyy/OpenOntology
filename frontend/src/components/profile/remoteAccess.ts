/**
 * 远程访问二维码的地址推导（纯函数，node:test 直接覆盖，不依赖 DOM）。
 *
 * 二维码内容 = 当前浏览器 origin + pathname + HashRouter 深链：桌面端正在
 * 使用的地址（公网 IP / 域名 / 局域网 IP）就是手机可达的地址，与分享链接
 * 的既有惯例一致（ManualDatasetSharingModals、HttpPublicationModal 等）。
 */

// 手机扫码后未登录会被 ProtectedRoute 带去登录页，登录成功经 returnTo 自动跳回
const SUPER_ASSISTANT_HASH = '#/super-assistant'

export function buildMobileAccessUrl(origin: string, pathname: string): string {
  return `${origin}${pathname}${SUPER_ASSISTANT_HASH}`
}

/**
 * 本机回环/未指地址扫码必然不通（手机访问不到桌面端自己的 localhost），
 * 命中时界面需给出兜底提示，请用户改用局域网 IP 或公网地址访问后再扫码。
 */
export function isLoopbackOrigin(origin: string): boolean {
  let hostname: string
  try {
    hostname = new URL(origin).hostname
  } catch {
    return false
  }
  if (hostname === 'localhost' || hostname.endsWith('.localhost')) return true
  if (hostname === '[::1]') return true
  if (hostname === '0.0.0.0') return true
  // 127.0.0.0/8 整段均为回环
  return /^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(hostname)
}
