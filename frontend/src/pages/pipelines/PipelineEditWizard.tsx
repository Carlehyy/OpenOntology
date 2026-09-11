import { useState, useEffect, useCallback } from 'react'
import { createPortal } from 'react-dom'
import {
  X, Loader2, CheckCircle2, XCircle, AlertTriangle, ChevronLeft, ChevronRight,
  KeyRound, Eye, Save, Rocket, Info, Lock, Sparkles, Table2, GitBranch, ShieldCheck,
  FileCode2,
} from 'lucide-react'
import pipelinesApi, { CONTRACT_FIELD_TYPES } from '@/api/v2/pipelines'
import type { Pipeline, DryRunResult, DryRunRowsPage, ColumnDefinition, ValidateDefinitionsResult } from '@/api/v2/pipelines'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'

const splitPk = (s?: string): string[] =>
  (s ?? '').split(',').map(x => x.trim()).filter(Boolean)

/** 旧词表（int/bool/datetime）映射到平台词表，读旧契约时兜底 */
const LEGACY_TYPE_MAP: Record<string, string> = {
  int: 'integer', bool: 'boolean', datetime: 'timestamp', str: 'string', number: 'float',
}
const normType = (t?: string): string => {
  const v = (t || 'string').toLowerCase()
  const mapped = LEGACY_TYPE_MAP[v] || v
  return (CONTRACT_FIELD_TYPES as readonly string[]).includes(mapped) ? mapped : 'string'
}

type RequiredContractField = 'field_key' | 'field_name' | 'field_type'

const REQUIRED_CONTRACT_FIELD_LABELS: Record<RequiredContractField, string> = {
  field_key: '字段标识',
  field_name: '字段名称',
  field_type: '字段类型',
}

const getMissingRequiredContractFields = (definitions: ColumnDefinition[]) =>
  definitions.flatMap((definition, rowIndex) =>
    (Object.keys(REQUIRED_CONTRACT_FIELD_LABELS) as RequiredContractField[])
      .filter(field => !String(definition[field] || '').trim())
      .map(field => ({
        rowIndex,
        field,
        label: REQUIRED_CONTRACT_FIELD_LABELS[field],
        sourceKey: definition.source_key,
      })),
  )

interface Props {
  pipeline: Pipeline
  onClose: () => void
  onSaved: (pipeline: Pipeline) => void
}

export default function PipelineEditWizard({ pipeline, onClose, onSaved }: Props) {
  const isPublished = pipeline.status === 'published'
  const isN8n = (pipeline.definition as { engine?: string } | null)?.engine === 'n8n'
  const isPython = (pipeline.definition as { engine?: string } | null)?.engine === 'python'
  const [step, setStep] = useState<1 | 2 | 3 | 4>(1)

  // ── 阶段 1: 流水线信息 ──
  const [name, setName] = useState(pipeline.name || '')
  const [description, setDescription] = useState(pipeline.description || '')

  // ── 阶段 2: 执行预览 ──
  const [dryRunPhase, setDryRunPhase] = useState<'idle' | 'running' | 'done' | 'error'>('idle')
  const [dryRunResult, setDryRunResult] = useState<DryRunResult | null>(null)
  const [dryRunError, setDryRunError] = useState('')
  const [cachedColumns, setCachedColumns] = useState<string[]>([])
  const [cachedSample, setCachedSample] = useState<Record<string, unknown>[]>([])
  const [totalRows, setTotalRows] = useState(0)
  const [expanded, setExpanded] = useState(false)
  // 已发布：展示最近一次运行的输出样本（不重跑，避免触发生产 workflow）
  const [lastRun, setLastRun] = useState<'loading' | 'none' | { at: string | null; columns: string[]; sample: Record<string, unknown>[] }>('loading')

  const multiOutput = (dryRunResult?.outputs.length ?? 0) > 1
  // 湖中已固化的主键（试运行预检返回）——预填提示但可修改：改动后的契约
  // 需以「全量覆盖」运行一次重建资产（重写湖中声明），增量/合并入库在对齐前会失败
  const lakeDeclaredPk = dryRunResult && dryRunResult.outputs.length === 1 && dryRunResult.outputs[0].pk_source === 'lake'
    ? dryRunResult.outputs[0].pk : ''
  const lakePkCols = new Set(splitPk(lakeDeclaredPk))

  // ── 阶段 3: 设置主键组 ──
  const [columnDefs, setColumnDefs] = useState<ColumnDefinition[]>([])
  const [validateResult, setValidateResult] = useState<ValidateDefinitionsResult | null>(null)
  const [validating, setValidating] = useState(false)
  const [requiredValidationAttempted, setRequiredValidationAttempted] = useState(false)

  // ── 阶段 4: 确认配置 ──
  const [saving, setSaving] = useState(false)
  const [publishing, setPublishing] = useState(false)
  const [actionError, setActionError] = useState('')
  const [pendingConfirm, setPendingConfirm] = useState<null | { title: string; description: string; action: () => void }>(null)

  // ── 初始化字段契约：按原始列合并已有定义（旧数据缺 source_key 时回退 field_key）──
  const initColumnDefs = useCallback((columns: string[], existing?: ColumnDefinition[] | null, lockedPk?: Set<string>) => {
    const existingMap = new Map((existing || []).map(d => [d.source_key || d.field_key, d]))
    return columns.map(col => {
      const prev = existingMap.get(col)
      const def: ColumnDefinition = prev ? {
        source_key: prev.source_key || prev.field_key,
        field_key: prev.field_key,
        field_name: prev.field_name || prev.field_key,
        field_type: normType(prev.field_type),
        is_primary_key: !!prev.is_primary_key,
        nullable: prev.nullable !== false,
      } : {
        source_key: col,
        field_key: col,
        field_name: col,
        field_type: 'string',
        is_primary_key: false,
        nullable: true,
      }
      // 湖中已固化主键：预填勾选（列名即入湖列名）；仍可修改，改动走全量覆盖重建
      if (lockedPk?.has(def.field_key)) def.is_primary_key = true
      if (def.is_primary_key) def.nullable = false
      return def
    })
  }, [])

  // ── 阶段 2: 执行 dryRun ──
  const runDryRun = async () => {
    setDryRunPhase('running')
    setDryRunError('')
    setExpanded(false)
    try {
      const res = await pipelinesApi.dryRun(pipeline.id, 100)
      setDryRunResult(res)
      const allCols: string[] = []
      const allSample: Record<string, unknown>[] = []
      let total = 0
      for (const o of res.outputs) {
        for (const c of o.columns) {
          if (!allCols.includes(c)) allCols.push(c)
        }
        if (o.sample?.length) allSample.push(...o.sample)
        total += o.rows_out
      }
      setCachedColumns(allCols)
      setCachedSample(allSample)
      setTotalRows(total)
      const declared = res.outputs.length === 1 && res.outputs[0].pk_source === 'lake'
        ? new Set(splitPk(res.outputs[0].pk)) : undefined
      setColumnDefs(initColumnDefs(allCols, pipeline.column_definitions, declared))
      setRequiredValidationAttempted(false)
      setDryRunPhase('done')
    } catch (e: unknown) {
      const err = e as { detail?: string; message?: string }
      setDryRunError(err?.detail || err?.message || '试运行失败')
      setDryRunPhase('error')
    }
  }

  // 进入阶段 2：未发布自动触发一次；已发布只读，改为加载最近一次运行样本
  useEffect(() => {
    if (step !== 2) return
    if (!isPublished && dryRunPhase === 'idle') {
      runDryRun()
      return
    }
    if (isPublished && dryRunPhase === 'idle') {
      const defs = (pipeline.column_definitions || []).map(d => ({
        ...d,
        source_key: d.source_key || d.field_key,
        field_type: normType(d.field_type),
      }))
      setColumnDefs(defs)
      setCachedColumns(defs.map(d => d.field_key))
      setDryRunPhase('done')
      pipelinesApi.runs(pipeline.id).then(runs => {
        const ok = runs.find(r => r.status === 'success' && r.stats)
        const outputs = ((ok?.stats as { meta?: { outputs?: Array<Record<string, unknown>> } } | null)?.meta?.outputs) || []
        const first = outputs[0] as { output_sample?: Record<string, unknown>[]; output_columns?: string[] } | undefined
        if (ok && first?.output_sample?.length) {
          const cols = first.output_columns?.length ? first.output_columns : Object.keys(first.output_sample[0] || {})
          setLastRun({ at: ok.finished_at || ok.started_at, columns: cols, sample: first.output_sample })
          setCachedColumns(prev => prev.length ? prev : cols)
        } else {
          setLastRun('none')
        }
      }).catch(() => setLastRun('none'))
    }
  }, [step])

  // ── 阶段 3: 校验（全量，基于第 2 步暂存数据）──
  const runValidate = async () => {
    setRequiredValidationAttempted(true)
    const requiredErrors = getMissingRequiredContractFields(columnDefs)
    if (requiredErrors.length > 0) {
      setValidateResult({
        valid: false,
        errors: requiredErrors.map(error => ({
          field_key: error.sourceKey,
          severity: 'error',
          message: `原始列「${error.sourceKey || error.rowIndex + 1}」的${error.label}不能为空`,
        })),
      })
      return
    }
    if (!dryRunResult?.dry_run_id) {
      setValidateResult({ valid: false, errors: [{ field_key: '', message: '缺少试运行数据，请回到「执行预览」重新执行', severity: 'error' }] })
      return
    }
    setValidating(true)
    setValidateResult(null)
    try {
      const res = await pipelinesApi.validateDefinitions(pipeline.id, { column_definitions: columnDefs }, dryRunResult.dry_run_id)
      setValidateResult(res)
      if (res.valid && (res.errors || []).length === 0) setStep(4)
    } catch (e: unknown) {
      const err = e as { detail?: string; message?: string }
      setValidateResult({ valid: false, errors: [{ field_key: '', message: err?.detail || err?.message || '校验请求失败', severity: 'error' }] })
    } finally {
      setValidating(false)
    }
  }

  // ── 阶段 4: 保存 / 发布（发布后仅编排与字段契约只读） ──
  const handleSave = async () => {
    setSaving(true)
    setActionError('')
    try {
      const updated = await pipelinesApi.update(pipeline.id, {
        name,
        description,
        column_definitions: multiOutput ? undefined : columnDefs,
      })
      onSaved(updated)
      onClose()
    } catch (e: unknown) {
      const err = e as { detail?: string; message?: string }
      setActionError(err?.detail || err?.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const handlePublish = async () => {
    setPublishing(true)
    setActionError('')
    try {
      const updated = await pipelinesApi.update(pipeline.id, {
        name,
        description,
        column_definitions: multiOutput ? undefined : columnDefs,
      })
      // 发布只负责封版；是否启用由用户发布后在流水线列表中单独决定。
      const published = await pipelinesApi.publish(pipeline.id, false)
      onSaved({ ...updated, ...published })
      onClose()
    } catch (e: unknown) {
      const err = e as { detail?: string; message?: string }
      setActionError(err?.detail || err?.message || '发布失败')
    } finally {
      setPublishing(false)
    }
  }

  // ── 辅助 ──
  const pkFields = columnDefs.filter(d => d.is_primary_key)
  const hasErrors = validateResult && !validateResult.valid
  const errorMap = new Map((validateResult?.errors || []).map(e => [e.field_key, e]))
  const missingRequiredFields = getMissingRequiredContractFields(columnDefs)
  const missingRequiredByRow = new Map<number, Set<RequiredContractField>>()
  for (const error of missingRequiredFields) {
    const fields = missingRequiredByRow.get(error.rowIndex) || new Set<RequiredContractField>()
    fields.add(error.field)
    missingRequiredByRow.set(error.rowIndex, fields)
  }
  const contractEditable = !isPublished && !multiOutput

  // 名称/描述到第 4 步保存才落库——中途关窗要提醒，避免静默丢改动
  const infoDirty = name !== (pipeline.name || '') || description !== (pipeline.description || '')
  const handleClose = () => {
    if (!infoDirty) { onClose(); return }
    setPendingConfirm({
      title: '关闭并放弃未保存修改',
      description: '流水线名称/描述有未保存的修改，关闭后将丢失。确认关闭？',
      action: () => { onClose() },
    })
  }

  const updateColDef = (index: number, patch: Partial<ColumnDefinition>) => {
    setValidateResult(null)
    setColumnDefs(prev => prev.map((d, i) => {
      if (i !== index) return d
      const next = { ...d, ...patch }
      // 主键（包括复合主键中的每一列）必须非空，界面层直接保持该不变量。
      if (next.is_primary_key) next.nullable = false
      return next
    }))
  }

  const handleSaveBasicInfo = async () => {
    if (!name.trim()) {
      setActionError('流水线名称不能为空')
      return
    }
    setSaving(true)
    setActionError('')
    try {
      const updated = await pipelinesApi.update(pipeline.id, {
        name: name.trim(),
        description,
      })
      onSaved(updated)
      onClose()
    } catch (e: unknown) {
      const err = e as { detail?: string; message?: string }
      setActionError(err?.detail || err?.message || '基础信息保存失败')
    } finally {
      setSaving(false)
    }
  }

  const steps = [
    { num: 1, label: '流水线信息' },
    { num: 2, label: '执行预览' },
    { num: 3, label: '设置主键组' },
    { num: 4, label: '确认配置' },
  ]

  return createPortal(
    <div className="fixed inset-0 z-[var(--z-modal)] flex items-center justify-center bg-accent p-4 backdrop-blur-[2px] sm:p-6">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="pipeline-edit-title"
        className="flex h-auto max-h-[88vh] w-[1040px] max-w-full flex-col overflow-hidden rounded-2xl border border-border bg-card shadow-[0_28px_90px_rgba(15,23,42,0.24)]"
      >
        {/* 头部 */}
        <div className="flex shrink-0 items-center justify-between border-b border-border px-6 py-3.5">
          <div className="min-w-0">
            <h3 id="pipeline-edit-title" className="flex items-center gap-2.5 truncate text-base font-semibold tracking-tight text-foreground">
              {isPublished ? '查看已发布流水线' : '配置流水线'}「{pipeline.name}」
              {isN8n ? (
                <span className="inline-flex shrink-0 items-center gap-1 rounded-lg border border-brand-line bg-brand-soft px-2 py-1 text-[11px] font-medium text-brand-ink">
                  <Sparkles size={10} /> n8n 流水线
                </span>
              ) : isPython ? (
                <span className="inline-flex shrink-0 items-center gap-1 rounded-lg border border-viz-indigo-soft bg-viz-indigo-soft px-2 py-1 text-[11px] font-medium text-viz-indigo">
                  <FileCode2 size={10} /> Python 脚本
                </span>
              ) : (
                <span className="inline-flex items-center gap-1 rounded border border-border bg-muted px-1.5 py-0.5 text-[11px] font-normal text-muted-foreground">
                  <GitBranch size={10} /> 未知引擎
                </span>
              )}
            </h3>
            <p className="mt-1 text-xs text-muted-foreground">{isPublished ? '名称与描述可更新；发布版本的编排与字段契约保持只读' : '按顺序完成执行验证、字段契约和发布确认'}</p>
          </div>
          <button onClick={handleClose} aria-label="关闭" className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl text-[var(--color-text-tertiary)] transition hover:bg-muted hover:text-foreground">
            <X size={18} />
          </button>
        </div>

        {/* 步骤指示器 */}
        <div className="flex shrink-0 items-center gap-1.5 overflow-x-auto border-b border-border bg-muted px-6 py-2.5">
          {steps.map((s, index) => (
            <div key={s.num} className="contents">
              <button
                disabled={s.num > step || (s.num === 3 && !cachedColumns.length)}
                onClick={() => setStep(s.num as 1 | 2 | 3 | 4)}
                className={`group flex h-9 min-w-[150px] flex-1 items-center justify-center gap-2 rounded-xl border px-3 text-center text-xs font-medium transition-all duration-200 active:scale-[0.98] ${
                  step === s.num
                    ? 'border-brand bg-brand-deep text-[var(--color-text-inverse)] shadow-[0_6px_18px_rgba(15,118,110,0.18)]'
                    : step > s.num
                      ? 'cursor-pointer border-brand-line bg-card text-brand-ink hover:-translate-y-0.5 hover:border-brand hover:shadow-sm'
                      : 'cursor-not-allowed border-border bg-muted text-[var(--color-text-tertiary)]'
                }`}
              >
                {step > s.num ? <CheckCircle2 size={14} className="shrink-0" /> : <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border border-current text-[10px]">{s.num}</span>}
                <span className="truncate">{s.label}</span>
              </button>
              {index < steps.length - 1 && (
                <ChevronRight size={15} className={`shrink-0 transition-colors duration-200 ${step > s.num ? 'text-brand-ink' : 'text-[var(--color-text-tertiary)]'}`} />
              )}
            </div>
          ))}
        </div>

        {/* 内容区 */}
        <div className="scrollbar-thin min-h-0 flex-1 overflow-auto overscroll-contain bg-card p-5 sm:px-6 sm:py-5">
          {/* ──────── 阶段 1: 流水线信息 ──────── */}
          {step === 1 && (
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-foreground mb-1">流水线名称</label>
                <input
                  value={name}
                  onChange={e => setName(e.target.value)}
                  className="w-full rounded-xl border border-border px-3.5 py-2.5 text-sm outline-none transition focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring"
                  placeholder="输入流水线名称"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-foreground mb-1">描述</label>
                <textarea
                  value={description}
                  onChange={e => setDescription(e.target.value)}
                  className="h-20 w-full resize-none rounded-xl border border-border px-3.5 py-2.5 text-sm outline-none transition focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring"
                  placeholder="输入流水线描述（可选）"
                />
              </div>
              <div className={`flex items-start gap-2 rounded-xl border px-3.5 py-3 text-xs leading-5 ${isPublished ? 'border-brand-line bg-brand-soft text-brand-ink' : 'border-border bg-muted text-muted-foreground'}`}>
                {isPublished ? <ShieldCheck size={14} className="mt-0.5 shrink-0" /> : <Info size={14} className="mt-0.5 shrink-0" />}
                <span>{isPublished
                  ? '名称与描述属于基础信息，可以随时更新；已发布的编排、字段类型与主键契约仍保持封版。'
                  : '名称与描述属于基础信息，可在此随时单独保存；编排、字段契约与发布配置仍需在最后一步完成。'}</span>
              </div>
              {actionError && (
                <div role="alert" className="flex items-start gap-1.5 rounded-xl border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] bg-[var(--color-danger-bg)] px-3.5 py-2.5 text-xs text-[var(--color-danger)]">
                  <XCircle size={13} className="mt-0.5 shrink-0" />
                  <span>{actionError}</span>
                </div>
              )}
            </div>
          )}

          {/* ──────── 阶段 2: 执行预览 ──────── */}
          {step === 2 && (
            <div className="space-y-3">
              {dryRunPhase === 'running' && (
                <div className="flex items-center gap-3 text-[var(--color-info)] py-8 justify-center">
                  <Loader2 size={20} className="animate-spin" />
                  <span className="text-sm">正在执行流水线，获取预览数据…</span>
                </div>
              )}

              {dryRunPhase === 'error' && (
                <div className="bg-[var(--color-danger-bg)] border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] rounded-lg p-4 flex items-start gap-2">
                  <XCircle size={16} className="text-[var(--color-danger)] mt-0.5 shrink-0" />
                  <div>
                    <p className="text-sm text-[var(--color-danger)] font-medium">试运行失败</p>
                    <p className="text-xs text-[var(--color-danger)] mt-0.5">{dryRunError}</p>
                    <button
                      onClick={runDryRun}
                      className="mt-2 text-xs text-[var(--color-danger)] underline hover:text-[var(--color-danger)]"
                    >
                      重试
                    </button>
                  </div>
                </div>
              )}

              {dryRunPhase === 'done' && !isPublished && (
                <div className="flex items-center justify-between text-xs text-[var(--color-text-tertiary)] mb-1">
                  <span>
                    共 {totalRows.toLocaleString()} 行，默认展示前 {Math.min(cachedSample.length, 100)} 行
                  </span>
                  <div className="flex items-center gap-3">
                    {totalRows > cachedSample.length && dryRunResult && !expanded && (
                      <button
                        onClick={() => setExpanded(true)}
                        className="text-[var(--color-nav-bg)] hover:underline flex items-center gap-1"
                      >
                        <Table2 size={12} /> 展开查看全部数据
                      </button>
                    )}
                    <button
                      onClick={runDryRun}
                      className="text-[var(--color-nav-bg)] hover:underline flex items-center gap-1"
                    >
                      <Eye size={12} /> 重新执行
                    </button>
                  </div>
                </div>
              )}

              {dryRunPhase === 'done' && !isPublished && (dryRunResult?.outputs || []).some(o => o.gate_error || o.warnings.length > 0) && (
                <div className="bg-[var(--color-warning-bg)] border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] rounded-lg px-3 py-2 space-y-1">
                  {(dryRunResult?.outputs || []).map((o, i) => (
                    <div key={i} className="text-xs text-[var(--color-warning)] space-y-0.5">
                      {o.gate_error && <p className="text-[var(--color-danger)]">入湖预检：{o.gate_error}</p>}
                      {o.warnings.map((w, j) => <p key={j}>{w}</p>)}
                    </div>
                  ))}
                </div>
              )}

              {dryRunPhase === 'done' && isPublished && (
                <div className="flex items-center gap-2 text-xs text-[var(--color-text-tertiary)] mb-1">
                  <Info size={12} />
                  <span>
                    已发布流水线不在此处重新执行（避免触发生产工作流）。
                    {typeof lastRun === 'object' && lastRun.at ? `下方为最近一次执行（${new Date(lastRun.at).toLocaleString('zh-CN')}）的输出样本。` : ''}
                  </span>
                </div>
              )}

              {/* 数据表格：采样 / 展开分页 / 最近一次运行样本 */}
              {dryRunPhase === 'done' && expanded && dryRunResult ? (
                <DryRunPagedTable
                  pipelineId={pipeline.id}
                  dryRunId={dryRunResult.dry_run_id}
                  outputCount={dryRunResult.outputs.length}
                  onCollapse={() => setExpanded(false)}
                />
              ) : dryRunPhase === 'done' && (
                <SampleTable
                  columns={isPublished && typeof lastRun === 'object' ? lastRun.columns : cachedColumns}
                  rows={isPublished ? (typeof lastRun === 'object' ? lastRun.sample : []) : cachedSample.slice(0, 100)}
                  emptyText={
                    isPublished
                      ? (lastRun === 'loading' ? '加载最近一次执行结果…' : '暂无成功的执行记录，可在列表页「执行」查看输出')
                      : '暂无数据'
                  }
                />
              )}

              {dryRunPhase === 'done' && !isPublished && cachedColumns.length === 0 && (
                <div className="text-center py-8 text-[var(--color-text-tertiary)] text-sm">
                  <AlertTriangle size={24} className="mx-auto mb-2 opacity-40" />
                  <p>流水线执行未产生任何数据列</p>
                </div>
              )}
            </div>
          )}

          {/* ──────── 阶段 3: 设置主键组 ──────── */}
          {step === 3 && (
            <div className="space-y-3">
              <div className="flex items-center gap-2 text-xs text-[var(--color-text-tertiary)]">
                <KeyRound size={12} />
                <span>定义每个字段的入湖契约：字段标识（入湖列名，可改名）、名称、类型、主键、允许空值；<span className="text-[var(--color-danger)]">*</span> 为必填项</span>
                {isPublished && <span className="text-[var(--color-warning)] font-medium">（已发布 · 只读）</span>}
              </div>

              {multiOutput && !isPublished && (
                <div className="bg-[var(--color-warning-bg)] border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] rounded-lg px-3 py-2 text-xs text-[var(--color-warning)] flex items-start gap-1.5">
                  <AlertTriangle size={13} className="mt-0.5 shrink-0" />
                  <span>
                    该流水线单次执行产出多个数据集，流水线级字段契约暂不适用（主键的正确粒度是每个数据集一个，
                    请在任务/资产湖粒度管理）。可直接进入下一步。
                  </span>
                </div>
              )}

              {lakePkCols.size > 0 && (
                <div className="bg-[var(--color-warning-bg)] border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] rounded-lg px-3 py-2 text-xs text-[var(--color-warning)] flex items-center gap-1.5">
                  <Lock size={11} className="shrink-0" />
                  <span>
                    资产湖已固化主键契约 <b className="font-mono">{lakeDeclaredPk}</b>（已自动勾选）。
                    可以修改，但改动后需以「全量覆盖」方式运行一次重建资产（会重写湖中声明并重建实例身份）；
                    增量/合并入库在对齐前会失败。
                  </span>
                </div>
              )}

              {pkFields.length > 0 && (
                <div className="bg-[var(--color-info-bg)] border border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] rounded-lg px-3 py-2 text-xs text-[var(--color-info)]">
                  主键：{pkFields.map(d => d.field_key).join('、')}
                  {pkFields.length > 1 && '（复合主键）'}
                </div>
              )}

              <div className="border rounded-lg overflow-x-auto">
                <table className="w-full min-w-[700px] text-xs">
                  <thead className="bg-muted border-b">
                    <tr>
                      <th className="text-left px-3 py-2 font-medium text-muted-foreground w-36">原始列名</th>
                      <th className="text-left px-3 py-2 font-medium text-muted-foreground w-32">字段标识（入湖列名）<span className="ml-0.5 text-[var(--color-danger)]" aria-label="必填">*</span></th>
                      <th className="text-left px-3 py-2 font-medium text-muted-foreground w-32">字段名称<span className="ml-0.5 text-[var(--color-danger)]" aria-label="必填">*</span></th>
                      <th className="text-left px-3 py-2 font-medium text-muted-foreground w-24">字段类型<span className="ml-0.5 text-[var(--color-danger)]" aria-label="必填">*</span></th>
                      <th className="text-center px-3 py-2 font-medium text-muted-foreground w-16">主键</th>
                      <th className="text-center px-3 py-2 font-medium text-muted-foreground w-16">允许空</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y">
                    {columnDefs.map((d, i) => {
                      const declaredInLake = lakePkCols.has(d.field_key)
                      const disabled = !contractEditable
                      const fieldErrors = errorMap.get(d.field_key)
                      const allErrors = [fieldErrors].filter(Boolean)
                      const missingFields = requiredValidationAttempted ? missingRequiredByRow.get(i) : undefined
                      return (
                        <tr key={i} className={allErrors.length > 0 || missingFields?.size ? 'bg-[var(--color-danger-bg)]' : 'hover:bg-muted'}>
                          <td className="px-3 py-1.5 text-muted-foreground font-mono text-[11px]" title={d.source_key}>
                            <div className="flex min-w-0 items-center gap-1.5">
                              <span className="truncate">{d.source_key}</span>
                              {d.is_primary_key && (
                                <span
                                  role="img"
                                  aria-label="主键"
                                  title="主键"
                                  className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-md bg-brand-soft text-brand-ink ring-1 ring-inset ring-ring"
                                >
                                  <KeyRound size={10} />
                                </span>
                              )}
                              {!d.nullable && (
                                <span
                                  role="img"
                                  aria-label="非空"
                                  title="非空"
                                  className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground ring-1 ring-inset ring-[var(--color-border-hover)]"
                                >
                                  <Lock size={10} />
                                </span>
                              )}
                            </div>
                          </td>
                          <td className="px-1 py-1">
                            <div className="relative">
                              <input
                                value={d.field_key}
                                disabled={disabled}
                                required
                                aria-required="true"
                                aria-invalid={missingFields?.has('field_key') || undefined}
                                aria-describedby={missingFields?.has('field_key') ? `field-key-error-${i}` : undefined}
                                onChange={e => updateColDef(i, { field_key: e.target.value })}
                                title={declaredInLake ? '湖中已固化的主键列：改名后需以「全量覆盖」运行一次重建资产' : d.source_key !== d.field_key ? `入湖时 ${d.source_key} → ${d.field_key}` : undefined}
                                className={`w-full px-2 py-1 border rounded text-xs font-mono ${disabled ? 'bg-muted text-[var(--color-text-tertiary)] cursor-not-allowed' : missingFields?.has('field_key') ? 'border-[var(--color-danger)] bg-[var(--color-danger-bg)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring' : d.source_key !== d.field_key ? 'border-[color-mix(in_srgb,var(--color-info)_35%,transparent)] bg-[var(--color-info-bg)]' : ''}`}
                              />
                              {declaredInLake && <Lock size={10} className="absolute right-1.5 top-1/2 -translate-y-1/2 text-[var(--color-warning)] pointer-events-none" />}
                            </div>
                            {missingFields?.has('field_key') && <p id={`field-key-error-${i}`} className="mt-0.5 text-[10px] text-[var(--color-danger)]">字段标识为必填项</p>}
                          </td>
                          <td className="px-1 py-1">
                            <input
                              value={d.field_name}
                              disabled={disabled}
                              required
                              aria-required="true"
                              aria-invalid={missingFields?.has('field_name') || undefined}
                              aria-describedby={missingFields?.has('field_name') ? `field-name-error-${i}` : undefined}
                              onChange={e => updateColDef(i, { field_name: e.target.value })}
                              className={`w-full px-2 py-1 border rounded text-xs ${disabled ? 'bg-muted text-[var(--color-text-tertiary)] cursor-not-allowed' : missingFields?.has('field_name') ? 'border-[var(--color-danger)] bg-[var(--color-danger-bg)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring' : ''}`}
                            />
                            {missingFields?.has('field_name') && <p id={`field-name-error-${i}`} className="mt-0.5 text-[10px] text-[var(--color-danger)]">字段名称为必填项</p>}
                          </td>
                          <td className="px-1 py-1">
                            <Select
                              value={d.field_type}
                              onValueChange={value => updateColDef(i, { field_type: value })}
                            >
                              <SelectTrigger
                                disabled={disabled}
                                aria-required="true"
                                aria-invalid={missingFields?.has('field_type') || undefined}
                                aria-describedby={missingFields?.has('field_type') ? `field-type-error-${i}` : undefined}
                                className={`w-full px-2 py-1 border rounded text-xs ${disabled ? 'bg-muted text-[var(--color-text-tertiary)] cursor-not-allowed' : missingFields?.has('field_type') ? 'border-[var(--color-danger)] bg-[var(--color-danger-bg)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring' : ''}`}
                              >
                                <SelectValue />
                              </SelectTrigger>
                              <SelectContent>
                                {CONTRACT_FIELD_TYPES.map(t => (
                                  <SelectItem key={t} value={t}>{t}</SelectItem>
                                ))}
                              </SelectContent>
                            </Select>
                            {missingFields?.has('field_type') && <p id={`field-type-error-${i}`} className="mt-0.5 text-[10px] text-[var(--color-danger)]">字段类型为必填项</p>}
                          </td>
                          <td className="px-1 py-1 text-center">
                            <input
                              type="checkbox"
                              checked={d.is_primary_key}
                              disabled={disabled}
                              onChange={e => updateColDef(i, { is_primary_key: e.target.checked })}
                              className={disabled ? 'opacity-50 cursor-not-allowed' : ''}
                            />
                          </td>
                          <td className="px-1 py-1 text-center">
                            <input
                              type="checkbox"
                              checked={d.is_primary_key ? false : d.nullable}
                              disabled={disabled || d.is_primary_key}
                              onChange={e => updateColDef(i, { nullable: e.target.checked })}
                              title={d.is_primary_key ? '主键列必须非空' : '允许该列为空'}
                              className={(disabled || d.is_primary_key) ? 'opacity-50 cursor-not-allowed' : ''}
                            />
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>

              {/* 校验错误/警告展示 */}
              {hasErrors && (
                <div role="alert" aria-live="polite" className="bg-[var(--color-danger-bg)] border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] rounded-lg p-3 space-y-1">
                  {(validateResult?.errors || []).map((e, i) => (
                    <div key={i} className="flex items-start gap-1.5 text-xs">
                      {e.severity === 'warning'
                        ? <AlertTriangle size={12} className="text-[var(--color-warning)] mt-0.5 shrink-0" />
                        : <XCircle size={12} className="text-[var(--color-danger)] mt-0.5 shrink-0" />}
                      <span className={e.severity === 'warning' ? 'text-[var(--color-warning)]' : 'text-[var(--color-danger)]'}>
                        {e.field_key && <span className="font-mono mr-1">[{e.field_key}]</span>}
                        {e.message}
                      </span>
                    </div>
                  ))}
                </div>
              )}

              {validateResult?.valid && (validateResult.errors || []).length === 0 && (
                <div className="bg-[var(--color-success-bg)] border border-[color-mix(in_srgb,var(--color-success)_35%,transparent)] rounded-lg p-3">
                  <div className="flex items-center gap-2 text-xs text-[var(--color-success)]">
                    <CheckCircle2 size={14} />
                    全量校验通过（共 {totalRows.toLocaleString()} 行），可以进入下一步
                  </div>
                </div>
              )}

              {validateResult?.valid && (validateResult.errors || []).length > 0 && (
                <div className="bg-[var(--color-warning-bg)] border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] rounded-lg p-3 space-y-1">
                  <div className="flex items-center gap-2 text-xs text-[var(--color-warning)] font-medium">
                    <AlertTriangle size={14} />
                    校验完成，但存在以下问题需要处理
                  </div>
                  {(validateResult.errors || []).map((e, i) => (
                    <div key={i} className="flex items-start gap-1.5 text-xs text-[var(--color-warning)] ml-6">
                      <span>{e.field_key && <span className="font-mono mr-1">[{e.field_key}]</span>}{e.message}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* ──────── 阶段 4: 确认配置 ──────── */}
          {step === 4 && (
            <div className="space-y-4">
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                <div className="border rounded-lg p-3">
                  <p className="text-xs text-[var(--color-text-tertiary)] mb-1">流水线名称</p>
                  <p className="text-sm font-medium text-foreground">{name || '未设置'}</p>
                </div>
                <div className="border rounded-lg p-3">
                  <p className="text-xs text-[var(--color-text-tertiary)] mb-1">描述</p>
                  <p className="text-sm text-foreground">{description || '未填写'}</p>
                </div>
              </div>

              {!multiOutput && columnDefs.length > 0 && (
                <div>
                  <p className="text-xs text-[var(--color-text-tertiary)] mb-2">字段契约预览{isPublished ? '（已封版）' : '（发布后封版）'}</p>
                  <div className="border rounded-lg overflow-x-auto max-h-[200px]">
                    <table className="w-full min-w-[560px] text-xs">
                      <thead className="bg-muted border-b sticky top-0">
                        <tr>
                          <th className="text-left px-3 py-1.5 font-medium text-muted-foreground">原始列名</th>
                          <th className="text-left px-3 py-1.5 font-medium text-muted-foreground">字段标识</th>
                          <th className="text-left px-3 py-1.5 font-medium text-muted-foreground">字段名称</th>
                          <th className="text-left px-3 py-1.5 font-medium text-muted-foreground">类型</th>
                          <th className="text-center px-3 py-1.5 font-medium text-muted-foreground">主键</th>
                          <th className="text-center px-3 py-1.5 font-medium text-muted-foreground">允许空</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y">
                        {columnDefs.map((d, i) => (
                          <tr key={i} className="hover:bg-muted">
                            <td className="px-3 py-1 font-mono text-[11px] text-muted-foreground">{d.source_key}</td>
                            <td className="px-3 py-1 font-mono text-[11px]">
                              {d.field_key}
                              {d.source_key !== d.field_key && <span className="ml-1 text-[var(--color-info)]">（改名）</span>}
                            </td>
                            <td className="px-3 py-1">{d.field_name}</td>
                            <td className="px-3 py-1">
                              <span className="px-1.5 py-0.5 bg-muted rounded text-[11px]">{d.field_type}</span>
                            </td>
                            <td className="px-3 py-1 text-center">
                              {d.is_primary_key ? <KeyRound size={12} className="text-[var(--color-warning)] inline" /> : '-'}
                            </td>
                            <td className="px-3 py-1 text-center">
                              {d.nullable ? '是' : <span className="text-[var(--color-danger)] font-medium">否</span>}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              {!isPublished && dryRunResult && (
                <DryRunPagedTable
                  pipelineId={pipeline.id}
                  dryRunId={dryRunResult.dry_run_id}
                  outputCount={dryRunResult.outputs.length}
                  title="执行预览数据"
                  description="第二步试执行的完整输出，分页读取，不会重新执行流水线"
                />
              )}

              {multiOutput && (
                <div className="bg-[var(--color-warning-bg)] border border-[color-mix(in_srgb,var(--color-warning)_35%,transparent)] rounded-lg px-3 py-2 text-xs text-[var(--color-warning)]">
                  多产物流水线：本次仅保存名称与描述，字段契约不适用（主键在任务/资产湖粒度管理）。
                </div>
              )}

              {!isPublished && isN8n && (
                <div className="flex items-start gap-2 rounded-xl border border-brand-line bg-brand-soft px-3.5 py-3 text-xs leading-5 text-brand-ink">
                  <Sparkles size={13} className="mt-0.5 shrink-0" />
                  <span>
                    发布会锁定当前 n8n 版本、唯一输入输出节点与字段契约。发布后不可撤回或编辑；
                    如需变更，请新建流水线并在验证通过后替换旧版本。
                  </span>
                </div>
              )}

              {isPublished && (
                <div className="flex items-start gap-2 rounded-xl border border-brand-line bg-brand-soft px-3.5 py-3">
                  <ShieldCheck size={15} className="mt-0.5 shrink-0 text-brand-ink" />
                  <div>
                    <p className="text-xs font-semibold text-brand-ink">已发布，版本已锁定</p>
                    <p className="mt-1 text-xs leading-5 text-brand-ink">
                      该版本只能执行、停用或归档，不能撤回到草稿。需要变更时请创建新的流水线版本。
                    </p>
                  </div>
                </div>
              )}

              {actionError && (
                <div className="bg-[var(--color-danger-bg)] border border-[color-mix(in_srgb,var(--color-danger)_35%,transparent)] rounded-lg px-3 py-2 text-xs text-[var(--color-danger)] flex items-start gap-1.5">
                  <XCircle size={13} className="mt-0.5 shrink-0" />
                  <span>{actionError}</span>
                </div>
              )}
            </div>
          )}
        </div>

        {/* 底部按钮 */}
        <div className="flex shrink-0 items-center justify-between border-t border-border bg-card px-6 py-4">
          <div>
            {step === 1 && (
              <button
                type="button"
                onClick={handleClose}
                className="flex h-10 items-center gap-1.5 rounded-xl border border-border bg-card px-4 text-sm font-medium text-muted-foreground transition-all duration-200 hover:border-border hover:bg-muted hover:text-foreground active:scale-[0.98]"
              >
                <X size={14} /> 取消
              </button>
            )}
            {step > 1 && (
              <button
                type="button"
                onClick={() => setStep((step - 1) as 1 | 2 | 3 | 4)}
                className="flex h-10 items-center gap-1.5 rounded-xl border border-border bg-card px-4 text-sm font-medium text-muted-foreground transition-all duration-200 hover:border-border hover:bg-muted hover:text-foreground active:scale-[0.98]"
              >
                <ChevronLeft size={14} /> 上一步
              </button>
            )}
          </div>
          <div className="flex items-center gap-2">
            {/* 第 3 步（草稿、契约适用）只能走「校验并下一步」——校验通过才能继续 */}
            {step < 4 && !(step === 3 && contractEditable) && (
              <button
                type="button"
                onClick={() => {
                  setValidateResult(null)
                  setStep((step + 1) as 1 | 2 | 3 | 4)
                }}
                disabled={step === 2 && dryRunPhase !== 'done'}
                className="flex h-10 items-center gap-1.5 rounded-xl bg-brand-deep px-4 text-sm font-medium text-[var(--color-text-inverse)] shadow-[0_6px_18px_rgba(15,118,110,0.16)] transition-all duration-200 hover:bg-brand-deep active:scale-[0.98] disabled:cursor-not-allowed disabled:bg-muted disabled:text-[var(--color-text-tertiary)] disabled:shadow-none"
              >
                下一步 <ChevronRight size={14} />
              </button>
            )}
            {step === 3 && contractEditable && (
              <button
                type="button"
                onClick={runValidate}
                disabled={validating}
                className="flex h-10 items-center gap-1.5 rounded-xl bg-brand-deep px-4 text-sm font-medium text-[var(--color-text-inverse)] shadow-[0_6px_18px_rgba(15,118,110,0.16)] transition-all duration-200 hover:bg-brand-deep active:scale-[0.98] disabled:cursor-not-allowed disabled:bg-muted disabled:text-[var(--color-text-tertiary)] disabled:shadow-none"
              >
                {validating ? (
                  <><Loader2 size={14} className="animate-spin" /> 全量校验中…</>
                ) : (
                  '校验并下一步'
                )}
              </button>
            )}
            {step === 4 && !isPublished && (
              <>
                <button
                  type="button"
                  onClick={handleSave}
                  disabled={saving || publishing}
                  className="flex h-10 items-center gap-1.5 rounded-xl border border-border bg-card px-4 text-sm font-medium text-muted-foreground transition-all duration-200 hover:border-border hover:bg-muted hover:text-foreground active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
                  保存草稿
                </button>
                <button
                  type="button"
                  onClick={handlePublish}
                  disabled={publishing || saving}
                  className="flex h-10 items-center gap-1.5 rounded-xl bg-brand-deep px-4 text-sm font-medium text-[var(--color-text-inverse)] shadow-[0_6px_18px_rgba(15,118,110,0.16)] transition-all duration-200 hover:bg-brand-deep active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {publishing ? <Loader2 size={14} className="animate-spin" /> : <Rocket size={14} />}
                  发布
                </button>
              </>
            )}
            {step === 1 && (
              <button
                type="button"
                onClick={handleSaveBasicInfo}
                disabled={saving || !infoDirty || !name.trim()}
                className="flex h-10 items-center gap-1.5 rounded-xl bg-brand-deep px-4 text-sm font-medium text-[var(--color-text-inverse)] shadow-[0_6px_18px_rgba(15,118,110,0.16)] transition-all duration-200 hover:bg-brand-deep active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40"
              >
                {saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
                保存基础信息
              </button>
            )}
            {step === 4 && isPublished && (
              <button
                type="button"
                onClick={infoDirty ? handleSaveBasicInfo : onClose}
                disabled={saving || !name.trim()}
                className="flex h-10 items-center gap-1.5 rounded-xl bg-brand-deep px-4 text-sm font-medium text-[var(--color-text-inverse)] shadow-[0_6px_18px_rgba(15,118,110,0.16)] transition-all duration-200 hover:bg-brand-deep active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40"
              >
                {saving ? <Loader2 size={14} className="animate-spin" /> : infoDirty ? <Save size={14} /> : null}
                {infoDirty ? '保存并完成' : '完成'}
              </button>
            )}
          </div>
        </div>
      </div>

      <ConfirmDialog
        open={pendingConfirm !== null}
        onClose={() => setPendingConfirm(null)}
        onConfirm={() => { const c = pendingConfirm; setPendingConfirm(null); c?.action() }}
        title={pendingConfirm?.title ?? ''}
        description={pendingConfirm?.description}
        variant="warning"
      />
    </div>,
    document.body,
  )
}

/** 采样数据表：水平滚动 + 吸顶表头 */
function SampleTable({ columns, rows, emptyText }: {
  columns: string[]
  rows: Record<string, unknown>[]
  emptyText: string
}) {
  if (columns.length === 0) return null
  const tableMinWidth = 52 + columns.length * 168
  return (
    <div className="border rounded-lg overflow-hidden">
      <div className="overflow-x-auto max-h-[400px]">
        <table className="w-full table-fixed text-xs" style={{ minWidth: tableMinWidth }}>
          <colgroup>
            <col style={{ width: 52 }} />
            {columns.map(col => <col key={col} style={{ width: 168 }} />)}
          </colgroup>
          <thead className="bg-muted border-b sticky top-0">
            <tr>
              <th className="text-left px-3 py-2 font-medium text-muted-foreground w-10">#</th>
              {columns.map(col => (
                <th key={col} className="overflow-hidden text-ellipsis whitespace-nowrap px-3 py-2 text-left font-medium text-muted-foreground" title={col}>
                  {col}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y">
            {rows.length === 0 ? (
              <tr>
                <td colSpan={columns.length + 1} className="px-3 py-8 text-center text-[var(--color-text-tertiary)]">
                  {emptyText}
                </td>
              </tr>
            ) : (
              rows.map((row, i) => (
                <tr key={i} className="hover:bg-muted">
                  <td className="px-3 py-1.5 text-[var(--color-text-tertiary)]">{i + 1}</td>
                  {columns.map(col => (
                    <td key={col} className="px-3 py-1.5 text-foreground whitespace-nowrap overflow-hidden text-ellipsis" title={String(row[col] ?? '')}>
                      {row[col] === null || row[col] === undefined ? (
                        <span className="text-[var(--color-text-tertiary)] italic">null</span>
                      ) : (
                        String(row[col])
                      )}
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

/** 全量数据分页浏览：读取 dry-run 暂存（服务端分页，不重跑流水线） */
function DryRunPagedTable({ pipelineId, dryRunId, outputCount, onCollapse, title = '全量数据浏览', description }: {
  pipelineId: string
  dryRunId: string
  outputCount: number
  onCollapse?: () => void
  title?: string
  description?: string
}) {
  const [outputIndex, setOutputIndex] = useState(0)
  const [page, setPage] = useState(1)
  const [data, setData] = useState<DryRunRowsPage | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const pageSize = 50

  useEffect(() => {
    let alive = true
    setLoading(true)
    setError('')
    pipelinesApi.dryRunRows(pipelineId, dryRunId, { output_index: outputIndex, page, page_size: pageSize })
      .then(res => { if (alive) setData(res) })
      .catch((e: { detail?: string; message?: string }) => {
        if (alive) setError(e?.detail || e?.message || '加载失败')
      })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [pipelineId, dryRunId, outputIndex, page])

  const total = data?.total ?? 0
  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const columns = data?.columns ?? []
  const tableMinWidth = 52 + columns.length * 168

  return (
    <div className="overflow-hidden rounded-xl border border-border bg-card">
      <div className="flex items-center gap-2 border-b border-border bg-muted px-3.5 py-2.5 text-xs text-muted-foreground">
        <Table2 size={12} className="text-[var(--color-text-tertiary)]" />
        <div className="min-w-0">
          <p className="font-medium text-foreground">{title}</p>
          {description && <p className="mt-0.5 truncate text-[11px] text-[var(--color-text-tertiary)]">{description}</p>}
        </div>
        <div className="ml-auto flex shrink-0 items-center gap-3">
          {outputCount > 1 && (
            <Select
              value={String(outputIndex)}
              onValueChange={value => { setOutputIndex(Number(value)); setPage(1) }}
            >
              <SelectTrigger className="w-fit rounded-lg bg-card px-2 py-1 text-xs" aria-label="选择产物">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {Array.from({ length: outputCount }, (_, i) => (
                  <SelectItem key={i} value={String(i)}>产物 {i + 1}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          {onCollapse && (
            <button type="button" onClick={onCollapse} className="text-brand-ink transition hover:text-brand-ink hover:underline">
              收起
            </button>
          )}
        </div>
      </div>
      <div className="overflow-x-auto max-h-[400px] overflow-y-auto">
        {loading ? (
          <div className="py-10 text-center text-xs text-[var(--color-text-tertiary)] flex items-center justify-center gap-1.5">
            <Loader2 size={13} className="animate-spin" /> 加载数据…
          </div>
        ) : error ? (
          <div className="py-10 text-center text-xs text-[var(--color-danger)]">{error}</div>
        ) : !data || data.rows.length === 0 ? (
          <div className="py-10 text-center text-xs text-[var(--color-text-tertiary)]">该页没有数据</div>
        ) : (
          <table className="w-full table-fixed text-xs" style={{ minWidth: tableMinWidth }}>
            <colgroup>
              <col style={{ width: 52 }} />
              {columns.map(c => <col key={c} style={{ width: 168 }} />)}
            </colgroup>
            <thead className="bg-muted sticky top-0">
              <tr>
                <th className="text-left px-3 py-2 font-medium text-muted-foreground border-b whitespace-nowrap w-12">#</th>
                {columns.map(c => (
                  <th key={c} className="overflow-hidden text-ellipsis whitespace-nowrap border-b px-3 py-2 text-left font-medium text-muted-foreground" title={c}>{c}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y">
              {data.rows.map((row, ri) => (
                <tr key={ri} className="hover:bg-muted">
                  <td className="px-3 py-1.5 text-[var(--color-text-tertiary)] whitespace-nowrap tabular-nums">
                    {(page - 1) * pageSize + ri + 1}
                  </td>
                  {columns.map(c => (
                    <td key={c} className="px-3 py-1.5 text-foreground whitespace-nowrap max-w-[220px] overflow-hidden text-ellipsis" title={String(row[c] ?? '')}>
                      {row[c] === null || row[c] === undefined ? (
                        <span className="text-[var(--color-text-tertiary)] italic">null</span>
                      ) : (
                        String(row[c])
                      )}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <div className="flex items-center justify-between px-3 py-2 border-t bg-muted text-xs text-[var(--color-text-tertiary)]">
        <span>共 {total.toLocaleString()} 行 · 第 {page}/{totalPages} 页</span>
        <div className="flex items-center gap-1">
          <button
            disabled={page <= 1 || loading}
            onClick={() => setPage(p => Math.max(1, p - 1))}
            className="p-1 rounded hover:bg-card disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <ChevronLeft size={14} />
          </button>
          <button
            disabled={page >= totalPages || loading}
            onClick={() => setPage(p => Math.min(totalPages, p + 1))}
            className="p-1 rounded hover:bg-card disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <ChevronRight size={14} />
          </button>
        </div>
      </div>
    </div>
  )
}
