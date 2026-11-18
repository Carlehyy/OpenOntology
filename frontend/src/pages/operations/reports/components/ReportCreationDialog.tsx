import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Bot, Code2, Database, LayoutTemplate } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Modal } from '@/components/ui/Modal'
import { Alert } from '@/components/ui/Alert'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import operationsReportsApi, { operationsApiErrorText, type OperationsArticleCreate } from '@/api/v2/operations-reports'
import { emptyReportPlan, planFromTemplate, planPayload, REPORT_PLAN_TEMPLATES, validateReportPlan, type ReportPlan } from '../reportPlanModel'
import ReportAIPanel from './ReportAIPanel'
import ReportPlanEditor from './ReportPlanEditor'
import ReportPlanPreview from './ReportPlanPreview'

type CreationMode = 'ai' | 'asset' | 'template' | 'markdown'
const CREATION_MODES = [
  { id: 'ai', name: 'AI 设计报告', detail: '描述目标，审阅并接受候选', icon: Bot },
  { id: 'asset', name: '从资产搭建', detail: '选择字段、指标、图表与筛选', icon: Database },
  { id: 'template', name: '从模板开始', detail: '基于数据概览或趋势调整', icon: LayoutTemplate },
  { id: 'markdown', name: '高级 Markdown', detail: '编写 Markdown 与受限 SQL', icon: Code2 },
] as const

function changedAt(value: string | null | undefined) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { hour12: false })
}

export default function ReportCreationDialog({ reportId, saving, error, onCreate, onClose }: {
  reportId: string
  saving: boolean
  error: unknown
  onCreate: (payload: OperationsArticleCreate) => void
  onClose: () => void
}) {
  const [title, setTitle] = useState('')
  const [datasetId, setDatasetId] = useState('')
  const [mode, setMode] = useState<CreationMode>('asset')
  const [templateId, setTemplateId] = useState('overview')
  const [plan, setPlan] = useState<ReportPlan>(emptyReportPlan)
  const [acceptedAI, setAcceptedAI] = useState(false)
  const [testedPlan, setTestedPlan] = useState('')
  const datasets = useQuery({ queryKey: ['operations-report-datasets'], queryFn: () => operationsReportsApi.datasets() })
  const selected = datasets.data?.items.find(dataset => dataset.id === datasetId)
  const needsSchema = !!selected?.reportable && !selected.schema?.columns.length
  const schemaQuery = useQuery({
    queryKey: ['operations-report-dataset-schema', datasetId],
    queryFn: () => operationsReportsApi.datasetSchema(datasetId),
    enabled: needsSchema,
  })
  const columns = selected?.schema?.columns.length ? selected.schema.columns : (schemaQuery.data?.columns ?? [])
  const loadedSchema = useRef('')
  useEffect(() => {
    if (!needsSchema || !schemaQuery.data || loadedSchema.current === datasetId) return
    loadedSchema.current = datasetId
    const template = REPORT_PLAN_TEMPLATES.find(item => item.id === (mode === 'template' ? templateId : 'overview'))!
    if (mode === 'asset' || (mode === 'template' && !template.disabledReason?.(columns))) {
      setPlan(planFromTemplate(template, columns))
      setTestedPlan('')
    }
  }, [needsSchema, schemaQuery.data, datasetId, mode, templateId, columns])
  const issues = useMemo(() => validateReportPlan(plan, columns), [plan, columns])
  const fingerprint = JSON.stringify(plan)
  const ready = selected?.reportable === true && (!needsSchema || schemaQuery.isSuccess)
  const canCreate = !!title.trim() && ready && (mode === 'markdown' || (
    !issues.length && testedPlan === fingerprint && (mode !== 'ai' || acceptedAI)
  ))

  const initialize = (nextMode: CreationMode, nextDatasetId: string, nextTemplate = templateId) => {
    if (nextDatasetId !== datasetId) loadedSchema.current = ''
    const dataset = datasets.data?.items.find(item => item.id === nextDatasetId)
    const template = REPORT_PLAN_TEMPLATES.find(item => item.id === (nextMode === 'template' ? nextTemplate : 'overview'))!
    const nextColumns = dataset?.schema?.columns.length ? dataset.schema.columns : (nextDatasetId === datasetId ? columns : [])
    setPlan(nextMode === 'ai' || !dataset || template.disabledReason?.(nextColumns)
      ? emptyReportPlan()
      : planFromTemplate(template, nextColumns))
    setAcceptedAI(false)
    setTestedPlan('')
  }

  const submit = () => {
    if (!canCreate) return
    const common = { title: title.trim(), dataset_id: datasetId }
    onCreate(mode === 'markdown' ? common : { ...common, plan: planPayload(plan) })
  }

  return (
    <Modal
      open
      onClose={onClose}
      disableClose={saving}
      title="新建文章"
      description="选择数据资产与创作方式，预览后确认创建。"
      size="3xl"
      panelClassName="max-w-6xl"
      footer={(
        <>
          <Button variant="outline" onClick={onClose} disabled={saving}>取消</Button>
          <Button onClick={submit} loading={saving} disabled={!canCreate} data-testid="report-create-article-submit">创建文章</Button>
        </>
      )}
    >
      <div className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-muted-foreground">文章标题</span>
            <input value={title} maxLength={240} disabled={saving} onChange={event => setTitle(event.target.value)} placeholder="例如：第 47 周运营周报" aria-label="文章标题" className="h-9 w-full rounded-lg border border-border bg-card px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" />
          </label>
          <div>
            <span className="mb-1 block text-xs font-medium text-muted-foreground">绑定数据资产</span>
            <Select value={datasetId} disabled={saving || datasets.isLoading} onValueChange={value => { setDatasetId(value); initialize(mode, value) }}>
              <SelectTrigger className="h-9 w-full bg-card text-sm" aria-label="选择数据集"><SelectValue placeholder={datasets.isLoading ? '加载数据资产…' : '选择数据集'} /></SelectTrigger>
              <SelectContent>
                {(datasets.data?.items ?? []).map(dataset => <SelectItem key={dataset.id} value={dataset.id} disabled={!dataset.reportable}>{dataset.name}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>
        </div>
        {datasets.isError && <Alert variant="danger" role="alert">数据资产加载失败：{operationsApiErrorText(datasets.error)} <Button variant="link" size="sm" onClick={() => void datasets.refetch()}>重试</Button></Alert>}
        {needsSchema && schemaQuery.isPending && <Alert variant="info">正在读取所选资产字段…</Alert>}
        {needsSchema && schemaQuery.isError && <Alert variant="danger" role="alert">字段读取失败：{operationsApiErrorText(schemaQuery.error)} <Button variant="link" size="sm" onClick={() => void schemaQuery.refetch()}>重试</Button></Alert>}
        {datasets.data && datasets.data.items.length === 0 && <Alert variant="info">资产湖中尚无结构化、集成或成品数据集，请先准备数据资产。</Alert>}
        {datasets.data?.items.some(dataset => !dataset.reportable) && (
          <details className="rounded-lg border border-border px-3 py-2" open>
            <summary className="cursor-pointer text-xs text-muted-foreground">暂不可用的资产</summary>
            <ul className="mt-2 space-y-1 text-xs text-muted-foreground">{datasets.data.items.filter(dataset => !dataset.reportable).map(dataset => <li key={dataset.id}>{dataset.name}：{dataset.reason || '资产尚未就绪'}</li>)}</ul>
          </details>
        )}
        {selected && (
          <div className="rounded-lg border border-border bg-muted px-3 py-2 text-xs text-muted-foreground" data-testid="report-create-asset-meta">
            <p>{selected.kind === 'curated' ? '成品数据集' : selected.kind === 'integrated' ? '集成数据集' : '结构化数据集'} · {ready ? '可查询' : '暂不可用'} · v{selected.version?.version_no ?? '—'} · {columns.length} 个字段{selected.version?.rowcount != null ? ` · ${selected.version.rowcount} 行` : ''}</p>
            <p className="mt-1">数据时间 {changedAt(selected.version?.as_of)} · 资产更新时间 {changedAt(selected.updated_at)}</p>
            <p className="mt-1 break-words">{columns.map(column => `${column.name} (${column.type})`).join('、')}</p>
          </div>
        )}
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4" role="group" aria-label="选择报告创作方式">
          {CREATION_MODES.map(item => (
            <button type="button" key={item.id} disabled={saving} aria-pressed={mode === item.id} onClick={() => { if (mode === item.id) return; setMode(item.id); initialize(item.id, datasetId) }} data-testid={`report-create-mode-${item.id}`} className={'rounded-lg border p-3 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50 ' + (mode === item.id ? 'border-brand-line bg-brand-soft text-brand-ink' : 'border-border bg-card text-foreground hover:bg-muted')}>
              <span className="flex items-center gap-2 text-sm font-medium"><item.icon size={15} />{item.name}</span>
              <span className="mt-1 block text-xs text-muted-foreground">{item.detail}</span>
            </button>
          ))}
        </div>
        {!!error && <Alert variant="danger" role="alert">创建失败：{operationsApiErrorText(error)}</Alert>}
        {ready && mode === 'markdown' && <Alert variant="info">创建后进入文章，可在“编辑文章”中编写 Markdown、试跑命名 SQL 并保存草稿。</Alert>}
        {ready && mode !== 'markdown' && (
          <>
            {mode === 'template' && (
              <div className="flex flex-wrap gap-2" role="group" aria-label="报告模板">
                {REPORT_PLAN_TEMPLATES.map(template => {
                  const reason = template.disabledReason?.(columns)
                  return <Button key={template.id} size="sm" variant={templateId === template.id ? 'default' : 'outline'} aria-pressed={templateId === template.id} disabled={saving || !!reason} title={reason || template.description} onClick={() => { if (templateId === template.id) return; setTemplateId(template.id); initialize(mode, datasetId, template.id) }}>{template.name}{reason ? `（${reason}）` : ''}</Button>
                })}
              </div>
            )}
            {mode === 'ai' && <ReportAIPanel key={datasetId} reportId={reportId} datasetId={datasetId} datasetName={selected.name} columns={columns} plan={plan} onAccept={candidate => { setPlan(candidate); setAcceptedAI(true); setTestedPlan(JSON.stringify(candidate)) }} />}
            {(mode !== 'ai' || acceptedAI) && (
              <div className="grid items-start gap-4 lg:grid-cols-2">
                <ReportPlanEditor plan={plan} columns={columns} onChange={setPlan} readOnly={saving} />
                <div className="min-w-0 space-y-2">
                  <p className="text-xs text-muted-foreground">点击“查询”预览当前计划，通过后可确认创建。</p>
                  <ReportPlanPreview reportId={reportId} datasetId={datasetId} datasetName={selected.name} plan={plan} onSuccess={() => setTestedPlan(fingerprint)} onInvalidate={() => setTestedPlan('')} />
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </Modal>
  )
}
