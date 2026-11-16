/**
 * 新建 / 编辑看板信息弹窗（创建与编辑共用）。
 *
 * - 冻结契约 §4/§7：名称 trim 后 1..200、描述 trim 后 ≤500、图标必选且属于
 *   后端白名单；错误必须在弹窗内以 role="alert" 展示并保留已填内容，
 *   不能只靠 toast；
 * - 编辑总是携带列表 revision（expected_revision）；409 冲突时保留输入、
 *   展示最新 revision，并提供「刷新最新数据」后再次保存的可恢复路径；
 * - 编辑文案明确「仅影响平台卡片，看板内的标题、数据源与面板不会被改动」。
 */
import { useState, type FormEvent } from 'react'
import { AlertCircle } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  dashboardErrorCode,
  dashboardErrorRevision,
  operationsApiErrorText,
  type OperationsDashboardCard,
} from '@/api/v2/operations-dashboards'
import {
  DASHBOARD_DESCRIPTION_MAX_LENGTH,
  DASHBOARD_NAME_MAX_LENGTH,
  DEFAULT_DASHBOARD_ICON,
  dashboardCreatePayload,
  dashboardUpdatePayload,
  validateDashboardForm,
  type DashboardFormErrors,
  type DashboardFormValues,
} from './dashboardModel'
import { DASHBOARD_ICON_OPTIONS, dashboardIconOption } from './dashboardIcons'

type DashboardFormSubmitPayload = {
  name: string
  description: string
  icon: string
  /** 编辑模式携带列表 revision；新建模式不出现。 */
  expected_revision?: number
}

type DashboardFormDialogProps = {
  mode: 'create' | 'edit'
  open: boolean
  /** 编辑模式的目标卡片；父组件从列表派生，列表刷新后 revision 同步更新。 */
  dashboard?: OperationsDashboardCard | null
  onClose: () => void
  /** 提交并等待父组件 mutation；失败向上抛出，由本弹窗展示 role=alert。 */
  onSubmit: (payload: DashboardFormSubmitPayload) => Promise<unknown>
  /** 刷新列表（冲突恢复路径），返回后父组件会以最新 revision 重新渲染。 */
  onRefresh?: () => Promise<unknown>
}

export function DashboardFormDialog({
  mode,
  open,
  dashboard,
  onClose,
  onSubmit,
  onRefresh,
}: DashboardFormDialogProps) {
  const isEdit = mode === 'edit'
  const [name, setName] = useState(isEdit ? dashboard?.name ?? '' : '')
  const [description, setDescription] = useState(isEdit ? dashboard?.description ?? '' : '')
  const [icon, setIcon] = useState(isEdit ? dashboard?.icon ?? DEFAULT_DASHBOARD_ICON : DEFAULT_DASHBOARD_ICON)
  const [saving, setSaving] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [submitError, setSubmitError] = useState('')
  const [fieldErrors, setFieldErrors] = useState<DashboardFormErrors>({})
  const [conflictRevision, setConflictRevision] = useState<number | null>(null)

  const resetError = () => {
    setSubmitError('')
    setFieldErrors({})
    setConflictRevision(null)
  }

  const handleClose = () => {
    if (saving) return
    onClose()
  }

  const submit = async (event?: FormEvent) => {
    event?.preventDefault()
    const values: DashboardFormValues = { name, description, icon }
    const errors = validateDashboardForm(values)
    if (Object.keys(errors).length > 0) {
      setFieldErrors(errors)
      setSubmitError(Object.values(errors)[0] ?? '请检查表单内容')
      return
    }
    setSaving(true)
    resetError()
    try {
      const payload = isEdit && dashboard
        ? dashboardUpdatePayload(values, dashboard.revision)
        : dashboardCreatePayload(values)
      await onSubmit(payload)
    } catch (error) {
      setFieldErrors({})
      setSubmitError(operationsApiErrorText(
        error,
        isEdit ? '保存修改失败，请稍后重试' : '创建看板失败，请稍后重试',
      ))
      setConflictRevision(dashboardErrorCode(error) === 'dashboard_revision_conflict'
        ? dashboardErrorRevision(error)
        : null)
    } finally {
      setSaving(false)
    }
  }

  const refreshAfterConflict = async () => {
    if (!onRefresh) return
    setRefreshing(true)
    try {
      await onRefresh()
      setSubmitError('')
      setConflictRevision(null)
    } catch (error) {
      setSubmitError(operationsApiErrorText(error, '刷新看板列表失败，请稍后重试'))
    } finally {
      setRefreshing(false)
    }
  }

  const iconOption = dashboardIconOption(icon)

  return (
    <Dialog open={open} onOpenChange={next => { if (!next) handleClose() }}>
      <DialogContent className="w-[min(92vw,32rem)]" dismissible={!saving}>
        <DialogHeader icon={<iconOption.icon size={18} />}>
          <DialogTitle>{isEdit ? '编辑看板信息' : '新建监控看板'}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? '修改监控看板基本信息，仅影响平台卡片；看板内的标题、数据源与面板不会被改动。'
              : '创建后会在 Grafana 中生成一个空白看板。'}
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={submit} className="space-y-4" noValidate data-testid="dashboard-form-dialog">
          <Input
            label="看板名称"
            required
            autoFocus
            maxLength={DASHBOARD_NAME_MAX_LENGTH}
            value={name}
            error={fieldErrors.name}
            onChange={event => { setName(event.target.value); setFieldErrors(current => ({ ...current, name: undefined })) }}
            placeholder="例如：核心链路运行态势"
          />

          <div>
            <label htmlFor="dashboard-description" className="mb-1.5 block text-sm font-medium text-[var(--color-text-primary)]">
              描述（可选）
            </label>
            <textarea
              id="dashboard-description"
              value={description}
              maxLength={DASHBOARD_DESCRIPTION_MAX_LENGTH}
              rows={3}
              onChange={event => { setDescription(event.target.value); setFieldErrors(current => ({ ...current, description: undefined })) }}
              placeholder="说明这个看板关注的运行态势，便于后续识别"
              className="w-full resize-none rounded-md border border-[var(--color-border)] bg-card px-3 py-2 text-sm text-[var(--color-text-primary)] placeholder:text-[var(--color-text-tertiary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
            <p className="mt-1 text-right text-[11px] tabular-nums text-[var(--color-text-tertiary)]">
              {description.length}/{DASHBOARD_DESCRIPTION_MAX_LENGTH}
            </p>
            {fieldErrors.description && (
              <p className="mt-1 text-xs text-[var(--color-danger)]" role="alert">{fieldErrors.description}</p>
            )}
          </div>

          <fieldset>
            <legend className="mb-2 text-sm font-medium text-[var(--color-text-primary)]">看板图标</legend>
            <div className="grid grid-cols-5 gap-2">
              {DASHBOARD_ICON_OPTIONS.map(option => {
                const OptionIcon = option.icon
                const selected = icon === option.key
                return (
                  <button
                    key={option.key}
                    type="button"
                    onClick={() => { setIcon(option.key); setFieldErrors(current => ({ ...current, icon: undefined })) }}
                    title={option.label}
                    aria-label={option.label}
                    aria-pressed={selected}
                    className={`flex aspect-square items-center justify-center rounded-xl border transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                      selected
                        ? 'border-brand bg-brand-soft text-brand-ink shadow-sm'
                        : 'border-border bg-card text-muted-foreground hover:border-brand-line hover:bg-muted'
                    }`}
                  >
                    <OptionIcon size={18} />
                  </button>
                )
              })}
            </div>
            {fieldErrors.icon && (
              <p className="mt-1 text-xs text-[var(--color-danger)]" role="alert">{fieldErrors.icon}</p>
            )}
          </fieldset>

          {submitError && (
            <div
              role="alert"
              data-testid="dashboard-form-error"
              className="flex items-start gap-2.5 rounded-xl border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-3.5 py-3 text-sm text-[var(--color-danger)]"
            >
              <AlertCircle size={16} className="mt-0.5 shrink-0" />
              <div className="min-w-0">
                <p className="leading-5">{submitError}</p>
                {conflictRevision !== null && (
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <span className="text-xs">服务端最新版本：{conflictRevision}</span>
                    {onRefresh && (
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        loading={refreshing}
                        onClick={() => void refreshAfterConflict()}
                        data-testid="dashboard-form-refresh"
                      >
                        刷新最新数据
                      </Button>
                    )}
                  </div>
                )}
              </div>
            </div>
          )}

          <DialogFooter>
            <Button type="button" variant="outline" onClick={handleClose} disabled={saving}>取消</Button>
            <Button type="submit" loading={saving} data-testid="dashboard-form-submit">
              {isEdit ? '保存修改' : '创建看板'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
