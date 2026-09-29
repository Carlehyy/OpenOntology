// 查询密钥状态判定（纯函数，独立成模块供 node:test 单测覆盖，先例：
// profile/remoteAccess.ts）。expires_at 与展示侧同一 UTC 口径（utils/datetime：
// 后端无 Z 串语义为 UTC），避免"显示未过期但徽标已过期"的 8 小时边界分裂。
import { parseServerTime } from '../../utils/datetime.ts'

export type QueryKeyStatus = 'active' | 'expired' | 'revoked'

export function keyStatus(item: { revoked_at: string | null; expires_at: string | null }): QueryKeyStatus {
  if (item.revoked_at) return 'revoked'
  if (item.expires_at) {
    const expires = parseServerTime(item.expires_at)
    if (expires && expires.getTime() <= Date.now()) return 'expired'
  }
  return 'active'
}

export const STATUS_META: Record<QueryKeyStatus, { label: string; variant: 'success' | 'warning' | 'secondary' }> = {
  active: { label: '有效', variant: 'success' },
  expired: { label: '已过期', variant: 'warning' },
  revoked: { label: '已吊销', variant: 'secondary' },
}
