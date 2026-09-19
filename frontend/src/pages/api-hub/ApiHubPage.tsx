import { useCallback, useEffect, useState } from 'react'
import { Navigate, useParams } from 'react-router-dom'
import { CloudOff, RefreshCw } from 'lucide-react'
import { toast } from 'sonner'
import { apiError, apiHub, type HubInterface } from '@/api/apiHub'
import { Button } from '@/components/ui/Button'
import { LoadingState } from '@/components/ui/LoadingState'
import InterfaceManager from './InterfaceManager'
import RunHistory from './RunHistory'

export default function ApiHubPage() {
  const { tab = 'interfaces' } = useParams()
  const [interfaces, setInterfaces] = useState<HubInterface[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')

  const reloadInterfaces = useCallback(async () => {
    const items = await apiHub.listInterfaces()
    setInterfaces(items)
    return items
  }, [])

  const reportError = useCallback((message: string) => {
    if (message) toast.error(message)
  }, [])

  // 首载失败必须区别于「真空列表」：错误占用整个工作区并给重试入口，
  // 避免把服务故障/权限过期误读成「还没有接口」。
  const loadInitial = useCallback(async () => {
    setLoading(true)
    setLoadError('')
    try {
      await reloadInterfaces()
    } catch (error) {
      setLoadError(apiError(error))
    } finally {
      setLoading(false)
    }
  }, [reloadInterfaces])

  useEffect(() => {
    if (tab === 'interfaces') void loadInitial()
    else setLoading(false)
  }, [loadInitial, tab])

  if (tab === 'operations' || tab === 'authorization') return <Navigate to="/api-hub/interfaces" replace />
  if (!['interfaces', 'history'].includes(tab)) return <Navigate to="/api-hub/interfaces" replace />

  return (
    <div className="relative h-full min-h-0 bg-[var(--color-bg-base)]">
      {tab === 'history' ? (
        <RunHistory />
      ) : loading ? (
        <LoadingState className="h-full" message="正在加载接口代理…" />
      ) : loadError ? (
        <div role="alert" className="flex h-full flex-col items-center justify-center px-6 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-[var(--color-danger-bg)] text-[var(--color-danger)]">
            <CloudOff size={22} />
          </div>
          <p className="mt-4 text-sm font-semibold text-[var(--color-text-primary)]">接口清单加载失败</p>
          <p className="mt-1.5 max-w-md text-xs leading-5 text-[var(--color-text-tertiary)]">
            {loadError}。请检查网络或登录状态后重试。
          </p>
          <Button size="sm" className="mt-5" onClick={() => void loadInitial()}>
            <RefreshCw size={14} />重试
          </Button>
        </div>
      ) : (
        <InterfaceManager interfaces={interfaces} reload={reloadInterfaces} onError={reportError} />
      )}
    </div>
  )
}
