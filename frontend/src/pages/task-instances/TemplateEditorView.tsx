/**
 * YAML 编排页（作者面核心，设计方案 §10-2）：
 * 左列版本历史（revision/canonical_hash/行级 diff/回滚）；
 * 中央 CodeMirror YAML 编辑器（语法高亮 + 节点片段插入）；
 * 右侧校验面板（后端 validate 结构化错误定位）；
 * 底部编译预览图（只读 DAG，与实例详情共享组件）。
 * 保存 = 新 revision（canonical_hash 幂等：内容不变不建版）。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import CodeMirror, { type ReactCodeMirrorRef } from '@uiw/react-codemirror'
import { EditorView } from '@codemirror/view'
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language'
import { tags } from '@lezer/highlight'
import { yaml } from '@codemirror/lang-yaml'
import {
  ArrowLeft, CheckCircle2, FileCode2, GitCompare, History, Save,
  ShieldAlert, Undo2,
} from 'lucide-react'
import { toast } from 'sonner'
import {
  apiError, taskInstancesApi,
  type TemplateRevision, type WorkflowSpecSnapshot,
} from '@/api/taskInstances'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { formatDateTime } from '@/utils/datetime'
import { lineDiff } from './instanceView'
import WorkflowPreviewGraph from './WorkflowPreviewGraph'

// 与世界模型开发页一致的编辑器观感（GitHub light 高亮；注释不用斜体）
const editorTheme = EditorView.theme({
  '.cm-content': { lineHeight: '1.6' },
})

// 语法色引用 tokens.css 语义令牌（颜色唯一事实源；check:color-tokens 约束）
const yamlHighlight = syntaxHighlighting(HighlightStyle.define([
  { tag: tags.keyword, color: 'var(--code-syntax-keyword)', fontWeight: '600' },
  { tag: [tags.string, tags.docComment], color: 'var(--code-syntax-string)' },
  { tag: tags.comment, color: 'var(--code-syntax-comment)' },
  { tag: [tags.number, tags.bool, tags.null], color: 'var(--code-syntax-number)' },
  { tag: tags.propertyName, color: 'var(--code-syntax-property)' },
  { tag: tags.operator, color: 'var(--code-syntax-operator)' },
]))

const SPEC_SNIPPETS: Array<{ label: string; code: string }> = [
  { label: 'agent 节点', code: '  <id>:\n    kind: agent\n    system: 执行说明\n    outputs:\n      done: { contract: <契约名> }' },
  { label: 'human 节点', code: '  <id>:\n    kind: human\n    role: 角色说明' },
  { label: 'approval 节点', code: '  <id>:\n    kind: approval\n    approvers: [admin]' },
  { label: 'condition 节点', code: '  <id>:\n    kind: condition\n    on: <上游>.done\n    branches:\n      - { when: { field: <字段>, equals: <值> }, to: [<目标>] }\n    default: [<目标>]' },
  { label: 'join 节点', code: '  <id>:\n    kind: join\n    mode: all' },
  { label: 'terminal 节点', code: '  <id>: { kind: terminal, outcome: success }' },
  { label: '契约', code: '  <契约名>:\n    type: object\n    required: [<字段>]\n    properties:\n      <字段>: { type: string, minLength: 1 }' },
  { label: '打回边', code: '  - { from: <审批>.rejected, to: <目标>, rework: true }' },
]

const STARTER_SPEC = `api_version: openontology.task/v1
kind: Workflow
metadata:
  name: 我的第一个流程
contracts:
  ChangeSummary:
    type: object
    required: [category, summary]
    properties:
      category: { enum: [frontend, backend, defect_fix] }
      summary: { type: string, minLength: 1 }
nodes:
  analyze:
    kind: agent
    system: 分析任务并产出结构化摘要
    outputs: { done: { contract: ChangeSummary } }
  route:
    kind: condition
    on: analyze.done
    branches:
      - { when: { field: category, equals: frontend }, to: [human_review] }
      - { when: { field: category, equals: defect_fix }, to: [auto_fix] }
    default: [human_review]
  human_review: { kind: human, role: 前端变更评审 }
  auto_fix: { kind: agent, system: 执行缺陷修复 }
  gate: { kind: approval, approvers: [admin] }
  end_ok: { kind: terminal, outcome: success }
edges:
  - { from: human_review.done, to: gate }
  - { from: auto_fix.done, to: gate }
  - { from: gate.approved, to: end_ok }
  - { from: gate.rejected, to: auto_fix, rework: true }
policies:
  corrections_per_node: 2
  rework_per_edge: 3
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

export default function TemplateEditorView({ templateId }: { templateId?: string }) {
  const params = useParams<{ id: string }>()
  const effectiveId = templateId ?? (params.id === 'new' ? undefined : params.id)
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const isNew = !effectiveId
  const [specYaml, setSpecYaml] = useState(STARTER_SPEC)
  const [savedYaml, setSavedYaml] = useState('')
  const [issues, setIssues] = useState<ValidationIssue[] | null>(null)
  const [showRevisions, setShowRevisions] = useState(false)
  const [diffRevision, setDiffRevision] = useState<TemplateRevision | null>(null)
  const [confirmRestore, setConfirmRestore] = useState<TemplateRevision | null>(null)
  const editorRef = useRef<ReactCodeMirrorRef>(null)

  const template = useQuery({
    queryKey: ['task-instances', 'template', effectiveId],
    enabled: Boolean(effectiveId),
    queryFn: () => taskInstancesApi.getTemplate(effectiveId as string),
  })

  useEffect(() => {
    if (template.data?.spec_yaml !== undefined) {
      setSpecYaml(template.data.spec_yaml)
      setSavedYaml(template.data.spec_yaml)
    }
  }, [template.data?.spec_yaml])

  const revisions = useQuery({
    queryKey: ['task-instances', 'template-revisions', effectiveId],
    enabled: Boolean(effectiveId) && showRevisions,
    queryFn: () => taskInstancesApi.listRevisions(effectiveId as string),
  })

  const save = useMutation({
    mutationFn: async () => {
      if (isNew) return taskInstancesApi.createTemplate(specYaml)
      return taskInstancesApi.updateTemplate(effectiveId as string, specYaml)
    },
    onSuccess: data => {
      toast.success(isNew ? '模板已创建' : '已保存')
      setSavedYaml(specYaml)
      setIssues(null)
      void queryClient.invalidateQueries({ queryKey: ['task-instances', 'templates'] })
      void queryClient.invalidateQueries({
        queryKey: ['task-instances', 'template', effectiveId] })
      if (isNew) {
        // 新建成功后进入正式编辑路由（replace 不新增标签）
        navigate(`/task-instances/templates/${data.id}`, { replace: true })
      }
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

  const previewSpec = (template.data as { spec_compiled?: WorkflowSpecSnapshot } | undefined)
    ?.spec_compiled ?? null

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="ghost" size="sm" onClick={() => navigate('/task-instances')} className="gap-1.5">
          <ArrowLeft className="h-4 w-4" />返回
        </Button>
        <div className="flex items-center gap-2">
          <FileCode2 className="h-4 w-4 text-[var(--color-text-tertiary)]" />
          <span className="text-sm font-medium text-[var(--color-text-primary)]">
            {isNew ? '新建流程模板' : (template.data?.name ?? '流程模板')}
          </span>
          {dirty ? (
            <span className="rounded bg-[var(--color-bg-warning)] px-1.5 py-0.5 text-[11px] text-[var(--color-warning)]">未保存</span>
          ) : null}
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={runValidate} className="gap-1.5">
            <ShieldAlert className="h-4 w-4" />校验
          </Button>
          <Button size="sm" onClick={() => save.mutate()} disabled={save.isPending} className="gap-1.5">
            <Save className="h-4 w-4" />{isNew ? '创建模板' : '保存新版本'}
          </Button>
        </div>
      </div>

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
            {effectiveId && showRevisions ? (
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
                        <Button variant="ghost" size="sm" className="h-6 gap-1 px-1.5 text-[11px]"
                          onClick={() => setConfirmRestore(revision)}>
                          <Undo2 className="h-3 w-3" />回滚
                        </Button>
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
              extensions={[yaml(), editorTheme, yamlHighlight]}
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
                  <div key={index} className="rounded-md bg-[var(--color-bg-danger)] px-2 py-1.5 text-xs">
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
                契约可选、逐端口收紧；打回边携带理由触发上游重做（驳回-修复环）。
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
    </div>
  )
}
