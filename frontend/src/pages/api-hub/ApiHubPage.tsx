import { useCallback, useEffect, useState } from 'react'
import { Navigate, useParams } from 'react-router-dom'
import { toast } from 'sonner'
import { apiError, apiHub, type HubInterface } from '@/api/apiHub'
import { LoadingState } from '@/components/ui/LoadingState'
import InterfaceManager from './InterfaceManager'
import RunHistory from './RunHistory'

export default function ApiHubPage() {
  const { tab = 'interfaces' } = useParams()
  const [interfaces, setInterfaces] = useState<HubInterface[]>([])
  const [loading, setLoading] = useState(true)

  const reloadInterfaces = useCallback(async () => {
    const items = await apiHub.listInterfaces()
    setInterfaces(items)
    return items
  }, [])

  const reportError = useCallback((message: string) => {
    if (message) toast.error(message)
  }, [])

  useEffect(() => {
    setLoading(true)
    const request = tab === 'interfaces'
      ? reloadInterfaces()
      : Promise.resolve()
    request.catch(error => reportError(apiError(error))).finally(() => setLoading(false))
  }, [reloadInterfaces, reportError, tab])

  if (tab === 'operations' || tab === 'authorization') return <Navigate to="/api-hub/interfaces" replace />
  if (!['interfaces', 'history'].includes(tab)) return <Navigate to="/api-hub/interfaces" replace />

  return (
    <div className="relative h-full min-h-0 bg-[var(--color-bg-base)]">
      {loading ? (
        <LoadingState className="h-full" message="正在加载接口代理…" />
      ) : tab === 'interfaces' ? (
        <InterfaceManager interfaces={interfaces} reload={reloadInterfaces} onError={reportError} />
      ) : (
        <RunHistory />
      )}
    </div>
  )
}
