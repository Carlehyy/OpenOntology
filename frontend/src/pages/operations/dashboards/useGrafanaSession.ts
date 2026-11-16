/**
 * 看板会话续签（`POST /operations/dashboards/grafana-session`，8h HttpOnly cookie）。
 *
 * 触发时机（冻结契约 §3/§7）：
 * - 进入工作台时签发一次；
 * - 页面停留期间每 30 分钟续签一次（卸载时清除计时器，忽略在途结果）；
 * - 打开「编排」「预览」前再签一次（由调用方 await 本 hook 的 ensureSession，
 *   失败时禁止导航或挂载 iframe）。
 *
 * ensureSession 失败时不自行 toast：调用方决定是阻止编排/预览还是给出页面级
 * 提示，避免同一失败被重复反馈。
 */
import { useCallback, useEffect } from 'react'
import { toast } from 'sonner'
import operationsDashboardsApi, {
  operationsApiErrorText,
} from '@/api/v2/operations-dashboards'

/** 与 8 小时 TTL 配套的安全续签间隔。 */
export const GRAFANA_SESSION_RENEW_INTERVAL_MS = 30 * 60 * 1000

export function useGrafanaSession() {
  /** 每次调用都重新签发（廉价），任何失败向上抛出，由调用方阻止后续动作。 */
  const ensureSession = useCallback(async (): Promise<void> => {
    await operationsDashboardsApi.ensureSession()
  }, [])

  useEffect(() => {
    let cancelled = false
    const renew = async () => {
      try {
        await operationsDashboardsApi.ensureSession()
      } catch (error) {
        if (cancelled) return
        toast.error('看板会话签发失败', {
          description: operationsApiErrorText(
            error,
            '编排与预览可能不可用，请稍后重试或刷新页面。',
          ),
        })
      }
    }
    void renew()
    const timer = window.setInterval(() => { void renew() }, GRAFANA_SESSION_RENEW_INTERVAL_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [])

  return { ensureSession }
}
