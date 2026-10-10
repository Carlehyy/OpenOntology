/**
 * 实例详情视图（设计方案 §10-4）：
 * 状态 DAG 快照（节点按状态着色/条件分叉高亮）+ SSE 实时事件时间线
 * （断线重拉快照）+ 节点检查器（审批/人工交活/打回/插话/产物下载）。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft, Ban, CheckCircle2, Download, FileText, MessageSquarePlus,
  Send, ThumbsDown,
} from 'lucide-react'
import { toast } from 'sonner'
import {
  apiError, streamInstanceEvents, taskInstancesApi,
  type InstanceDetail, type NodeRunView,
} from '@/api/taskInstances'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { formatDateTime } from '@/utils/datetime'
import {
  EVENT_LABELS, INSTANCE_STATUS_LABELS, INSTANCE_STATUS_TONE,
  NODE_KIND_LABELS, NODE_STATUS_LABELS, NODE_STATUS_TONE, waitingNodesOf,
} from './instanceView'
import WorkflowPreviewGraph from './WorkflowPreviewGraph'

interface TimelineEntry {
  seq: number
  type: string
  node?: string
  payload: Record<string, unknown>
}

export default function InstanceDetailView() {
  const { instanceId } = useParams<{ instanceId: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [timeline, setTimeline] = useState<TimelineEntry[]>([])
  const [streamError, setStreamError] = useState(false)
  const [humanOutput, setHumanOutput] = useState('{}')
  const [rejectReason, setRejectReason] = useState('')
  const [steeringText, setSteeringText] = useState('')
  const abortRef = useRef<AbortController | null>(null)

  const instance = useQuery({
    queryKey: ['task-instances', 'instance', instanceId],
    enabled: Boolean(instanceId),
    queryFn: () => taskInstancesApi.getInstance(instanceId as string),
  })

  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({
      queryKey: ['task-instances', 'instance', instanceId] })
    void queryClient.invalidateQueries({
      queryKey: ['task-instances', 'instances'] })
  }, [queryClient, instanceId])

  // SSE 事件流：权威快照 + 事件增量；断线重拉快照（KernelRunTaskCard 范式）
  useEffect(() => {
    if (!instanceId || !instance.data) return
    const controller = new AbortController()
    abortRef.current = controller
    let lastSeq = -1
    streamInstanceEvents(instanceId, event => {
      if (event.event === 'instance.snapshot') return
      if (event.id) {
        const seq = Number(event.id.split(':')[1])
        if (Number.isFinite(seq)) lastSeq = Math.max(lastSeq, seq)
      }
      const data = event.data as { node_id?: string; seq?: number }
      setTimeline(previous => [
        ...previous.filter(item => item.seq !== data.seq),
        {
          seq: data.seq ?? previous.length + 1,
          type: event.event,
          node: data.node_id,
          payload: event.data,
        },
      ].sort((a, b) => a.seq - b.seq))
      refresh()
    }, { signal: controller.signal }).catch(() => {
      setStreamError(true)
    })
    return () => controller.abort()
    // 仅在实例 id 与终态变化时重建订阅
  }, [instanceId, instance.data?.status])

  const detail = instance.data
  const waiting = waitingNodesOf(detail)
  const runningNodes = useMemo(
    () => (detail?.nodes ?? []).filter(node =>
      node.status === 'dispatched' || node.status === 'running'),
    [detail])

  const decide = useMutation({
    mutationFn: (input: { approvalId: string; decision: 'approved' | 'rejected'; reason?: string }) =>
      taskInstancesApi.decideApproval(
        instanceId as string, waiting.approval[0]?.node_id ?? '',
        input.approvalId, input.decision, input.reason),
    onSuccess: () => {
      toast.success('审批决定已提交')
      setRejectReason('')
      refresh()
    },
    onError: error => toast.error(apiError(error)),
  })

  const submit = useMutation({
    mutationFn: () => {
      let output: Record<string, unknown>
      try {
        output = JSON.parse(humanOutput) as Record<string, unknown>
      } catch {
        throw new Error('交活产出必须是合法 JSON 对象')
      }
      return taskInstancesApi.humanSubmit(
        instanceId as string, waiting.human[0]?.node_id ?? '', output)
    },
    onSuccess: () => {
      toast.success('已交活')
      setHumanOutput('{}')
      refresh()
    },
    onError: error => toast.error(apiError(error)),
  })

  const reject = useMutation({
    mutationFn: () => taskInstancesApi.humanReject(
      instanceId as string, waiting.human[0]?.node_id ?? '', rejectReason),
    onSuccess: () => {
      toast.success('已打回上游，等待重做')
      setRejectReason('')
      refresh()
    },
    onError: error => toast.error(apiError(error)),
  })

  const steer = useMutation({
    mutationFn: () => taskInstancesApi.submitSteering(
      instanceId as string, runningNodes[0]?.node_id ?? '', steeringText,
      `steer-${Date.now()}`),
    onSuccess: () => {
      toast.success('插话已发送至执行中节点')
      setSteeringText('')
    },
    onError: error => toast.error(apiError(error)),
  })

  if (instance.isLoading) {
    return <div className="py-16 text-center text-sm text-[var(--color-text-tertiary)]">加载中…</div>
  }
  if (!detail) {
    return <div className="py-16 text-center text-sm text-[var(--color-text-tertiary)]">实例不存在</div>
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="ghost" size="sm" onClick={() => navigate('/task-instances')} className="gap-1.5">
          <ArrowLeft className="h-4 w-4" />返回
        </Button>
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-[var(--color-text-primary)]">{detail.name}</span>
          <span className={`rounded px-1.5 py-0.5 text-[11px] ${INSTANCE_STATUS_TONE[detail.status] ?? ''}`}>
            {INSTANCE_STATUS_LABELS[detail.status] ?? detail.status}
          </span>
          {streamError ? (
            <span className="text-[11px] text-[var(--color-text-tertiary)]">实时通道断开，轮询兜底中</span>
          ) : null}
        </div>
        <span className="text-xs text-[var(--color-text-tertiary)]">
          创建于 {formatDateTime(detail.created_at)}
        </span>
      </div>

      <Card>
        <CardContent className="space-y-1.5">
          <div className="text-xs text-[var(--color-text-tertiary)]">目标</div>
          <p className="whitespace-pre-wrap text-sm text-[var(--color-text-secondary)]">{detail.goal}</p>
          {detail.fail_reason ? (
            <p className="rounded bg-[var(--color-bg-danger)] px-2 py-1 text-xs text-[var(--color-danger)]">
              失败原因：{detail.fail_reason}
            </p>
          ) : null}
          {detail.cancel_reason ? (
            <p className="rounded bg-[var(--color-bg-hover)] px-2 py-1 text-xs text-[var(--color-text-tertiary)]">
              取消原因：{detail.cancel_reason}
            </p>
          ) : null}
        </CardContent>
      </Card>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_380px]">
        <div className="space-y-4">
          {/* 状态 DAG 快照 */}
          <Card>
            <CardContent className="space-y-2">
              <div className="text-sm font-medium">流程快照</div>
              <WorkflowPreviewGraph spec={detail.spec_snapshot} nodes={detail.nodes} />
            </CardContent>
          </Card>

          {/* 节点表 */}
          <Card>
            <CardContent className="space-y-2">
              <div className="text-sm font-medium">节点尝试</div>
              <div className="space-y-1.5">
                {detail.nodes.map(node => (
                  <NodeRunRow key={node.node_run_id} node={node} />
                ))}
              </div>
            </CardContent>
          </Card>

          {/* 产物 */}
          <Card>
            <CardContent className="space-y-2">
              <div className="text-sm font-medium">产物（{detail.artifacts.length}）</div>
              {detail.artifacts.length ? (
                <div className="space-y-1.5">
                  {detail.artifacts.map(artifact => (
                    <div key={artifact.id} className="flex items-center gap-2 rounded-md border border-border px-2.5 py-1.5 text-xs">
                      <FileText className="h-3.5 w-3.5 text-[var(--color-text-tertiary)]" />
                      <span className="font-medium text-[var(--color-text-primary)]">{artifact.name}</span>
                      <span className="text-[var(--color-text-tertiary)]">{artifact.mime_type} · {artifact.size_bytes}B</span>
                      <span className="ml-auto font-mono text-[10px] text-[var(--color-text-tertiary)]">
                        {artifact.sha256.slice(0, 12)}…
                      </span>
                      <Button variant="ghost" size="sm" aria-label={`下载 ${artifact.name}`} className="h-6 w-6 p-0">
                        <Download className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-xs text-[var(--color-text-tertiary)]">暂无产物</p>
              )}
            </CardContent>
          </Card>
        </div>

        {/* 右列：交互面板 */}
        <div className="space-y-4">
          <InteractionPanel
            detail={detail}
            waiting={waiting}
            runningCount={runningNodes.length}
            humanOutput={humanOutput} setHumanOutput={setHumanOutput}
            rejectReason={rejectReason} setRejectReason={setRejectReason}
            steeringText={steeringText} setSteeringText={setSteeringText}
            onDecide={(approvalId, decision, reason) =>
              decide.mutate({ approvalId, decision, reason })}
            onSubmit={() => submit.mutate()}
            onReject={() => reject.mutate()}
            onSteer={() => steer.mutate()}
          />

          {/* 事件时间线 */}
          <Card>
            <CardContent className="space-y-2">
              <div className="text-sm font-medium">事件时间线</div>
              <div className="max-h-96 space-y-1 overflow-auto">
                {timeline.length ? timeline.map(entry => (
                  <div key={entry.seq} className="flex items-center gap-2 text-xs">
                    <span className="w-8 shrink-0 font-mono text-[10px] text-[var(--color-text-tertiary)]">
                      #{entry.seq}
                    </span>
                    <span className={
                      entry.type === 'contract.violated' || entry.type === 'node.failed'
                        ? 'text-[var(--color-danger)]'
                        : entry.type === 'instance.terminal'
                          ? 'text-[var(--color-success)]'
                          : 'text-[var(--color-text-secondary)]'
                    }>
                      {EVENT_LABELS[entry.type] ?? entry.type}
                      {entry.node ? ` · ${entry.node}` : ''}
                    </span>
                  </div>
                )) : (
                  <p className="text-xs text-[var(--color-text-tertiary)]">等待实时事件…</p>
                )}
              </div>
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  )
}

function NodeRunRow({ node }: { node: NodeRunView }) {
  return (
    <div className="flex items-center gap-2 rounded-md border border-border px-2.5 py-1.5 text-xs">
      <span className="font-medium text-[var(--color-text-primary)]">{node.node_id}</span>
      <span className="text-[var(--color-text-tertiary)]">#{node.attempt_no}</span>
      <span className={`rounded px-1.5 py-0.5 text-[11px] ${NODE_STATUS_TONE[node.status] ?? ''}`}>
        {NODE_STATUS_LABELS[node.status] ?? node.status}
      </span>
      {node.correction_count > 0 ? (
        <span className="text-[11px] text-[var(--color-warning)]">纠正×{node.correction_count}</span>
      ) : null}
      {node.rework_count > 0 ? (
        <span className="text-[11px] text-[var(--color-warning)]">重做×{node.rework_count}</span>
      ) : null}
      {node.error ? (
        <span className="truncate text-[11px] text-[var(--color-danger)]" title={node.error}>
          {node.error.slice(0, 60)}
        </span>
      ) : null}
      <span className="ml-auto text-[11px] text-[var(--color-text-tertiary)]">
        {formatDateTime(node.finished_at ?? node.created_at)}
      </span>
    </div>
  )
}

function InteractionPanel(props: {
  detail: InstanceDetail
  waiting: { human: NodeRunView[]; approval: NodeRunView[] }
  runningCount: number
  humanOutput: string
  setHumanOutput: (value: string) => void
  rejectReason: string
  setRejectReason: (value: string) => void
  steeringText: string
  setSteeringText: (value: string) => void
  onDecide: (approvalId: string, decision: 'approved' | 'rejected', reason?: string) => void
  onSubmit: () => void
  onReject: () => void
  onSteer: () => void
}) {
  const { detail, waiting } = props
  const approvalNode = waiting.approval[0]
  const approval = approvalNode
    ? detail.approvals.find(item => item.node_run_id === approvalNode.node_run_id
      && item.status === 'pending')
    : undefined
  const humanNode = waiting.human[0]
  const isTerminal = ['completed', 'failed', 'cancelled'].includes(detail.status)

  if (isTerminal && !approvalNode && !humanNode) {
    return (
      <Card>
        <CardContent className="flex items-center gap-2 text-sm text-[var(--color-text-tertiary)]">
          <Ban className="h-4 w-4" />实例已结束，交互面板关闭
        </CardContent>
      </Card>
    )
  }

  return (
    <Card>
      <CardContent className="space-y-4">
        <div className="text-sm font-medium">节点检查器</div>

        {/* 审批 */}
        {approvalNode && approval ? (
          <div className="space-y-2 rounded-md border border-border p-2.5">
            <div className="flex items-center gap-2 text-xs">
              <CheckCircle2 className="h-4 w-4 text-[var(--color-warning)]" />
              <span className="font-medium text-[var(--color-text-primary)]">
                审批关口 {approvalNode.node_id}
              </span>
            </div>
            <pre className="max-h-32 overflow-auto rounded bg-[var(--color-bg-hover)] p-2 font-mono text-[10px] text-[var(--color-text-secondary)]">
              {JSON.stringify(approval.proposal, null, 2)}
            </pre>
            <Input
              aria-label="驳回理由（驳回时必填）"
              placeholder="驳回理由（驳回时必填，将随打回送达上游）"
              value={props.rejectReason}
              onChange={event => props.setRejectReason(event.target.value)}
            />
            <div className="flex gap-2">
              <Button size="sm" className="flex-1 gap-1.5"
                onClick={() => props.onDecide(approval.id, 'approved')}>
                <CheckCircle2 className="h-4 w-4" />批准
              </Button>
              <Button size="sm" variant="outline" className="flex-1 gap-1.5 text-[var(--color-danger)]"
                onClick={() => props.onDecide(approval.id, 'rejected', props.rejectReason)}>
                <ThumbsDown className="h-4 w-4" />驳回并打回
              </Button>
            </div>
          </div>
        ) : null}

        {/* 人工交活 / 打回 */}
        {humanNode ? (
          <div className="space-y-2 rounded-md border border-border p-2.5">
            <div className="text-xs font-medium text-[var(--color-text-primary)]">
              人工待办 {humanNode.node_id}
              <span className="ml-1.5 font-normal text-[var(--color-text-tertiary)]">
                {NODE_KIND_LABELS.human} · 尝试#{humanNode.attempt_no}
              </span>
            </div>
            <textarea
              aria-label="交活产出 JSON"
              className="min-h-20 w-full rounded-lg border border-border bg-background px-3 py-2 font-mono text-xs"
              value={props.humanOutput}
              onChange={event => props.setHumanOutput(event.target.value)}
              placeholder='交活产出（JSON 对象，须满足端口契约）'
            />
            <Input
              aria-label="打回理由"
              placeholder="打回直接上游的理由（可选操作）"
              value={props.rejectReason}
              onChange={event => props.setRejectReason(event.target.value)}
            />
            <div className="flex gap-2">
              <Button size="sm" className="flex-1 gap-1.5" onClick={props.onSubmit}>
                <Send className="h-4 w-4" />交活
              </Button>
              <Button size="sm" variant="outline" className="flex-1 gap-1.5 text-[var(--color-danger)]"
                onClick={props.onReject}>
                <ThumbsDown className="h-4 w-4" />打回上游
              </Button>
            </div>
          </div>
        ) : null}

        {/* 插话 */}
        <div className="space-y-2 rounded-md border border-border p-2.5">
          <div className="flex items-center gap-2 text-xs font-medium text-[var(--color-text-primary)]">
            <MessageSquarePlus className="h-4 w-4 text-[var(--color-text-tertiary)]" />
            中途插话
            <span className="font-normal text-[var(--color-text-tertiary)]">
              （{props.runningCount} 个节点执行中；送达运行中的会话）
            </span>
          </div>
          <textarea
            aria-label="插话内容"
            className="min-h-16 w-full rounded-lg border border-border bg-background px-3 py-2 text-xs"
            value={props.steeringText}
            onChange={event => props.setSteeringText(event.target.value)}
            placeholder="补充要求、纠偏提示…将注入正在执行的节点"
            disabled={props.runningCount === 0}
          />
          <Button size="sm" className="w-full gap-1.5"
            disabled={props.runningCount === 0 || !props.steeringText.trim()}
            onClick={props.onSteer}>
            <Send className="h-4 w-4" />发送插话
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}
