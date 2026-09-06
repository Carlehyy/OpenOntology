import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import {
  BellRing, CheckCircle2, Loader2, ShieldCheck, XCircle,
} from 'lucide-react'
import { agentApi, type AgentSentinelProposal } from '@/api/agent'

const operationLabel = {
  create: '创建', update: '更新', enable: '启用', disable: '停用', delete: '删除',
} as const

const completionText = {
  create: '创建完成；新哨兵默认停用，请在动态哨兵面板完成全量试跑后启用。',
  update: '更新完成；该哨兵已自动停用，需重新全量试跑后才能启用。',
  enable: '启用完成；后续触发将进入与公共哨兵相同的执行引擎。',
  disable: '停用完成。',
  delete: '删除完成；既有执行审计记录仍会保留。',
} as const

export function SentinelProposalCard({ oid, proposal }: {
  oid: string
  proposal: AgentSentinelProposal
}) {
  const queryClient = useQueryClient()
  const [executing, setExecuting] = useState(false)
  const [done, setDone] = useState(false)
  const [error, setError] = useState('')
  const valid = proposal.status === 'success'

  const execute = async () => {
    setExecuting(true)
    setError('')
    try {
      await agentApi.executeDynamicSentinelProposal(oid, proposal)
      setDone(true)
      await queryClient.invalidateQueries({ queryKey: ['agent-dynamic-sentinels', oid] })
    } catch (requestError: any) {
      const detail = requestError?.detail
      const validationErrors = Array.isArray(detail?.errors)
        ? detail.errors.map((item: any) => item?.message || item?.msg || String(item))
        : []
      setError(typeof detail === 'string' ? detail : [
        detail?.message || requestError?.message || '动态哨兵操作失败',
        ...validationErrors,
      ].filter(Boolean).join('；'))
    } finally {
      setExecuting(false)
    }
  }

  return (
    <div className="mt-3 overflow-hidden rounded-xl border border-brand-line bg-gradient-to-b from-brand-soft to-transparent">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-brand-line px-4 py-2.5">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-lg bg-brand-soft text-brand-ink">
            <BellRing size={12} />
          </span>
          <span className="truncate text-sm font-semibold text-[var(--color-text-primary)]">
            {operationLabel[proposal.operation]} · {proposal.sentinelName}
          </span>
          <span className={`rounded-md px-1.5 py-0.5 text-[10px] font-medium ${valid
            ? 'bg-[var(--color-success-bg)] text-[var(--color-success)]' : 'bg-[var(--color-danger-bg)] text-[var(--color-danger)]'}`}>
            强校验{valid ? '通过' : '未通过'}
          </span>
        </div>
        {!done && (
          <button
            type="button"
            onClick={execute}
            disabled={!valid || executing}
            className="inline-flex h-7 shrink-0 items-center gap-1.5 rounded-lg bg-brand px-3 text-xs font-medium text-[var(--color-text-inverse)] transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-30"
          >
            {executing ? <Loader2 size={11} className="animate-spin" /> : <ShieldCheck size={11} />}
            确认{operationLabel[proposal.operation]}
          </button>
        )}
      </div>
      <div className="space-y-2 px-4 py-3 text-xs">
        {proposal.definition && (
          <div className="grid grid-cols-[72px_1fr] gap-x-3 gap-y-1 text-[var(--color-text-secondary)]">
            <span className="text-[var(--color-text-tertiary)]">技术名称</span>
            <span className="break-all">{proposal.definition.name}</span>
            <span className="text-[var(--color-text-tertiary)]">监听对象</span>
            <span>{proposal.definition.bindings.map(item => item.alias).join('、')}</span>
            {(proposal.definition as any).triggerMode === 'on_pattern' && (proposal.definition as any).pattern && (
              <>
                <span className="text-[var(--color-text-tertiary)]">事件模式</span>
                <span className="break-all">
                  {((proposal.definition as any).pattern.stages || [])
                    .map((stage: any) => stage.alias).join(' → ')}
                  {((proposal.definition as any).pattern.absence || {}).enabled ? '（含缺失分支）' : ''}
                  {(proposal.definition as any).pattern.aggregate
                    ? `（聚合 ${(proposal.definition as any).pattern.aggregate.function}）`
                    : ''}
                </span>
              </>
            )}
            <span className="text-[var(--color-text-tertiary)]">触发动作</span>
            <span>{proposal.definition.actionIds.length} 个</span>
          </div>
        )}
        {proposal.validationErrors.length > 0 && (
          <div className="space-y-0.5 text-[var(--color-danger)]">
            {proposal.validationErrors.map((item, index) => <div key={index}>· {item}</div>)}
          </div>
        )}
        {done && (
          <div className="flex items-center gap-1.5 rounded-lg bg-[var(--color-success-bg)] px-3 py-2 font-medium text-[var(--color-success)]">
            <CheckCircle2 size={13} />{completionText[proposal.operation]}
          </div>
        )}
        {error && (
          <div className="flex items-center gap-1.5 rounded-lg bg-[var(--color-danger-bg)] px-3 py-2 font-medium text-[var(--color-danger)]">
            <XCircle size={13} />{error}
          </div>
        )}
      </div>
    </div>
  )
}
