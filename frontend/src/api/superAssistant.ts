import { apiClientV2 } from '@/api/client'

export interface SuperConversation {
  id: string
  title: string
  model_config_id: string | null
  status: string
  created_at: string
  updated_at: string
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

/** 记忆宫殿文件（用户级长期资产）：图谱抽取状态机 draft→pending→building→built/failed */
export interface PalaceFile {
  id: string
  filename: string
  /** 目录层级（"/" 分隔，根目录为空串）：单传文件落根目录，ZIP 导入保留包内结构 */
  path: string
  mimeType: string
  size: number
  sha256: string
  extractedChars: number
  status: 'draft' | 'pending' | 'building' | 'built' | 'failed' | string
  error: string | null
  entityCount: number
  relationCount: number
  /** md/txt 纯文本文件支持在线编辑（PUT content），其余格式只读 */
  editable: boolean
  /** 图片只入库+预览（/raw 原图），不参与图谱抽取，状态直接定格 built */
  isImage: boolean
  createdAt: string
  updatedAt: string
}

/** 记忆宫殿目录（一等公民）：path 为 "/" 分隔归一路径，根目录不落行 */
export interface PalaceFolder {
  id: string
  path: string
  createdAt: string
  updatedAt: string
}

/** 本体发布态业务文档的宫殿镜像（平台共享只读）：发布/回滚事件自动同步建图 */
export interface PalaceOntologyDocument {
  id: string
  ontologyId: string
  ontologyName: string
  versionId: string
  versionNumber: string
  title: string
  fingerprint: string
  status: 'pending' | 'building' | 'built' | 'failed' | string
  error: string | null
  entityCount: number
  relationCount: number
  extractedChars: number
  size: number
  updatedAt: string
}

/** 本体文档内容预览（GET ontology-documents preview）：与文件预览同响应形状 */
export interface PalaceOntologyDocumentPreview {
  file: PalaceOntologyDocument
  content: string
  truncated: boolean
  previewable: boolean
}

export interface PalaceGraphNode {
  id: string
  name: string
  type: string
  aliases: string[]
  source_files: string[]
  /** 溯源文件行 id：与文件库列表联动（选中文件高亮 / 点节点定位文档） */
  file_ids: string[]
  mention_count: number
  /** 检索/对话中被引用命中的次数（graph/search 与图谱详情展示口径） */
  match_count: number
}

export interface PalaceGraphEdge {
  source: string
  target: string
  name: string
  source_files: string[]
  file_ids: string[]
}

/** 记忆宫殿图谱视图：available=false 表示 Neo4j 暂不可用（非 5xx） */
export interface PalaceGraph {
  available: boolean
  nodes: PalaceGraphNode[]
  edges: PalaceGraphEdge[]
  totals: { entities: number; relations: number }
  truncated: boolean
  /** 画布下统计条：已建图/全部文档数与最近一次成功建图完成时间（无则 null） */
  builtFiles: number
  totalFiles: number
  updatedAt: string | null
}

/** 单文件预览（GET preview）：previewable=false 表示该格式不支持预览 */
export interface PalaceFilePreview {
  file: PalaceFile
  content: string
  truncated: boolean
  previewable: boolean
}

/** ZIP 批量导入结果（POST files/batch）：created 为新建文件，skipped 为跳过项及原因 */
export interface PalaceImportResult {
  created: PalaceFile[]
  skipped: Array<{ filename: string; reason: string }>
}

/** 图谱邻域检索返回的关系边（自带两端实体名，供高亮与邻域展示） */
export interface PalaceGraphSearchEdge {
  source: string
  target: string
  source_name: string
  target_name: string
  name: string
  source_files: string[]
  file_ids: string[]
}

/** 图谱邻域检索（GET graph/search）：available=false 表示图谱服务暂不可用 */
export interface PalaceGraphSearchResult {
  available: boolean
  entities: PalaceGraphNode[]
  relations: PalaceGraphSearchEdge[]
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
  | 'cancelled' | 'error' | 'done'

export interface StreamEvent {
  event: StreamEventName
  data: Record<string, any>
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
    throw new Error(message)
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

export interface SuperMemory {
  id: string
  content: string
  zone: string
  pinned: boolean
  confidence: string
  source: string
  tags: string[]
  supersedes: string[]
  superseded: boolean
  match_count: number
  reference_count: number
  last_accessed_at: string | null
  created_at: string
  updated_at: string
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
  auto_accept_enabled: boolean
  palace_index: string | null
  profile: string | null
  memory_count: number
  pending_count: number
}

/** 悬浮 AI 助手的页面可见范围（平台级配置）：隐藏名单语义，空名单 = 全部页面可见 */
export interface AssistantWidgetConfig {
  hidden_menu_keys: string[]
  updated_at: string | null
}

export interface MemoryConflictError {
  detail: string
  existing?: { id: string; content: string; similarity: number }
}

export interface DistillMember {
  id: string
  content: string
  zone: string
  pinned: boolean
  match_count: number
  reference_count: number
  created_at: string
}

export interface DistillCluster {
  cluster_key: string
  members: DistillMember[]
  survivor_id: string
  protected: boolean
}

export interface DistillReport {
  clusters: DistillCluster[]
}

export const superAssistantApi = {
  conversations: () => apiClientV2.get<SuperConversation[]>('/super-assistant/conversations'),
  createConversation: (body: { title?: string; model_config_id?: string | null } = {}) =>
    apiClientV2.post<SuperConversation>('/super-assistant/conversations', body),
  updateConversation: (id: string, body: { title?: string; model_config_id?: string | null; status?: 'active' | 'archived' }) =>
    apiClientV2.patch<SuperConversation>(`/super-assistant/conversations/${id}`, body),
  deleteConversation: (id: string) => apiClientV2.delete(`/super-assistant/conversations/${id}`),
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
  palaceFiles: () => apiClientV2.get<PalaceFile[]>('/super-assistant/palace/files'),
  /** folderPath 非空时上传归位到该目录（目录不存在时后端按 mkdir -p 补行） */
  uploadPalaceFile: (file: File, folderPath = '') => {
    const form = new FormData()
    form.append('file', file)
    if (folderPath) form.append('folder_path', folderPath)
    return apiClientV2.post<PalaceFile>('/super-assistant/palace/files', form)
  },
  palaceFolders: () => apiClientV2.get<PalaceFolder[]>('/super-assistant/palace/folders'),
  createPalaceFolder: (path: string) =>
    apiClientV2.post<PalaceFolder>('/super-assistant/palace/folders', { path }),
  renamePalaceFolder: (folderId: string, path: string) =>
    apiClientV2.patch<PalaceFolder>(`/super-assistant/palace/folders/${folderId}`, { path }),
  deletePalaceFolder: (folderId: string) =>
    apiClientV2.delete(`/super-assistant/palace/folders/${folderId}`),
  /** 新建 md/txt 空笔记（status=draft，首次保存内容才触发图谱抽取） */
  createPalaceNote: (filename: string, folderPath: string) =>
    apiClientV2.post<PalaceFile>('/super-assistant/palace/files/notes', { filename, folderPath }),
  /** 拖拽移动文件（不改内容、不触发抽取）；folderPath 空串表示根目录 */
  movePalaceFile: (fileId: string, folderPath: string) =>
    apiClientV2.patch<PalaceFile>(`/super-assistant/palace/files/${fileId}`, { folderPath }),
  deletePalaceFile: (fileId: string) =>
    apiClientV2.delete(`/super-assistant/palace/files/${fileId}`),
  rebuildPalaceFile: (fileId: string) =>
    apiClientV2.post<{ dispatched: boolean }>(`/super-assistant/palace/files/${fileId}/rebuild`),
  palaceFilePreview: (fileId: string, maxChars = 60000) =>
    apiClientV2.get<PalaceFilePreview>(`/super-assistant/palace/files/${fileId}/preview`, { params: { max_chars: maxChars } }),
  /** 原始字节（图片原图预览）：Bearer 鉴权走 axios blob，消费方自行 createObjectURL 并释放 */
  palaceFileRaw: (fileId: string) =>
    apiClientV2.get<Blob>(`/super-assistant/palace/files/${fileId}/raw`, { responseType: 'blob' }),
  updatePalaceFileContent: (fileId: string, content: string) =>
    apiClientV2.put<PalaceFile>(`/super-assistant/palace/files/${fileId}/content`, { content }),
  replacePalaceFile: (fileId: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return apiClientV2.post<PalaceFile>(`/super-assistant/palace/files/${fileId}/replace`, form)
  },
  importPalaceZip: (archive: File) => {
    const form = new FormData()
    form.append('archive', archive)
    return apiClientV2.post<PalaceImportResult>('/super-assistant/palace/files/batch', form)
  },
  palaceGraph: () => apiClientV2.get<PalaceGraph>('/super-assistant/palace/graph'),
  palaceGraphSearch: (q: string) =>
    apiClientV2.get<PalaceGraphSearchResult>('/super-assistant/palace/graph/search', { params: { q } }),
  /** 本体发布文档镜像列表（平台共享只读，树中「本体文档」目录数据源之一） */
  palaceOntologyDocuments: () =>
    apiClientV2.get<PalaceOntologyDocument[]>('/super-assistant/palace/ontology-documents'),
  palaceOntologyDocumentPreview: (docId: string, maxChars = 60000) =>
    apiClientV2.get<PalaceOntologyDocumentPreview>(`/super-assistant/palace/ontology-documents/${docId}/preview`, { params: { max_chars: maxChars } }),
  /** 手动重试失败的本体文档建图（正常路径由发布事件/每日对账驱动） */
  rebuildPalaceOntologyDocument: (docId: string) =>
    apiClientV2.post<{ dispatched: boolean }>(`/super-assistant/palace/ontology-documents/${docId}/rebuild`),
  streamChat,
  cancel: (id: string) => apiClientV2.post(`/super-assistant/conversations/${id}/cancel`),
  decideToolRun: (id: string, decision: 'approve' | 'deny') =>
    apiClientV2.post(`/super-assistant/tool-runs/${id}/decision`, { decision }),

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
    enabled: boolean; require_confirmation: boolean
  }>) => apiClientV2.patch<SuperMcpServer>(`/super-assistant/mcp-servers/${id}`, body),
  deleteMcpServer: (id: string) => apiClientV2.delete(`/super-assistant/mcp-servers/${id}`),
  testMcpServer: (id: string) => apiClientV2.post<{ ok: boolean; message: string; tools: McpTool[] }>(
    `/super-assistant/mcp-servers/${id}/test`,
  ),

  assistantTools: () => apiClientV2.get<AssistantTool[]>('/super-assistant/tools'),
  updateAssistantTool: (name: string, enabled: boolean) =>
    apiClientV2.patch<AssistantTool>(`/super-assistant/tools/${encodeURIComponent(name)}`, { enabled }),

  memories: (params: { zone?: string; include_superseded?: boolean } = {}) => {
    const search = new URLSearchParams()
    if (params.zone) search.set('zone', params.zone)
    if (params.include_superseded) search.set('include_superseded', 'true')
    const suffix = search.size ? `?${search.toString()}` : ''
    return apiClientV2.get<SuperMemory[]>(`/super-assistant/memories${suffix}`)
  },
  createMemory: (body: { content: string; zone?: string; pinned?: boolean; tags?: string[] }) =>
    apiClientV2.post<SuperMemory>('/super-assistant/memories', body),
  updateMemory: (id: string, body: Partial<Pick<SuperMemory, 'content' | 'zone' | 'pinned' | 'tags'>>) =>
    apiClientV2.patch<SuperMemory>(`/super-assistant/memories/${id}`, body),
  deleteMemory: (id: string) => apiClientV2.delete(`/super-assistant/memories/${id}`),
  distillReport: () => apiClientV2.get<DistillReport>('/super-assistant/memories/distill-report'),
  applyDistill: (body: { member_ids: string[]; merged_content?: string; use_llm?: boolean }) =>
    apiClientV2.post<SuperMemory>('/super-assistant/memories/distill', body),

  reflectionCandidates: (status: 'pending' | 'accepted' | 'rejected' | 'all' = 'pending') =>
    apiClientV2.get<ReflectionCandidate[]>(`/super-assistant/reflection/candidates?status=${status}`),
  decideReflectionCandidate: (id: string, decision: string, payload?: Record<string, any>) =>
    apiClientV2.post<ReflectionCandidate>(`/super-assistant/reflection/candidates/${id}/decision`, { decision, payload }),
  runFullReflection: (conversationId: string) =>
    apiClientV2.post<{ dispatched: boolean; runId?: string | null }>('/super-assistant/reflection/full', { conversation_id: conversationId }),
  reflectionSettings: () => apiClientV2.get<ReflectionSettings>('/super-assistant/reflection/settings'),
  updateReflectionSettings: (body: { auto_accept_enabled: boolean }) =>
    apiClientV2.put<ReflectionSettings>('/super-assistant/reflection/settings', body),

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

  // 文件夹同步（记忆宫殿）：重置长效同步令牌（明文仅此一次返回）与下载
  // 内嵌平台地址 + 当前令牌的同步脚本（Blob）。下载依赖浏览器副作用，按
  // AGENTS.md §5：E2E 必须断言下载文件内容，不能只断言"提示出现"。
  resetPalaceSyncToken: () =>
    apiClientV2.post<{ token: string }>('/super-assistant/palace/sync/token'),
  downloadPalaceSyncScript: () =>
    apiClientV2.get('/super-assistant/palace/sync/script', { responseType: 'blob' }) as Promise<Blob>,

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
}
