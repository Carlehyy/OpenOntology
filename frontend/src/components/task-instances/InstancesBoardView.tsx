/**
 * 实例看板视图：需介入 / 流转中 / 已完结 / 已取消-失败 四阶分栏 + 激活入口。
 */
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertCircle, CircleCheck, Loader2, Play, XCircle } from 'lucide-react'
import { toast } from 'sonner'
import {
  apiError, taskInstancesApi,
  type InstanceListItem, type TemplateSummary,
} from '@/api/taskInstances'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { Modal } from '@/components/ui/Modal'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { formatDateTime } from '@/utils/datetime'
import { BOARD_GROUPS, INSTANCE_STATUS_LABELS, INSTANCE_STATUS_TONE, instanceGroup } from './instanceView'

const GROUP_ICONS = {
  attention: AlertCircle,
  running: Loader2,
  done: CircleCheck,
  stopped: XCircle,
} as const

function randomIdempotencyKey(): string {
  return `ui-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}

export default function InstancesBoardView({ templates }: {
  templates: TemplateSummary[] | undefined
}) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [activating, setActivating] = useState<TemplateSummary | null>(null)
  const [instanceName, setInstanceName] = useState('')
  const [goal, setGoal] = useState('')
  const [inputsText, setInputsText] = useState('{}')
  const [confirmCancel, setConfirmCancel] = useState<string | null>(null)
  const [cancelReason, setCancelReason] = useState('不需要了')

  const instances = useQuery({
    queryKey: ['task-instances', 'instances'],
    queryFn: () => taskInstancesApi.listInstances({ size: 100 }),
    refetchInterval: 8000,
  })

  const activate = useMutation({
    mutationFn: () => {
      let inputs: Record<string, unknown>
      try {
        inputs = inputsText.trim() ? JSON.parse(inputsText) as Record<string, unknown> : {}
      } catch {
        throw new Error('实例入参必须是合法 JSON')
      }
      return taskInstancesApi.activateInstance(
        activating?.id as string,
        { name: instanceName, goal, inputs },
        randomIdempotencyKey())
    },
    onSuccess: data => {
      toast.success('实例已激活')
      setActivating(null)
      void queryClient.invalidateQueries({ queryKey: ['task-instances', 'instances'] })
      navigate(`/task-instances/instances/${data.id}`)
    },
    onError: error => toast.error(apiError(error)),
  })

  const cancel = useMutation({
    mutationFn: (id: string) => taskInstancesApi.cancelInstance(id, cancelReason),
    onSuccess: () => {
      toast.success('取消请求已提交')
      setConfirmCancel(null)
      void queryClient.invalidateQueries({ queryKey: ['task-instances', 'instances'] })
    },
    onError: error => toast.error(apiError(error)),
  })

  const byGroup = new Map<string, InstanceListItem[]>(
    BOARD_GROUPS.map(group => [group.key, []]))
  for (const item of instances.data?.items ?? []) {
    byGroup.get(instanceGroup(item))?.push(item)
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <div className="flex gap-4 text-xs text-[var(--color-text-tertiary)]">
          <span>共 {instances.data?.total ?? 0} 个实例</span>
          <span>需介入 {(byGroup.get('attention') ?? []).length}</span>
          <span>7 秒轮询刷新</span>
        </div>
        <Button
          size="sm" className="ml-auto gap-1.5"
          onClick={() => {
            if (!templates?.length) {
              toast.info('请先创建流程模板')
              return
            }
            setInstanceName('')
            setGoal('')
            setInputsText('{}')
            setActivating(templates[0])
          }}
        >
          <Play className="h-4 w-4" />激活实例
        </Button>
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        {BOARD_GROUPS.map(group => {
          const Icon = GROUP_ICONS[group.key]
          const items = byGroup.get(group.key) ?? []
          return (
            <div key={group.key} className="space-y-2">
              <div className="flex items-center gap-1.5 px-1 text-sm font-medium text-[var(--color-text-secondary)]">
                <Icon className="h-4 w-4 text-[var(--color-text-tertiary)]" />
                {group.label}
                <span className="text-xs text-[var(--color-text-tertiary)]">{items.length}</span>
              </div>
              <div className="space-y-2">
                {items.map(item => (
                  <Card
                    key={item.id}
                    className="cursor-pointer transition-shadow hover:shadow-md"
                    onClick={() => navigate(`/task-instances/instances/${item.id}`)}
                  >
                    <CardContent className="space-y-1.5">
                      <div className="flex items-start justify-between gap-2">
                        <span className="truncate text-sm font-medium text-[var(--color-text-primary)]">
                          {item.name}
                        </span>
                        <span className={`shrink-0 rounded px-1.5 py-0.5 text-[11px] ${INSTANCE_STATUS_TONE[item.status] ?? ''}`}>
                          {INSTANCE_STATUS_LABELS[item.status] ?? item.status}
                        </span>
                      </div>
                      <p className="line-clamp-2 text-xs text-[var(--color-text-tertiary)]">{item.goal}</p>
                      <div className="flex items-center justify-between text-[11px] text-[var(--color-text-tertiary)]">
                        <span>{formatDateTime(item.created_at)}</span>
                        {item.status === 'active' ? (
                          <Button variant="ghost" size="sm" className="h-6 px-1.5 text-[11px] text-[var(--color-danger)]"
                            onClick={event => {
                              event.stopPropagation()
                              setConfirmCancel(item.id)
                            }}>
                            取消
                          </Button>
                        ) : null}
                      </div>
                    </CardContent>
                  </Card>
                ))}
                {!items.length ? (
                  <div className="rounded-lg border border-dashed border-border py-8 text-center text-xs text-[var(--color-text-tertiary)]">
                    暂无
                  </div>
                ) : null}
              </div>
            </div>
          )
        })}
      </div>

      {/* 激活弹窗 */}
      <Modal
        open={activating !== null}
        onClose={() => setActivating(null)}
        title="激活任务实例"
      >
        <div className="space-y-4">
          <div>
            <label className="mb-1.5 block text-sm font-medium text-[var(--color-text-primary)]">
              模板<span className="ml-0.5 text-[var(--color-danger)]">*</span>
            </label>
            <Select
              value={activating?.id ?? ''}
              onValueChange={value => {
                const found = templates?.find(item => item.id === value)
                if (found) setActivating(found)
              }}
            >
              <SelectTrigger aria-label="选择模板" className="h-9 w-full rounded-lg">
                <SelectValue placeholder="选择模板" />
              </SelectTrigger>
              <SelectContent>
                {(templates ?? []).map(template => (
                  <SelectItem key={template.id} value={template.id}>
                    {template.name}（v{template.latest_revision_no ?? '-'}）
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <Input
            label="实例名称" required maxLength={200}
            value={instanceName}
            onChange={event => setInstanceName(event.target.value)}
            placeholder="例如：登录页改版评审-1023"
          />
          <div>
            <label className="mb-1.5 block text-sm font-medium text-[var(--color-text-primary)]">
              目标（Goal）<span className="ml-0.5 text-[var(--color-danger)]">*</span>
            </label>
            <textarea
              aria-label="任务目标"
              className="min-h-24 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm"
              value={goal}
              onChange={event => setGoal(event.target.value)}
              placeholder="这次任务要完成什么；agent 节点的执行提示以此为骨干"
            />
          </div>
          <div>
            <label className="mb-1.5 block text-sm font-medium text-[var(--color-text-primary)]">
              实例入参（JSON，可选）
            </label>
            <textarea
              aria-label="实例入参 JSON"
              className="min-h-16 w-full rounded-lg border border-border bg-background px-3 py-2 font-mono text-xs"
              value={inputsText}
              onChange={event => setInputsText(event.target.value)}
            />
          </div>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setActivating(null)}>取消</Button>
            <Button
              onClick={() => activate.mutate()}
              disabled={activate.isPending || !instanceName.trim() || !goal.trim()}
            >
              {activate.isPending ? '激活中…' : '激活'}
            </Button>
          </div>
        </div>
      </Modal>

      <ConfirmDialog
        open={confirmCancel !== null}
        title="取消实例"
        description={
          <div className="space-y-2">
            <p>取消为两阶段冻结：在途节点停止、等待中节点作废，不可恢复。</p>
            <Input
              aria-label="取消理由"
              value={cancelReason}
              onChange={event => setCancelReason(event.target.value)}
            />
          </div>
        }
        confirmText="确认取消"
        variant="danger"
        onClose={() => setConfirmCancel(null)}
        onConfirm={() => {
          if (confirmCancel) cancel.mutate(confirmCancel)
        }}
      />
    </div>
  )
}
