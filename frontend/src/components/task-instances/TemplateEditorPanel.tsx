/**
 * YAML 编排面板（自 TemplateEditorView 抽取，YIYA-44/45）：
 * 弹窗内「编辑模板」与独立编排页共用同一套 YAML 加载/校验/保存/版本/预览能力。
 * 左列版本历史（revision/canonical_hash/行级 diff/回滚）；
 * 中央 CodeMirror YAML 编辑器（语法高亮 + 节点片段插入）；
 * 右侧校验面板（后端 validate 结构化错误定位）；
 * 底部编译预览图（只读 DAG，与实例详情共享组件）。
 * 保存 = 既有 template ID 的新 revision（canonical_hash 幂等：内容不变不建版），
 * 编辑原文 YAML 不丢未知字段；预置模板保存/回滚入口禁用（服务端 403 是最终防线）。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import CodeMirror, { type ReactCodeMirrorRef } from '@uiw/react-codemirror'
import { yaml } from '@codemirror/lang-yaml'
import {
  ArrowLeft, CheckCircle2, FileCode2, GitCompare, History, Save,
  ShieldAlert, Undo2,
} from 'lucide-react'
import { toast } from 'sonner'
import {
  apiError, taskInstancesApi,
  type TemplateDetail, type TemplateRevision,
} from '@/api/taskInstances'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { formatDateTime } from '@/utils/datetime'
import { lineDiff } from '@/components/task-instances/instanceView'
import { yamlEditorTheme, yamlHighlight } from '@/components/task-instances/yamlEditorTheme'
import * as copy from '@/components/task-instances/copy'
import WorkflowPreviewGraph from '@/components/task-instances/WorkflowPreviewGraph'

const SPEC_SNIPPETS: Array<{ label: string; code: string }> = [
  { label: 'Agent 节点', code: '  <id>:\n    kind: agent\n    system: 执行说明\n    outputs:\n      done: { contract: <契约名> }' },
  { label: 'human 节点', code: '  <id>:\n    kind: human\n    role: 角色说明' },
  { label: 'approval 节点', code: '  <id>:\n    kind: approval\n    approvers: [admin]' },
  { label: 'condition 节点', code: '  <id>:\n    kind: condition\n    on: <上游>.done\n    branches:\n      - { when: { field: <字段>, equals: <值> }, to: [<目标>] }\n    default: [<目标>]' },
  { label: 'join 节点', code: '  <id>:\n    kind: join\n    mode: all' },
  { label: 'terminal 节点', code: '  <id>: { kind: terminal, outcome: success }' },
  { label: '契约', code: '  <契约名>:\n    type: object\n    required: [<字段>]\n    properties:\n      <字段>: { type: string, minLength: 1 }' },
  { label: '退回边', code: '  - { from: <审批>.rejected, to: <目标>, rework: true }' },
]

const STARTER_SPEC = `api_version: openontology.task/v1
kind: Workflow
metadata:
  name: 未命名流程
nodes:
  analyze:
    kind: agent
    system: 执行说明
  gate:
    kind: approval
    approvers: [admin]
  end_ok: { kind: terminal, outcome: success }
edges:
  - { from: analyze.done, to: gate }
  - { from: gate.approved, to: end_ok }
`

interface ValidationIssue {
  code: string
  message: string
  ref?: string
  line?: number
}

function extractIssues(error: unknown): ValidationIssue[] {
  const detail = (error as { detail?: unknown }).detail
  if (detail && typeof detail === 'object') {
    const parsed = detail as { errors?: ValidationIssue[]; message?: string }
    if (Array.isArray(parsed.errors)) return parsed.errors
    if (parsed.message) return [{ code: 'TEMPLATE_INVALID', message: parsed.message }]
  }
  return [{ code: 'REQUEST_FAILED', message: apiError(error) }]
}

export interface TemplateEditorPanelProps {
  /** 既有模板 ID：编辑模式（新 revision）。缺省为创建模式（仅独立页使用）。 */
  templateId?: string
  /** 退出编辑（返回列表/关闭视图）；有未保存修改时面板内先行确认 */
  onExit: () => void
  /** 保存成功后回调（面板已失效模板列表与详情缓存） */
  onSaved?: (template: TemplateDetail) => void
  /** 脏稿变化上报（宿主对「离开编辑态」的外部出口做统一确认守卫） */
  onDirtyChange?: (dirty: boolean) => void
  exitLabel?: string
}

export default function TemplateEditorPanel({
  templateId, onExit, onSaved, onDirtyChange, exitLabel = copy.RETURN_TO_LIST,
}: TemplateEditorPanelProps) {
  const queryClient = useQueryClient()
  const isNew = !templateId
  const [specYaml, setSpecYaml] = useState(STARTER_SPEC)
  const [savedYaml, setSavedYaml] = useState('')
  const [issues, setIssues] = useState<ValidationIssue[] | null>(null)
  const [showRevisions, setShowRevisions] = useState(false)
  const [diffRevision, setDiffRevision] = useState<TemplateRevision | null>(null)
  const [confirmRestore, setConfirmRestore] = useState<TemplateRevision | null>(null)
  const [confirmExit, setConfirmExit] = useState(false)
  const editorRef = useRef<ReactCodeMirrorRef>(null)

  const template = useQuery({
    queryKey: ['task-instances', 'template', templateId],
    enabled: Boolean(templateId),
    queryFn: () => taskInstancesApi.getTemplate(templateId as string),
  })

  useEffect(() => {
    if (template.data?.spec_yaml !== undefined) {
      setSpecYaml(template.data.spec_yaml)
      setSavedYaml(template.data.spec_yaml)
    }
  }, [template.data?.spec_yaml])

  // 预置模板：只读保护（保存/回滚入口禁用；服务端 403 兜底，含直接 API）
  const presetLocked = template.data?.is_preset === true

  const revisions = useQuery({
    queryKey: ['task-instances', 'template-revisions', templateId],
    enabled: Boolean(templateId) && showRevisions,
    queryFn: () => taskInstancesApi.listRevisions(templateId as string),
  })

  const save = useMutation({
    mutationFn: async () => {
      if (isNew) return taskInstancesApi.createTemplate(specYaml)
      return taskInstancesApi.updateTemplate(templateId as string, specYaml)
    },
    onSuccess: data => {
      toast.success(isNew ? '模板已创建' : '已保存')
      setSavedYaml(specYaml)
      setIssues(null)
      void queryClient.invalidateQueries({ queryKey: ['task-instances', 'templates'] })
      void queryClient.invalidateQueries({
        queryKey: ['task-instances', 'template', templateId] })
      onSaved?.(data)
    },
    onError: error => {
      setIssues(extractIssues(error))
      toast.error('保存未通过校验')
    },
  })

  const runValidate = async () => {
    try {
      await taskInstancesApi.validateTemplate(specYaml)
      setIssues([])
      toast.success('校验通过')
    } catch (error) {
      setIssues(extractIssues(error))
      toast.error('校验未通过')
    }
  }

  const insertSnippet = (code: string) => {
    const view = editorRef.current?.view
    if (!view) return
    const pos = view.state.selection.main.to
    view.dispatch({
      changes: { from: pos, insert: code + '\n' },
      selection: { anchor: pos + code.length + 1 },
    })
    view.focus()
  }

  const dirty = specYaml !== savedYaml
  const diffLines = useMemo(
    () => diffRevision ? lineDiff(diffRevision.spec_yaml ?? '', specYaml) : [],
    [diffRevision, specYaml])

  // 脏稿上报：宿主（弹窗）对外部出口（关闭/左栏切视图/Esc）做统一确认守卫
  useEffect(() => {
    onDirtyChange?.(dirty)
  }, [dirty, onDirtyChange])

  const previewSpec = template.data?.spec_compiled ?? null

  const requestExit = () => {
    if (dirty) {
      setConfirmExit(true)
      return
    }
    onExit()
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="ghost" size="sm" onClick={requestExit} className="gap-1.5">
          <ArrowLeft className="h-4 w-4" />{exitLabel}
        </Button>
        <div className="flex items-center gap-2">
          <FileCode2 className="h-4 w-4 text-[var(--color-text-tertiary)]" />
          <span className="text-sm font-medium text-[var(--color-text-primary)]">
            {isNew ? copy.TEMPLATE_NEW_BUTTON : (template.data?.name ?? '流程模板')}
          </span>
          {dirty ? (
            <span className="rounded bg-[var(--color-warning-bg)] px-1.5 py-0.5 text-[11px] text-[var(--color-warning)]">未保存</span>
          ) : null}
          {presetLocked ? (
            <span className="rounded bg-brand-soft px-1.5 py-0.5 text-[11px] font-medium text-brand-ink">
              {copy.TEMPLATE_SOURCE_PRESET}
            </span>
          ) : null}
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={runValidate} className="gap-1.5">
            <ShieldAlert className="h-4 w-4" />校验
          </Button>
          <span title={presetLocked ? copy.TEMPLATE_PRESET_EDIT_DISABLED : undefined}>
            <Button
              size="sm"
              onClick={() => save.mutate()}
              disabled={save.isPending || presetLocked}
              aria-disabled={presetLocked}
              aria-label={presetLocked
                ? `保存新版本（${copy.TEMPLATE_PRESET_EDIT_DISABLED}）`
                : undefined}
              className="gap-1.5 disabled:cursor-not-allowed"
            >
              <Save className="h-4 w-4" />{isNew ? '创建' : '保存新版本'}
            </Button>
          </span>
        </div>
      </div>
      {presetLocked ? (
        <p className="rounded-md bg-[var(--color-warning-bg)] px-3 py-2 text-xs text-[var(--color-warning)]">
          {copy.TEMPLATE_PRESET_EDIT_DISABLED}：预置模板由平台统一提供，可查看与激活，但不能修改或回滚。
        </p>
      ) : null}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[220px_minmax(0,1fr)_320px]">
        {/* 左列：版本历史 */}
        <Card className="h-fit">
          <CardContent className="space-y-2">
            <button
              type="button"
              className="flex w-full items-center gap-1.5 text-sm font-medium text-[var(--color-text-primary)]"
              onClick={() => setShowRevisions(previous => !previous)}
            >
              <History className="h-4 w-4 text-[var(--color-text-tertiary)]" />版本历史
            </button>
            {templateId && showRevisions ? (
              revisions.isLoading ? (
                <p className="text-xs text-[var(--color-text-tertiary)]">加载中…</p>
              ) : (
                <div className="space-y-1.5">
                  {(revisions.data ?? []).map(revision => (
                    <div key={revision.id} className="rounded-md border border-border px-2.5 py-2 text-xs">
                      <div className="flex items-center justify-between">
                        <span className="font-medium text-[var(--color-text-primary)]">v{revision.revision_no}</span>
                        <span className="font-mono text-[10px] text-[var(--color-text-tertiary)]">
                          {revision.canonical_hash.slice(0, 8)}
                        </span>
                      </div>
                      <div className="mt-0.5 text-[var(--color-text-tertiary)]">
                        {formatDateTime(revision.created_at)}
                      </div>
                      <div className="mt-1.5 flex gap-1.5">
                        <Button variant="ghost" size="sm" className="h-6 gap-1 px-1.5 text-[11px]"
                          onClick={() => setDiffRevision(diffRevision?.id === revision.id ? null : revision)}>
                          <GitCompare className="h-3 w-3" />对比
                        </Button>
                        <span title={presetLocked ? copy.TEMPLATE_PRESET_EDIT_DISABLED : undefined}>
                          <Button variant="ghost" size="sm" className="h-6 gap-1 px-1.5 text-[11px]"
                            disabled={presetLocked}
                            aria-label={presetLocked
                              ? `回滚到 v${revision.revision_no}（${copy.TEMPLATE_PRESET_EDIT_DISABLED}）`
                              : undefined}
                            onClick={() => setConfirmRestore(revision)}>
                            <Undo2 className="h-3 w-3" />回滚
                          </Button>
                        </span>
                      </div>
                    </div>
                  ))}
                  {!revisions.data?.length ? (
                    <p className="text-xs text-[var(--color-text-tertiary)]">暂无版本</p>
                  ) : null}
                </div>
              )
            ) : (
              <p className="text-xs text-[var(--color-text-tertiary)]">
                保存后自动累积不可变版本；内容不变不建新版。
              </p>
            )}
          </CardContent>
        </Card>

        {/* 中央：编辑器 + diff */}
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-1.5">
            {SPEC_SNIPPETS.map(snippet => (
              <Button key={snippet.label} variant="outline" size="sm"
                className="h-7 px-2 text-[11px]" onClick={() => insertSnippet(snippet.code)}>
                + {snippet.label}
              </Button>
            ))}
          </div>
          <div className="overflow-hidden rounded-lg border border-border">
            <CodeMirror
              ref={editorRef}
              value={specYaml}
              onChange={setSpecYaml}
              extensions={[yaml(), yamlEditorTheme, yamlHighlight]}
              height="480px"
              aria-label="WorkflowSpec YAML 编辑器"
            />
          </div>
          {diffRevision ? (
            <Card>
              <CardContent>
                <div className="mb-2 flex items-center justify-between">
                  <span className="text-sm font-medium">当前草稿 与 v{diffRevision.revision_no} 对比</span>
                  <Button variant="ghost" size="sm" className="h-6 text-[11px]"
                    onClick={() => setDiffRevision(null)}>关闭</Button>
                </div>
                <pre className="max-h-56 overflow-auto rounded bg-[var(--color-bg-hover)] p-2 font-mono text-xs">
                  {diffLines.map((line, index) => (
                    <div key={index} className={
                      line.sign === '+' ? 'text-[var(--color-success)]'
                        : line.sign === '-' ? 'text-[var(--color-danger)]'
                          : 'text-[var(--color-text-secondary)]'}>
                      {line.sign}{line.text}
                    </div>
                  ))}
                </pre>
              </CardContent>
            </Card>
          ) : null}
        </div>

        {/* 右列：校验面板 */}
        <Card className="h-fit">
          <CardContent className="space-y-2">
            <div className="text-sm font-medium">校验面板</div>
            {issues === null ? (
              <p className="text-xs text-[var(--color-text-tertiary)]">
                点击「校验」做服务端结构 + 语义检查（环/端口/契约引用/兜底路由）。
              </p>
            ) : !issues.length ? (
              <div className="flex items-center gap-1.5 text-xs text-[var(--color-success)]">
                <CheckCircle2 className="h-4 w-4" />校验通过，可以保存
              </div>
            ) : (
              <div className="space-y-1.5">
                {issues.map((issue, index) => (
                  <div key={index} className="rounded-md bg-[var(--color-danger-bg)] px-2 py-1.5 text-xs">
                    <div className="font-mono text-[10px] text-[var(--color-danger)]">{issue.code}</div>
                    <div className="text-[var(--color-text-secondary)]">{issue.message}</div>
                    {issue.ref ? (
                      <div className="font-mono text-[10px] text-[var(--color-text-tertiary)]">{issue.ref}</div>
                    ) : null}
                  </div>
                ))}
              </div>
            )}
            <div className="border-t border-border pt-2">
              <p className="text-xs text-[var(--color-text-tertiary)]">
                契约可选、逐端口收紧；退回边携带理由触发上游重做（退回重做环）。
              </p>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* 底部：编译预览图（保存后快照） */}
      <Card>
        <CardContent className="space-y-2">
          <div className="text-sm font-medium">编译预览</div>
          {previewSpec ? (
            <WorkflowPreviewGraph spec={previewSpec} />
          ) : (
            <p className="text-xs text-[var(--color-text-tertiary)]">
              保存后此处渲染只读 DAG 预览；草稿态以编辑器内容为准。
            </p>
          )}
        </CardContent>
      </Card>

      <ConfirmDialog
        open={confirmRestore !== null}
        title={`回滚到 v${confirmRestore?.revision_no ?? ''}`}
        description="将以该版本内容填充编辑器；确认后再点「保存新版本」（内容相同则幂等命中既有版本）。"
        confirmText="载入编辑器"
        onClose={() => setConfirmRestore(null)}
        onConfirm={() => {
          if (confirmRestore?.spec_yaml) {
            setSpecYaml(confirmRestore.spec_yaml)
            toast.info('已载入目标版本内容，保存后生效')
          }
          setConfirmRestore(null)
        }}
      />
      <ConfirmDialog
        open={confirmExit}
        title="放弃未保存的修改"
        description="编辑器中有未保存的修改；退出后这些修改不会保留。"
        confirmText="放弃修改并退出"
        variant="warning"
        onClose={() => setConfirmExit(false)}
        onConfirm={() => {
          setConfirmExit(false)
          onExit()
        }}
      />
    </div>
  )
}
