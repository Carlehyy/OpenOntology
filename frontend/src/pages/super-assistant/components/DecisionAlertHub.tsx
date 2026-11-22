import { useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { hasMenuAccess } from '@/config/navigation'
import { useAuthStore } from '@/stores/authStore'
import { startDecisionAlerts, stopDecisionAlerts } from '../decisionAlerts'
import { decisionDeepLink } from '../decisionAlertModel'

/**
 * 待处理决策事项提醒的全局挂载点：挂在 App 根部、路由之外，整个 SPA 生命周期只挂一次，
 * 用户停在平台任何页面都能收到提醒。无 super_assistant 菜单授权的用户完全不轮询，与
 * 聊天区右键入口、决策弹窗同一门控。提醒点击经 ?decision=<id> 深链落到超级助手页，
 * 由页面既有的 URL 同步打开弹窗。
 */
export function DecisionAlertHub() {
  const user = useAuthStore(state => state.user)
  const navigate = useNavigate()
  const openDecision = useRef<(focusId: string | null) => void>(() => {})
  openDecision.current = focusId => {
    navigate(decisionDeepLink(window.location.hash, focusId), { replace: true })
  }
  const enabled = hasMenuAccess(user, 'super_assistant')
  const userId = user?.id ?? null
  useEffect(() => {
    if (!enabled) {
      stopDecisionAlerts()
      return
    }
    startDecisionAlerts({ userId, onOpen: focusId => openDecision.current(focusId) })
    return stopDecisionAlerts
  }, [enabled, userId])
  return null
}
