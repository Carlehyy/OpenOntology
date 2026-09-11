/**
 * 授权边界配置 — 管理员划定 agent 的世界
 *
 * 三张白名单（对象类型 / 链接类型 / 动作）+ 配额 + 附加指令。
 * 白名单三态：null=全部允许，[]=全部拒绝，[id...]=仅勾选项。
 * 动作默认全部拒绝（读开放、写授权），与后端安全默认一致。
 */
import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { X, Shield, Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { agentApi, type AgentProfile } from '@/api/agent'
import { apiClientV2 } from '@/api/client'

interface Item { id: string; displayName: string; hint?: string }

function WhitelistSection({ title, items, value, onChange, emptyHint }: {
  title: string
  items: Item[]
  value: string[] | null            // null = 全部允许
  onChange: (v: string[] | null) => void
  emptyHint?: string
}) {
  const allowAll = value === null
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <h4 className="text-xs font-semibold text-[var(--color-text-primary)]">{title}</h4>
        <label className="flex items-center gap-1.5 text-xs text-[var(--color-text-secondary)] cursor-pointer">
          <input type="checkbox" checked={allowAll}
                 onChange={e => onChange(e.target.checked ? null : items.map(i => i.id))} />
          全部允许
        </label>
      </div>
      {items.length === 0 && (
        <p className="text-[11px] text-[var(--color-text-tertiary)]">{emptyHint || '（本体中还没有此类定义）'}</p>
      )}
      {!allowAll && (
        <div className="flex flex-wrap gap-1.5">
          {items.map(item => {
            const checked = (value || []).includes(item.id)
            return (
              <button key={item.id}
                onClick={() => onChange(checked ? (value || []).filter(x => x !== item.id) : [...(value || []), item.id])}
                className={`px-2 py-1 rounded-md text-xs border transition-colors ${checked
                  ? 'border-[var(--color-primary)] bg-[var(--color-primary-light)] text-[var(--color-primary)]'
                  : 'border-[var(--color-border)] text-[var(--color-text-tertiary)] hover:border-[var(--color-primary)]'}`}>
                {item.displayName}{item.hint ? ` · ${item.hint}` : ''}
              </button>
            )
          })}
        </div>
      )}
      {allowAll && items.length > 0 && (
        <p className="text-[11px] text-[var(--color-text-tertiary)]">{items.length} 项全部对智能体可见</p>
      )}
    </div>
  )
}

export function BoundaryDrawer({ oid, open, onClose }: {
  oid: string; open: boolean; onClose: () => void
}) {
  const queryClient = useQueryClient()
  const { data: profile } = useQuery<AgentProfile>({
    queryKey: ['agent-profile', oid],
    queryFn: () => agentApi.getProfile(oid),
    enabled: open && !!oid,
  })
  const { data: full } = useQuery<any>({
    queryKey: ['agent-full-ontology', oid],
    queryFn: () => apiClientV2.get(`/formal/ontologies/${oid}/full`),
    enabled: open && !!oid,
  })

  const [enabled, setEnabled] = useState(true)
  const [objectTypes, setObjectTypes] = useState<string[] | null>(null)
  const [linkTypes, setLinkTypes] = useState<string[] | null>(null)
  const [actions, setActions] = useState<string[] | null>([])
  const [allowProposals, setAllowProposals] = useState(true)
  const [maxRows, setMaxRows] = useState(50)
  const [maxSteps, setMaxSteps] = useState(8)
  const [extra, setExtra] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!profile) return
    setEnabled(profile.enabled)
    setObjectTypes(profile.allowedObjectTypeIds)
    setLinkTypes(profile.allowedLinkTypeIds)
    setActions(profile.allowedActionIds)
    setAllowProposals(profile.allowActionProposals)
    setMaxRows(profile.maxRowsPerQuery)
    setMaxSteps(profile.maxSteps)
    setExtra(profile.systemPromptExtra || '')
  }, [profile])

  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose, open])

  if (!open) return null

  const save = async () => {
    setSaving(true)
    setError('')
    try {
      const resetToAll: string[] = []
      if (objectTypes === null) resetToAll.push('allowed_object_type_ids')
      if (linkTypes === null) resetToAll.push('allowed_link_type_ids')
      if (actions === null) resetToAll.push('allowed_action_ids')
      await agentApi.updateProfile(oid, {
        enabled,
        ...(objectTypes !== null ? { allowedObjectTypeIds: objectTypes } : {}),
        ...(linkTypes !== null ? { allowedLinkTypeIds: linkTypes } : {}),
        ...(actions !== null ? { allowedActionIds: actions } : {}),
        allowActionProposals: allowProposals,
        maxRowsPerQuery: maxRows,
        maxSteps,
        systemPromptExtra: extra,
        resetToAll,
      })
      await queryClient.invalidateQueries({ queryKey: ['agent-profile', oid] })
      await queryClient.invalidateQueries({ queryKey: ['agent-capabilities', oid] })
      onClose()
    } catch (e: any) {
      setError(e?.detail || e?.message || '保存失败（需要管理员权限）')
    } finally {
      setSaving(false)
    }
  }

  const ots: Item[] = (full?.objectTypes || []).map((t: any) => ({ id: t.id, displayName: t.displayName }))
  const lts: Item[] = (full?.linkTypes || []).map((t: any) => ({ id: t.id, displayName: t.displayName }))
  const acts: Item[] = (full?.actions || []).map((a: any) => ({
    id: a.id, displayName: a.displayName, hint: a.requiresApproval ? '需审批' : undefined,
  }))

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-accent px-4 pt-[7vh] backdrop-blur-[1px]" onMouseDown={onClose}>
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby="agent-boundary-title"
        data-testid="agent-boundary-dialog"
        className="animate-slide-up relative flex max-h-[86dvh] w-[720px] max-w-[94vw] flex-col overflow-hidden rounded-xl border border-border bg-[var(--color-bg-elevated)] shadow-[0_24px_80px_rgba(15,23,42,0.22)]"
        onMouseDown={event => event.stopPropagation()}
      >
        <div className="flex shrink-0 items-center justify-between border-b border-[var(--color-border)] px-4 py-3">
          <div className="flex items-center gap-2">
            <Shield size={15} className="text-[var(--color-primary)]" />
            <h3 id="agent-boundary-title" className="text-sm font-semibold text-[var(--color-text-primary)]">智能体授权边界</h3>
          </div>
          <button onClick={onClose} aria-label="关闭授权边界配置" className="rounded-md p-1.5 text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            <X size={16} />
          </button>
        </div>

        <div data-testid="agent-boundary-body" className="min-h-0 space-y-5 overflow-y-auto p-4">
          <p className="text-[11px] leading-relaxed text-[var(--color-text-tertiary)] bg-[var(--color-bg-base)] rounded-md p-2.5 border border-[var(--color-border)]">
            智能体不访问底层数据库、不扫描数据集 schema——它的全部世界就是这里授权的对象、链接与动作。
            读默认开放、写默认拒绝；动作即使授权，真实执行也必须由用户确认，需审批的动作还会进入人工审批队列。
          </p>

          <label className="flex items-center justify-between text-sm text-[var(--color-text-primary)] cursor-pointer">
            <span>启用智能体</span>
            <input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />
          </label>

          <WhitelistSection title="可见对象类型" items={ots} value={objectTypes} onChange={setObjectTypes} />
          <WhitelistSection title="可见链接类型（两端类型都可见时才生效）" items={lts} value={linkTypes} onChange={setLinkTypes} />
          <WhitelistSection title="可提案的动作" items={acts} value={actions} onChange={setActions}
                            emptyHint="（本体中还没有定义动作 — 智能体只能查询和分析）" />

          <label className="flex items-center justify-between text-sm text-[var(--color-text-primary)] cursor-pointer">
            <span>允许提出动作提案（dry-run 预演）</span>
            <input type="checkbox" checked={allowProposals} onChange={e => setAllowProposals(e.target.checked)} />
          </label>

          <div className="grid grid-cols-2 gap-3">
            <label className="text-xs text-[var(--color-text-secondary)] space-y-1">
              <span>单次查询最大行数</span>
              <input type="number" min={1} max={500} value={maxRows}
                     onChange={e => setMaxRows(Math.max(1, Number(e.target.value) || 50))}
                     className="w-full px-2 py-1.5 text-sm border border-[var(--color-border)] rounded-md bg-[var(--color-bg-base)]" />
            </label>
            <label className="text-xs text-[var(--color-text-secondary)] space-y-1">
              <span>单回合最大推理步数</span>
              <input type="number" min={1} max={30} value={maxSteps}
                     onChange={e => setMaxSteps(Math.max(1, Number(e.target.value) || 8))}
                     className="w-full px-2 py-1.5 text-sm border border-[var(--color-border)] rounded-md bg-[var(--color-bg-base)]" />
            </label>
          </div>

          <label className="text-xs text-[var(--color-text-secondary)] space-y-1 block">
            <span>附加指令（追加到系统提示，如业务口径、回答风格）</span>
            <textarea rows={3} value={extra} onChange={e => setExtra(e.target.value)} data-testid="agent-boundary-extra"
                      className="w-full px-2 py-1.5 text-sm border border-[var(--color-border)] rounded-md bg-[var(--color-bg-base)] resize-none" />
          </label>

          {error && <Badge variant="danger">{error}</Badge>}
        </div>

        <div data-testid="agent-boundary-footer" className="flex shrink-0 justify-center gap-3 border-t border-[var(--color-border)] px-4 py-3">
          <Button variant="outline" size="sm" onClick={onClose} className="min-w-24 border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] text-[var(--color-success)] hover:bg-[var(--color-success-bg)]">取消</Button>
          <Button variant="success" size="sm" onClick={save} disabled={saving} className="min-w-24 bg-[var(--color-success)] hover:bg-[var(--color-success)]">
            {saving && <Loader2 size={12} className="animate-spin" />}保存边界
          </Button>
        </div>
      </section>
    </div>
  )
}
