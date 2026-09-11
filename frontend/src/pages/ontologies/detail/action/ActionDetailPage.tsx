import { formatDateTime } from '@/utils/datetime'
import { useState } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useForm } from 'react-hook-form'
import { Button } from '@/components/ui/Button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ontologyApi } from '@/api/ontologies'
import ConfidenceBar from '@/components/ConfidenceBar'
import { ArrowLeft, Pencil, Trash2, Save, X, Plus, Check } from 'lucide-react'
import type { Action, Entity, LogicRule } from '@/types/ontology'

function ChipEditor({
  editing, items, onRemove, availableOptions, onAdd, color,
}: {
  editing: boolean
  items: { id: string; label: string; href: string }[]
  onRemove: (id: string) => void
  availableOptions: { id: string; label: string }[]
  onAdd: (id: string) => void
  color: 'blue' | 'orange' | 'purple'
}) {
  const [addId, setAddId] = useState('')
  const cls = {
    blue:   { chip: 'bg-[var(--color-info-bg)] text-[var(--color-info)] border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] hover:bg-[var(--color-info-bg)]', del: 'text-[var(--color-info)] hover:text-[var(--color-info)]' },
    orange: { chip: 'bg-viz-orange-soft text-viz-orange border-viz-orange-soft hover:bg-viz-orange-soft', del: 'text-viz-orange hover:text-viz-orange' },
    purple: { chip: 'bg-viz-violet-soft text-viz-violet border-viz-violet-soft hover:bg-viz-violet-soft', del: 'text-viz-violet hover:text-viz-violet' },
  }[color]

  if (!editing) {
    if (items.length === 0) return <p className="text-sm text-[var(--color-text-tertiary)]">暂无</p>
    return (
      <div className="flex flex-wrap gap-2">
        {items.map(item => (
          <Link key={item.id} to={item.href}
            className={`px-3 py-1.5 rounded-full text-xs border ${cls.chip}`}>
            {item.label}
          </Link>
        ))}
      </div>
    )
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        {items.map(item => (
          <span key={item.id} className={`flex items-center gap-1 px-2.5 py-1 rounded-full text-xs border ${cls.chip}`}>
            {item.label}
            <button onClick={() => onRemove(item.id)} className={`${cls.del} ml-0.5`}>
              <X size={10} />
            </button>
          </span>
        ))}
      </div>
      {availableOptions.length > 0 && (
        <div className="flex items-center gap-2">
          <Select value={addId || '__none__'} onValueChange={value => setAddId(value === '__none__' ? '' : value)}>
            <SelectTrigger className="h-8 flex-1 rounded-lg px-2 text-xs" aria-label="选择添加">
              <SelectValue placeholder="— 选择添加 —" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="__none__">— 选择添加 —</SelectItem>
              {availableOptions.map(o => (
                <SelectItem key={o.id} value={o.id}>{o.label}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button size="sm" disabled={!addId} onClick={() => { if (addId) { onAdd(addId); setAddId('') } }}>
            <Plus size={12} /> 添加
          </Button>
        </div>
      )}
    </div>
  )
}

export default function ActionDetailPage() {
  const { id: oid, aid } = useParams<{ id: string; aid: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)
  const [entitiesEditing, setEntitiesEditing] = useState(false)
  const [logicEditing, setLogicEditing] = useState(false)
  const { register, handleSubmit, reset } = useForm<Partial<Action>>()

  const { data: action, isLoading } = useQuery({
    queryKey: ['action', oid, aid],
    queryFn: () => ontologyApi.listActions(oid!).then((list: any) => {
      const found = (list as Action[]).find(a => a.id === aid)
      if (!found) throw new Error('Action not found')
      return found
    }),
    enabled: !!oid && !!aid,
  })

  const { data: allEntities = [] } = useQuery({
    queryKey: ['entities', oid],
    queryFn: () => ontologyApi.listEntities(oid!) as any,
    enabled: !!oid,
  })

  const { data: allLogic = [] } = useQuery({
    queryKey: ['logic', oid],
    queryFn: () => ontologyApi.listLogic(oid!) as any,
    enabled: !!oid,
  })

  const updateMut = useMutation({
    mutationFn: (data: Partial<Action>) => ontologyApi.updateAction(oid!, aid!, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['action', oid, aid] })
      qc.invalidateQueries({ queryKey: ['actions', oid] })
      setEditing(false)
    },
  })

  const deleteMut = useMutation({
    mutationFn: () => ontologyApi.deleteAction(oid!, aid!),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['actions', oid] })
      qc.invalidateQueries({ queryKey: ['stats'] })
      navigate(`/ontologies/${oid}?tab=actions`)
    },
  })

  const onSubmit = (data: Partial<Action>) => updateMut.mutate(data)

  const startEdit = () => {
    if (action) reset(action)
    setEditing(true)
  }

  if (isLoading) return <div className="p-6 text-[var(--color-text-tertiary)]">加载中...</div>
  if (!action) return <div className="p-6 text-[var(--color-danger)]">动作未找到</div>

  // linked_entities 兼容历史显示名及当前实体类型名。
  const linkedKeys = new Set(action.linked_entities ?? [])
  const entityHit = (e: Entity) =>
    linkedKeys.has(e.name_cn) || (e.type ? linkedKeys.has(e.type) : false) || (e.name_en ? linkedKeys.has(e.name_en) : false)
  const relatedEntities = (allEntities as Entity[]).filter(entityHit)
  const unlinkedEntities = (allEntities as Entity[]).filter(e => !entityHit(e))

  // 关联逻辑: 显式 linked_logic_ids, 或与本动作共享 linked_entities(同一实体类)
  const linkedLogicIds = new Set(action.linked_logic_ids ?? [])
  const logicHit = (r: LogicRule) =>
    linkedLogicIds.has(r.id) || (r.linked_entities ?? []).some(x => linkedKeys.has(x))
  const relatedLogic = (allLogic as LogicRule[]).filter(logicHit)
  const unlinkedLogic = (allLogic as LogicRule[]).filter(r => !logicHit(r))

  // Entity link helpers
  const removeEntity = (entityId: string) => {
    const entity = (allEntities as Entity[]).find(e => e.id === entityId)
    if (!entity) return
    const next = (action.linked_entities ?? []).filter(n => n !== entity.name_cn && n !== entity.name_en)
    updateMut.mutate({ linked_entities: next } as any)
  }
  const addEntity = (entityId: string) => {
    const entity = (allEntities as Entity[]).find(e => e.id === entityId)
    if (!entity) return
    const next = [...(action.linked_entities ?? []), entity.name_cn]
    updateMut.mutate({ linked_entities: next } as any)
  }

  // Logic link helpers
  const removeLogic = (logicId: string) => {
    const next = (action.linked_logic_ids ?? []).filter(i => i !== logicId)
    updateMut.mutate({ linked_logic_ids: next } as any)
  }
  const addLogic = (logicId: string) => {
    const next = [...(action.linked_logic_ids ?? []), logicId]
    updateMut.mutate({ linked_logic_ids: next } as any)
  }

  const formatDate = (s: string) => formatDateTime(s)

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <button onClick={() => navigate(`/ontologies/${oid}?tab=actions`)}
          className="flex items-center gap-2 text-muted-foreground hover:text-foreground text-sm w-9 h-9 rounded-lg hover:bg-muted justify-center" title="返回动作列表">
          <ArrowLeft size={18} />
        </button>
        <div className="flex items-center gap-2">
          {editing ? (
            <>
              <button onClick={() => setEditing(false)}
                className="flex items-center gap-1.5 px-3 py-1.5 border rounded-lg text-sm text-muted-foreground hover:bg-muted">
                <X size={14} /> 取消
              </button>
              <button onClick={handleSubmit(onSubmit)}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-[var(--color-bg-overlay)] text-[var(--color-text-inverse)] rounded-lg text-sm">
                <Save size={14} /> 保存
              </button>
            </>
          ) : (
            <>
              <button onClick={startEdit}
                className="flex items-center gap-1.5 px-3 py-1.5 border rounded-lg text-sm text-muted-foreground hover:bg-muted">
                <Pencil size={14} /> 编辑
              </button>
              <button onClick={() => setShowDeleteConfirm(true)}
                className="flex items-center gap-1.5 px-3 py-1.5 border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] text-[var(--color-danger)] rounded-lg text-sm hover:bg-[var(--color-danger-bg)]">
                <Trash2 size={14} /> 删除
              </button>
            </>
          )}
        </div>
      </div>

      {/* Action Info Card */}
      <div className="bg-card border rounded-xl p-6">
        <h3 className="font-semibold mb-4">动作信息</h3>
        {editing ? (
          <form className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-xs text-muted-foreground mb-1">中文名 *</label>
                <input {...register('name_cn', { required: true })} className="w-full border rounded-lg px-3 py-2 text-sm" />
              </div>
              <div>
                <label className="block text-xs text-muted-foreground mb-1">英文名</label>
                <input {...register('name_en')} className="w-full border rounded-lg px-3 py-2 text-sm" />
              </div>
              <div>
                <label className="block text-xs text-muted-foreground mb-1">置信度 (0-1)</label>
                <input {...register('confidence', { valueAsNumber: true })} type="number" step="0.01" min="0" max="1" className="w-full border rounded-lg px-3 py-2 text-sm" />
              </div>
            </div>
            <div>
              <label className="block text-xs text-muted-foreground mb-1">描述</label>
              <textarea {...register('description')} rows={2} className="w-full border rounded-lg px-3 py-2 text-sm resize-none" />
            </div>
            <div>
              <label className="block text-xs text-muted-foreground mb-1">执行规则</label>
              <textarea {...register('execution_rule')} rows={2} className="w-full border rounded-lg px-3 py-2 text-sm resize-none" />
            </div>
            <div>
              <label className="block text-xs text-muted-foreground mb-1">函数代码</label>
              <textarea {...register('function_code')} rows={6} className="w-full border rounded-lg px-3 py-2 text-sm font-mono resize-none" />
            </div>
          </form>
        ) : (
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <div>
                <p className="text-xs text-muted-foreground mb-1">中文名</p>
                <p className="text-sm font-medium">{action.name_cn}</p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground mb-1">英文名</p>
                <p className="text-sm">{action.name_en || '—'}</p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground mb-1">版本</p>
                <p className="text-sm font-mono">{action.version}</p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground mb-1">置信度</p>
                <div className="flex items-center gap-3">
                  <div className="w-32"><ConfidenceBar value={action.confidence} /></div>
                  <span className="text-sm text-muted-foreground">{Math.round(action.confidence * 100)}%</span>
                </div>
              </div>
            </div>
            <div>
              <p className="text-xs text-muted-foreground mb-1">描述</p>
              <p className="text-sm text-foreground">{action.description || '—'}</p>
            </div>
            {action.execution_rule && (
              <div>
                <p className="text-xs text-muted-foreground mb-1">执行规则</p>
                <p className="text-sm text-foreground whitespace-pre-wrap">{action.execution_rule}</p>
              </div>
            )}
            {action.function_code && (
              <div>
                <p className="text-xs text-muted-foreground mb-1">函数代码</p>
                <div className="bg-accent rounded-lg p-4 font-mono text-xs text-[var(--color-success)] whitespace-pre-wrap overflow-x-auto">{action.function_code}</div>
              </div>
            )}
            <div className="grid grid-cols-2 gap-4 pt-2 border-t">
              <div>
                <p className="text-xs text-muted-foreground mb-1">创建时间</p>
                <p className="text-xs text-muted-foreground">{formatDate(action.created_at)}</p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground mb-1">更新时间</p>
                <p className="text-xs text-muted-foreground">{formatDate(action.updated_at)}</p>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Related Entities — inline link management */}
      <div className="bg-card border rounded-xl p-6">
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-semibold">关联实体</h3>
          <button onClick={() => setEntitiesEditing(v => !v)}
            className={`flex items-center gap-1 px-2.5 py-1 rounded-lg text-xs border ${entitiesEditing ? 'bg-[var(--color-bg-overlay)] text-[var(--color-text-inverse)] border-border' : 'text-muted-foreground hover:bg-muted'}`}>
            {entitiesEditing ? <><Check size={11} /> 完成</> : <><Pencil size={11} /> 编辑</>}
          </button>
        </div>
        <ChipEditor
          editing={entitiesEditing}
          items={relatedEntities.map(e => ({ id: e.id, label: e.name_cn, href: `/ontologies/${oid}/entities/${e.id}` }))}
          onRemove={removeEntity}
          availableOptions={unlinkedEntities.map(e => ({ id: e.id, label: e.name_cn }))}
          onAdd={addEntity}
          color="blue"
        />
      </div>

      {/* Related Logic Rules — inline link management */}
      <div className="bg-card border rounded-xl p-6">
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-semibold">关联逻辑规则</h3>
          <button onClick={() => setLogicEditing(v => !v)}
            className={`flex items-center gap-1 px-2.5 py-1 rounded-lg text-xs border ${logicEditing ? 'bg-[var(--color-bg-overlay)] text-[var(--color-text-inverse)] border-border' : 'text-muted-foreground hover:bg-muted'}`}>
            {logicEditing ? <><Check size={11} /> 完成</> : <><Pencil size={11} /> 编辑</>}
          </button>
        </div>
        <ChipEditor
          editing={logicEditing}
          items={relatedLogic.map(r => ({ id: r.id, label: r.name_cn, href: `/ontologies/${oid}/logic/${r.id}` }))}
          onRemove={removeLogic}
          availableOptions={unlinkedLogic.map(r => ({ id: r.id, label: r.name_cn }))}
          onAdd={addLogic}
          color="orange"
        />
      </div>

      {/* Delete Confirm Dialog */}
      {showDeleteConfirm && (
        <div className="fixed inset-0 bg-[var(--color-bg-overlay)] flex items-center justify-center z-50">
          <div className="bg-card rounded-xl shadow-lg p-6 w-80">
            <h3 className="font-semibold mb-2">确认删除</h3>
            <p className="text-sm text-muted-foreground mb-4">确定要删除动作「{action.name_cn}」吗？此操作不可撤销。</p>
            <div className="flex justify-end gap-3">
              <button onClick={() => setShowDeleteConfirm(false)}
                className="px-4 py-2 border rounded-lg text-sm">取消</button>
              <button onClick={() => deleteMut.mutate()}
                className="px-4 py-2 bg-[var(--color-danger)] text-[var(--color-text-inverse)] rounded-lg text-sm">删除</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
