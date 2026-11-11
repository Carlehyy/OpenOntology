import { apiClientV2 } from '@/api/client'
import {
  browserLiveWsUrl,
  type BrowserCapture,
  type BrowserCollaborationApi,
  type BrowserCollaborationState,
  type BrowserLiveFrame,
  type BrowserSource,
} from '@/api/browserCollaboration'
import type {
  OntologyConfirmationState,
  PendingToolConfirmation,
} from '@/types/ontologyConfirmation'

export interface SuperConversation {
  id: string
  title: string
  model_config_id: string | null
  status: string
  created_at: string
  updated_at: string
  /** 会话绑定的浏览器来源（后端 ConversationOut 已透出；未绑定为 null，
   *  面板回退默认的 managed） */
  browser_source_id?: string | null
}

export interface SuperMessage {
  id: string
  conversation_id: string
  role: 'user' | 'assistant'
  content: string
  status: 'streaming' | 'complete' | 'cancelled' | 'error'
  steps: ToolStep[]
  token_usage: Record<string, number>
  created_at: string
  /** 流式期间的 thinking 轮次（来自 SSE thinking 事件，仅本地缓冲使用，服务端不存储） */
  thinking_round?: number | null
}

export interface ToolStep {
  toolName: string
  status: string
  arguments?: Record<string, unknown>
  preview?: string
  /** 流式期间用于对齐 tool_result；message_end 回放可能没有此字段 */
  toolRunId?: string
}

/** 会话附件（服务端会话目录 manifest 行）：仅所属会话可见 */
export interface SuperConversationFile {
  id: string
  filename: string
  mimeType: string
  size: number
  extractedChars: number
  extractError: string | null
  createdAt: string
}

/** 全局搜索命中：会话标题或消息内容（camelCase 响应，与附件接口同风格） */
export interface SuperSearchMessageHit {
  messageId: string
  role: string
  snippet: string
  createdAt: string
}

/** 语音听写后端就绪状态（snake_case 响应，与 voice.py 子路由契约一致） */
export interface VoiceStatus {
  ready: boolean
  model_present: boolean
  model_file: string
  languages: string[]
  sample_rate: number
  max_duration_seconds: number
  max_body_bytes: number
  download: VoiceModelDownload
}

/** 语音听写转写结果 */
export interface VoiceTranscription {
  text: string
  duration_ms: number
  language: string
}

/** 语音模型在线下载进度（cc-haha 式显式下载：idle/running/done/failed/cancelled） */
export interface VoiceModelDownload {
  state: 'idle' | 'running' | 'done' | 'failed' | 'cancelled'
  received_bytes: number
  total_bytes: number
  current_file: string | null
  error: string | null
}

export interface SuperSearchConversationHit {
  id: string
  title: string
  status: string
  updatedAt: string
  titleMatched: boolean
  messageHits: SuperSearchMessageHit[]
}

export interface SuperSearchResult {
  query: string
  conversations: SuperSearchConversationHit[]
}

/** WeKnora 原生 Web 入口（后端只回公网 URL，不读取 state/知识库/模型可用性） */
export interface WeKnoraIntegration {
  /** WeKnora 原生 Web 页面公网地址：前端只负责新标签打开，登录使用其原生账号体系 */
  web_url: string
}

/** Kaneo 项目管理原生 Web 入口（后端只回 Web URL，不涉及账号打通） */
export interface KaneoIntegration {
  /** Kaneo 原生 Web 页面地址：前端只负责新标签打开，登录使用其单用户独立账号 */
  web_url: string
}

export interface SkillFile {
  path: string
  size: number
  editable: boolean
}

export interface SuperSkill {
  id: string
  name: string
  description: string
  manifest: SkillFile[]
  enabled: boolean
  always_active: boolean
  use_count: number
  last_used_at: string | null
  revision: number
  created_at: string
  updated_at: string
}

export interface McpTool {
  name: string
  description: string
  input_schema: Record<string, unknown>
}

export type McpTransport = 'stdio' | 'sse' | 'streamable_http' | 'developed'

/** 内置工具目录项（GET /super-assistant/tools）。
 *  available 是平台/配置条件可用性（如 web_search 平台未配后端），
 *  enabled 是用户启停状态，两者独立。 */
export interface AssistantTool {
  name: string
  description: string
  parameters: Record<string, unknown>
  category: 'read_only' | 'confirmation_required' | 'standard'
  available: boolean
  unavailable_reason: string | null
  enabled: boolean
}

// 输入框快捷内容（用户自管常用文本片段）：sort_order 为相对展示优先级
export interface QuickInput {
  id: string
  title: string
  content: string
  sort_order: number
  created_at: string
  updated_at: string
}

export interface SuperMcpServer {
  id: string
  name: string
  display_name: string
  description: string
  builtin_key: string | null
  dev_project_id: string | null
  transport: McpTransport
  url: string
  header_names: string[]
  command: string | null
  args: string[]
  env_names: string[]
  enabled: boolean
  require_confirmation: boolean
  /** 平台管理员受约束登记位：仅成功测试且工具清单非空后可登记 */
  constrained: boolean
  tool_manifest: McpTool[]
  last_test_status: 'success' | 'error' | null
  last_test_message: string | null
  last_tested_at: string | null
  created_at: string
  updated_at: string
}

export type StreamEventName =
  | 'meta' | 'thinking' | 'text_delta' | 'tool_start'
  | 'tool_confirmation_required' | 'tool_result' | 'message_end'
  | 'cancelled' | 'error' | 'done' | 'memory_status'

export interface StreamEvent {
  event: StreamEventName
  data: Record<string, any>
}

/** kernel.v1 长任务运行时状态。旧 Conversation 流仍使用上面的 StreamEvent。 */
export type KernelRunStatus =
  | 'queued' | 'active' | 'waiting_input' | 'waiting_approval' | 'waiting_external'
  | 'waiting_retry' | 'paused' | 'cancel_requested' | 'cancelling' | 'cancelled'
  | 'expired' | 'completed' | 'failed' | string

export interface KernelRunBinding {
  binding_mode: 'direct_ui' | 'delegated' | 'legacy'
  ontology_id?: string
  draft_version_id?: string
  lifecycle?: string
  write_permission_hash?: string
}

export interface KernelRunAccepted {
  run_id: string
  execution_version: string
  stream_url: string
  request_id: string
}

export interface KernelRunCallSummary {
  call_id: string
  status: string
  outcome: string | null
  capability_key: string
  /** 本体动作确认的精简同源对象（confirmation_out display）；非动作类 call 无 */
  confirmation?: OntologyConfirmationState | null
  /** 服务端标记：结果待核实/需人工处理的保守态 */
  manual_attention?: boolean
}

/** kernel 审批的服务端持久摘要（Approval 表出参，供 run 视图与 SSE 同步） */
export interface KernelApprovalSummary {
  approval_id: string
  status: string
  call_id: string | null
  target_summary: string
  parameter_summary: string
  scope_summary: string
  expires_at: string | null
  decided_at: string | null
}

export interface KernelRunArtifactSummary {
  artifact_id: string
  kind: string
  mime_type: string
  size: number
  checksum: string
  status: string
  business_status: string
}

export interface KernelRunInboxItem {
  inbox_id: string
  kind: string
  question_id: string | null
  approval_id?: string | null
  expires_at: string | null
  /** kind=approval_decision 时的服务端摘要（无则为旧负载，仅显示占位） */
  approval?: KernelApprovalSummary | null
}

export interface KernelRunView {
  run_id: string
  conversation_id: string
  status: KernelRunStatus
  wait_reason: string | null
  version: number
  execution_version: string
  goal: string
  deadline: string | null
  binding_snapshot: Record<string, unknown>
  current_inbox: KernelRunInboxItem[]
  calls: KernelRunCallSummary[]
  artifacts: KernelRunArtifactSummary[]
}

export interface KernelRunSummary {
  run_id: string
  conversation_id: string
  status: KernelRunStatus
  wait_reason: string | null
  version: number
  goal: string
  deadline: string | null
  created_at: string
  updated_at: string
}

export type KernelRunEvent = {
  event: string
  data: Record<string, any>
  id?: string
}

const pathPart = (path: string) => path.split('/').map(encodeURIComponent).join('/')

const runtimeApiBase = () => {
  const injected = typeof window !== 'undefined' ? (window as any).__API_BASE_URL__ || '' : ''
  return `${injected}/api/v2`
}

const streamChat = async (
  conversationId: string,
  body: { message: string; model_config_id?: string | null; agent_mode?: boolean },
  onEvent: (event: StreamEvent) => void,
  signal?: AbortSignal,
) => {
  const token = localStorage.getItem('token')
  const response = await fetch(`${runtimeApiBase()}/super-assistant/conversations/${conversationId}/chat`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body),
    signal,
  })
  if (!response.ok) {
    let message = `请求失败 (${response.status})`
    try {
      const payload = await response.json()
      message = payload.detail || payload.message || message
    } catch { /* keep HTTP status */ }
    // 携带状态码供调用方区分 409 单飞护栏等业务错误
    const error = new Error(message) as Error & { status?: number }
    error.status = response.status
    throw error
  }
  if (!response.body) throw new Error('浏览器未提供流式响应体')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const dispatch = (block: string) => {
    let event: StreamEventName = 'done'
    const dataLines: string[] = []
    for (const line of block.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim() as StreamEventName
      if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
    }
    if (!dataLines.length) return
    try {
      onEvent({ event, data: JSON.parse(dataLines.join('\n')) })
    } catch {
      onEvent({ event: 'error', data: { message: '无法解析服务端流式事件' } })
    }
  }
  while (true) {
    const { value, done } = await reader.read()
    buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, '\n')
    let split = buffer.indexOf('\n\n')
    while (split >= 0) {
      dispatch(buffer.slice(0, split))
      buffer = buffer.slice(split + 2)
      split = buffer.indexOf('\n\n')
    }
    if (done) break
  }
  if (buffer.trim()) dispatch(buffer)
}

/** 消费 kernel.v1 SSE。用 fetch 而非 EventSource，以便复用现有 Bearer 鉴权并支持取消。 */
const streamKernelRun = async (
  runId: string,
  onEvent: (event: KernelRunEvent) => void,
  options: { afterSeq?: number; lastEventId?: string; signal?: AbortSignal } = {},
) => {
  const token = localStorage.getItem('token')
  const params = options.afterSeq === undefined ? '' : `?after_seq=${encodeURIComponent(options.afterSeq)}`
  const response = await fetch(`${runtimeApiBase()}/super-assistant/runs/${encodeURIComponent(runId)}/events${params}`, {
    headers: {
      Accept: 'text/event-stream',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.lastEventId ? { 'Last-Event-ID': options.lastEventId } : {}),
    },
    signal: options.signal,
  })
  if (!response.ok) {
    const error = new Error(`运行事件请求失败 (${response.status})`) as Error & { status?: number }
    error.status = response.status
    throw error
  }
  if (!response.body) throw new Error('浏览器未提供流式响应体')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const dispatch = (block: string) => {
    let event = 'message'
    let id: string | undefined
    const dataLines: string[] = []
    for (const line of block.split('\n')) {
      if (line.startsWith('id:')) id = line.slice(3).trim()
      else if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
    }
    if (!dataLines.length) return // heartbeat comment
    try { onEvent({ event, id, data: JSON.parse(dataLines.join('\n')) }) }
    catch { onEvent({ event: 'error', id, data: { message: '无法解析运行事件' } }) }
  }
  while (true) {
    const { value, done } = await reader.read()
    buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, '\n')
    let split = buffer.indexOf('\n\n')
    while (split >= 0) {
      dispatch(buffer.slice(0, split))
      buffer = buffer.slice(split + 2)
      split = buffer.indexOf('\n\n')
    }
    if (done) break
  }
  if (buffer.trim()) dispatch(buffer)
}

/** 远程助手（声明式注册，每用户多条）：key 命名空间 remote.*，配置后经
 *  委派目录进入 delegate_to_assistant 工具枚举；token 加密存储、只回显
 *  token_set 标志。 */
export interface RemoteAgent {
  id: string
  key: string
  label: string
  description: string
  endpoint: string
  token_set: boolean
  enabled: boolean
  timeout_seconds: number
  /** direct = 平台主动外呼端点；pull = 远端长轮询回连（NAT 友好） */
  mode: 'direct' | 'pull'
  /** 回连模式最近轮询时间（在线状态展示） */
  last_seen_at: string | null
  /** 最近一次被委派执行回合的时间（直连模式的活动信号） */
  last_turn_at: string | null
}

export interface RemoteAgentPayload {
  key?: string
  label?: string
  description?: string
  endpoint?: string
  token?: string | null
  enabled?: boolean
  timeout_seconds?: number
}

export interface RemoteAgentTestResult {
  ok: boolean
  message: string
}

/** 接入邀请状态（pending | used | expired | revoked），不含令牌本体 */
export interface RemoteAgentInvite {
  id: string
  status: 'pending' | 'used' | 'expired' | 'revoked'
  created_at: string
  expires_at: string
  redeemed_agent_key: string | null
  redeemed_agent_label: string | null
}

/** 创建邀请的响应：邀请函全文（含一次性令牌）由后端单源生成 */
export interface RemoteAgentInviteCreated {
  id: string
  status: string
  expires_at: string
  prompt_text: string
}

export type ScheduledKind = 'once' | 'daily' | 'weekly'
export type ScheduledTargetKind = 'super_assistant' | 'task_template'
export type ScheduledRunStatus = 'queued' | 'running' | 'completed' | 'failed' | 'skipped'

export interface ScheduledTask {
  id: string
  title: string
  instruction: string
  target_kind: ScheduledTargetKind
  target_template_id: string | null
  /** 模板目标的服务端回填名；模板已删/不存在为 null（界面显示「已删除」） */
  target_template_name: string | null
  schedule_kind: ScheduledKind
  timezone: string
  run_at: string | null
  hour: number | null
  minute: number | null
  weekday: number | null
  enabled: boolean
  next_run_at: string | null
  last_dispatched_at: string | null
  last_run_id: string | null
  last_run_status: ScheduledRunStatus | null
  last_run_at: string | null
  created_at: string
  updated_at: string
}

export interface ScheduledTaskPayload {
  title?: string
  instruction: string
  target_kind?: ScheduledTargetKind
  target_template_id?: string | null
  schedule_kind: ScheduledKind
  run_at?: string | null
  hour?: number | null
  minute?: number | null
  weekday?: number | null
  enabled?: boolean
}

export type ScheduledTaskUpdatePayload = Partial<ScheduledTaskPayload>

export interface ScheduledRun {
  id: string
  task_id: string
  conversation_id: string | null
  /** 模板目标触发创建的任务实例 id；超级助手目标为 null */
  instance_id: string | null
  scheduled_for: string
  status: ScheduledRunStatus
  error: string | null
  result_summary: string | null
  started_at: string | null
  finished_at: string | null
  created_at: string
}

/** multica 外部集成配置（每用户一条）：commands 由后端下发，未配置/未启用时为空，
 *  输入框据此决定是否展示 /multica: 命令提示 */
export interface MulticaCommand {
  command: string
  title: string
  description: string
  usage: string
  write: boolean
}

export interface MulticaConfig {
  configured: boolean
  enabled: boolean
  base_url: string
  workspace_id: string
  /** 工作区显示名（保存/测试连接时回填）：下拉兜底显示名称而非裸 UUID */
  workspace_name: string
  token_set: boolean
  commands: MulticaCommand[]
  last_test_status: 'success' | 'error' | null
  last_test_message: string | null
  last_tested_at: string | null
}

export interface MulticaTestResult {
  ok: boolean
  message: string
  account_name: string | null
  workspaces: Array<{ id: string; name: string; slug: string }>
}

/** 已保存配置的实时工作区列表（GET /super-assistant/multica/workspaces） */
export interface MulticaWorkspacesResult {
  workspaces: MulticaTestResult['workspaces']
}

/** Hindsight 长期记忆（用户级，跨全部 SA 会话共享；删除会话不影响记忆）。
 *  旧 SuperMemory/分区/蒸馏体系已随本迭代退役。 */
export interface MemorySettings {
  enabled: boolean
  model_config_id: string | null
  /** 原生控制台地址仅向管理员提供；空值表示未配置。 */
  web_url?: string | null
}

export type MemoryRuntimeState = 'ready' | 'unconfigured' | 'unavailable' | 'syncing' | 'error'

export interface MemoryStatus {
  state: MemoryRuntimeState
  message: string | null
  pending_count: number
  failed_count: number
  last_synced_at: string | null
}

/** 记忆来源状态机：终态 synced/forgotten；中间态按 string 宽容（后端新增不破坏前端） */
export type MemorySourceStatus =
  | 'synced' | 'pending' | 'submitted' | 'processing' | 'retry_wait'
  | 'failed' | 'forgetting' | 'forgotten' | string

/** 记忆来源（SourceOut）：对话自动沉淀的已提交用户消息/助手完整 final，或手动记住/更正 */
export interface MemorySource {
  id: string
  /** 来源角色：user / assistant；手动记住与更正由 explicit 标记 */
  role: string
  content: string
  occurred_at: string | null
  conversation_id: string | null
  message_id: string | null
  explicit: boolean
  /** 追加更正所针对的来源 id；非更正为 null */
  correction_of: string | null
  status: MemorySourceStatus
  error: string | null
  created_at: string
  updated_at: string
}

export interface MemorySourcePage {
  items: MemorySource[]
  total: number
}

/** Hindsight 召回命中（召回展示用，非来源本体） */
export interface MemoryRecallItem {
  id: string
  text: string
  type: string
  document_id: string | null
  occurred_start: string | null
  occurred_end: string | null
  source_ids: string[]
}

/** POST /memory/search 响应：除命中外还携带召回链路状态；state 非 ready 时
 *  前端须呈现失败信息，不得把空 items 当作成功空结果 */
export interface MemorySearchResult {
  items: MemoryRecallItem[]
  state?: MemoryRuntimeState | string
  message?: string | null
}

export interface ReflectionCandidate {
  id: string
  run_id: string
  conversation_id: string
  kind: 'memory' | 'skill' | 'conflict' | string
  status: 'pending' | 'accepted' | 'rejected' | string
  confidence: string
  payload: Record<string, any>
  decision: string | null
  created_at: string
  decided_at: string | null
}

export interface ReflectionSettings {
  /** 待审批技能候选数（面板徽标）；旧 auto-accept/palace/memory_count 字段随旧记忆体系退役 */
  pending_count: number
}

/** 会话纪要五维微卡片（后端解析 LLM 模板输出的结构化字段；宽容解析，缺失为空串/空数组） */
export interface ConversationSummaryFields {
  title: string
  status: string
  tags: string[]
  goal: string
  actions: string
  pitfalls: string
  outcome: string
  todos: string
}

/** 会话纪要（每会话仅最新一份）：markdown 是 LLM 原文，结构化字段缺失时的展示兜底 */
export interface ConversationSummary {
  conversation_id: string
  structured: ConversationSummaryFields
  markdown: string | null
  model_config_id: string | null
  message_count: number
  generated_at: string
}

/** 用户级纪要生成模型配置（对所有会话生效）：model_config_id 为 null 表示跟随各会话当前模型 */
export interface SummarySettings {
  model_config_id: string | null
  updated_at: string | null
}

/** 悬浮 AI 助手的页面可见范围（平台级配置）：隐藏名单语义，空名单 = 全部页面可见 */
export interface AssistantWidgetConfig {
  hidden_menu_keys: string[]
  updated_at: string | null
}

export const superAssistantApi = {
  conversations: () => apiClientV2.get<SuperConversation[]>('/super-assistant/conversations'),
  createConversation: (body: { title?: string; model_config_id?: string | null } = {}) =>
    apiClientV2.post<SuperConversation>('/super-assistant/conversations', body),
  updateConversation: (id: string, body: { title?: string; model_config_id?: string | null; status?: 'active' | 'archived' }) =>
    apiClientV2.patch<SuperConversation>(`/super-assistant/conversations/${id}`, body),
  deleteConversation: (id: string) => apiClientV2.delete(`/super-assistant/conversations/${id}`),
  /** 导出完整会话审计 JSON（format: openontology.super-assistant.conversation，不截断消息） */
  exportConversation: (id: string) =>
    apiClientV2.get<Record<string, unknown>>(`/super-assistant/conversations/${id}/export`),
  messages: (id: string) => apiClientV2.get<SuperMessage[]>(`/super-assistant/conversations/${id}/messages`),
  conversationFiles: (id: string) =>
    apiClientV2.get<SuperConversationFile[]>(`/super-assistant/conversations/${id}/files`),
  uploadConversationFile: (id: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return apiClientV2.post<SuperConversationFile>(`/super-assistant/conversations/${id}/files`, form)
  },
  deleteConversationFile: (id: string, fileId: string) =>
    apiClientV2.delete(`/super-assistant/conversations/${id}/files/${fileId}`),
  searchConversations: (q: string, limit = 20) =>
    apiClientV2.get<SuperSearchResult>('/super-assistant/search/conversations', { params: { q, limit } }),
  /** 语音听写：后端模型就绪状态与下载进度（助手配置「语音」tab 展示用） */
  voiceStatus: () => apiClientV2.get<VoiceStatus>('/super-assistant/voice/status'),
  /** 语音模型在线下载（仅管理员；已在进行中返回 409） */
  downloadVoiceModel: () =>
    apiClientV2.post<{ started: boolean }>('/super-assistant/voice/model/download'),
  cancelVoiceModelDownload: () =>
    apiClientV2.post<{ cancelled: boolean }>('/super-assistant/voice/model/download/cancel'),
  /** 语音听写：上传 16k 单声道 16-bit PCM WAV（原始字节），返回转写文本。
   *  显式超时：转写是 CPU 推理（秒级），不能沿用 axios 无默认超时；
   *  signal 支持转写阶段用户主动取消 */
  transcribeVoice: (wav: Uint8Array, language: string, config?: { signal?: AbortSignal }) =>
    apiClientV2.post<VoiceTranscription>('/super-assistant/voice/transcribe', wav, {
      params: { language },
      headers: { 'Content-Type': 'audio/wav' },
      timeout: 90_000,
      signal: config?.signal,
    }),
  /** WeKnora 原生 Web 入口（页面加载即读公网 URL；不依赖 state/知识库/模型） */
  getWeKnoraIntegration: () =>
    apiClientV2.get<WeKnoraIntegration>('/super-assistant/weknora'),
  /** Kaneo 项目管理原生 Web 入口（页面加载即读 URL；登录走 Kaneo 自己的账号体系） */
  getKaneoIntegration: () =>
    apiClientV2.get<KaneoIntegration>('/super-assistant/kaneo'),
  streamChat,
  /** kernel.v1 长任务 API；与旧 Conversation 流并行，调用方需自行保存 run_id/version。 */
  createKernelRun: (conversationId: string, body: {
    goal: string
    idempotency_key: string
    deadline?: string
    parent_run_id?: string
    join_policy?: 'all' | 'any'
    binding?: KernelRunBinding
  }) => apiClientV2.post<KernelRunAccepted>(
    `/super-assistant/conversations/${encodeURIComponent(conversationId)}/runs`,
    body,
    { headers: { 'Idempotency-Key': body.idempotency_key } },
  ),
  kernelRun: (runId: string) => apiClientV2.get<KernelRunView>(`/super-assistant/runs/${encodeURIComponent(runId)}`),
  kernelRuns: (conversationId: string, limit = 50) => apiClientV2.get<KernelRunSummary[]>(
    `/super-assistant/conversations/${encodeURIComponent(conversationId)}/runs`, { params: { limit } },
  ),
  cancelKernelRun: (runId: string, body: { reason: 'user' | 'parent' | 'deadline'; idempotency_key: string }, version: number) =>
    apiClientV2.post<{ command_id: string; status: KernelRunStatus; version: number }>(
      `/super-assistant/runs/${encodeURIComponent(runId)}/cancel`, body, { headers: { 'If-Match': String(version), 'Idempotency-Key': body.idempotency_key } },
    ),
  pauseKernelRun: (runId: string, body: { idempotency_key: string }, version: number) =>
    apiClientV2.post<{ command_id: string; status: KernelRunStatus; version: number }>(
      `/super-assistant/runs/${encodeURIComponent(runId)}/pause`, body, { headers: { 'If-Match': String(version), 'Idempotency-Key': body.idempotency_key } },
    ),
  resumeKernelRun: (runId: string, body: { idempotency_key: string }, version: number) =>
    apiClientV2.post<{ command_id: string; status: KernelRunStatus; version: number }>(
      `/super-assistant/runs/${encodeURIComponent(runId)}/resume`, body, { headers: { 'If-Match': String(version), 'Idempotency-Key': body.idempotency_key } },
    ),
  retryKernelRun: (runId: string, body: { idempotency_key: string; max_steps?: number }, version: number) =>
    apiClientV2.post<KernelRunAccepted>(
      `/super-assistant/runs/${encodeURIComponent(runId)}/retry`, body, { headers: { 'If-Match': String(version), 'Idempotency-Key': body.idempotency_key } },
    ),
  submitKernelInput: (runId: string, body: { kind: string; content?: string; content_ref?: string; question_id?: string; idempotency_key: string }, version: number) =>
    apiClientV2.post<{ inbox_id: string; status: string; run_id: string }>(
      `/super-assistant/runs/${encodeURIComponent(runId)}/inputs`, body,
      { headers: { 'If-Match': String(version), 'Idempotency-Key': body.idempotency_key } },
    ),
  decideKernelApproval: (runId: string, approvalId: string, body: { decision: 'approved' | 'denied'; idempotency_key: string }, version: number) =>
    apiClientV2.post<{ command_id: string; status: string; version: number }>(
      `/super-assistant/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(approvalId)}/decision`, body, { headers: { 'If-Match': String(version), 'Idempotency-Key': body.idempotency_key } },
    ),
  streamKernelRun,
  kernelArtifact: (runId: string, artifactId: string) =>
    apiClientV2.get<{ artifact_id: string; mime_type: string; size: number; checksum: string; status: string; business_status: string; content: string | null }>(
      `/super-assistant/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactId)}`,
    ),
  kernelArtifactDownload: (runId: string, artifactId: string) =>
    apiClientV2.getBlob(`/super-assistant/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactId)}/download`),
  cancel: (id: string) => apiClientV2.post(`/super-assistant/conversations/${id}/cancel`),
  decideToolRun: (id: string, decision: 'approve' | 'deny') =>
    apiClientV2.post(`/super-assistant/tool-runs/${id}/decision`, { decision }),
  /** reload 后恢复 direct 待确认卡：与 tool_confirmation_required 事件同形，无待确认为 null */
  pendingConfirmation: (conversationId: string) =>
    apiClientV2.get<PendingToolConfirmation | null>(
      `/super-assistant/conversations/${encodeURIComponent(conversationId)}/pending-confirmation`,
    ),

  skills: () => apiClientV2.get<SuperSkill[]>('/super-assistant/skills'),
  createSkill: (body: {
    name: string; description: string; content: string; enabled: boolean; always_active?: boolean
  }) => apiClientV2.post<SuperSkill>('/super-assistant/skills', body),
  updateSkill: (id: string, body: Partial<Pick<SuperSkill, 'enabled' | 'always_active'>>) =>
    apiClientV2.patch<SuperSkill>(`/super-assistant/skills/${id}`, body),
  deleteSkill: (id: string) => apiClientV2.delete(`/super-assistant/skills/${id}`),
  importSkill: (archive: File) => {
    const form = new FormData()
    form.append('archive', archive)
    return apiClientV2.post<SuperSkill>('/super-assistant/skills/import', form)
  },
  skillFiles: (id: string) => apiClientV2.get<SkillFile[]>(`/super-assistant/skills/${id}/files`),
  skillFile: (id: string, path: string) =>
    apiClientV2.get<{ path: string; content: string }>(`/super-assistant/skills/${id}/files/${pathPart(path)}`),
  putSkillFile: (id: string, path: string, content: string) =>
    apiClientV2.put(`/super-assistant/skills/${id}/files/${pathPart(path)}`, { content }),
  deleteSkillFile: (id: string, path: string) =>
    apiClientV2.delete(`/super-assistant/skills/${id}/files/${pathPart(path)}`),

  mcpServers: () => apiClientV2.get<SuperMcpServer[]>('/super-assistant/mcp-servers'),
  createMcpServer: (body: {
    name: string; display_name?: string; description?: string;
    transport: McpTransport; url: string; headers: Record<string, string>;
    command?: string | null; args?: string[]; env?: Record<string, string>;
    enabled: boolean; require_confirmation: boolean
  }) => apiClientV2.post<SuperMcpServer>('/super-assistant/mcp-servers', body),
  updateMcpServer: (id: string, body: Partial<{
    display_name: string; description: string;
    transport: McpTransport; url: string; headers: Record<string, string>;
    command: string | null; args: string[]; env: Record<string, string>;
    enabled: boolean; require_confirmation: boolean; constrained: boolean
  }>) => apiClientV2.patch<SuperMcpServer>(`/super-assistant/mcp-servers/${id}`, body),
  deleteMcpServer: (id: string) => apiClientV2.delete(`/super-assistant/mcp-servers/${id}`),
  testMcpServer: (id: string) => apiClientV2.post<{ ok: boolean; message: string; tools: McpTool[] }>(
    `/super-assistant/mcp-servers/${id}/test`,
  ),
  installPlatformApiHubMcp: () => apiClientV2.post<SuperMcpServer>(
    '/super-assistant/mcp-servers/platform-api-hub',
  ),

  assistantTools: () => apiClientV2.get<AssistantTool[]>('/super-assistant/tools'),
  updateAssistantTool: (name: string, enabled: boolean) =>
    apiClientV2.patch<AssistantTool>(`/super-assistant/tools/${encodeURIComponent(name)}`, { enabled }),

  listQuickInputs: () => apiClientV2.get<QuickInput[]>('/super-assistant/quick-inputs'),
  createQuickInput: (body: { title: string; content: string }) =>
    apiClientV2.post<QuickInput>('/super-assistant/quick-inputs', body),
  updateQuickInput: (id: string, body: { title?: string; content?: string }) =>
    apiClientV2.patch<QuickInput>(`/super-assistant/quick-inputs/${id}`, body),
  deleteQuickInput: (id: string) => apiClientV2.delete(`/super-assistant/quick-inputs/${id}`),
  reorderQuickInputs: (ids: string[]) =>
    apiClientV2.put<QuickInput[]>('/super-assistant/quick-inputs/reorder', { ids }),

  // Hindsight 长期记忆：设置 / 状态 / 来源 / 召回（详见 backend super_assistant memory 路由）
  memorySettings: () => apiClientV2.get<MemorySettings>('/super-assistant/memory/settings'),
  /** 保存即交给后端能力校验（模型可用性由后端验证，前端不冒充 test） */
  updateMemorySettings: (body: { enabled: boolean; model_config_id?: string | null }) =>
    apiClientV2.patch<MemorySettings>('/super-assistant/memory/settings', body),
  memoryStatus: () => apiClientV2.get<MemoryStatus>('/super-assistant/memory/status'),
  memorySources: (params: { limit?: number; offset?: number; query?: string } = {}) =>
    apiClientV2.get<MemorySourcePage>('/super-assistant/memory/sources', { params }),
  searchMemory: (query: string) =>
    apiClientV2.post<MemorySearchResult>('/super-assistant/memory/search', { query }),
  /** 手动记住（explicit）或对既有来源追加更正（correction_of） */
  createMemorySource: (body: { content: string; correction_of?: string }) =>
    apiClientV2.post<MemorySource>('/super-assistant/memory/sources', body),
  /** 按来源忘记：异步进入 forgetting/forgotten，返回的是携带最新状态的 SourceOut */
  deleteMemorySource: (id: string) =>
    apiClientV2.delete<MemorySource>(`/super-assistant/memory/sources/${id}`),
  retryMemorySource: (id: string) =>
    apiClientV2.post<MemorySource>(`/super-assistant/memory/sources/${id}/retry`),

  reflectionCandidates: (status: 'pending' | 'accepted' | 'rejected' | 'all' = 'pending') =>
    apiClientV2.get<ReflectionCandidate[]>(`/super-assistant/reflection/candidates?status=${status}`),
  decideReflectionCandidate: (id: string, decision: string, payload?: Record<string, any>) =>
    apiClientV2.post<ReflectionCandidate>(`/super-assistant/reflection/candidates/${id}/decision`, { decision, payload }),
  runFullReflection: (conversationId: string) =>
    apiClientV2.post<{ dispatched: boolean; runId?: string | null }>('/super-assistant/reflection/full', { conversation_id: conversationId }),
  reflectionSettings: () => apiClientV2.get<ReflectionSettings>('/super-assistant/reflection/settings'),

  // 会话纪要：读取每会话最新一份；generate 由页面在「发送后 ~1s」与
  // 「完全响应结束后 ~1s」触发（失败静默，不打扰对话主流程）
  conversationSummary: (conversationId: string) =>
    apiClientV2.get<ConversationSummary | null>(`/super-assistant/conversations/${conversationId}/summary`),
  generateConversationSummary: (conversationId: string) =>
    apiClientV2.post<ConversationSummary>(`/super-assistant/conversations/${conversationId}/summary/generate`, {}),
  /** 未配置时后端返回全空字段对象（model_config_id=null 表示跟随会话模型） */
  summarySettings: () => apiClientV2.get<SummarySettings>('/super-assistant/summary/settings'),
  updateSummarySettings: (body: { model_config_id: string | null }) =>
    apiClientV2.put<SummarySettings>('/super-assistant/summary/settings', body),

  widgetConfig: () => apiClientV2.get<AssistantWidgetConfig>('/super-assistant/widget-config'),
  updateWidgetConfig: (hiddenMenuKeys: string[]) =>
    apiClientV2.put<AssistantWidgetConfig>('/super-assistant/widget-config', { hidden_menu_keys: hiddenMenuKeys }),

  multicaConfig: () => apiClientV2.get<MulticaConfig>('/super-assistant/multica/config'),
  updateMulticaConfig: (body: { base_url: string; token?: string | null; workspace_id: string; workspace_name?: string | null; enabled: boolean }) =>
    apiClientV2.put<MulticaConfig>('/super-assistant/multica/config', body),
  testMultica: (body: { base_url?: string | null; token?: string | null } = {}) =>
    apiClientV2.post<MulticaTestResult>('/super-assistant/multica/test', body),
  multicaWorkspaces: () =>
    apiClientV2.get<MulticaWorkspacesResult>('/super-assistant/multica/workspaces'),

  listRemoteAgents: () => apiClientV2.get<RemoteAgent[]>('/super-assistant/remote-agents'),
  createRemoteAgent: (body: RemoteAgentPayload) =>
    apiClientV2.post<RemoteAgent>('/super-assistant/remote-agents', body),
  updateRemoteAgent: (id: string, body: RemoteAgentPayload) =>
    apiClientV2.put<RemoteAgent>(`/super-assistant/remote-agents/${id}`, body),
  deleteRemoteAgent: (id: string) =>
    apiClientV2.delete(`/super-assistant/remote-agents/${id}`),
  testRemoteAgent: (id: string) =>
    apiClientV2.post<RemoteAgentTestResult>(`/super-assistant/remote-agents/${id}/test`, {}),

  listRemoteAgentInvites: () =>
    apiClientV2.get<RemoteAgentInvite[]>('/super-assistant/remote-agent-invites'),
  createRemoteAgentInvite: () =>
    apiClientV2.post<RemoteAgentInviteCreated>('/super-assistant/remote-agent-invites', {}),
  remoteAgentInvitePrompt: (id: string) =>
    apiClientV2.get<string>(`/super-assistant/remote-agent-invites/${id}/prompt`),
  revokeRemoteAgentInvite: (id: string) =>
    apiClientV2.delete(`/super-assistant/remote-agent-invites/${id}`),

  listScheduledTasks: () =>
    apiClientV2.get<ScheduledTask[]>('/super-assistant/scheduled-tasks'),
  /** 创建定时任务：显式超时对齐 transcribeVoice 先例——axios 默认无超时，
   *  请求悬挂会让弹窗 saving 态永久锁死所有关闭路径（UX 对抗审查 P1） */
  createScheduledTask: (body: ScheduledTaskPayload) =>
    apiClientV2.post<ScheduledTask>('/super-assistant/scheduled-tasks', body, { timeout: 30_000 }),
  getScheduledTask: (id: string) =>
    apiClientV2.get<ScheduledTask>(`/super-assistant/scheduled-tasks/${id}`),
  updateScheduledTask: (id: string, body: ScheduledTaskUpdatePayload) =>
    apiClientV2.patch<ScheduledTask>(`/super-assistant/scheduled-tasks/${id}`, body),
  deleteScheduledTask: (id: string) =>
    apiClientV2.delete(`/super-assistant/scheduled-tasks/${id}`),
  listScheduledRuns: (taskId: string) =>
    apiClientV2.get<ScheduledRun[]>(`/super-assistant/scheduled-tasks/${taskId}/runs`),
  getScheduledRun: (taskId: string, runId: string) =>
    apiClientV2.get<ScheduledRun>(`/super-assistant/scheduled-tasks/${taskId}/runs/${runId}`),
}

// ---------- 浏览器协作（与数据管家共用面板，运行时复用 steward BrowserManager） ----------
// 端点族见 backend/app/super_assistant/browser.py：响应沿用 steward {"data": ...}
// 包络，由 apiClientV2 响应拦截器统一解包；实时画面 WS 由 steward 处理器以
// /api/v2/super-assistant 前缀二次挂载（ticket 鉴权，处理器与会话模型无关）。
export const superAssistantBrowserApi: BrowserCollaborationApi = {
  listSources: () => apiClientV2.get<BrowserSource[]>('/super-assistant/browser/sources'),
  // 超助无读取单会话绑定来源的 GET 端点：从会话列表取（ConversationOut
  // 已透出 browser_source_id；未绑定时为 null，面板回退默认的 managed）。
  conversationBrowserSourceId: async cid =>
    (await superAssistantApi.conversations()).find(item => item.id === cid)?.browser_source_id ?? null,
  createSource: body =>
    apiClientV2.post<BrowserSource>('/super-assistant/browser/sources', body),
  testSource: id =>
    apiClientV2.post<{ reachable: boolean; sourceType: string; label: string }>(
      `/super-assistant/browser/sources/${id}/test`),
  deleteSource: id => apiClientV2.delete(`/super-assistant/browser/sources/${id}`),
  bindSource: (cid, sourceId) =>
    apiClientV2.put<{ conversationId: string; browserSourceId: string }>(
      `/super-assistant/conversations/${cid}/browser/source`, { sourceId }),
  start: (cid, url) =>
    apiClientV2.post<{ url: string; title: string }>(
      `/super-assistant/conversations/${cid}/browser/start`, { url }),
  navigate: (cid, url) =>
    apiClientV2.post<{ url: string; title: string }>(
      `/super-assistant/conversations/${cid}/browser/navigate`, { url }),
  session: cid =>
    apiClientV2.get<{
      active: boolean; url: string; live: boolean; collaboration: BrowserCollaborationState
    }>(`/super-assistant/conversations/${cid}/browser/session`),
  ticket: cid =>
    apiClientV2.post<{ ticket: string; expiresIn: number }>(
      `/super-assistant/conversations/${cid}/browser/ticket`),
  liveHttpAttach: cid =>
    apiClientV2.post<{
      leaseId: string; expiresIn: number; frameIntervalMs: number
      collaboration: BrowserCollaborationState
    }>(`/super-assistant/conversations/${cid}/browser/live-http`),
  liveHttpFrame: (cid, leaseId) =>
    apiClientV2.post<BrowserLiveFrame>(
      `/super-assistant/conversations/${cid}/browser/live-http/frame`, { leaseId }),
  liveHttpInput: (cid, leaseId, message) =>
    apiClientV2.post<{ accepted: boolean; collaboration: BrowserCollaborationState }>(
      `/super-assistant/conversations/${cid}/browser/live-http/input`, { leaseId, message }),
  liveHttpControl: (cid, leaseId, action) =>
    apiClientV2.post<{ collaboration: BrowserCollaborationState }>(
      `/super-assistant/conversations/${cid}/browser/live-http/control`, { leaseId, action }),
  liveHttpRelease: (cid, leaseId) =>
    apiClientV2.post<{ released: boolean }>(
      `/super-assistant/conversations/${cid}/browser/live-http/release`, { leaseId }),
  captures: cid =>
    apiClientV2.get<BrowserCapture[]>(`/super-assistant/conversations/${cid}/browser/captures`, {
      params: { keyword: '', limit: 100 },
    }),
  downloadCapture: (cid, captureId) =>
    apiClientV2.post(`/super-assistant/conversations/${cid}/browser/captures/${captureId}/download`),
  // companion 脚本下载照 steward.ts 的 token + fetch 范式（Blob 直下，无包络）
  downloadCompanionScript: async () => {
    const token = localStorage.getItem('token') || ''
    const resp = await fetch(`${runtimeApiBase()}/super-assistant/browser/companion/script`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    if (!resp.ok) throw new Error(`助手下载失败 (${resp.status})`)
    const blob = await resp.blob()
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = 'openontology-browser-companion.mjs'
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    URL.revokeObjectURL(url)
  },
  liveWsUrl: (cid, ticket) =>
    browserLiveWsUrl(`/api/v2/super-assistant/conversations/${cid}/browser/live?ticket=${encodeURIComponent(ticket)}`),
}
