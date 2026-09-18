import { QRCode } from 'antd'
import { QrCode, TriangleAlert } from 'lucide-react'

import { buildMobileAccessUrl, isLoopbackOrigin } from '@/components/profile/remoteAccess'

/**
 * 远程访问版块（个人资料弹窗 → 隐私变量 tab，隐私变量分区下方）：
 * 展示当前访问地址的二维码，手机扫码后在浏览器打开超级助手（需登录）。
 * 二维码只含 URL、不含任何凭据；扫码免登（一次性票据）为后续独立迭代。
 * QRCode 自带白底衬边（antd 内置样式），黑码白底保证扫码可靠性。
 */
export default function RemoteAccessSection() {
  const accessUrl = buildMobileAccessUrl(window.location.origin, window.location.pathname)
  const loopback = isLoopbackOrigin(window.location.origin)

  return (
    <section aria-label="远程访问" className="mt-5 border-t border-[var(--color-border)] pt-4">
      <h4 className="flex items-center gap-1.5 text-sm font-medium text-[var(--color-text-primary)]">
        <QrCode size={14} />远程访问
      </h4>
      <p className="mt-0.5 text-xs text-[var(--color-text-tertiary)]">
        用手机相机或微信扫码，在手机浏览器打开超级助手；扫码后需登录你的账号。二维码只包含访问地址，不含任何凭据。
      </p>
      <div className="mt-3 flex flex-wrap items-start gap-4">
        <div className="shrink-0 overflow-hidden rounded-lg" data-testid="remote-access-qr">
          <QRCode value={accessUrl} size={144} type="svg" />
        </div>
        <div className="min-w-0 flex-1 space-y-2">
          <label className="block">
            <span className="mb-1.5 block text-xs font-medium text-[var(--color-text-secondary)]">访问地址</span>
            <input
              readOnly
              value={accessUrl}
              onFocus={event => event.currentTarget.select()}
              aria-label="移动端访问地址"
              className="h-9 w-full rounded-md border border-border bg-[var(--color-bg-elevated)] px-3 font-mono text-xs text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </label>
          {loopback && (
            <p className="flex items-start gap-1.5 rounded-lg border border-[var(--color-warning)] bg-[var(--color-warning-bg)] px-2.5 py-2 text-xs text-[var(--color-text-primary)]">
              <TriangleAlert size={13} className="mt-0.5 shrink-0 text-[var(--color-warning)]" aria-hidden="true" />
              当前是本机回环地址，手机无法连通。请改用局域网 IP 或公网地址访问本平台后，再打开本弹窗扫码。
            </p>
          )}
        </div>
      </div>
    </section>
  )
}
