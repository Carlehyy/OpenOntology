/**
 * 浏览器系统通知（Notification API）的最小封装。
 *
 * 约束：仅在安全上下文（HTTPS / localhost）存在；必须由用户在点击手势内授权一次；
 * 浏览器完全关闭后无法送达（未引入 Service Worker / Web Push）。页面在后台标签页时
 * 仍可弹出，这正是本能力相对应用内提示的价值所在。
 */

export type DesktopNotifyPermission = NotificationPermission | 'unsupported'

export function desktopNotifyPermission(): DesktopNotifyPermission {
  if (typeof window === 'undefined' || typeof window.Notification === 'undefined') return 'unsupported'
  return window.Notification.permission
}

/** 必须在用户点击的处理函数内直接调用：浏览器要求瞬态激活才弹出授权框。 */
export async function requestDesktopNotifyPermission(): Promise<DesktopNotifyPermission> {
  if (desktopNotifyPermission() === 'unsupported') return 'unsupported'
  try {
    return await window.Notification.requestPermission()
  } catch {
    return window.Notification.permission
  }
}

export interface DesktopNotificationInput {
  title: string
  body: string
  tag: string
  onClick: () => void
}

/** 返回是否成功创建。同 tag 的通知被浏览器合并替换，多标签页同时提醒时只留一条。 */
export function showDesktopNotification(input: DesktopNotificationInput): boolean {
  if (desktopNotifyPermission() !== 'granted') return false
  try {
    const notification = new window.Notification(input.title, { body: input.body, tag: input.tag })
    notification.onclick = () => {
      window.focus()
      input.onClick()
      notification.close()
    }
    return true
  } catch {
    return false
  }
}
