/**
 * 三维场景新增/编辑共用表单弹窗（基本信息：名称/描述）。
 * 定义内容不在此编辑——保存定义必须走版本冻结通道（详情页/场景助手）。
 */
import { useEffect, useState } from 'react'
import { AlertCircle } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Modal } from '@/components/ui/Modal'

export interface SceneFormValue {
  name: string
  description: string
}

function errorText(error: unknown, fallback: string): string {
  if (!error || typeof error !== 'object') return fallback
  const candidate = error as { detail?: unknown; message?: unknown }
  const detail = candidate.detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object') {
    const d = detail as { message?: unknown }
    if (typeof d.message === 'string') return d.message
  }
  if (typeof candidate.message === 'string') return candidate.message
  return fallback
}

export function SceneFormModal({ open, title, initial, onClose, onSubmit }: {
  open: boolean
  title: string
  initial?: SceneFormValue | null
  onClose: () => void
  onSubmit: (value: SceneFormValue) => Promise<unknown>
}) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (open) {
      setName(initial?.name ?? '')
      setDescription(initial?.description ?? '')
      setError('')
    }
  }, [open, initial])

  const submit = async () => {
    if (!name.trim()) {
      setError('请输入场景名称')
      return
    }
    setSaving(true)
    setError('')
    try {
      await onSubmit({ name: name.trim(), description: description.trim() })
    } catch (submitError) {
      setError(errorText(submitError, title + '失败，请稍后重试'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={() => !saving && onClose()}
      title={title}
      description={initial ? '更新场景的名称与描述，不影响已冻结的版本定义。'
        : '填写基本信息即可创建草稿态场景，场景定义可在建模页或详情页继续完善。'}
      size="lg"
      footer={(
        <>
          <Button variant="ghost" onClick={onClose} disabled={saving}>取消</Button>
          <Button onClick={submit} loading={saving} disabled={!name.trim()}>{initial ? '保存' : '创建'}</Button>
        </>
      )}
    >
      <div className="space-y-5">
        <Input
          label="场景名称"
          required
          autoFocus
          maxLength={120}
          value={name}
          onChange={event => setName(event.target.value)}
          placeholder="例如：供应链园区"
        />
        <div>
          <label className="mb-1.5 block text-sm font-medium text-[var(--color-text-primary)]">场景描述</label>
          <textarea
            value={description}
            onChange={event => setDescription(event.target.value)}
            maxLength={500}
            rows={3}
            placeholder="简要说明场景覆盖的业务范围和用途"
            className="w-full resize-none rounded-md border border-[var(--color-border)] bg-card px-3 py-2 text-sm text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          <p className="mt-1 text-right text-[11px] text-[var(--color-text-tertiary)]">{description.length}/500</p>
        </div>
        {error && (
          <div role="alert" className="flex items-start gap-2.5 rounded-xl border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-3.5 py-3 text-sm text-[var(--color-danger)]">
            <AlertCircle size={16} className="mt-0.5 shrink-0" />
            <span className="leading-5">{error}</span>
          </div>
        )}
      </div>
    </Modal>
  )
}
