/**
 * 助手评估（系统设置子页）— 基于 OpenJudge 的助手会话质量评估。
 *
 * 结构：发起评估表单（单一提交边界）→ 评估任务列表（对象携带状态）→
 * 报告详情抽屉（总览 / 维度得分 / 会话明细下钻 / 导出）。
 */
import { formatDateTime } from '@/utils/datetime'
import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Checkbox,
  Drawer,
  Input,
  InputNumber,
  Modal,
  Progress,
  Segmented,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import { toast } from 'sonner'
import type { ColumnsType } from 'antd/es/table'
import { Download, Eye, FlaskConical, Plus, Trash2 } from 'lucide-react'
import {
  assistantEvaluationApi,
  type AssistantMeta,
  type EvalItemTrace,
  type EvalMeta,
  type EvalRubric,
  type EvalTask,
  type EvalTaskDetail,
  type EvalTaskItem,
  type TrendPoint,
} from '@/api/assistantEvaluation'
import { modelApi } from '@/api/ontologies'
import AssistantFlywheelSection from './AssistantFlywheelSection'
import ReactECharts from 'echarts-for-react'
import type { EChartsOption } from 'echarts'
import {
  baseChartOption,
  CHART_AXIS,
  CHART_SERIES_PALETTE,
  CHART_SPLIT,
  CHART_TEXT,
  CHART_TEXT_STRONG,
  CHART_TOOLTIP_BG,
  CHART_TOOLTIP_BORDER,
  CHART_TOOLTIP_CSS,
} from '@/lib/echartsTheme'

const { Text } = Typography

function formatTime(value: string | null): string {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return formatDateTime(date)
}

const STATUS_TAG: Record<string, { color: string; label: string }> = {
  queued: { color: 'default', label: '排队中' },
  running: { color: 'processing', label: '评估中' },
  success: { color: 'success', label: '已完成' },
  error: { color: 'error', label: '失败' },
}

/**
 * 评分对应的语义色（取平台 token，浅深自适应）：
 * ≥80 success / ≥60 warning / <60 danger。
 * 返回 CSS 变量引用，深色模式随 .dark 自动翻转。
 */
function scoreColor(score: number): string {
  if (score >= 80) return 'var(--color-success)'
  if (score >= 60) return 'var(--color-warning)'
  return 'var(--color-danger)'
}

function AssistantEvalPanel() {
  const queryClient = useQueryClient()
  const [assistantKey, setAssistantKey] = useState<string>('')
  const [mode, setMode] = useState<'manual' | 'sample'>('sample')
  const [selectedConversationIds, setSelectedConversationIds] = useState<string[]>([])
  const [sampleSize, setSampleSize] = useState<number>(10)
  const [sampleDays, setSampleDays] = useState<number>(30)
  const [dimensionKeys, setDimensionKeys] = useState<string[]>([])
  const [modelConfigId, setModelConfigId] = useState<string | undefined>(undefined)
  const [rubricId, setRubricId] = useState<string | undefined>(undefined)
  const [rubricModalOpen, setRubricModalOpen] = useState(false)
  const [traceItemId, setTraceItemId] = useState<string | null>(null)
  const [detailTaskId, setDetailTaskId] = useState<string | null>(null)

  const metaQuery = useQuery({
    queryKey: ['assistant-eval', 'meta'],
    queryFn: () => assistantEvaluationApi.meta(),
  })

  const meta: EvalMeta | undefined = metaQuery.data
  const assistants: AssistantMeta[] = useMemo(() => meta?.assistants ?? [], [meta])
  const activeAssistant = assistants.find(a => a.key === assistantKey)

  const effectiveDimensions = useMemo(() => {
    if (!meta) return []
    if (dimensionKeys.length) return dimensionKeys
    return meta.base_dimension_keys
  }, [meta, dimensionKeys])

  const conversationsQuery = useQuery({
    queryKey: ['assistant-eval', 'conversations', assistantKey],
    queryFn: () => assistantEvaluationApi.conversations(assistantKey, 100, 0),
    enabled: !!assistantKey && mode === 'manual',
  })

  const modelsQuery = useQuery({
    queryKey: ['assistant-eval', 'models'],
    queryFn: () => modelApi.list(),
    staleTime: 60_000,
  })

  const llmModels = useMemo(
    () => (modelsQuery.data ?? []).filter(m => m.config_type === 'llm' && m.enabled !== false),
    [modelsQuery.data],
  )

  const tasksQuery = useQuery({
    queryKey: ['assistant-eval', 'tasks'],
    queryFn: () => assistantEvaluationApi.tasks(),
    refetchInterval: (query) => {
      const tasks = query.state.data ?? []
      const inFlight = tasks.some(t => t.status === 'queued' || t.status === 'running')
      return inFlight ? 3_000 : false
    },
  })

  const createTaskMutation = useMutation({
    mutationFn: () =>
      assistantEvaluationApi.createTask({
        assistant_key: assistantKey,
        conversation_ids: selectedConversationIds,
        sample_size: sampleSize,
        sample_days: sampleDays,
        dimension_keys: effectiveDimensions,
        model_config_id: modelConfigId ?? null,
        rubric_id: rubricId ?? null,
      }),
    onSuccess: (task) => {
      void queryClient.invalidateQueries({ queryKey: ['assistant-eval', 'tasks'] })
      toast.success(`评估任务已创建：${task.title}`)
      setSelectedConversationIds([])
    },
  })

  const deleteTaskMutation = useMutation({
    mutationFn: (taskId: string) => assistantEvaluationApi.deleteTask(taskId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['assistant-eval', 'tasks'] })
      if (detailTaskId) setDetailTaskId(null)
    },
  })

  const detailQuery = useQuery({
    queryKey: ['assistant-eval', 'task', detailTaskId],
    queryFn: () => assistantEvaluationApi.taskDetail(detailTaskId!),
    enabled: !!detailTaskId,
  })

  const rubricsQuery = useQuery({
    queryKey: ['assistant-eval', 'rubrics'],
    queryFn: () => assistantEvaluationApi.rubrics(),
  })

  const trendQuery = useQuery({
    queryKey: ['assistant-eval', 'trend', detailTaskId],
    queryFn: () => assistantEvaluationApi.trend(detailQuery.data!.assistant_key, 12),
    enabled: !!detailQuery.data && detailQuery.data.status === 'success',
  })

  const traceQuery = useQuery({
    queryKey: ['assistant-eval', 'trace', detailTaskId, traceItemId],
    queryFn: () => assistantEvaluationApi.itemTrace(detailTaskId!, traceItemId!),
    enabled: !!detailTaskId && !!traceItemId,
  })

  const handleAssistantChange = (key: string) => {
    setAssistantKey(key)
    setSelectedConversationIds([])
  }

  const canSubmit =
    !!activeAssistant &&
    effectiveDimensions.length > 0 &&
    (mode === 'sample' || selectedConversationIds.length > 0)

  const conversationColumns: ColumnsType<{ id: string; title: string; created_at: string | null; message_count: number }> = [
    { title: '会话标题', dataIndex: 'title', ellipsis: true },
    { title: '消息数', dataIndex: 'message_count', width: 80 },
    { title: '创建时间', dataIndex: 'created_at', width: 120, render: formatTime },
  ]

  const taskColumns: ColumnsType<EvalTask> = [
    {
      title: '助手',
      dataIndex: 'assistant_label',
      width: 110,
      render: (label: string, record) => (
        <Space size={4}>
          <span>{label}</span>
          <Text type="secondary" style={{ fontSize: 11 }}>
            {record.params.mode === 'sample' ? '抽样' : '指定'}
          </Text>
        </Space>
      ),
    },
    { title: '任务', dataIndex: 'title', ellipsis: true },
    {
      title: '状态',
      dataIndex: 'status',
      width: 130,
      render: (_: string, record) => {
        const tag = STATUS_TAG[record.status] ?? { color: 'default', label: record.status }
        const progress =
          record.status === 'running' ? `${record.completed_conversations}/${record.conversation_count}` : null
        return (
          <Space size={4}>
            <Tag color={tag.color}>{tag.label}</Tag>
            {progress && <Text type="secondary" style={{ fontSize: 11 }}>{progress}</Text>}
          </Space>
        )
      },
    },
    {
      title: '综合分',
      width: 90,
      dataIndex: ['summary', 'overall'],
      render: (score: number | null | undefined) =>
        score == null ? '-' : <Text strong style={{ color: scoreColor(score) }}>{score}</Text>,
    },
    { title: 'judge 模型', dataIndex: 'judge_model_name', width: 150, ellipsis: true },
    { title: '创建时间', dataIndex: 'created_at', width: 120, render: formatTime },
    {
      title: '操作',
      key: 'actions',
      width: 120,
      render: (_, record) => (
        <Space size={4}>
          <Button type="link" size="small" onClick={() => setDetailTaskId(record.id)}>
            报告
          </Button>
          {record.status !== 'queued' && record.status !== 'running' && (
            <Button
              type="text"
              size="small"
              aria-label="删除任务"
              icon={<Trash2 size={14} />}
              onClick={() => {
                Modal.confirm({
                  title: '删除该评估任务？',
                  content: '任务报告与明细将一并删除，历史对比基线随之失效。',
                  okText: '删除',
                  okButtonProps: { danger: true },
                  cancelText: '取消',
                  onOk: () => deleteTaskMutation.mutateAsync(record.id),
                })
              }}
            />
          )}
        </Space>
      ),
    },
  ]


  const detail = detailQuery.data

  return (
    <div className="space-y-6">
      {/* 发起评估 */}
      <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-4">
        <div className="mb-3 flex items-center gap-2">
          <FlaskConical size={16} className="text-[var(--color-text-tertiary)]" />
          <span className="text-sm font-medium text-[var(--color-text-primary)]">发起评估</span>
          {meta && (
            <Tag>{meta.engine === 'openjudge' ? 'OpenJudge 引擎' : '内置引擎'}</Tag>
          )}
        </div>
        <div className="grid gap-x-6 gap-y-3 md:grid-cols-2">
          <div>
            <div className="mb-1 text-xs text-[var(--color-text-secondary)]">评估对象</div>
            <Select
              className="w-full"
              placeholder="选择助手"
              value={assistantKey || undefined}
              onChange={handleAssistantChange}
              options={assistants.map(a => ({
                value: a.key,
                label: `${a.label} · ${a.conversation_count} 个会话`,
              }))}
            />
            {activeAssistant && (
              <div className="mt-1 text-[11px] text-[var(--color-text-tertiary)]">{activeAssistant.description}</div>
            )}
          </div>
          <div>
            <div className="mb-1 text-xs text-[var(--color-text-secondary)]">judge 模型（LLM 型维度使用）</div>
            <Select
              className="w-full"
              placeholder="默认：平台默认模型"
              allowClear
              value={modelConfigId}
              onChange={setModelConfigId}
              options={llmModels.map(m => ({
                value: m.id,
                label: m.is_default ? `${m.name}（默认）` : m.name,
              }))}
              notFoundContent={<Text type="secondary" style={{ fontSize: 12 }}>暂无可用 LLM 配置，请先到「模型配置」添加</Text>}
            />
          </div>
          <div>
            <div className="mb-1 text-xs text-[var(--color-text-secondary)]">评分标准（可选，自定义 rubric 维度）</div>
            <Space.Compact className="w-full">
              <Select
                className="flex-1"
                placeholder="不使用自定义评分标准"
                allowClear
                value={rubricId}
                onChange={setRubricId}
                options={(rubricsQuery.data ?? []).map(r => ({
                  value: r.id,
                  label: r.name + '（' + r.min_score + '-' + r.max_score + ' 分）',
                }))}
                notFoundContent={<Text type="secondary" style={{ fontSize: 12 }}>暂无评分标准，点击右侧新建</Text>}
              />
              <Button icon={<Plus size={14} />} onClick={() => setRubricModalOpen(true)}>
                新建
              </Button>
            </Space.Compact>
            <div className="mt-1 text-[11px] text-[var(--color-text-tertiary)]">
              基于任务描述由 judge 模型生成评分标准，评估时作为额外维度计入综合分
            </div>
          </div>
          <div className="md:col-span-2">
            <div className="mb-1 text-xs text-[var(--color-text-secondary)]">会话范围</div>
            <Segmented
              value={mode}
              onChange={(v) => setMode(v as 'manual' | 'sample')}
              options={[
                { value: 'sample', label: '批量抽样' },
                { value: 'manual', label: '手动选择' },
              ]}
            />
            {mode === 'sample' ? (
              <Space className="mt-2" size={16} wrap>
                <Space size={4}>
                  <Text type="secondary" style={{ fontSize: 12 }}>最近</Text>
                  <InputNumber min={1} max={180} value={sampleDays} onChange={(v) => setSampleDays(v ?? 30)} />
                  <Text type="secondary" style={{ fontSize: 12 }}>天内</Text>
                </Space>
                <Space size={4}>
                  <Text type="secondary" style={{ fontSize: 12 }}>抽取</Text>
                  <InputNumber min={1} max={30} value={sampleSize} onChange={(v) => setSampleSize(v ?? 10)} />
                  <Text type="secondary" style={{ fontSize: 12 }}>条会话</Text>
                </Space>
              </Space>
            ) : (
              <Table
                className="mt-2"
                size="small"
                rowKey="id"
                loading={conversationsQuery.isLoading}
                columns={conversationColumns}
                dataSource={conversationsQuery.data?.items ?? []}
                pagination={false}
                scroll={{ y: 220 }}
                rowSelection={{
                  selectedRowKeys: selectedConversationIds,
                  onChange: (keys) => setSelectedConversationIds(keys as string[]),
                }}
                locale={{ emptyText: assistantKey ? '该助手暂无会话' : '请先选择助手' }}
              />
            )}
          </div>
          <div className="md:col-span-2">
            <div className="mb-1 text-xs text-[var(--color-text-secondary)]">
              评分维度<span className="ml-2 text-[11px] text-[var(--color-text-tertiary)]">LLM 型消耗 judge 模型；代码型零成本固定执行</span>
            </div>
            <Checkbox.Group
              className="grid grid-cols-1 gap-2 sm:grid-cols-2"
              value={effectiveDimensions}
              onChange={(keys) => setDimensionKeys(keys as string[])}
            >
              {(meta?.dimension_catalog ?? [])
                .filter(d => !assistantKey || activeAssistant?.supported_dimension_keys.includes(d.key))
                .map(d => {
                  const isLlm = d.kind === 'llm'
                  return (
                    <div
                      key={d.key}
                      className="flex items-start gap-2 rounded-md border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-3 py-2 text-sm transition-colors hover:border-[var(--color-border-hover)]"
                    >
                      <Checkbox value={d.key} className="mt-0.5" />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5">
                          <span className="font-medium text-[var(--color-text-primary)]">{d.label}</span>
                          <Tag
                            color={isLlm ? 'blue' : 'default'}
                            className="!m-0 !px-1 !text-[10px] !leading-tight"
                          >
                            {isLlm ? 'LLM' : '代码'}
                          </Tag>
                        </div>
                        <div className="mt-0.5 truncate text-[11px] text-[var(--color-text-tertiary)]" title={d.description}>
                          {d.description}
                        </div>
                      </div>
                    </div>
                  )
                })}
            </Checkbox.Group>
          </div>
        </div>
        <div className="mt-4 flex items-center justify-between">
          <Text type="secondary" style={{ fontSize: 11 }}>
            {mode === 'sample'
              ? `将从最近 ${sampleDays} 天的会话中抽取最多 ${sampleSize} 条`
              : `已选 ${selectedConversationIds.length} 条会话`}
            {' · '}单次最多 50 条
          </Text>
          <Button
            type="primary"
            disabled={!canSubmit}
            loading={createTaskMutation.isPending}
            onClick={() => createTaskMutation.mutate()}
          >
            开始评估
          </Button>
        </div>
        {createTaskMutation.isError && (
          <Alert
            className="mt-2"
            type="error"
            showIcon
            message={createTaskMutation.error instanceof Error ? createTaskMutation.error.message : '创建评估任务失败'}
          />
        )}
      </section>

      <RubricCreateModal
        open={rubricModalOpen}
        onClose={() => setRubricModalOpen(false)}
        models={llmModels.map(m => ({ id: m.id, name: m.name, is_default: m.is_default }))}
        onCreated={(rubric) => {
          void queryClient.invalidateQueries({ queryKey: ['assistant-eval', 'rubrics'] })
          setRubricId(rubric.id)
          setRubricModalOpen(false)
        }}
      />

      {/* 任务列表 */}
      <section>
        <Table
          rowKey="id"
          size="small"
          loading={tasksQuery.isLoading}
          columns={taskColumns}
          dataSource={tasksQuery.data ?? []}
          pagination={{ pageSize: 8, hideOnSinglePage: true }}
          locale={{ emptyText: '暂无评估任务，先在上方发起一次评估' }}
          expandable={{
            expandedRowRender: (record) =>
              record.status === 'error' ? (
                <Alert type="error" showIcon message="任务执行失败" description={record.error} />
              ) : (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  维度：{(record.params.dimension_keys ?? []).join('、')} · 会话{' '}
                  {record.completed_conversations}/{record.conversation_count}
                </Text>
              ),
            rowExpandable: (record) => record.status === 'error' || record.status === 'success',
          }}
        />
      </section>

      {/* 报告详情 */}
      <Drawer
        title={`质量报告 · ${detail?.title ?? ''}`}
        placement="right"
        width={720}
        open={!!detailTaskId}
        onClose={() => setDetailTaskId(null)}
        extra={
          detail?.status === 'success' && (
            <Button
              size="small"
              icon={<Download size={14} />}
              onClick={() => detail && void assistantEvaluationApi.exportReport(detail.id)}
            >
              导出 Markdown
            </Button>
          )
        }
      >
        {!detail ? (
          <Text type="secondary">加载中…</Text>
        ) : detail.status === 'running' || detail.status === 'queued' ? (
          <Alert
            type="info"
            showIcon
            message={`评估进行中：${detail.completed_conversations}/${detail.conversation_count} 条会话`}
          />
        ) : detail.status === 'error' ? (
          <Alert type="error" showIcon message="任务执行失败" description={detail.error} />
        ) : (
          <>
            <EvalReportBody detail={detail} onOpenTrace={(item) => setTraceItemId(item.id)} />
            <EvalTrendChart trend={trendQuery.data ?? []} loading={trendQuery.isLoading} />
          </>
        )}
      </Drawer>

      <TraceModal
        open={!!traceItemId}
        onClose={() => setTraceItemId(null)}
        loading={traceQuery.isLoading}
        trace={traceQuery.data}
      />
    </div>
  )
}

function EvalReportBody({ detail, onOpenTrace }: { detail: EvalTaskDetail; onOpenTrace?: (item: EvalTaskItem) => void }) {
  const itemColumns: ColumnsType<EvalTaskItem> = [
    { title: '会话', dataIndex: 'conversation_title', ellipsis: true },
    {
      title: '总分',
      dataIndex: 'overall_score',
      width: 90,
      render: (score: number | null) =>
        score == null ? <Text type="secondary">未产出</Text> : <Text strong style={{ color: scoreColor(score) }}>{score}</Text>,
    },
    { title: '根因归类', dataIndex: 'root_cause', width: 200, ellipsis: true },
    {
      title: '标记',
      key: 'flags',
      width: 160,
      render: (_, record) => {
        const notes: string[] = []
        if (record.flags.loop_detected) notes.push('动作循环')
        if (record.flags.tool_error_count) notes.push(`${record.flags.tool_error_count} 次工具失败`)
        if (record.flags.engine_error) notes.push('评分执行异常')
        return notes.length ? <Tag color="warning">{notes.join('、')}</Tag> : <Text type="secondary">-</Text>
      },
    },
    {
      title: '操作',
      key: 'actions',
      width: 70,
      render: (_, record) =>
        onOpenTrace ? (
          <Button type="link" size="small" icon={<Eye size={13} />} onClick={() => onOpenTrace(record)}>
            轨迹
          </Button>
        ) : null,
    },
  ]

  const summary = detail.summary
  const overall = summary.overall
  const itemReasons = (item: EvalTaskItem) => (
    <div className="space-y-1 pl-2">
      {Object.entries(item.reasons).map(([key, reason]) => (
        <div key={key} className="text-xs text-[var(--color-text-secondary)]">
          {reason.score != null && (
            <span className="mr-1 inline-block min-w-9 font-medium">{reason.score}分</span>
          )}
          {reason.reason || '-'}
        </div>
      ))}
      {!!item.flags.low_dims?.length && (
        <div className="text-xs text-[var(--color-danger)]">薄弱维度：{item.flags.low_dims.join('、')}</div>
      )}
    </div>
  )

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-3 gap-3">
        <div className="rounded-lg border border-[var(--color-border)] px-4 py-3">
          <div className="text-xs text-[var(--color-text-secondary)]">综合得分</div>
          <div className="text-2xl font-semibold leading-tight" style={{ color: overall != null ? scoreColor(overall) : undefined }}>
            {overall ?? '-'}
          </div>
        </div>
        <div className="rounded-lg border border-[var(--color-border)] px-4 py-3">
          <div className="text-xs text-[var(--color-text-secondary)]">产出评分 / 失败会话</div>
          <div className="text-2xl font-semibold leading-tight text-[var(--color-text-primary)]">
            {summary.evaluated}
            <span className="text-sm text-[var(--color-text-tertiary)]"> / {summary.failed}</span>
            {!!(summary as { skipped?: number }).skipped && (
              <span className="ml-1 text-[11px] text-[var(--color-text-tertiary)]">跳过 {(summary as { skipped?: number }).skipped}</span>
            )}
          </div>
        </div>
        <div className="rounded-lg border border-[var(--color-border)] px-4 py-3">
          <div className="text-xs text-[var(--color-text-secondary)]">judge 模型调用</div>
          <div className="text-2xl font-semibold leading-tight text-[var(--color-text-primary)]">{summary.llm_calls}</div>
        </div>
      </div>

      <div>
        <div className="mb-2 text-sm font-medium text-[var(--color-text-primary)]">维度得分</div>
        <div className="space-y-2">
          {Object.entries(summary.dimensions ?? {}).map(([key, stat]) => (
            <div key={key} className="flex items-center gap-3">
              <span className="w-20 shrink-0 text-xs text-[var(--color-text-secondary)]">{stat.label}</span>
              <Progress
                percent={stat.avg}
                size="small"
                strokeColor={scoreColor(stat.avg)}
                format={() => `${stat.avg}`}
              />
              <span className="shrink-0 text-[11px] text-[var(--color-text-tertiary)]">
                最低 {stat.min} · 最高 {stat.max} · n={stat.count}
              </span>
            </div>
          ))}
        </div>
      </div>

      <div>
        <div className="mb-2 flex items-baseline justify-between">
          <span className="text-sm font-medium text-[var(--color-text-primary)]">会话明细</span>
          <span className="text-[11px] text-[var(--color-text-tertiary)]">
            引擎 {summary.engine} · judge：{detail.judge_model_name || '-'} · 耗时{' '}
            {detail.duration_ms != null ? `${(detail.duration_ms / 1000).toFixed(1)}s` : '-'}
          </span>
        </div>
        <Table
          rowKey="id"
          size="small"
          columns={itemColumns}
          dataSource={detail.items}
          pagination={false}
          expandable={{ expandedRowRender: itemReasons }}
        />
      </div>
    </div>
  )
}

function EvalTrendChart({ trend, loading }: { trend: TrendPoint[]; loading: boolean }) {
  const option = useMemo<EChartsOption>(() => {
    const latest = trend[trend.length - 1]
    const seriesKeys: string[] = latest ? ['overall', ...Object.keys(latest.dimensions ?? {})] : []
    const palette = CHART_SERIES_PALETTE
    return {
      ...baseChartOption(),
      // 趋势图是 axis tooltip，覆盖 base 的 item tooltip，但保留统一浮层样式（DESIGN.md §5.2）
      tooltip: {
        trigger: 'axis',
        backgroundColor: CHART_TOOLTIP_BG,
        borderColor: CHART_TOOLTIP_BORDER,
        textStyle: { color: CHART_TEXT_STRONG, fontSize: 12 },
        extraCssText: CHART_TOOLTIP_CSS,
      },
      legend: { top: 0, textStyle: { color: CHART_TEXT, fontSize: 11 } },
      grid: { left: 40, right: 16, top: 36, bottom: 28 },
      xAxis: {
        type: 'category',
        data: trend.map(p => formatTime(p.created_at)),
        axisLabel: { color: CHART_TEXT, fontSize: 10 },
        axisLine: { lineStyle: { color: CHART_AXIS } },
      },
      yAxis: {
        type: 'value',
        min: 0,
        max: 100,
        axisLabel: { color: CHART_TEXT, fontSize: 10 },
        splitLine: { lineStyle: { color: CHART_SPLIT } },
      },
      series: seriesKeys.map((key, idx) => ({
        name: key === 'overall' ? '综合分' : (latest?.dimensions?.[key]?.label ?? key),
        type: 'line',
        smooth: true,
        symbolSize: 5,
        lineStyle: { width: key === 'overall' ? 2.5 : 1.5 },
        color: palette[idx % palette.length],
        data: trend.map(p =>
          key === 'overall' ? p.overall : p.dimensions?.[key]?.avg ?? null,
        ),
      })),
    }
  }, [trend])

  if (loading) {
    return <Text type="secondary" style={{ fontSize: 12 }}>历史趋势加载中…</Text>
  }
  if (trend.length < 2) {
    return null
  }
  return (
    <div className="mt-5">
      <div className="mb-2 text-sm font-medium text-[var(--color-text-primary)]">历史趋势（同助手）</div>
      <ReactECharts option={option} style={{ height: 220 }} notMerge />
    </div>
  )
}

function TraceModal({ open, onClose, loading, trace }: {
  open: boolean
  onClose: () => void
  loading: boolean
  trace: EvalItemTrace | undefined
}) {
  return (
    <Modal
      title={'会话轨迹 · ' + (trace?.conversation_title ?? '')}
      open={open}
      onCancel={onClose}
      footer={null}
      width={760}
    >
      {loading ? (
        <Text type="secondary">加载中…</Text>
      ) : !trace ? (
        <Text type="secondary">暂无轨迹数据</Text>
      ) : (
        <div className="space-y-4">
          <div>
            <div className="text-xs text-[var(--color-text-secondary)]">用户问题</div>
            <div className="mt-1 whitespace-pre-wrap rounded bg-[var(--color-muted)] px-3 py-2 text-sm text-[var(--color-text-primary)]">
              {trace.query}
            </div>
          </div>
          <div>
            <div className="text-xs text-[var(--color-text-secondary)]">助手答复</div>
            <div className="mt-1 whitespace-pre-wrap rounded bg-[var(--color-muted)] px-3 py-2 text-sm text-[var(--color-text-primary)]">
              {trace.response}
            </div>
          </div>
          {trace.actions.length > 0 && (
            <div>
              <div className="mb-1 text-xs text-[var(--color-text-secondary)]">
                工具调用（{trace.actions.length} · 失败 {trace.tool_error_count}）
              </div>
              <div className="space-y-1">
                {trace.actions.map((a, i) => (
                  <div key={i} className="rounded border border-[var(--color-border)] px-2 py-1 text-xs">
                    <Space size={6}>
                      <Tag color={a.failed ? 'error' : 'default'}>{String(a.name)}</Tag>
                      <span className="text-[var(--color-text-secondary)]">{a.failed ? '失败' : '成功'}</span>
                    </Space>
                    <div className="mt-1 whitespace-pre-wrap break-all text-[var(--color-text-secondary)]">
                      {String(a.preview ?? '')}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
          <div>
            <div className="mb-1 text-xs text-[var(--color-text-secondary)]">消息轨迹（OpenAI 格式）</div>
            <div className="max-h-64 space-y-1 overflow-y-auto">
              {trace.openai_messages.map((m, i) => {
                const toolCalls = Array.isArray(m.tool_calls) ? (m.tool_calls as unknown[]) : []
                return (
                  <div key={i} className="rounded bg-[var(--color-muted)] px-2 py-1 text-xs">
                    <span className="mr-1 font-medium">{String(m.role)}</span>
                    <span className="text-[var(--color-text-secondary)]">{String(m.content ?? '')}</span>
                    {toolCalls.length > 0 && (
                      <span className="ml-1 text-[var(--color-text-tertiary)]">（{toolCalls.length} 次工具调用）</span>
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        </div>
      )}
    </Modal>
  )
}

function RubricCreateModal({ open, onClose, onCreated, models }: {
  open: boolean
  onClose: () => void
  onCreated: (rubric: EvalRubric) => void
  models: Array<{ id: string; name: string; is_default?: boolean }>
}) {
  const [name, setName] = useState('')
  const [taskDescription, setTaskDescription] = useState('')
  const [samples, setSamples] = useState('')
  const [minScore, setMinScore] = useState(0)
  const [maxScore, setMaxScore] = useState(5)
  const [modelId, setModelId] = useState<string | undefined>(undefined)
  const [submitting, setSubmitting] = useState(false)

  const handleCreate = async () => {
    if (!name.trim() || !taskDescription.trim()) {
      toast.warning('请填写名称与任务描述')
      return
    }
    setSubmitting(true)
    try {
      const rubric = await assistantEvaluationApi.createRubric({
        name: name.trim(),
        task_description: taskDescription.trim(),
        sample_queries: samples.split('\n').map(s => s.trim()).filter(Boolean),
        min_score: minScore,
        max_score: maxScore,
        model_config_id: modelId ?? null,
      })
      toast.success('评分标准已生成')
      setName('')
      setTaskDescription('')
      setSamples('')
      setMinScore(0)
      setMaxScore(5)
      setModelId(undefined)
      onCreated(rubric)
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '生成失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal
      title="新建评分标准（rubric）"
      open={open}
      onCancel={onClose}
      onOk={() => void handleCreate()}
      okText="生成"
      cancelText="取消"
      confirmLoading={submitting}
    >
      <div className="space-y-3">
        <div>
          <div className="mb-1 text-xs text-[var(--color-text-secondary)]">名称</div>
          <Input value={name} onChange={e => setName(e.target.value)} placeholder="如：本体构建质量" maxLength={200} />
        </div>
        <div>
          <div className="mb-1 text-xs text-[var(--color-text-secondary)]">任务描述（生成的评分标准将围绕它展开）</div>
          <Input.TextArea
            value={taskDescription}
            onChange={e => setTaskDescription(e.target.value)}
            rows={3}
            placeholder="描述要评估的答复任务，例如：评估助手答复在构建本体时的准确性、完整性与规范性"
          />
        </div>
        <div>
          <div className="mb-1 text-xs text-[var(--color-text-secondary)]">样例问题（可选，每行一条）</div>
          <Input.TextArea value={samples} onChange={e => setSamples(e.target.value)} rows={2} />
        </div>
        <div className="flex items-center gap-4">
          <div className="text-xs text-[var(--color-text-secondary)]">分值区间</div>
          <InputNumber min={0} max={10} value={minScore} onChange={v => setMinScore(v ?? 0)} />
          <span className="text-[var(--color-text-tertiary)]">-</span>
          <InputNumber min={1} max={10} value={maxScore} onChange={v => setMaxScore(v ?? 5)} />
        </div>
        <div>
          <div className="mb-1 text-xs text-[var(--color-text-secondary)]">生成所用 judge 模型（默认：平台默认模型）</div>
          <Select
            className="w-full"
            allowClear
            value={modelId}
            onChange={setModelId}
            placeholder="默认：平台默认模型"
            options={models.map(m => ({ value: m.id, label: m.is_default ? m.name + '（默认）' : m.name }))}
          />
        </div>
      </div>
    </Modal>
  )
}

/**
 * 页签外壳：评估任务（既有旁路评估）| 数据飞轮（基准集→提案→实验→值守→时间线）。
 */
function AssistantEvalTab() {
  const [pane, setPane] = useState<'eval' | 'flywheel'>('eval')
  return (
    <div className="space-y-5">
      {/* 页头骨架（DESIGN.md §6）：标题 + 副标题左置，视图切换右置 */}
      <div className="flex flex-wrap items-end justify-between gap-3 border-b border-[var(--color-border)] pb-3">
        <div className="min-w-0">
          <h1 className="text-lg font-semibold leading-tight text-[var(--color-text-primary)]">
            助手评估
          </h1>
          <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">
            基于 OpenJudge 的助手会话质量评估与数据飞轮（基准集 → 提案 → 实验 → 投产 → 值守）
          </p>
        </div>
        <Segmented
          value={pane}
          onChange={value => setPane(value as 'eval' | 'flywheel')}
          options={[
            { label: '评估任务', value: 'eval' },
            { label: '数据飞轮', value: 'flywheel' },
          ]}
        />
      </div>
      {pane === 'eval' ? <AssistantEvalPanel /> : <AssistantFlywheelSection />}
    </div>
  )
}

export default AssistantEvalTab
