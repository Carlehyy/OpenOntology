import { expect, test, type Locator, type Page, type Route } from '@playwright/test'

import { expectSameBoundingBox } from './support/geometry'

// AI 原生工作台（前台）：登录默认落地、九项入口、近期会话单列表、归档流转、
// 本体治理跳后台并返回。全部接口本地 mock，不触真实后端。
// 本 spec 另覆盖：分组限量展开、naive UTC 时区显示、行悬停不抖动、
// 会话附件上传/移除/位于输入框上方与跨会话隔离、流式生成跨会话隔离、ReUI 模型选择器、
// 删除确认弹窗、新建会话不落库（点击仅清空聊天区、发送首条消息才建会话）、
// 空态品牌文案与占位符、配置面板白底、
// 重命名 blur 取消、知识图谱页签弹窗（文件库上传/删除/预览/在线编辑/ZIP 导入 +
// 知识图谱过滤/节点详情/邻域检索高亮）、外部集成（multica 配置弹窗 + /multica:
// 命令提示的配置门控）、消息通知弹窗（未读徽章、tab 筛选、Markdown 详情与附件、
// 标记/归档/全部已读/发送消息）、⌘K/Ctrl+K 唤起全局搜索、输入草稿按会话缓存、
// 全局搜索 Command 面板检索与跳转、历史分组 shadcn Sidebar 原语、空态品牌字号、
// 左下角头像个人资料弹窗（固定尺寸、分区 tab 与后台同一弹窗）。

const json = (route: Route, data: unknown, status = 200) => route.fulfill({
  status,
  contentType: 'application/json',
  body: JSON.stringify({ data }),
})

/** 相对当前时刻的本地日期 ISO 串：daysAgo=0 今天、1 昨天……保证分组断言与环境无关 */
const at = (daysAgo: number, hour: number) => {
  const date = new Date()
  date.setDate(date.getDate() - daysAgo)
  date.setHours(hour, 0, 0, 0)
  return date.toISOString()
}

const sseBody = (text: string) => [
  'event: meta\ndata: {"conversationId":"c-today","model":"deepseek-v4-pro"}',
  `event: text_delta\ndata: ${JSON.stringify({ delta: text })}`,
  `event: message_end\ndata: ${JSON.stringify({ message: { content: text, steps: [], tokenUsage: {} } })}`,
  'event: done\ndata: {}',
  '',
].join('\n\n')

/** 远程助手邀请函夹具：与后端 build_invite_prompt 同构的截断版 */
const remoteInvitePromptFixture = [
  '你是即将接入 OpenOntology 平台「超级助手」的外部 AI 助手。请阅读本函并自行完成接入。',
  '【第一步：判断你的网络，二选一】',
  '- 平台服务器能直接访问你的 HTTP 端点 → 选「方式 A：直连」；',
  '- 你在局域网 / NAT 后，平台访问不到你，但你能访问公网 → 选「方式 B：回连」。',
  '【本函邀请码】',
  'rai_e2e_fixture_invite_token_1234',
].join('\n')

const conversationsFixture = [
  { id: 'c-today', title: '今日需求梳理', model_config_id: 'model-1', status: 'active', created_at: at(0, 9), updated_at: at(0, 9) },
  { id: 'c-yesterday', title: '昨日方案讨论', model_config_id: 'model-1', status: 'active', created_at: at(1, 20), updated_at: at(1, 20) },
  { id: 'c-earlier', title: '上周数据摸底', model_config_id: 'model-1', status: 'active', created_at: at(5, 10), updated_at: at(5, 10) },
  { id: 'c-archived', title: '旧会话存档', model_config_id: 'model-1', status: 'archived', created_at: at(2, 8), updated_at: at(2, 8) },
]

// 消息通知夹具：内部告警（带附件）、外部投递（已标记未读）、已归档噪音各一条
interface NotificationFixtureRow {
  id: string
  eventId: string | null
  sourceSystem: string
  sourceType: string
  title: string
  priority: string
  isRead: boolean
  isStarred: boolean
  isArchived: boolean
  readAt: string | null
  starredAt: string | null
  archivedAt: string | null
  createdAt: string
  updatedAt: string
  bodyPreview: string
  attachmentCount: number
}
const notificationsFixture: NotificationFixtureRow[] = [
  { id: 'n-1', eventId: null, sourceSystem: 'platform', sourceType: 'internal', title: '数据任务失败：供应商每日同步', priority: 'high', isRead: false, isStarred: false, isArchived: false, readAt: null, starredAt: null, archivedAt: null, createdAt: at(0, 9), updatedAt: at(0, 9), bodyPreview: '# 数据任务失败报告\n\n数据库连接超时，重试 3 次后放弃。', attachmentCount: 1 },
  { id: 'n-2', eventId: 'billing:inc-1', sourceSystem: 'billing', sourceType: 'ingest', title: '账单系统异常告警', priority: 'urgent', isRead: false, isStarred: true, isArchived: false, readAt: null, starredAt: at(1, 9), archivedAt: null, createdAt: at(1, 9), updatedAt: at(1, 9), bodyPreview: '本月账单金额环比上涨 300%', attachmentCount: 0 },
  { id: 'n-3', eventId: null, sourceSystem: 'platform', sourceType: 'internal', title: '历史巡检完成', priority: 'low', isRead: true, isStarred: false, isArchived: true, readAt: at(2, 8), starredAt: null, archivedAt: at(2, 8), createdAt: at(2, 8), updatedAt: at(2, 8), bodyPreview: '例行巡检完成', attachmentCount: 0 },
]

const notificationDetailBodies: Record<string, string> = {
  'n-1': '# 数据任务失败报告\n\n数据库连接超时，重试 3 次后放弃。\n\n| 任务 | 状态 |\n| --- | --- |\n| 供应商每日同步 | 失败 |\n\n```json\n{"code": "ETIMEDOUT"}\n```\n',
  'n-2': '## 账单异常\n\n本月金额环比上涨 **300%**，请关注。',
  'n-new': '# 今日巡检\n一切正常',
}

const notificationDetailAttachments: Record<string, Array<Record<string, unknown>>> = {
  'n-1': [{ id: 'na-1', filename: '错误日志.txt', fileSize: 2048, mimeType: 'text/plain', sha256: 'ab', createdAt: at(0, 9) }],
}

const palaceGraphFixture = {
  available: true,
  nodes: [
    { id: 'e-1', name: '张三', type: '人物', aliases: [], source_files: ['个人知识库.md'], file_ids: ['pf-1'], mention_count: 5, match_count: 2 },
    { id: 'e-2', name: 'ACME', type: '组织', aliases: [], source_files: ['个人知识库.md', '行业研究.pdf'], file_ids: ['pf-1', 'pf-2'], mention_count: 3, match_count: 1 },
    { id: 'e-3', name: '知识图谱', type: '技术', aliases: [], source_files: ['行业研究.pdf'], file_ids: ['pf-2'], mention_count: 2, match_count: 0 },
  ],
  edges: [
    { source: 'e-1', target: 'e-2', name: '任职', source_files: ['个人知识库.md'], file_ids: ['pf-1'] },
    { source: 'e-2', target: 'e-3', name: '使用', source_files: ['行业研究.pdf'], file_ids: ['pf-2'] },
  ],
  totals: { entities: 3, relations: 2 },
  truncated: false,
  builtFiles: 2,
  totalFiles: 3,
  updatedAt: at(0, 6),
}

// 本体发布文档：宫殿镜像（已建图）+ 本体侧权威清单（多一个未同步的 v0 本体）
const palaceOntologyDocsFixture = [
  { id: 'pod-1', ontologyId: 'ont-1', ontologyName: '供应链本体', versionId: 'rel-2', versionNumber: 'v2',
    title: '供应链业务文档', fingerprint: 'fp-1', status: 'built', error: null,
    entityCount: 4, relationCount: 2, extractedChars: 520, size: 640, updatedAt: at(0, 6) },
]
const ontologyPublishedDocsFixture = {
  items: [
    { ontologyId: 'ont-1', ontologyName: '供应链本体', versionId: 'rel-2', versionNumber: 'v2',
      title: '供应链业务文档', fingerprint: 'fp-1', documentChars: 520, publishedAt: at(0, 6) },
    { ontologyId: 'ont-2', ontologyName: '合规本体', versionId: 'rel-9', versionNumber: 'v0',
      title: '合规本体 业务文档', fingerprint: 'fp-9', documentChars: 100, publishedAt: at(0, 7) },
  ],
}

const multicaCommandsFixture = [
  { command: 'list_agents', title: '查看智能体', description: '列出 multica 工作台的全部智能体及其运行时绑定状态。', usage: '/multica:list_agents', write: false },
  { command: 'list_tasks', title: '查看任务清单', description: '查看当前工作台的任务清单，可按状态或负责人过滤。', usage: '/multica:list_tasks [过滤条件]', write: false },
  { command: 'create_task', title: '下发任务', description: '在 multica 创建任务并指派给指定智能体（写操作，需用户确认）。', usage: '/multica:create_task 任务描述…', write: true },
]

const multicaUnconfiguredFixture = {
  configured: false, enabled: false, base_url: '', workspace_id: '', token_set: false,
  commands: [], last_test_status: null, last_test_message: null, last_tested_at: null,
}

const multicaEnabledFixture = {
  configured: true, enabled: true, base_url: 'http://127.0.0.1:8080', workspace_id: 'ws-1',
  workspace_name: 'My Workspace', token_set: true, commands: multicaCommandsFixture,
  last_test_status: 'success', last_test_message: '连接成功', last_tested_at: at(0, 8),
}

// 内置工具目录夹具：覆盖三个分类 + 一条条件不可用（web_search 平台未配后端）
const assistantToolsFixture = [
  {
    name: 'memory_search', description: '按相关性搜索跨会话记忆，返回最匹配的若干条（含 id）。',
    parameters: { type: 'object', properties: { query: { type: 'string', description: '检索词' } }, required: ['query'] },
    category: 'read_only', available: true, unavailable_reason: null, enabled: true,
  },
  {
    name: 'web_search', description: '搜索互联网，返回标题/链接/摘要列表。',
    parameters: { type: 'object', properties: { query: { type: 'string', description: '搜索词' } }, required: ['query'] },
    category: 'read_only', available: false, unavailable_reason: '平台未配置搜索后端', enabled: true,
  },
  {
    name: 'multica_create_task', description: '在 multica 创建任务并指派给指定智能体。',
    parameters: { type: 'object', properties: { title: { type: 'string', description: '任务标题' } }, required: ['title'] },
    category: 'confirmation_required', available: true, unavailable_reason: null, enabled: true,
  },
]

async function seedAuth(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: { id: 'u-1', username: 'workbench-tester', role: 'admin' },
      },
      version: 0,
    }))
  })
}

interface MockOptions {
  /** chat SSE 响应的延迟毫秒数：用于模拟「生成中」窗口期 */
  chatDelayMs?: number
  /** 消息通知列表启用游标分页（每页 2 条）：验证「加载更多」链路 */
  notificationsPagination?: boolean
  /** 全部消息置为已读：制造「无未读 + 站内有历史」场景（默认全部消息视图收纳历史、「全部已读」禁用） */
  notificationsAllRead?: boolean
  /** 完全无消息：制造中转站冷启动空态 */
  notificationsNone?: boolean
  /** GET /super-assistant/multica/config 的返回（默认未配置态） */
  multicaConfig?: Record<string, unknown>
  /** 知识图谱「本体文档」目录：true 时提供发布文档镜像 + 权威清单桩 */
  withOntologyDocs?: boolean
  /** 覆盖远程助手目录；传入空数组即空目录 */
  remoteAgents?: Array<Record<string, unknown>>
  /** GET /remote-agents 延迟，用于断言加载完成前不展示空态 CTA */
  remoteAgentsDelayMs?: number
}

async function mockApis(page: Page, options: MockOptions = {}) {
  const patchBodies: string[] = []
  const deleteCalls: string[] = []
  const createCalls: string[] = []
  const fileUploads: string[] = []
  const fileDeletes: string[] = []
  const chatCalls: string[] = []
  const exportCalls: string[] = []
  const ticketCreates: string[] = []
  // 懒建会话（c-new-*）的消息落库：真实后端在 chat 事务里持久化 user/assistant 两条，
  // 流结束后前端会用服务端列表整体替换乐观行——mock 需同样保真，否则懒建路径的
  // 渲染收尾会被空列表抹掉（c-today 的落库由下方 chatDone 分支单独承载）。
  const chatPersisted = new Map<string, Array<Record<string, unknown>>>()
  const searchQueries: string[] = []
  const createdConvs: Array<Record<string, unknown>> = []
  const multicaPuts: Array<Record<string, unknown>> = []
  const remoteAgentCreates: Array<Record<string, unknown>> = []
  const remoteInviteCreates: string[] = []
  const notifStatePatches: string[] = []
  const notifCreates: string[] = []
  const notifReadAllCalls: string[] = []
  const notifStatsDays: number[] = []
  const notifDeletes: string[] = []
  const notifKeyCreates: string[] = []
  const notifKeyRevokes: string[] = []
  const notifChannelCreates: string[] = []
  const notifSmtpPuts: string[] = []
  const notifSmtpTests: string[] = []
  const notifForwards: string[] = []
  const notifKeyTests: string[] = []
  const notifChannelUpdates: string[] = []
  const notifChannelTests: string[] = []
  const notifChannelDeletes: string[] = []
  // 会话夹具 per-test 克隆：PATCH 写回只落在本次 mock 的副本上（归档/改名
  // 持久化、2s 轮询不洗回，与真实后端一致）；模块级常量保持原样，用例间
  // 零污染——同 worker 顺序执行下直接改 conversationsFixture 会把改名/
  // 归档泄漏给后续用例的 aria-label 断言（retries=1 的全新 worker 掩盖它）
  const conversations = conversationsFixture.map(item => ({ ...item }))
  const notificationsState = options.notificationsNone
    ? []
    : notificationsFixture.map(item => ({
        ...item,
        // 可选变体：全部置为已读，只留星标/归档维度的差异（未读空态引导场景）
        ...(options.notificationsAllRead ? { isRead: true, readAt: item.readAt ?? at(0, 10) } : {}),
      }))
  if (options.notificationsPagination) {
    notificationsState.push(
      { id: 'n-4', eventId: null, sourceSystem: 'platform', sourceType: 'internal', title: '分页样例一', priority: 'normal', isRead: true, isStarred: false, isArchived: false, readAt: at(3, 8), starredAt: null, archivedAt: null, createdAt: at(3, 8), updatedAt: at(3, 8), bodyPreview: '第 4 条', attachmentCount: 0 },
      { id: 'n-5', eventId: null, sourceSystem: 'platform', sourceType: 'internal', title: '分页样例二', priority: 'normal', isRead: true, isStarred: false, isArchived: false, readAt: at(4, 8), starredAt: null, archivedAt: null, createdAt: at(4, 8), updatedAt: at(4, 8), bodyPreview: '第 5 条', attachmentCount: 0 },
    )
  }
  // 远程助手目录夹具：直连（带端点）+ 回连（在线，仅邀请函路径接入的形态）
  const remoteAgents: Array<Record<string, unknown>> = options.remoteAgents
    ? [...options.remoteAgents]
    : [
      { id: 'ra-direct', key: 'remote.kb', label: '客服知识库助手', description: '擅长客服问答', endpoint: 'https://kb.example.com/turn', token_set: true, enabled: true, timeout_seconds: 120, mode: 'direct', last_seen_at: null, last_turn_at: new Date(Date.now() - 3 * 3600 * 1000).toISOString() },
      { id: 'ra-pull', key: 'remote.lan-helper', label: '内网文档助手', description: '内网文档检索', endpoint: '', token_set: false, enabled: true, timeout_seconds: 60, mode: 'pull', last_seen_at: new Date().toISOString(), last_turn_at: null },
    ]
  const remoteInvites: Array<Record<string, unknown>> = []
  const scheduledTasks: Array<Record<string, unknown>> = [
    {
      id: 'st-daily', title: '每日站会纪要', instruction: '根据昨天的会话整理站会纪要。',
      target_kind: 'super_assistant', target_template_id: null, target_template_name: null,
      schedule_kind: 'daily', timezone: 'Asia/Shanghai', run_at: null, hour: 9, minute: 0, weekday: null,
      enabled: true, next_run_at: at(0, 9), last_dispatched_at: at(1, 9),
      last_run_id: 'sr-1', last_run_status: 'completed', last_run_at: at(1, 9),
      created_at: at(5, 9), updated_at: at(1, 9),
    },
    {
      id: 'st-tpl', title: '变更评审例会', instruction: '例行跑一遍代码变更评审流程。',
      target_kind: 'task_template', target_template_id: 'tpl-1', target_template_name: '代码变更评审',
      schedule_kind: 'weekly', timezone: 'Asia/Shanghai', run_at: null, hour: 10, minute: 30, weekday: 0,
      enabled: true, next_run_at: at(1, 10), last_dispatched_at: at(0, 10),
      last_run_id: 'sr-2', last_run_status: 'completed', last_run_at: at(0, 10),
      created_at: at(5, 10), updated_at: at(0, 10),
    },
  ]
  const scheduledRuns: Array<Record<string, unknown>> = [
    {
      id: 'sr-1', task_id: 'st-daily', conversation_id: 'c-today', instance_id: null,
      scheduled_for: at(1, 9),
      status: 'completed', error: null, result_summary: '昨日三项待办均已推进。',
      started_at: at(1, 9), finished_at: at(1, 9), created_at: at(1, 9),
    },
    {
      id: 'sr-2', task_id: 'st-tpl', conversation_id: null, instance_id: 'ins-1',
      scheduled_for: at(0, 10),
      status: 'completed', error: null,
      result_summary: '已按模板「代码变更评审」创建实例「变更评审例会 · 10-13 10:30」，进度与结果请在任务实例中查看',
      started_at: at(0, 10), finished_at: at(0, 10), created_at: at(0, 10),
    },
  ]
  const multicaTests: Array<Record<string, unknown>> = []
  const multicaWorkspaceCalls: string[] = []
  const toolPatchCalls: Array<{ name: string; body: { enabled: boolean } }> = []
  const toolsState = assistantToolsFixture.map(item => ({ ...item }))
  let chatDone = false
  const filesByConv: Record<string, Array<Record<string, unknown>>> = {
    'c-today': [{
      id: 'f-1', filename: '需求清单.md', mimeType: 'text/markdown', size: 1204,
      extractedChars: 100, extractError: null, createdAt: at(0, 9),
    }],
  }
  const palaceFiles: Array<Record<string, unknown>> = [
    {
      id: 'pf-1', filename: '个人知识库.md', path: '', mimeType: 'text/markdown', size: 2048,
      sha256: 'pf-1-hash', extractedChars: 1800, status: 'built', error: null,
      entityCount: 3, relationCount: 2, editable: true, isImage: false, createdAt: at(0, 8), updatedAt: at(0, 8),
    },
    {
      id: 'pf-2', filename: '行业研究.pdf', path: '', mimeType: 'application/pdf', size: 40960,
      sha256: 'pf-2-hash', extractedChars: 12000, status: 'building', error: null,
      entityCount: 0, relationCount: 0, editable: false, isImage: false, createdAt: at(0, 9), updatedAt: at(0, 9),
    },
    {
      id: 'pf-img', filename: '架构图.png', path: '设计图', mimeType: 'image/png', size: 8192,
      sha256: 'pf-img-hash', extractedChars: 0, status: 'built', error: null,
      entityCount: 0, relationCount: 0, editable: false, isImage: true, createdAt: at(0, 7), updatedAt: at(0, 7),
    },
  ]
  const palaceUploads: string[] = []
  const palaceDeletes: string[] = []
  const palacePreviews: string[] = []
  const palaceContentPuts: string[] = []
  const palaceReplaces: string[] = []
  const palaceBatchImports: string[] = []
  const palaceGraphSearches: string[] = []
  const palaceRaws: string[] = []
  const palaceNotes: string[] = []
  const palaceMoves: string[] = []
  const palaceFolderCreates: string[] = []
  const palaceFolderRenames: string[] = []
  const palaceFolderDeletes: string[] = []
  // 目录一等公民：设计图目录行与文件 path='设计图' 对应
  const palaceFolders: Array<Record<string, unknown>> = [
    { id: 'pfd-design', path: '设计图', createdAt: at(0, 7), updatedAt: at(0, 7) },
  ]
  // 本体文档共享镜像 + 本体侧权威清单（默认空：树中「本体文档」目录不出现）
  const palaceOntologyDocs = options.withOntologyDocs ? palaceOntologyDocsFixture : []
  const ontologyPublishedDocs = options.withOntologyDocs ? ontologyPublishedDocsFixture : { items: [] }
  await page.route('**/api/**', route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (!path.startsWith('/api/')) return route.continue()

    if (path === '/api/v1/models') {
      return json(route, [{
        id: 'model-1', name: 'DeepSeek', config_type: 'llm', provider: 'deepseek',
        api_base: 'https://api.deepseek.com', has_api_key: true, enabled: true, is_default: true,
        last_test_status: 'success', last_tested_at: at(0, 8), last_test_message: 'ok',
        models: ['deepseek-v4-pro'], options: {}, created_by: 'admin',
        created_at: at(0, 8), updated_at: at(0, 8),
      }, {
        id: 'model-2', name: 'Qwen', config_type: 'llm', provider: 'qwen',
        api_base: 'https://dashscope.aliyuncs.com', has_api_key: true, enabled: true, is_default: false,
        last_test_status: 'success', last_tested_at: at(0, 8), last_test_message: 'ok',
        models: ['qwen-max'], options: {}, created_by: 'admin',
        created_at: at(0, 8), updated_at: at(0, 8),
      }, {
        // 后台管线配置（记忆宫殿抽取）：会话下拉不应出现
        id: 'model-3', name: 'MiniMax-记忆宫殿抽取', config_type: 'llm', provider: 'minimax',
        api_base: 'https://api.minimax.io', has_api_key: true, enabled: true, is_default: false,
        last_test_status: 'success', last_tested_at: at(0, 8), last_test_message: 'ok',
        models: ['MiniMax-M3'], options: { usage_tags: ['super_assistant_palace'] }, created_by: 'admin',
        created_at: at(0, 8), updated_at: at(0, 8),
      }, {
        // 仅带 super_assistant 偏好标记的配置仍是可选对话模型
        id: 'model-4', name: 'GLM 对话专用', config_type: 'llm', provider: 'zhipu',
        api_base: 'https://open.bigmodel.cn', has_api_key: true, enabled: true, is_default: false,
        last_test_status: 'success', last_tested_at: at(0, 8), last_test_message: 'ok',
        models: ['glm-5'], options: { usage_tags: ['super_assistant'] }, created_by: 'admin',
        created_at: at(0, 8), updated_at: at(0, 8),
      }])
    }
    // 会话附件：list / upload / delete（仅按会话隔离）
    const filesMatch = path.match(/^\/api\/v2\/super-assistant\/conversations\/([^/]+)\/files(?:\/([^/]+))?$/)
    if (filesMatch) {
      const [, conversationId, artifactId] = filesMatch
      if (request.method() === 'GET' && !artifactId) return json(route, filesByConv[conversationId] || [])
      if (request.method() === 'POST' && !artifactId) {
        const filename = /filename="([^"]+)"/.exec(request.postData() || '')?.[1] || 'upload.bin'
        fileUploads.push(filename)
        const row = {
          id: `f-${fileUploads.length + 1}`, filename, mimeType: 'text/markdown', size: 64,
          extractedChars: 10, extractError: null, createdAt: at(0, 10),
        }
        filesByConv[conversationId] = [...(filesByConv[conversationId] || []), row]
        return json(route, row, 201)
      }
      if (request.method() === 'DELETE' && artifactId) {
        fileDeletes.push(artifactId)
        filesByConv[conversationId] = (filesByConv[conversationId] || []).filter(row => row.id !== artifactId)
        return route.fulfill({ status: 204 })
      }
    }
    // 知识图谱：文件库 / 图谱 / 上传 / 删除 / 重建 / 目录 / 笔记 / 移动
    if (path === '/api/v2/super-assistant/palace/files') {
      if (request.method() === 'GET') return json(route, palaceFiles)
      if (request.method() === 'POST') {
        const body = request.postData() || ''
        const filename = /filename="([^"]+)"/.exec(body)?.[1] || 'palace.bin'
        const isImage = /\.(png|jpe?g|gif|webp)$/i.test(filename)
        // folder_path 表单字段（上传归位到选中目录），归一化同后端口径
        const rawFolder = /name="folder_path"\r?\n\r?\n([^\r\n]*)/.exec(body)?.[1] ?? ''
        const folderPath = rawFolder.split('/').map(part => part.trim()).filter(Boolean).join('/')
        palaceUploads.push(`${filename}@${folderPath}`)
        palaceFiles.unshift({
          id: `pf-new-${palaceUploads.length}`, filename, path: folderPath,
          mimeType: isImage ? 'image/png' : 'text/markdown', size: 64,
          sha256: 'new-hash', extractedChars: 10, status: isImage ? 'built' : 'pending', error: null,
          entityCount: 0, relationCount: 0, editable: !isImage, isImage,
          createdAt: at(0, 10), updatedAt: at(0, 10),
        })
        return json(route, palaceFiles[0], 201)
      }
    }
    // 新建 md 笔记：draft 态，不触发抽取
    if (path === '/api/v2/super-assistant/palace/files/notes' && request.method() === 'POST') {
      const body = JSON.parse(request.postData() || '{}')
      const row = {
        id: `pf-note-${palaceNotes.length + 1}`, filename: body.filename, path: body.folderPath ?? '',
        mimeType: 'text/markdown', size: 0, sha256: 'note-hash', extractedChars: 0,
        status: 'draft', error: null, entityCount: 0, relationCount: 0, editable: true, isImage: false,
        createdAt: at(0, 10), updatedAt: at(0, 10),
      }
      palaceFiles.unshift(row)
      palaceNotes.push(`${body.filename}@${body.folderPath ?? ''}`)
      return json(route, row, 201)
    }
    // 拖拽移动文件：PATCH folderPath（空串=根目录）
    const palaceMoveMatch = path.match(/^\/api\/v2\/super-assistant\/palace\/files\/([^/]+)$/)
    if (palaceMoveMatch && request.method() === 'PATCH') {
      const row = palaceFiles.find(item => item.id === palaceMoveMatch[1])
      if (!row) return json(route, { detail: '文件不存在' }, 404)
      const body = JSON.parse(request.postData() || '{}')
      palaceMoves.push(`${palaceMoveMatch[1]}:${row.path}->${body.folderPath}`)
      row.path = body.folderPath ?? ''
      row.updatedAt = at(0, 10)
      return json(route, row)
    }
    // 目录 CRUD：POST 支持 mkdir -p；PATCH 做前缀重写（子孙目录+文件）；DELETE 仅空目录
    if (path === '/api/v2/super-assistant/palace/folders') {
      if (request.method() === 'GET') return json(route, palaceFolders)
      if (request.method() === 'POST') {
        const raw = String(JSON.parse(request.postData() || '{}').path ?? '')
        const norm = raw.split('/').map(part => part.trim()).filter(Boolean).join('/')
        const parts = norm.split('/')
        for (let depth = 1; depth < parts.length; depth += 1) {
          const ancestor = parts.slice(0, depth).join('/')
          if (!palaceFolders.some(folder => folder.path === ancestor)) {
            palaceFolders.push({ id: `pfd-${palaceFolders.length + 1}-${ancestor}`, path: ancestor, createdAt: at(0, 10), updatedAt: at(0, 10) })
          }
        }
        if (palaceFolders.some(folder => folder.path === norm)) {
          return json(route, { detail: '同名目录已存在' }, 409)
        }
        const row = { id: `pfd-${palaceFolders.length + 1}`, path: norm, createdAt: at(0, 10), updatedAt: at(0, 10) }
        palaceFolders.push(row)
        palaceFolderCreates.push(norm)
        return json(route, row, 201)
      }
    }
    const palaceFolderMatch = path.match(/^\/api\/v2\/super-assistant\/palace\/folders\/([^/]+)$/)
    if (palaceFolderMatch && request.method() === 'PATCH') {
      const row = palaceFolders.find(folder => folder.id === palaceFolderMatch[1])
      if (!row) return json(route, { detail: '目录不存在' }, 404)
      const body = JSON.parse(request.postData() || '{}')
      const oldPath = String(row.path)
      const newPath = String(body.path ?? '')
      for (const folder of palaceFolders) {
        if (folder.id !== row.id && String(folder.path).startsWith(`${oldPath}/`)) {
          folder.path = newPath + String(folder.path).slice(oldPath.length)
        }
      }
      row.path = newPath
      for (const fileRow of palaceFiles) {
        if (fileRow.path === oldPath) fileRow.path = newPath
        else if (String(fileRow.path).startsWith(`${oldPath}/`)) {
          fileRow.path = newPath + String(fileRow.path).slice(oldPath.length)
        }
      }
      palaceFolderRenames.push(`${oldPath}->${newPath}`)
      return json(route, row)
    }
    if (palaceFolderMatch && request.method() === 'DELETE') {
      const index = palaceFolders.findIndex(folder => folder.id === palaceFolderMatch[1])
      if (index < 0) return json(route, { detail: '目录不存在' }, 404)
      const targetPath = String(palaceFolders[index].path)
      const hasChild = palaceFolders.some((folder, position) =>
        position !== index && String(folder.path).startsWith(`${targetPath}/`))
        || palaceFiles.some(fileRow =>
          fileRow.path === targetPath || String(fileRow.path).startsWith(`${targetPath}/`))
      if (hasChild) return json(route, { detail: '目录非空：请先删除其中的文件与子目录' }, 409)
      palaceFolders.splice(index, 1)
      palaceFolderDeletes.push(targetPath)
      return route.fulfill({ status: 204 })
    }
    const palaceDeleteMatch = path.match(/^\/api\/v2\/super-assistant\/palace\/files\/([^/]+)$/)
    if (palaceDeleteMatch && request.method() === 'DELETE') {
      palaceDeletes.push(palaceDeleteMatch[1])
      const index = palaceFiles.findIndex(row => row.id === palaceDeleteMatch[1])
      if (index >= 0) palaceFiles.splice(index, 1)
      return route.fulfill({ status: 204 })
    }
    const palaceRebuildMatch = path.match(/^\/api\/v2\/super-assistant\/palace\/files\/([^/]+)\/rebuild$/)
    if (palaceRebuildMatch && request.method() === 'POST') {
      return json(route, { dispatched: true })
    }
    // ZIP 批量导入：created 1 个 + skipped 1 个（mock 不校验真实 zip 字节，只看 multipart 文件名）
    if (path === '/api/v2/super-assistant/palace/files/batch' && request.method() === 'POST') {
      const filename = /filename="([^"]+)"/.exec(request.postData() || '')?.[1] || 'import.zip'
      palaceBatchImports.push(filename)
      const root = filename.replace(/\.zip$/i, '')
      const created = {
        id: 'pf-zip-1', filename: '导入笔记.md', path: root, mimeType: 'text/markdown', size: 128,
        sha256: 'pf-zip-1-hash', extractedChars: 0, status: 'pending', error: null,
        entityCount: 0, relationCount: 0, editable: true, isImage: false, createdAt: at(0, 10), updatedAt: at(0, 10),
      }
      palaceFiles.unshift(created)
      return json(route, {
        created: [created],
        skipped: [{ filename: '重复笔记.md', reason: '同名文件已存在' }],
      }, 201)
    }
    const palaceRawMatch = path.match(/^\/api\/v2\/super-assistant\/palace\/files\/([^/]+)\/raw$/)
    if (palaceRawMatch && request.method() === 'GET') {
      const row = palaceFiles.find(item => item.id === palaceRawMatch[1])
      if (!row) return json(route, { detail: '文件不存在' }, 404)
      palaceRaws.push(palaceRawMatch[1])
      return route.fulfill({
        status: 200,
        headers: { 'content-type': String(row.mimeType) },
        body: Buffer.from('89504e470d0a1a0a-png-fake-bytes'),
      })
    }
    const palacePreviewMatch = path.match(/^\/api\/v2\/super-assistant\/palace\/files\/([^/]+)\/preview$/)
    if (palacePreviewMatch && request.method() === 'GET') {
      palacePreviews.push(palacePreviewMatch[1])
      const row = palaceFiles.find(item => item.id === palacePreviewMatch[1])
      if (!row) return json(route, { detail: '文件不存在' }, 404)
      const isText = row.mimeType === 'text/markdown' || String(row.filename).endsWith('.txt')
      // 与后端口径一致：md/txt 即使内容为空（draft 笔记）也 previewable=true
      const draft = row.status === 'draft'
      return json(route, {
        file: row,
        content: draft ? '' : isText ? `# ${row.filename}\n\n张三 任职 ACME，正在研究知识图谱。` : '',
        truncated: false,
        previewable: isText,
      })
    }
    const palaceContentMatch = path.match(/^\/api\/v2\/super-assistant\/palace\/files\/([^/]+)\/content$/)
    if (palaceContentMatch && request.method() === 'PUT') {
      palaceContentPuts.push(request.postData() || '')
      const row = palaceFiles.find(item => item.id === palaceContentMatch[1])
      if (!row) return json(route, { detail: '文件不存在' }, 404)
      row.status = 'pending'
      row.updatedAt = at(0, 10)
      return json(route, row)
    }
    const palaceReplaceMatch = path.match(/^\/api\/v2\/super-assistant\/palace\/files\/([^/]+)\/replace$/)
    if (palaceReplaceMatch && request.method() === 'POST') {
      const filename = /filename="([^"]+)"/.exec(request.postData() || '')?.[1] || 'replace.bin'
      palaceReplaces.push(`${palaceReplaceMatch[1]}:${filename}`)
      const row = palaceFiles.find(item => item.id === palaceReplaceMatch[1])
      if (!row) return json(route, { detail: '文件不存在' }, 404)
      row.filename = filename
      row.status = 'pending'
      row.updatedAt = at(0, 10)
      return json(route, row)
    }
    if (path === '/api/v2/super-assistant/palace/graph' && request.method() === 'GET') {
      return json(route, palaceGraphFixture)
    }
    // 本体文档共享镜像（宫殿侧状态）与本体侧权威清单（发布文档摘要）
    if (path === '/api/v2/super-assistant/palace/ontology-documents' && request.method() === 'GET') {
      return json(route, palaceOntologyDocs)
    }
    if (path === '/api/v1/ontologies/published-documents' && request.method() === 'GET') {
      return json(route, ontologyPublishedDocs)
    }
    const ontoPreviewMatch = path.match(/^\/api\/v2\/super-assistant\/palace\/ontology-documents\/([^/]+)\/preview$/)
    if (ontoPreviewMatch && request.method() === 'GET') {
      const row = palaceOntologyDocs.find(item => item.id === ontoPreviewMatch[1])
      if (!row) return json(route, { detail: '本体文档不存在' }, 404)
      return json(route, {
        file: row,
        content: '# 供应链业务文档\n\n张三 任职 ACME，ACME 位于上海。',
        truncated: false,
        previewable: true,
      })
    }
    if (path === '/api/v2/super-assistant/palace/graph/search' && request.method() === 'GET') {
      palaceGraphSearches.push(new URL(request.url()).searchParams.get('q') || '')
      return json(route, {
        available: true,
        entities: [palaceGraphFixture.nodes[0], palaceGraphFixture.nodes[1]],
        relations: [{
          source: 'e-1', target: 'e-2', source_name: '张三', target_name: 'ACME',
          name: '任职', source_files: ['个人知识库.md'], file_ids: ['pf-1'],
        }],
      })
    }
    // 会话流式对话：可控延迟的 SSE
    const chatMatch = path.match(/^\/api\/v2\/super-assistant\/conversations\/([^/]+)\/chat$/)
    if (chatMatch && request.method() === 'POST') {
      chatCalls.push(chatMatch[1])
      const convId = chatMatch[1]
      const chatBody = JSON.parse(request.postData() || '{}')
      const reply = '你好，我是超级助手'
      const fulfill = () => {
        chatDone = true
        if (convId !== 'c-today') {
          chatPersisted.set(convId, [
            { id: `m-${convId}-u`, conversation_id: convId, role: 'user', content: String(chatBody.message ?? ''), status: 'complete', steps: [], token_usage: {}, created_at: at(0, 12) },
            { id: `m-${convId}-a`, conversation_id: convId, role: 'assistant', content: reply, status: 'complete', steps: [], token_usage: {}, created_at: at(0, 12) },
          ])
        }
        return route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          body: sseBody(reply),
        })
      }
      if (options.chatDelayMs) {
        return new Promise(resolve => setTimeout(() => resolve(fulfill()), options.chatDelayMs))
      }
      return fulfill()
    }
    // —— 手动转发 + 入口测试（admin JWT）——
    const notifForwardMatch = path.match(/^\/api\/v2\/notifications\/([^/]+)\/forward$/)
    if (notifForwardMatch && request.method() === 'POST') {
      notifForwards.push(notifForwardMatch[1] + ':' + (request.postData() || ''))
      const body = JSON.parse(request.postData() || '{}')
      return json(route, (body.channelIds || []).map((id: string) => ({ channelId: id, ok: true, message: '已转发' })))
    }
    const notifKeyTestMatch = path.match(/^\/api\/v2\/notifications\/ingest-keys\/([^/]+)\/test$/)
    if (notifKeyTestMatch && request.method() === 'POST') {
      notifKeyTests.push(notifKeyTestMatch[1])
      return json(route, { ok: true, message: '测试消息已投递（来源 billing），出口即时投递：成功 1 条', messageId: 'n-lt' })
    }
    // —— 渠道模板与 SMTP（admin JWT）——
    if (path === '/api/v2/notifications/channel-templates') {
      return json(route, [
        { id: 'dingtalk', name: '钉钉机器人', description: '钉钉群', fields: [
          { key: 'token', label: 'access_token', hint: '钉钉群 → 设置 → 机器人', required: true, secret: true, placeholder: '' },
          { key: 'secret', label: '加签密钥（可选）', hint: '', required: false, secret: true, placeholder: '' },
        ] },
        { id: 'custom', name: '自定义 apprise（高级）', description: '裸 URL', fields: [
          { key: 'url', label: 'apprise URL', hint: '', required: true, secret: true, placeholder: '' },
        ] },
        { id: 'email', name: '邮件', description: '邮件', fields: [
          { key: 'recipients', label: '收件邮箱', hint: '多个逗号分隔', required: true, secret: false, placeholder: '' },
        ] },
      ])
    }
    if (path === '/api/v2/notifications/smtp') {
      if (request.method() === 'PUT') {
        notifSmtpPuts.push(request.postData() || '')
        return json(route, { host: 'smtp.test.com', port: 465, username: 'noreply@test.com', sender: '', useTls: true, configured: true, hasPassword: true })
      }
      return json(route, { host: '', port: 465, username: '', sender: '', useTls: true, configured: false, hasPassword: false })
    }
    if (path === '/api/v2/notifications/smtp/test') {
      notifSmtpTests.push(request.postData() || '')
      return json(route, { ok: true, message: '测试邮件已发送至 e2e@test.com' })
    }
    // —— 转发渠道管理（admin JWT）——
    if (path === '/api/v2/notifications/channels') {
      if (request.method() === 'POST') {
        notifChannelCreates.push(request.postData() || '')
        const body = JSON.parse(request.postData() || '{}')
        return json(route, {
          id: 'ch-2', name: String(body.name ?? ''), urlMasked: 'dingtalk://…demo',
          template: String(body.template ?? 'custom'), templateName: '钉钉机器人',
          display: 'toke…demo', fields: [],
          enabled: true, note: body.note ?? null, lastStatus: null, lastError: '',
          lastSentAt: null, createdAt: at(0, 10), updatedAt: at(0, 10),
        }, 201)
      }
      return json(route, [
        { id: 'ch-1', name: '钉钉运维群', urlMasked: 'dingtalk://…abcd',
          template: 'dingtalk', templateName: '钉钉机器人',
          display: 'toke…abcd · SEC…wxyz',
          fields: [
            { key: 'token', label: 'access_token', hint: '', required: true, secret: true, placeholder: '', value: 'toke…abcd' },
            { key: 'secret', label: '加签密钥（可选）', hint: '', required: false, secret: true, placeholder: '', value: 'SEC…wxyz' },
          ],
          enabled: true, note: '值班告警', lastStatus: 'sent', lastError: '', lastSentAt: at(0, 9),
          createdAt: at(1, 8), updatedAt: at(0, 9) },
        { id: 'ch-2', name: '双端收件邮箱', urlMasked: 'mailtos://…2925.com',
          template: 'email', templateName: '邮件',
          display: 'ops@example.com',
          fields: [
            { key: 'recipients', label: '收件邮箱', hint: '', required: true, secret: false, placeholder: '', value: 'ops@example.com' },
          ],
          enabled: true, note: null, lastStatus: null, lastError: '', lastSentAt: null,
          createdAt: at(1, 8), updatedAt: at(1, 8) },
      ])
    }
    const notifChannelTestMatch = path.match(/^\/api\/v2\/notifications\/channels\/([^/]+)\/test$/)
    if (notifChannelTestMatch && request.method() === 'POST') {
      notifChannelTests.push(notifChannelTestMatch[1])
      return json(route, { ok: true, message: '测试消息已发送' })
    }
    const notifChannelMatch = path.match(/^\/api\/v2\/notifications\/channels\/([^/]+)$/)
    if (notifChannelMatch) {
      if (request.method() === 'PATCH') {
        notifChannelUpdates.push(request.postData() || '')
        return json(route, {
          id: notifChannelMatch[1], name: '钉钉运维群', urlMasked: 'dingtalk://…abcd',
          enabled: !JSON.parse(request.postData() || '{}').enabled === false
            ? Boolean(JSON.parse(request.postData() || '{}').enabled)
            : false,
          note: '值班告警', lastStatus: 'sent', lastError: '', lastSentAt: at(0, 9),
          createdAt: at(1, 8), updatedAt: at(0, 10),
        })
      }
      if (request.method() === 'DELETE') {
        notifChannelDeletes.push(notifChannelMatch[1])
        return json(route, { deleted: notifChannelMatch[1] })
      }
    }
    // —— 对外投递密钥管理（admin JWT）——
    if (path === '/api/v2/notifications/ingest-keys') {
      if (request.method() === 'POST') {
        notifKeyCreates.push(request.postData() || '')
        const body = JSON.parse(request.postData() || '{}')
        return json(route, {
          id: 'nk-2', name: String(body.name ?? ''), keyPrefix: 'ob_notif_e2e',
          enabled: true, allowedSourceSystem: body.allowedSourceSystem ?? null,
          createdAt: at(0, 10), lastUsedAt: null, revokedAt: null,
          plaintextKey: 'ob_notif_e2e_demo_plaintext_secret',
        }, 201)
      }
      return json(route, [
        { id: 'nk-1', name: 'billing', keyPrefix: 'ob_notif_abc123', enabled: true,
          allowedSourceSystem: null, createdAt: at(1, 8), lastUsedAt: at(0, 9), revokedAt: null },
        { id: 'nk-0', name: 'legacy-portal', keyPrefix: 'ob_notif_old', enabled: false,
          allowedSourceSystem: null, createdAt: at(3, 8), lastUsedAt: at(2, 8), revokedAt: at(2, 9) },
      ])
    }
    const notifKeyMatch = path.match(/^\/api\/v2\/notifications\/ingest-keys\/([^/]+)$/)
    if (notifKeyMatch && request.method() === 'DELETE') {
      notifKeyRevokes.push(notifKeyMatch[1])
      return json(route, { id: notifKeyMatch[1], revoked: true })
    }
    // —— 消息通知（管理员消息总线；静态路由先于 /{id} 动态段）——
    if (path === '/api/v2/notifications/summary') {
      return json(route, {
        unreadCount: notificationsState.filter(item => !item.isRead && !item.isArchived).length,
        starredCount: notificationsState.filter(item => item.isStarred && !item.isArchived).length,
        archivedCount: notificationsState.filter(item => item.isArchived).length,
        // totalCount = 真全量（= 全部消息未归档 + 已归档，对账锚点）
        totalCount: notificationsState.length,
      })
    }
    if (path === '/api/v2/notifications/stats') {
      const days = Number(new URL(request.url()).searchParams.get('days') || 30)
      notifStatsDays.push(days)
      // 与真实 API 同序：升序补零，今天在末位
      const trend = Array.from({ length: days }, (_, i) => ({
        date: `2026-09-${String(30 - days + 1 + i).padStart(2, '0')}`,
        count: i === days - 1 ? 1 : i === days - 2 ? 2 : 0,
      }))
      return json(route, {
        overview: { totalCount: 5, unreadCount: 2, todayInbound: 1, forwardSuccessRate: 0.8 },
        trend,
        sources: [
          { source: 'platform', count: 3 },
          { source: 'warehouse', count: 1 },
          { source: 'billing', count: 1 },
        ],
        channels: [
          { channelId: 'ch-1', name: '钉钉运维群', sent: 4, failed: 1, pending: 0 },
          { channelId: 'ch-2', name: '双端收件邮箱', sent: 2, failed: 0, pending: 1 },
        ],
      })
    }
    if (path === '/api/v2/notifications/read-all' && request.method() === 'POST') {
      notifReadAllCalls.push('read-all')
      let updated = 0
      for (const item of notificationsState) {
        if (!item.isRead) { item.isRead = true; item.readAt = at(0, 10); updated += 1 }
      }
      return json(route, { updated })
    }
    if (path === '/api/v2/notifications' && request.method() === 'GET') {
      const searchParams = new URL(request.url()).searchParams
      const tab = searchParams.get('tab') ?? 'all'
      let items = notificationsState.filter(item => {
        if (tab === 'unread') return !item.isRead && !item.isArchived
        if (tab === 'starred') return item.isStarred && !item.isArchived
        if (tab === 'archived') return item.isArchived
        return !item.isArchived // all = 未归档收件箱（与已归档互补）
      })
      let nextCursor: string | null = null
      if (options.notificationsPagination) {
        // 数值游标按 2 条/页翻到底（作用于 tab 过滤后的条数；all 视图=未归档）
        const cursor = searchParams.get('cursor')
        const start = cursor ? Number(cursor) : 0
        nextCursor = start + 2 < items.length ? String(start + 2) : null
        items = items.slice(start, start + 2)
      }
      return json(route, { items, nextCursor, hasMore: nextCursor !== null })
    }
    if (path === '/api/v2/notifications' && request.method() === 'POST') {
      notifCreates.push(request.postData() || '')
      const body = JSON.parse(request.postData() || '{}')
      const created: NotificationFixtureRow = {
        id: 'n-new', eventId: null, sourceSystem: 'platform', sourceType: 'manual',
        title: String(body.title ?? '巡检通知'), priority: String(body.priority ?? 'normal'),
        isRead: true, isStarred: false, isArchived: false,
        readAt: at(0, 10), starredAt: null, archivedAt: null,
        createdAt: at(0, 10), updatedAt: at(0, 10), bodyPreview: '', attachmentCount: 0,
      }
      notificationsState.unshift(created)
      return json(route, { ...created, body: notificationDetailBodies['n-new'] ?? '', attachments: [] }, 201)
    }
    const notificationMatch = path.match(/^\/api\/v2\/notifications\/([^/]+)$/)
    if (notificationMatch && !['summary', 'read-all'].includes(notificationMatch[1])) {
      const id = notificationMatch[1]
      const source = notificationsState.find(item => item.id === id)
        ?? { id, eventId: null, sourceSystem: 'platform', sourceType: 'manual', title: '巡检通知', priority: 'normal', isRead: false, isStarred: false, isArchived: false, readAt: null, starredAt: null, archivedAt: null, createdAt: at(0, 10), updatedAt: at(0, 10), bodyPreview: '', attachmentCount: 0 }
      if (request.method() === 'GET') {
        // 服务端语义：查看详情即自动标为已读
        source.isRead = true
        source.readAt = at(0, 10)
        return json(route, { ...source, body: notificationDetailBodies[id] ?? '', attachments: notificationDetailAttachments[id] ?? [] })
      }
      if (request.method() === 'PATCH') {
        notifStatePatches.push(request.postData() || '')
        const patchBody = JSON.parse(request.postData() || '{}')
        Object.assign(source, patchBody)
        return json(route, { ...source, body: notificationDetailBodies[id] ?? '', attachments: notificationDetailAttachments[id] ?? [] })
      }
      if (request.method() === 'DELETE') {
        notifDeletes.push(id)
        const index = notificationsState.findIndex(item => item.id === id)
        if (index >= 0) notificationsState.splice(index, 1)
        return json(route, { deleted: id })
      }
    }
    if (path.startsWith('/api/v2/super-assistant/conversations/') && request.method() === 'PATCH') {
      const id = path.split('/')[5]
      const body = JSON.parse(request.postData() || '{}')
      patchBodies.push(request.postData() || '')
      const source = conversations.find(item => item.id === id)
      // 写回本用例副本：归档/改名持久化，2s 轮询不再洗回（与真实后端语义一致）
      if (source) Object.assign(source, body)
      return json(route, { ...source, ...body })
    }
    const conversationMatch = path.match(/^\/api\/v2\/super-assistant\/conversations\/([^/]+)$/)
    if (conversationMatch && request.method() === 'DELETE') {
      deleteCalls.push(conversationMatch[1])
      return route.fulfill({ status: 204 })
    }
    if (path === '/api/v2/super-assistant/conversations') {
      if (request.method() === 'POST') {
        createCalls.push(request.postData() || '')
        const row = {
          id: `c-new-${createCalls.length}`, title: '新会话', model_config_id: 'model-1',
          status: 'active', created_at: at(0, 12), updated_at: at(0, 12),
        }
        createdConvs.unshift(row)
        return json(route, row, 201)
      }
      return json(route, [...createdConvs, ...conversations])
    }
    if (/^\/api\/v2\/super-assistant\/conversations\/[^/]+\/messages$/.test(path)) {
      const id = path.split('/')[5]
      if (id === 'c-today' && chatDone) {
        return json(route, [
          { id: 'm-1', conversation_id: 'c-today', role: 'user', content: '你好', status: 'complete', steps: [], token_usage: {}, created_at: at(0, 9) },
          { id: 'm-2', conversation_id: 'c-today', role: 'assistant', content: '你好，我是超级助手', status: 'complete', steps: [], token_usage: {}, created_at: at(0, 9) },
        ])
      }
      // c-earlier 是「有消息的历史会话」：新建会话去重、底部输入框等场景以此为夹具
      if (id === 'c-earlier') {
        return json(route, [
          { id: 'm-e1', conversation_id: 'c-earlier', role: 'user', content: '上周的问题', status: 'complete', steps: [], token_usage: {}, created_at: at(5, 10) },
          { id: 'm-e2', conversation_id: 'c-earlier', role: 'assistant', content: '上周的答复', status: 'complete', steps: [], token_usage: {}, created_at: at(5, 10) },
        ])
      }
      // 懒建会话的消息在 chat 完成后随 chatPersisted 落库可见，流式期间仍返回空列表
      const persisted = chatPersisted.get(id)
      if (persisted) return json(route, persisted)
      return json(route, [])
    }
    // 会话审计导出：完整审计 JSON（与后端 export_conversation 同构，含会话审计字段）
    const exportMatch = path.match(/^\/api\/v2\/super-assistant\/conversations\/([^/]+)\/export$/)
    if (exportMatch && request.method() === 'GET') {
      exportCalls.push(exportMatch[1])
      const source = conversations.find(item => item.id === exportMatch[1])
      return json(route, {
        format: 'openontology.super-assistant.conversation',
        version: 1,
        exportedAt: at(0, 10),
        conversation: {
          ...(source ?? {}),
          browser_source_id: null,
          summary: null,
          summary_message_count: 0,
          messageCount: 2,
          messages: [
            { id: 'm-x1', conversation_id: exportMatch[1], role: 'user', content: '统计一下订单量', status: 'complete', steps: [], token_usage: {}, created_at: at(0, 9) },
            { id: 'm-x2', conversation_id: exportMatch[1], role: 'assistant', content: '本月订单量 1200', status: 'complete', steps: [{ tool: 'query', arguments: {}, summary: '查库' }], token_usage: { inputTokens: 30, outputTokens: 12 }, created_at: at(0, 9) },
          ],
        },
        summary: { messageCount: 2, toolStepCount: 1, totalTokens: 42 },
      })
    }
    // 工单提交（会话右键「反馈问题」复用全站工单弹窗）；
    // 形状与后端 ticket_out 对齐（camelCase、无 submitter 对象）
    if (path === '/api/v2/tickets' && request.method() === 'POST') {
      ticketCreates.push(request.postData() || '')
      const body = JSON.parse(request.postData() || '{}')
      return json(route, {
        id: 'tk-e2e-1', ticketNo: 'OB-20261011-0001', title: body.title ?? '',
        category: body.category ?? 'other', content: body.content ?? '',
        status: 'pending', pageUrl: body.page_url ?? null,
        submitterId: 'u-1', submitterName: 'workbench-tester',
        attachmentCount: 0, progressLogs: [],
        createdAt: at(0, 10), updatedAt: at(0, 10),
      }, 201)
    }
    // 远程助手：列表/新增（邀请接入的助手在真实后端经公开兑换端点落库，
    // mock 里直接由列表夹具承载；手动配置走 POST）
    if (path === '/api/v2/super-assistant/remote-agents') {
      if (request.method() === 'POST') {
        const body = request.postDataJSON() as Record<string, unknown>
        remoteAgentCreates.push(body)
        const created = {
          id: `ra-${remoteAgents.length + 1}`, key: body.key || 'remote.auto',
          label: body.label, description: body.description || '', endpoint: body.endpoint || '',
          token_set: !!body.token, enabled: body.enabled !== false,
          timeout_seconds: body.timeout_seconds || 120, mode: 'direct',
          last_seen_at: null, last_turn_at: null,
        }
        remoteAgents.push(created)
        return json(route, created)
      }
      if (options.remoteAgentsDelayMs) {
        return new Promise(resolve => setTimeout(() => resolve(json(route, remoteAgents)), options.remoteAgentsDelayMs))
      }
      return json(route, remoteAgents)
    }
    if (path === '/api/v2/super-assistant/remote-agents/ra-direct/test') {
      return json(route, { ok: true, message: 'pong' })
    }
    if (path === '/api/v2/super-assistant/remote-agent-invites') {
      if (request.method() === 'POST') {
        remoteInvites.push({
          id: `inv-${remoteInvites.length + 1}`, status: 'pending',
          created_at: new Date().toISOString(),
          expires_at: new Date(Date.now() + 24 * 3600 * 1000).toISOString(),
          redeemed_agent_key: null, redeemed_agent_label: null,
        })
        remoteInviteCreates.push('created')
        return json(route, { ...remoteInvites[remoteInvites.length - 1], prompt_text: remoteInvitePromptFixture })
      }
      return json(route, remoteInvites)
    }
    if (path.startsWith('/api/v2/super-assistant/remote-agent-invites/') && path.endsWith('/prompt')) {
      return route.fulfill({ status: 200, contentType: 'text/plain; charset=utf-8', body: remoteInvitePromptFixture })
    }
    if (path.startsWith('/api/v2/super-assistant/remote-agent-invites/') && request.method() === 'DELETE') {
      const target = remoteInvites.find(invite => path.endsWith(`/${invite.id}`))
      if (target) (target as Record<string, unknown>).status = 'revoked'
      return route.fulfill({ status: 204, body: '' })
    }
    if (path === '/api/v2/super-assistant/skills') return json(route, [])
    if (path === '/api/v2/super-assistant/mcp-servers') return json(route, [])
    // 内置工具目录与启停（PATCH 后按提交值刷新目录，驱动开关/徽章回显）
    if (path === '/api/v2/super-assistant/tools') return json(route, toolsState)
    const toolMatch = path.match(/^\/api\/v2\/super-assistant\/tools\/([^/]+)$/)
    if (toolMatch && request.method() === 'PATCH') {
      const name = decodeURIComponent(toolMatch[1])
      const body = request.postDataJSON() as { enabled: boolean }
      toolPatchCalls.push({ name, body })
      const target = toolsState.find(item => item.name === name)
      if (!target) return route.fulfill({ status: 404, body: '{"detail":"未知内置工具"}' })
      target.enabled = body.enabled
      return json(route, target)
    }
    // multica 外部集成：配置读写与连接测试（PUT 后按提交值回显已启用态）
    if (path === '/api/v2/super-assistant/multica/config') {
      if (request.method() === 'PUT') {
        const body = request.postDataJSON() as Record<string, unknown>
        multicaPuts.push(body)
        return json(route, {
          ...multicaUnconfiguredFixture,
          ...body,
          configured: true,
          token_set: true,
          commands: body.enabled === false ? [] : multicaCommandsFixture,
        })
      }
      return json(route, options.multicaConfig ?? multicaUnconfiguredFixture)
    }
    if (path === '/api/v2/super-assistant/multica/test') {
      multicaTests.push(request.postDataJSON() as Record<string, unknown>)
      return json(route, {
        ok: true,
        message: '连接成功：admin，可见 2 个工作区',
        account_name: 'admin',
        workspaces: [
          { id: 'ws-1', name: 'My Workspace', slug: 'my-workspace' },
          { id: 'ws-2', name: 'E2E 工作区', slug: 'e2e' },
        ],
      })
    }
    // 已保存配置的实时工作区列表：弹窗打开即拉取（与测试连接同一后端数据源）
    if (path === '/api/v2/super-assistant/multica/workspaces') {
      multicaWorkspaceCalls.push(request.url())
      return json(route, {
        workspaces: [
          { id: 'ws-1', name: 'My Workspace', slug: 'my-workspace' },
          { id: 'ws-2', name: 'E2E 工作区', slug: 'e2e' },
        ],
      })
    }
    // 全局搜索：会话标题 + 消息内容（查询词含「需求」时命中 c-today 的标题与一条消息）
    if (path === '/api/v2/super-assistant/search/conversations') {
      const q = new URL(request.url()).searchParams.get('q') || ''
      searchQueries.push(q)
      return json(route, {
        query: q,
        conversations: q.includes('需求') ? [{
          id: 'c-today', title: '今日需求梳理', status: 'active', updatedAt: at(0, 9),
          titleMatched: true,
          messageHits: [{
            messageId: 'm-1', role: 'user',
            snippet: '……这是需求梳理的上下文片段……', createdAt: at(0, 9),
          }],
        }] : [],
      })
    }
    if (path === '/api/v2/super-assistant/scheduled-tasks') {
      if (request.method() === 'POST') {
        const body = request.postDataJSON() as Record<string, unknown>
        const targetKind = body.target_kind ?? 'super_assistant'
        const targetTemplateId = (body.target_template_id ?? null) as string | null
        const created = {
          id: `st-${Date.now()}`,
          title: body.title || String(body.instruction || '定时任务').slice(0, 40),
          instruction: body.instruction,
          target_kind: targetKind,
          target_template_id: targetTemplateId,
          target_template_name: targetTemplateId
            ? tiTemplatesFixture.find(item => item.id === targetTemplateId)?.name ?? null
            : null,
          schedule_kind: body.schedule_kind,
          timezone: 'Asia/Shanghai',
          run_at: body.run_at || null,
          hour: body.hour ?? null,
          minute: body.minute ?? null,
          weekday: body.weekday ?? null,
          enabled: body.enabled !== false,
          next_run_at: body.run_at || at(0, 10),
          last_dispatched_at: null,
          last_run_id: null,
          last_run_status: null,
          last_run_at: null,
          created_at: at(0, 10),
          updated_at: at(0, 10),
        }
        scheduledTasks.unshift(created)
        return json(route, created, 201)
      }
      return json(route, scheduledTasks)
    }
    const scheduledRunMatch = path.match(/^\/api\/v2\/super-assistant\/scheduled-tasks\/([^/]+)\/runs(?:\/([^/]+))?$/)
    if (scheduledRunMatch) {
      const taskId = scheduledRunMatch[1]
      const runId = scheduledRunMatch[2]
      const taskRuns = scheduledRuns.filter(item => item.task_id === taskId)
      if (runId) {
        const row = taskRuns.find(item => item.id === runId)
        return row ? json(route, row) : json(route, { detail: '执行记录不存在' }, 404)
      }
      return json(route, taskRuns)
    }
    const scheduledTaskMatch = path.match(/^\/api\/v2\/super-assistant\/scheduled-tasks\/([^/]+)$/)
    if (scheduledTaskMatch) {
      const row = scheduledTasks.find(item => item.id === scheduledTaskMatch[1])
      if (!row) return json(route, { detail: '定时任务不存在' }, 404)
      if (request.method() === 'DELETE') {
        const index = scheduledTasks.findIndex(item => item.id === row.id)
        if (index >= 0) scheduledTasks.splice(index, 1)
        return route.fulfill({ status: 204 })
      }
      if (request.method() === 'PATCH') {
        Object.assign(row, request.postDataJSON())
        return json(route, row)
      }
      return json(route, row)
    }
    if (path === '/api/v2/inbox/summary') {
      return json(route, { openAlertCount: 0, actionableCount: 0, unreadCount: 0, resolvedCount: 0 })
    }
    if (path === '/api/v1/overview/stats') {
      return json(route, {
        ontology_count: 1, entity_count: 2, relation_count: 3, logic_count: 4, action_count: 5,
        rule_hits: 6, recent_ontologies: [], domain_counts: {}, status_counts: {},
      })
    }
    return json(route, [])
  })
  return {
    patchBodies,
    deleteCalls,
    createCalls,
    fileUploads,
    fileDeletes,
    chatCalls,
    exportCalls,
    ticketCreates,
    searchQueries,
    multicaPuts,
    multicaTests,
    remoteAgentCreates,
    remoteInviteCreates,
    notifStatePatches,
    notifCreates,
    notifReadAllCalls,
    notifStatsDays,
    notifDeletes,
    notifKeyCreates,
    notifKeyRevokes,
    notifChannelCreates,
    notifSmtpPuts,
    notifSmtpTests,
    notifForwards,
    notifKeyTests,
    notifChannelUpdates,
    notifChannelTests,
    notifChannelDeletes,
    multicaWorkspaceCalls,
    toolPatchCalls,
    palaceUploads,
    palaceDeletes,
    palacePreviews,
    palaceContentPuts,
    palaceReplaces,
    palaceBatchImports,
    palaceGraphSearches,
    palaceRaws,
    palaceNotes,
    palaceMoves,
    palaceFolderCreates,
    palaceFolderRenames,
    palaceFolderDeletes,
    isChatDone: () => chatDone,
  }
}

test('工作台骨架：九项入口齐备，近期会话单列表，归档折叠', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await page.goto('/#/super-assistant')

  await expect(page.getByRole('button', { name: '新建会话' })).toBeVisible()
  await expect(page.getByRole('button', { name: /全局搜索/ })).toBeVisible()
  // 消息通知与任务实例均已上线：可点击（任务实例打开弹窗）
  const notificationsEntry = page.getByRole('button', { name: /消息通知/ })
  await expect(notificationsEntry).toBeVisible()
  await expect(notificationsEntry).toBeEnabled()
  await expect(notificationsEntry).toContainText('2')
  await expect(page.getByRole('button', { name: '定时任务' })).toBeVisible()
  const taskInstancesEntry = page.getByRole('button', { name: /任务实例/ })
  await expect(taskInstancesEntry).toBeVisible()
  await expect(taskInstancesEntry).toBeEnabled()
  await expect(page.getByRole('button', { name: '知识图谱' })).toBeVisible()
  await expect(page.getByRole('link', { name: '本体治理' })).toBeVisible()
  await expect(page.getByRole('button', { name: '外部集成' })).toBeVisible()
  await expect(page.getByRole('button', { name: /退出登录/ })).toBeVisible()

  // 功能项顺序契约：全局搜索 → 消息通知 → 定时任务 → 任务实例 → 知识图谱 → 外部集成 → 本体治理
  const navEntries = page.locator('nav[aria-label="工作台功能"] > *')
  const expectedOrder = ['全局搜索', '消息通知', '定时任务', '任务实例', '知识图谱', '外部集成', '本体治理']
  await expect(navEntries).toHaveCount(expectedOrder.length)
  for (let index = 0; index < expectedOrder.length; index += 1) {
    await expect(navEntries.nth(index)).toContainText(expectedOrder[index])
  }

  // 近期会话合并为单列表（不再有今日/昨日/历史分组标签）
  await expect(page.locator('[data-workbench-group="recent"] [data-workbench-conversation="c-today"]')).toHaveCount(1)
  await expect(page.locator('[data-workbench-group="recent"] [data-workbench-conversation="c-yesterday"]')).toHaveCount(1)
  await expect(page.locator('[data-workbench-group="recent"] [data-workbench-conversation="c-earlier"]')).toHaveCount(1)
  await expect(page.locator('[data-slot="sidebar-group-label"]')).toHaveCount(0)
  // 归档区默认折叠：标题不再带计数，条目不可见
  await expect(page.getByRole('button', { name: '归档会话', exact: true })).toBeVisible()
  await expect(page.locator('[data-workbench-group="archived"] [data-workbench-conversation="c-archived"]')).toHaveCount(0)

  // 聊天区就绪（输入框占位符来自模型加载成功分支）
  await expect(page.getByTestId('super-assistant-composer')).toBeVisible()
})

test('消息通知：徽章弹窗、tab 顺序与筛选、Markdown 详情与附件、标记/归档/全部已读/定向发送', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page)
  await page.goto('/#/super-assistant')

  // 侧栏入口带未读徽章，点击打开弹窗
  const entry = page.getByRole('button', { name: /消息通知/ })
  await expect(entry).toBeVisible()
  await expect(entry).toContainText('2')
  await entry.click()

  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText('消息通知')

  // 误触遮罩不关闭弹窗：发送分区草稿与列表滚动位置不因误触丢失
  //（Esc 分层回退与右上关闭按钮不受影响，下文的 Esc 关闭契约仍生效）
  await page.mouse.click(5, 400)
  await expect(dialog).toBeVisible()

  // 分区导航四区：消息列表 / 发送消息 / 渠道配置 / 消息统计
  const navKeys = await dialog.locator('[data-notifications-nav]').evaluateAll(
    els => els.map(el => (el as HTMLElement).dataset.notificationsNav),
  )
  expect(navKeys).toEqual(['messages', 'compose', 'config', 'stats'])

  // tab 顺序契约：全部消息(默认) → 已标记 → 已归档
  const tabKeys = await dialog.locator('[data-notifications-tab]').evaluateAll(
    els => els.map(el => (el as HTMLElement).dataset.notificationsTab),
  )
  expect(tabKeys).toEqual(['all', 'starred', 'archived'])
  await expect(dialog.locator('[data-notifications-tab="all"]')).toHaveAttribute('aria-pressed', 'true')
  await expect(dialog.locator('[data-notifications-tab="all"]')).toContainText('全部消息')

  // 默认「全部消息」tab：未归档消息可见（n-1 平台告警、n-2 外部告警），已归档不可见
  await expect(page.locator('[data-notifications-item="n-1"]')).toBeVisible()
  await expect(page.locator('[data-notifications-item="n-2"]')).toBeVisible()
  await expect(page.locator('[data-notifications-item="n-3"]')).toHaveCount(0)

  // urgent 带左缘色条锚点（优先级不止 6px 圆点的颜色编码，有无色条是二值信号）
  await expect(page.locator('[data-notifications-item="n-2"]')).toHaveAttribute('data-notifications-priority', 'urgent')
  // 列表预览按行首锚定剥离 Markdown 语法：预览从正文派生（含标题行与换行），剥离由前端完成
  const preview = page.locator('[data-notifications-item="n-1"] [data-notifications-preview]')
  await expect(preview).toContainText('数据任务失败报告 数据库连接超时')
  await expect(preview).not.toContainText('#')

  // tab 筛选语义：已标记仅 n-2；已归档仅 n-3；全部消息 = 未归档收件箱（n-3 不可见，与已归档互补）
  await page.locator('[data-notifications-tab="starred"]').click()
  await expect(page.locator('[data-notifications-item="n-2"]')).toBeVisible()
  await expect(page.locator('[data-notifications-item="n-1"]')).toHaveCount(0)
  await page.locator('[data-notifications-tab="archived"]').click()
  await expect(page.locator('[data-notifications-item="n-3"]')).toBeVisible()
  await expect(page.locator('[data-notifications-item="n-1"]')).toHaveCount(0)
  await page.locator('[data-notifications-tab="all"]').click()
  await expect(page.locator('[data-notifications-item="n-1"]')).toBeVisible()
  await expect(page.locator('[data-notifications-item="n-3"]')).toHaveCount(0)

  // 详情：标题、Markdown 表格/代码块、附件行
  await page.locator('[data-notifications-item="n-1"]').click()
  await expect(page.locator('[data-notifications-detail]')).toContainText('数据任务失败报告')
  // 详情头无删除入口：处置只保留标记/归档/转发/标为未读（详情已渲染，此时计数才有守护力）
  await expect(page.locator('[data-notifications-delete]')).toHaveCount(0)
  // 详情头单行化：lg+ 宽度下来源/时间与优先级章、标题、操作图标同排（<lg 标题退居第二行保可读）
  await expect(page.locator('[data-notifications-detail-meta]')).toBeVisible()
  await expect(page.locator('[data-notifications-detail-meta]')).toContainText('platform')
  await expect(page.locator('[data-notifications-detail] table')).toBeVisible()
  await expect(page.locator('[data-notifications-detail] pre')).toBeVisible()
  await expect(page.locator('[data-notifications-attachment="na-1"]')).toBeVisible()
  await expect(page.locator('[data-notifications-attachment="na-1"]')).toContainText('错误日志.txt')

  // 图标操作钮带 Tooltip（与 aria-label 同源文案，鼠标悬停可见）
  await page.locator('[data-notifications-star]').hover()
  await expect(page.getByRole('tooltip')).toContainText('标记为重要')

  // 标记（星标）与归档触发状态 PATCH
  await page.locator('[data-notifications-star]').click()
  await expect.poll(() => mocks.notifStatePatches.length).toBe(1)
  await expect.poll(() => mocks.notifStatePatches[0] ?? '').toContain('isStarred')
  await page.locator('[data-notifications-archive]').click()
  await expect.poll(() => mocks.notifStatePatches.length).toBe(2)
  await expect.poll(() => mocks.notifStatePatches[1] ?? '').toContain('isArchived')

  // 全部已读
  await page.locator('[data-notifications-read-all]').click()
  await expect.poll(() => mocks.notifReadAllCalls.length).toBe(1)

  // 发送消息（独立分区）：转发渠道为下拉多选（触发器常显已选去向），默认全选，取消其一后定向转发
  await page.locator('[data-notifications-nav="compose"]').click()
  await expect(page.locator('[data-notifications-compose-panel]')).toBeVisible()
  const chipCh1 = page.locator('[data-notifications-compose-channel="ch-1"]')
  const chipCh2 = page.locator('[data-notifications-compose-channel="ch-2"]')
  const openChannelPicker = async () => {
    await page.locator('[data-notifications-compose-multiselect]').click()
    // toggle 语义防翻转：以 Radix 自带的 aria-expanded 断言真的打开了
    await expect(page.locator('[data-notifications-compose-multiselect]')).toHaveAttribute('aria-expanded', 'true')
  }
  await openChannelPicker() // 打开下拉勾选列表（列表常开，可连续多选）
  await expect(chipCh1).toHaveAttribute('aria-checked', 'true')
  await expect(chipCh2).toHaveAttribute('aria-checked', 'true')
  await chipCh2.click() // 取消邮件渠道 → 仅转发钉钉
  await expect(chipCh2).toHaveAttribute('aria-checked', 'false')
  await expect(page.getByText('已选 1 / 2')).toBeVisible()
  // 全不选：计数红化为 0 + 行内警示，发送被拦截——后端缺省=广播全部启用渠道，
  // 不能把「仅站内」的直觉发成全渠道扇出（2026-10-15 二次审查定稿）
  await chipCh1.click()
  await expect(page.getByText('已选 0 / 2')).toBeVisible()
  await expect(page.getByText('未选择转发渠道，发送前请至少选择一个')).toBeVisible()
  await page.keyboard.press('Escape') // Esc 分层：先关下拉弹层再走发送路径
  await page.locator('[data-notifications-compose-title]').fill('不应发出')
  await page.locator('[data-notifications-compose-send]').click()
  await expect(page.getByText('请至少选择一个转发渠道')).toBeVisible()
  await expect(page.locator('[data-notifications-compose-panel]')).toBeVisible()
  await expect.poll(() => mocks.notifCreates.length).toBe(0)
  await openChannelPicker()
  await chipCh1.click() // 恢复仅钉钉
  await expect(chipCh1).toHaveAttribute('aria-checked', 'true')
  await page.keyboard.press('Escape')
  await expect(page.getByText('已选 1 / 2')).toBeVisible()
  await page.locator('[data-notifications-compose-title]').fill('巡检通知')
  await page.locator('[data-notifications-compose-body]').fill('# 今日巡检\n一切正常')
  await page.locator('[data-notifications-compose-send]').click()
  await expect.poll(() => mocks.notifCreates.length).toBe(1)
  const createPayload = JSON.parse(mocks.notifCreates[0])
  expect(createPayload.title).toBe('巡检通知')
  expect(createPayload.channelIds).toEqual(['ch-1'])
  // 发送成功自动回到消息列表并选中新消息（全部消息视图，列表与详情同屏）
  await expect(page.locator('[data-notifications-detail]')).toContainText('今日巡检')
  await expect(page.locator('[data-notifications-tab="all"]')).toHaveAttribute('aria-pressed', 'true')
  await expect(page.locator('[data-notifications-compose-panel]')).toHaveCount(0)

  // 再发一条不动选择：默认全选 = 省略 channelIds（后端缺省语义，无渠道数上限）
  await page.locator('[data-notifications-nav="compose"]').click()
  await openChannelPicker()
  await expect(chipCh1).toHaveAttribute('aria-checked', 'true')
  await expect(chipCh2).toHaveAttribute('aria-checked', 'true')
  await page.keyboard.press('Escape')
  await page.locator('[data-notifications-compose-title]').fill('全渠道广播')
  await page.locator('[data-notifications-compose-send]').click()
  await expect.poll(() => mocks.notifCreates.length).toBe(2)
  const broadcastPayload = JSON.parse(mocks.notifCreates[1])
  expect(broadcastPayload.title).toBe('全渠道广播')
  expect(broadcastPayload.channelIds).toBeUndefined()

  // 弹窗关闭重开：compose 表单与渠道选择状态复位
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await entry.click()
  await page.locator('[data-notifications-nav="compose"]').click()
  await expect(page.locator('[data-notifications-compose-title]')).toHaveValue('')
  await openChannelPicker()
  await expect(chipCh1).toHaveAttribute('aria-checked', 'true')
  await expect(chipCh2).toHaveAttribute('aria-checked', 'true')
  await page.keyboard.press('Escape')
  await expect(page.getByText('已选 2 / 2')).toBeVisible()

  // 手动转发：回消息列表（重开后详情仍选中），详情操作 → 渠道多选 → 确认
  await page.locator('[data-notifications-nav="messages"]').click()
  await expect(page.locator('[data-notifications-detail]')).toContainText('全渠道广播')
  await page.locator('[data-notifications-forward]').click()
  await expect(page.locator('[data-notifications-forward-panel]')).toBeVisible()
  // 未选渠道时禁用并明示「先选择」，避免「转发（0）」被误读为 0 条成功
  await expect(page.locator('[data-notifications-forward-confirm]')).toBeDisabled()
  await expect(page.locator('[data-notifications-forward-confirm]')).toContainText('选择渠道后转发')
  await page.locator('[data-notifications-forward-channel="ch-1"]').check()
  await expect(page.locator('[data-notifications-forward-confirm]')).toContainText('转发（1 个渠道）')
  await page.locator('[data-notifications-forward-confirm]').click()
  await expect.poll(() => mocks.notifForwards.length).toBe(1)
  await expect(page.getByText('已转发到 1 个渠道')).toBeVisible()

  // 渠道配置分区：密钥旁内嵌「入口测试」（真实投递全链路，二次确认后才发请求）
  await page.locator('[data-notifications-nav="config"]').click()
  await expect(page.locator('[data-notifications-key-item="nk-1"]')).toBeVisible()
  await page.locator('[data-notifications-key-test]').first().click()
  const keyTestConfirm = page.getByRole('dialog').filter({ hasText: '发起入口链路测试' })
  await expect(keyTestConfirm).toBeVisible()
  await keyTestConfirm.getByRole('button', { name: '开始测试' }).click()
  await expect.poll(() => mocks.notifKeyTests.length).toBe(1)
  await expect(page.getByText('出口即时投递：成功 1 条')).toBeVisible()
})

test('消息通知：标为未读后重新查阅即时回已读（staleTime 不吞查阅动作）', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await page.goto('/#/super-assistant')

  // 未读数断言用弹窗内「消息列表」导航徽章（与侧栏角标同源 summary）——
  // 弹窗打开后 Radix 会把背景置 aria-hidden，getByRole 定位不到侧栏按钮
  const entry = page.getByRole('button', { name: /消息通知/ })
  const navBadge = page.locator('[data-notifications-nav="messages"]')
  await expect(entry).toContainText('2') // n-1、n-2 未读
  await entry.click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await expect(navBadge).toContainText('2')

  // 打开未读 n-1：GET 落已读，「标为未读」钮出现、徽章降为 1
  await page.locator('[data-notifications-item="n-1"]').click()
  await expect(page.locator('[data-notifications-unread]')).toBeVisible()
  await expect(navBadge).toContainText('1')

  // 标为未读：钮消失、徽章回 2（详情缓存此时在 30s staleTime 内且为未读）
  await page.locator('[data-notifications-unread]').click()
  await expect(page.locator('[data-notifications-unread]')).toHaveCount(0)
  await expect(navBadge).toContainText('2')

  // 切到 n-2 再点回 n-1：主动查阅必须穿透 staleTime 强制 GET——
  // 服务端落已读后「标为未读」钮重现（未修时钮不回来，未读态最长滞留 30s+15s 轮询）
  await page.locator('[data-notifications-item="n-2"]').click()
  await expect(page.locator('[data-notifications-detail]')).toContainText('账单系统异常告警')
  await page.locator('[data-notifications-item="n-1"]').click()
  await expect(page.locator('[data-notifications-unread]')).toBeVisible({ timeout: 10_000 })
  await expect(navBadge).not.toContainText(/\d/) // n-1、n-2 均已读，无未读徽章
})

test('消息通知：游标分页——首页两条，加载更多补齐后续', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page, { notificationsPagination: true })
  await page.goto('/#/super-assistant')

  await page.getByRole('button', { name: /消息通知/ }).click()
  await expect(page.getByRole('dialog')).toBeVisible()

  // 分页夹具的追加行均为已读；「全部消息」= 未归档 4 条（归档 n-3 不在本视图），2 条/页共两页
  await page.locator('[data-notifications-tab="all"]').click()

  // 首页：最新两条可见，其余未加载
  await expect(page.locator('[data-notifications-item="n-1"]')).toBeVisible()
  await expect(page.locator('[data-notifications-item="n-2"]')).toBeVisible()
  await expect(page.locator('[data-notifications-item="n-4"]')).toHaveCount(0)
  await expect(page.locator('[data-notifications-load-more]')).toBeVisible()

  await page.locator('[data-notifications-load-more]').click()
  await expect(page.locator('[data-notifications-item="n-4"]')).toBeVisible()
  await expect(page.locator('[data-notifications-item="n-5"]')).toBeVisible()
  // 归档项 n-3 永不出现在「全部消息」分页里
  await expect(page.locator('[data-notifications-item="n-3"]')).toHaveCount(0)
  // 全部加载完毕后「加载更多」消失
  await expect(page.locator('[data-notifications-load-more]')).toHaveCount(0)
})

test('消息通知：无未读时——默认全部消息视图收纳未归档历史，「全部已读」禁用', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page, { notificationsAllRead: true })
  await page.goto('/#/super-assistant')

  await page.getByRole('button', { name: /消息通知/ }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()

  // 默认「全部消息」tab：未归档历史可见（n-1、n-2），已归档 n-3 不在本视图（在「已归档」里）
  await expect(page.locator('[data-notifications-tab="all"]')).toHaveAttribute('aria-pressed', 'true')
  await expect(page.locator('[data-notifications-item="n-1"]')).toBeVisible()
  await expect(page.locator('[data-notifications-item="n-2"]')).toBeVisible()
  await expect(page.locator('[data-notifications-item="n-3"]')).toHaveCount(0)
  // 「全部已读」在无未读时禁用（图标化按钮仍可定位）；禁用态不派发 pointer 事件、
  // tooltip 悬停不出，用原生 title 提示禁用原因
  await expect(page.locator('[data-notifications-read-all]')).toBeDisabled()
  await expect(page.locator('[data-notifications-read-all]')).toHaveAttribute('title', '暂无未读消息')

  // 已标记 tab 有内容（n-2 已读但已标记仍可见）；已归档 tab 显示归档项 n-3
  // （已标记/已归档的空态文案语义由 notificationsFormat 单测覆盖）
  await page.locator('[data-notifications-tab="starred"]').click()
  await expect(page.locator('[data-notifications-item="n-2"]')).toBeVisible()
  await page.locator('[data-notifications-tab="archived"]').click()
  await expect(page.locator('[data-notifications-item="n-3"]')).toBeVisible()

  // 已归档 = 处置终态：详情只保留恢复/转发，标记与标为未读不出现
  await page.locator('[data-notifications-item="n-3"]').click()
  await expect(page.locator('[data-notifications-detail]')).toContainText('历史巡检完成')
  await expect(page.locator('[data-notifications-archive]')).toHaveAttribute('aria-label', '恢复消息')
  await expect(page.locator('[data-notifications-forward]')).toBeVisible()
  await expect(page.locator('[data-notifications-star]')).toHaveCount(0)
  await expect(page.locator('[data-notifications-unread]')).toHaveCount(0)
})

test('消息通知：冷启动空态——站内无任何消息时引导去发送', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page, { notificationsNone: true })
  await page.goto('/#/super-assistant')

  await page.getByRole('button', { name: /消息通知/ }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()

  // 默认「全部消息」空态 = 中转站冷启动：引导去发送，直达发送分区
  await expect(page.locator('[data-notifications-tab="all"]')).toHaveAttribute('aria-pressed', 'true')
  const allEmpty = page.locator('[data-notifications-list-empty="all"]')
  await expect(allEmpty).toBeVisible()
  await expect(allEmpty).toContainText('还没有消息')
  await page.locator('[data-notifications-empty-compose]').click()
  await expect(page.locator('[data-notifications-compose-panel]')).toBeVisible()
})

test('消息通知：三渠道时「全选」恢复默认广播——部分选后一键回到省略 channelIds 语义', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page)
  // 本用例专属三渠道列表（后注册的 route 先匹配，覆盖共享 handler）；
  // 不动共享 fixture，避免波及渠道配置/消息统计对 ch-1/ch-2 数量的断言
  await page.route('**/api/v2/notifications/channels', async route => {
    if (route.request().method() === 'POST') return route.fallback()
    return route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify(
        ['ch-1', 'ch-2', 'ch-3'].map((id, index) => ({
          id,
          name: `渠道${index + 1}`,
          template: 'email',
          templateName: '邮件',
          enabled: true,
        })),
      ),
    })
  })
  await page.goto('/#/super-assistant')

  await page.getByRole('button', { name: /消息通知/ }).click()
  await page.locator('[data-notifications-nav="compose"]').click()
  await expect(page.locator('[data-notifications-compose-panel]')).toBeVisible()
  // 打开下拉勾选列表；默认全选时不渲染「全选」行，取消一个后出现（计数 3/3 → 2/3）
  await page.locator('[data-notifications-compose-multiselect]').click()
  await expect(page.locator('[data-notifications-compose-select-all]')).toHaveCount(0)
  await expect(page.getByText('已选 3 / 3')).toBeVisible()
  await page.locator('[data-notifications-compose-channel="ch-3"]').click()
  await expect(page.getByText('已选 2 / 3')).toBeVisible()
  await expect(page.locator('[data-notifications-compose-select-all]')).toBeVisible()
  // 全选 = 恢复默认态（null），发送省略 channelIds（后端缺省=全部启用渠道）
  await page.locator('[data-notifications-compose-select-all]').click()
  await expect(page.getByText('已选 3 / 3')).toBeVisible()
  await expect(page.locator('[data-notifications-compose-select-all]')).toHaveCount(0)
  await page.keyboard.press('Escape')
  await page.locator('[data-notifications-compose-title]').fill('三渠道广播')
  await page.locator('[data-notifications-compose-send]').click()
  await expect.poll(() => mocks.notifCreates.length).toBe(1)
  expect(JSON.parse(mocks.notifCreates[0]).channelIds).toBeUndefined()
})

test('消息通知：渠道列表加载失败——行内报错可重试，发送钮禁用到列表就绪', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  let failChannels = true
  await page.route('**/api/v2/notifications/channels', async route => {
    if (route.request().method() === 'POST') return route.fallback()
    if (failChannels) {
      return route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'Internal Server Error' }) })
    }
    return route.fallback()
  })
  await page.goto('/#/super-assistant')

  await page.getByRole('button', { name: /消息通知/ }).click()
  await page.locator('[data-notifications-nav="compose"]').click()
  const composePanel = page.locator('[data-notifications-compose-panel]')
  await expect(composePanel).toBeVisible()
  // 失败态：行内 role=alert + 重试入口；发送钮视觉禁用（拒绝从点击后提前到禁用态）。
  // 渠道配置分区常挂载（隐藏不卸载）也渲染同款错误文案，断言须限定 compose 面板作用域
  await expect(composePanel.getByText('渠道列表加载失败：Internal Server Error')).toBeVisible()
  const send = page.locator('[data-notifications-compose-send]')
  await expect(send).toBeDisabled()
  // 重试成功后恢复：chips 回来（默认全选），按钮可用
  failChannels = false
  await composePanel.getByRole('button', { name: '重试' }).click()
  await page.locator('[data-notifications-compose-multiselect]').click()
  await expect(page.locator('[data-notifications-compose-channel="ch-1"]')).toHaveAttribute('aria-checked', 'true')
  await page.keyboard.press('Escape')
  await expect(send).toBeEnabled()
})

test('消息通知：无启用渠道空态——「渠道配置」直达跳转且焦点落在分区 tab 上', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await page.route('**/api/v2/notifications/channels', async route => {
    if (route.request().method() === 'POST') return route.fallback()
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify([]) })
  })
  await page.goto('/#/super-assistant')

  await page.getByRole('button', { name: /消息通知/ }).click()
  await page.locator('[data-notifications-nav="compose"]').click()
  await expect(page.locator('[data-notifications-compose-panel]')).toBeVisible()
  await expect(page.getByText(/暂无启用的渠道，消息将仅保存到站内列表/)).toBeVisible()
  await page.locator('[data-notifications-compose-goto-config]').click()
  // 同弹窗切到渠道配置分区：tab 选中态同步 + 焦点送上新 tab（不逃出弹窗）
  await expect(page.getByRole('tab', { name: '渠道配置' })).toBeFocused()
  await expect(page.getByRole('tab', { name: '渠道配置' })).toHaveAttribute('aria-selected', 'true')
  const focusInDialog = await page.evaluate(() => {
    const dialog = document.querySelector('[role="dialog"]')
    return !!dialog && dialog.contains(document.activeElement)
  })
  expect(focusInDialog).toBe(true)
})

test('消息通知（窄屏390px）：发送分区单列冒烟——下拉多选可点选、计数即时', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await seedAuth(page)
  await mockApis(page)
  await page.goto('/#/super-assistant')

  // 窄屏下工作台侧栏是抽屉态：先打开导航抽屉，再进入消息通知
  await page.getByRole('button', { name: '打开工作台导航' }).click()
  await page.getByRole('button', { name: /消息通知/ }).click()
  await page.locator('[data-notifications-nav="compose"]').click()
  await expect(page.locator('[data-notifications-compose-panel]')).toBeVisible()
  // <xl 单列：转发渠道多选框位于正文之后；下拉宽度跟随触发器（不横向溢出），点选反馈即时
  await page.locator('[data-notifications-compose-multiselect]').click()
  await page.locator('[data-notifications-compose-channel="ch-1"]').click()
  await expect(page.locator('[data-notifications-compose-channel="ch-1"]')).toHaveAttribute('aria-checked', 'false')
  await expect(page.getByText('已选 1 / 2')).toBeVisible()
})

test('消息通知：接入密钥——签发交付卡、明文仅一次展示、列表与吊销', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page)
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write'])
  await page.goto('/#/super-assistant')

  await page.getByRole('button', { name: /消息通知/ }).click()
  // 布局契约：渠道配置按「消息入口 / 消息出口」两大分组组织（入口=投递接入，出口=发件邮箱+转发渠道）
  await page.locator('[data-notifications-nav="config"]').click()
  await expect(page.locator('[data-notifications-group="inbound"]')).toBeVisible()
  await expect(page.locator('[data-notifications-group="outbound"]')).toBeVisible()
  await expect(page.locator('[data-notifications-keys-panel]')).toBeVisible()
  await expect(page.locator('[data-notifications-channels-panel]')).toBeVisible()

  // 有效密钥平铺可见（不携带明文）；已吊销密钥默认折叠留痕
  await expect(page.locator('[data-notifications-key-item="nk-1"]')).toBeVisible()
  await expect(page.locator('[data-notifications-key-item="nk-1"]')).toContainText('billing')
  await expect(page.locator('[data-notifications-revoked-keys]')).toContainText('已吊销密钥（1）')
  await expect(page.locator('[data-notifications-key-item="nk-0"]')).toBeHidden()
  await page.locator('[data-notifications-revoked-keys] summary').click()
  await expect(page.locator('[data-notifications-key-item="nk-0"]')).toBeVisible()
  await expect(page.locator('[data-notifications-key-item="nk-0"]')).toContainText('legacy-portal')

  // 签发表单默认收起：展开后填写
  await expect(page.locator('[data-notifications-key-name]')).toBeHidden()
  await page.locator('[data-notifications-key-mint-toggle]').click()
  await expect(page.locator('[data-notifications-key-name]')).toBeVisible()
  await page.locator('[data-notifications-key-name]').fill('crm')
  await page.locator('[data-notifications-key-mint]').click()
  await expect(page.locator('[data-notifications-key-plaintext]')).toBeVisible()
  await expect(page.locator('[data-notifications-key-plaintext]')).toContainText('ob_notif_e2e_demo_plaintext_secret')
  await expect.poll(() => mocks.notifKeyCreates.length).toBe(1)

  // 一次性交付卡：脚本注入真实密钥（复制即可用），消灭「复制密钥→复制示例→手改占位符」三步拼接
  const delivery = page.locator('[data-notifications-key-delivery]')
  await expect(delivery).toBeVisible()
  await expect(page.locator('[data-notifications-key-script]')).toContainText('ob_notif_e2e_demo_plaintext_secret')
  await expect(page.locator('[data-notifications-key-script]')).toContainText('/api/v2/notifications/ingest')
  // 副作用类交互按验收标准断言最终结果：真实剪贴板内容含密钥（不以 toast 出现代替）
  await page.locator('[data-notifications-copy-fullscript]').click()
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toContain('X-API-Key: ob_notif_e2e_demo_plaintext_secret')
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toContain('/api/v2/notifications/ingest')
  // 「我已妥善保存」主动关闭：明文与交付卡一起消失
  await page.locator('[data-notifications-key-dismiss]').click()
  await expect(delivery).toBeHidden()

  // P0 回归（对抗审查修复）：明文随父组件持有——切到消息列表再切回，交付卡与明文不丢
  await page.locator('[data-notifications-key-mint-toggle]').click()
  await page.locator('[data-notifications-key-name]').fill('keep-alive')
  await page.locator('[data-notifications-key-mint]').last().click()
  await expect(page.locator('[data-notifications-key-delivery]')).toBeVisible()
  await page.locator('[data-notifications-nav="messages"]').click()
  await page.waitForTimeout(200)
  await page.locator('[data-notifications-nav="config"]').click()
  await expect(page.locator('[data-notifications-key-delivery]')).toBeVisible()
  await expect(page.locator('[data-notifications-key-plaintext]')).toContainText('ob_notif_e2e_demo_plaintext_secret')
  await page.locator('[data-notifications-key-dismiss]').click()

  // 端点独立复制：剪贴板拿到端点地址（副作用真实验收）
  await page.locator('[data-notifications-copy-endpoint]').click()
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toContain('/api/v2/notifications/ingest')

  // 存量密钥行复制脚本：明文不可再取（安全设计），复制的是占位符版
  await page.locator('[data-notifications-key-copy-script]').first().click()
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toContain('<你的密钥>')

  // 吊销：二次确认后生效
  await page.locator('[data-notifications-key-revoke]').first().click()
  const confirmDialog = page.getByRole('dialog').filter({ hasText: '吊销这枚投递密钥' })
  await expect(confirmDialog).toBeVisible()
  await confirmDialog.getByRole('button', { name: '吊销' }).click()
  await expect.poll(() => mocks.notifKeyRevokes.length).toBe(1)
})

test('消息通知：转发渠道——新建/启停开关/测试直发/删除确认', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page)
  await page.goto('/#/super-assistant')

  await page.getByRole('button', { name: /消息通知/ }).click()
  await page.locator('[data-notifications-nav="config"]').click()
  await expect(page.locator('[data-notifications-channels-panel]')).toBeVisible()

  // 既有渠道：名称/掩码地址/最近投递状态可见，地址明文不出现在界面
  // 出口主次重排：转发渠道（主面板）位于发件邮箱（依赖配置）之前
  await expect(page.locator('[data-notifications-channels-panel]')).toBeVisible()
  const channelsPanel = await page.locator('[data-notifications-channels-panel]').evaluate(el => el.getBoundingClientRect().top)
  const smtpCard = await page.locator('[data-notifications-smtp-card]').evaluate(el => el.getBoundingClientRect().top)
  expect(channelsPanel).toBeLessThan(smtpCard)
  // 邮件渠道依赖显性化：SMTP 未配置（GET mock）→ 渠道卡直接给「去配置」引导，
  // 点击后展开发件邮箱卡（不再等用户点进新建表单才发现依赖）
  const smtpDep = page.locator('[data-notifications-channel-item="ch-2"] [data-notifications-channel-smtp-dep="missing"]')
  await expect(smtpDep).toContainText('需先配置「发件邮箱」')
  await smtpDep.click()
  await expect(page.locator('[data-notifications-smtp-card]')).toHaveAttribute('open', '')
  // 收回展开态，让下方既有 SMTP 流程仍从 summary 点击开始
  await page.locator('[data-notifications-smtp-card] summary').click()
  await expect(page.locator('[data-notifications-smtp-card]')).not.toHaveAttribute('open', '')
  // 渠道卡状态行：最近成功渠道显示投递成功；非失败行是纯文本（不伪装成可展开按钮）
  await expect(page.locator('[data-notifications-channel-item="ch-1"] [data-notifications-channel-status]')).toContainText('投递成功')
  await expect(page.locator('[data-notifications-channel-item="ch-1"] [data-notifications-channel-status]')).not.toHaveRole('button')
  await expect(page.locator('[data-notifications-channel-failure-detail]')).toHaveCount(0)

  const existing = page.locator('[data-notifications-channel-item="ch-1"]')
  await expect(existing).toBeVisible()
  await expect(existing).toContainText('钉钉运维群')
  await expect(existing).toContainText('钉钉机器人')
  await expect(existing).toContainText('toke…abcd')
  await expect(existing).toContainText('投递成功')

  // 新建渠道（模板网格起手）：打开向导 → 选钉钉类型卡 → 填关键字段（含指引文案可见）
  await page.locator('[data-notifications-channel-create-open]').click()
  await expect(page.locator('[data-notifications-template-grid]')).toBeVisible()
  await expect(page.locator('[data-notifications-template-card="dingtalk"]')).toBeVisible()
  await page.locator('[data-notifications-template-card="dingtalk"]').click()
  await expect(page.locator('[data-notifications-channel-form]')).toBeVisible()
  await page.locator('[data-notifications-channel-name]').fill('钉钉值班')
  await expect(page.getByText('钉钉群 → 设置 → 机器人')).toBeVisible()
  await page.locator('[data-notifications-channel-param="token"]').fill('6fb1fa7c2b-demo')
  await page.locator('[data-notifications-channel-create]').click()
  await expect.poll(() => mocks.notifChannelCreates.length).toBe(1)
  const createPayload = JSON.parse(mocks.notifChannelCreates[0])
  expect(createPayload.template).toBe('dingtalk')
  expect(createPayload.params.token).toBe('6fb1fa7c2b-demo')

  // SMTP 在线配置：展开卡片保存 + 测试邮件
  await page.locator('[data-notifications-smtp-card] summary').click()
  await page.locator('[data-notifications-smtp-host]').fill('smtp.test.com')
  await page.locator('[data-notifications-smtp-username]').fill('noreply@test.com')
  await page.locator('[data-notifications-smtp-password]').fill('pass-e2e')
  await page.locator('[data-notifications-smtp-save]').click()
  await expect.poll(() => mocks.notifSmtpPuts.length).toBe(1)
  await page.locator('[data-notifications-smtp-test-to]').fill('e2e@test.com')
  await page.locator('[data-notifications-smtp-test]').click()
  await expect.poll(() => mocks.notifSmtpTests.length).toBe(1)
  await expect(page.getByText('测试邮件已发送至 e2e@test.com')).toBeVisible()

  // 测试直发（渠道行内嵌测试按钮）：结果 toast 即时反馈
  await expect(page.locator('[data-notifications-channel-item="ch-1"] [data-notifications-channel-test]')).toBeVisible()
  await page.locator('[data-notifications-channel-test]').first().click()
  await expect.poll(() => mocks.notifChannelTests.length).toBe(1)
  await expect(page.getByText('测试消息已发送')).toBeVisible()

  // 启停开关触发 PATCH（仍在渠道配置分区）
  await page.locator('[data-notifications-channel-toggle]').first().click()
  await expect.poll(() => mocks.notifChannelUpdates.length).toBe(1)

  // 删除：二次确认后生效
  await page.locator('[data-notifications-channel-delete]').first().click()
  const confirm = page.getByRole('dialog').filter({ hasText: '删除这个转发渠道' })
  await expect(confirm).toBeVisible()
  await confirm.getByRole('button', { name: '删除' }).click()
  await expect.poll(() => mocks.notifChannelDeletes.length).toBe(1)
})

test('消息统计：第四分区图表可视化——概览/趋势/来源/渠道与时间窗切换', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page)
  await page.goto('/#/super-assistant')

  await page.getByRole('button', { name: /消息通知/ }).click()
  await page.locator('[data-notifications-nav="stats"]').click()

  const panel = page.locator('[data-notifications-stats-panel]')
  await expect(panel).toBeVisible()

  // 概览四卡：数值与空值语义
  const overview = page.locator('[data-notifications-stats-overview]')
  await expect(overview).toContainText('消息总量')
  await expect(page.locator('[data-notifications-stats-card="消息总量"]')).toContainText('5')
  await expect(page.locator('[data-notifications-stats-card="未读"]')).toContainText('2')
  await expect(page.locator('[data-notifications-stats-card="今日入站"]')).toContainText('1')
  await expect(page.locator('[data-notifications-stats-card="转发成功率"]')).toContainText('80%')

  // 入站趋势：recharts 柱状图渲染出 SVG，默认近 30 天
  await expect(page.locator('[data-notifications-stats-trend-chart] svg')).toBeVisible()
  await expect.poll(() => mocks.notifStatsDays.at(-1) ?? 0).toBe(30)

  // 时间窗切到 7 天：重新请求且按钮选中态切换
  await page.locator('[data-notifications-stats-range="7"]').click()
  await expect(page.locator('[data-notifications-stats-range="7"]')).toHaveAttribute('aria-pressed', 'true')
  await expect.poll(() => mocks.notifStatsDays.at(-1) ?? 0).toBe(7)

  // 来源分布：三个来源占比条与计数
  const sources = page.locator('[data-notifications-stats-sources-list]')
  await expect(sources.locator('[data-notifications-stats-source="platform"]')).toContainText('3')
  await expect(sources.locator('[data-notifications-stats-source="warehouse"]')).toContainText('1')

  // 渠道投递结果：成功/失败计数与堆叠占比条
  const channels = page.locator('[data-notifications-stats-channels-list]')
  await expect(channels.locator('[data-notifications-stats-channel="ch-1"]')).toContainText('成功 4')
  await expect(channels.locator('[data-notifications-stats-channel="ch-1"]')).toContainText('失败 1')
  await expect(channels.locator('[data-notifications-stats-channel="ch-2"]')).toContainText('待发 1')
})

test('消息通知（窄屏390px）：列表⇄详情单栏切换与管理态全宽', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await seedAuth(page)
  await mockApis(page)
  await page.goto('/#/super-assistant')

  // 窄屏下工作台侧栏是抽屉态：先打开导航抽屉，再进入消息通知
  await page.getByRole('button', { name: '打开工作台导航' }).click()
  await page.getByRole('button', { name: /消息通知/ }).click()
  await expect(page.getByRole('dialog')).toBeVisible()

  // 列表态：列表可见、详情隐藏；无横向溢出破坏（工具栏可横滚）
  await expect(page.locator('[data-notifications-list]')).toBeVisible()
  await expect(page.locator('[data-notifications-detail]')).toBeHidden()

  // 选中消息 → 单栏切到详情，统一返回条可回列表
  await page.locator('[data-notifications-item="n-1"]').click()
  await expect(page.locator('[data-notifications-detail]')).toBeVisible()
  await expect(page.locator('[data-notifications-list]')).toBeHidden()
  await expect(page.locator('[data-notifications-back-detail]')).toBeVisible()
  await expect(page.locator('[data-notifications-detail]')).toContainText('数据任务失败报告')
  await page.locator('[data-notifications-back-detail]').click()
  await expect(page.locator('[data-notifications-list]')).toBeVisible()
  await expect(page.locator('[data-notifications-detail]')).toBeHidden()

  // Esc 分层回退：窄屏详情态按 Esc 先回列表，而不是关掉整个弹窗
  // （n-1 首次查看详情时已被「GET 即已读」从未读列表移除，这里用仍未读的 n-2）
  await page.locator('[data-notifications-item="n-2"]').click()
  await expect(page.locator('[data-notifications-detail]')).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.locator('[data-notifications-list]')).toBeVisible()
  await expect(page.getByRole('dialog')).toBeVisible()

  // 分区导航在窄屏同样可达：切渠道配置再回消息列表
  await page.locator('[data-notifications-nav="config"]').click()
  await expect(page.locator('[data-notifications-keys-panel]')).toBeVisible()
  await page.locator('[data-notifications-nav="messages"]').click()
  await expect(page.locator('[data-notifications-list]')).toBeVisible()
})

test('会话选中回写地址栏：切会话 URL 跟随，深链直达指定会话', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await page.goto('/#/super-assistant')

  // 会话列表就绪后，默认选中的最新会话回写为 ?conversation=c-today
  await expect(page).toHaveURL(/#\/super-assistant\?conversation=c-today$/)

  // 页内切换会话 → 地址栏跟随（复制链接即可在其它浏览器打开同一会话）
  await page.locator('[data-workbench-conversation="c-earlier"] button').first().click()
  await expect(page).toHaveURL(/#\/super-assistant\?conversation=c-earlier$/)
  await expect(page.locator('[data-workbench-conversation="c-earlier"]')).toHaveClass(/bg-brand-soft/)

  // 深链直达：携带 ?conversation= 打开即选中该会话，参数不被清除
  await page.goto('/#/super-assistant?conversation=c-yesterday')
  await expect(page).toHaveURL(/#\/super-assistant\?conversation=c-yesterday$/)
  await expect(page.locator('[data-workbench-conversation="c-yesterday"]')).toHaveClass(/bg-brand-soft/)
})

test('归档流转：会话移入归档区且 PATCH 携带 status', async ({ page }) => {
  await seedAuth(page)
  const { patchBodies } = await mockApis(page)
  await page.goto('/#/super-assistant')

  const row = page.locator('[data-workbench-conversation="c-today"]')
  await row.hover()
  await row.getByRole('button', { name: '归档会话 今日需求梳理' }).click()

  await expect.poll(() => patchBodies.length).toBe(1)
  expect(JSON.parse(patchBodies[0])).toMatchObject({ status: 'archived' })
  await expect(page.locator('[data-workbench-group="recent"] [data-workbench-conversation="c-today"]')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '归档会话', exact: true })).toBeVisible()

  // 展开归档区后可恢复
  await page.getByRole('button', { name: '归档会话', exact: true }).click()
  const archivedRow = page.locator('[data-workbench-group="archived"] [data-workbench-conversation="c-today"]')
  await expect(archivedRow).toHaveCount(1)
  await archivedRow.hover()
  await archivedRow.getByRole('button', { name: '恢复会话 今日需求梳理' }).click()
  await expect.poll(() => patchBodies.length).toBe(2)
  expect(JSON.parse(patchBodies[1])).toMatchObject({ status: 'active' })
  await expect(page.locator('[data-workbench-group="recent"] [data-workbench-conversation="c-today"]')).toHaveCount(1)
})

test('左下角头像打开个人资料弹窗：分区 tab 与固定尺寸', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await page.goto('/#/super-assistant')

  await page.locator('[data-workbench-profile]').click()
  const dialog = page.getByRole('dialog', { name: '个人资料' })
  await expect(dialog).toBeVisible()

  // 与后台 Layout 头像入口同一弹窗：默认账号信息 tab，用户名只读回显
  await expect(dialog.getByRole('tab', { name: '账号信息' })).toHaveAttribute('aria-selected', 'true')
  await expect(dialog.getByLabel('用户名')).toBeDisabled()
  await expect(dialog.getByLabel('用户名')).toHaveValue('workbench-tester')
  await expect(dialog.getByRole('tab', { name: '环境变量' })).toBeVisible()
  await expect(dialog.getByRole('tab', { name: '隐私变量' })).toBeVisible()

  // 固定尺寸：切 tab 弹窗宽高不变，面板内容在弹窗内滚动
  // （expectSameBoundingBox 内置亚像素容差，见 support/geometry.ts）
  const before = await dialog.boundingBox()
  await dialog.getByRole('tab', { name: '隐私变量' }).click()
  await expect(dialog.getByRole('tabpanel', { name: '隐私变量' })).toBeVisible()
  const after = await dialog.boundingBox()
  expectSameBoundingBox(before, after, '个人资料弹窗')
})

test('本体治理跳转本体管理：落地 #/ontologies，可经左栏超级助手或悬浮助手返回工作台', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await page.goto('/#/super-assistant')

  await page.getByRole('link', { name: '本体治理' }).click()
  await page.waitForURL('**/#/ontologies')

  // 后台左侧平台导航提供「超级助手」入口；三维场景已暂时隐藏出导航
  const assistantLink = page.getByRole('link', { name: '超级助手', exact: true })
  await expect(assistantLink).toBeVisible()
  await expect(page.getByRole('link', { name: '三维场景', exact: true })).toHaveCount(0)

  // 经左栏「超级助手」返回工作台
  await assistantLink.click()
  await page.waitForURL(/#\/super-assistant/)
  await expect(page.getByRole('button', { name: '新建会话' })).toBeVisible()

  // 右下角悬浮助手仍是第二条返回路径
  await page.getByRole('link', { name: '本体治理' }).click()
  await page.waitForURL('**/#/ontologies')
  await page.getByTestId('assistant-widget-fab').click()
  await page.getByTestId('assistant-widget-open-full').click()
  await page.waitForURL(/#\/super-assistant/)
  await expect(page.getByRole('button', { name: '新建会话' })).toBeVisible()
})

test('定时任务弹窗：查看已有计划、创建、点进某次执行', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await page.goto('/#/super-assistant')

  await page.getByRole('button', { name: '全局搜索' }).click()
  await expect(page.getByPlaceholder('搜索会话标题与消息内容…')).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).toHaveCount(0)

  await page.getByRole('button', { name: '定时任务' }).click()
  const dialog = page.getByTestId('scheduled-tasks-dialog')
  await expect(dialog).toBeVisible()
  await expect(page.getByRole('heading', { name: '定时任务' })).toBeVisible()
  await expect(dialog.getByText('每日站会纪要')).toBeVisible()

  await dialog.getByRole('button', { name: /每日站会纪要/ }).click()
  await expect(dialog.getByText('执行记录')).toBeVisible()
  await dialog.getByRole('button', { name: /\d{4}-\d{2}-\d{2}.*已完成/ }).click()
  await expect(dialog.getByText('昨日三项待办均已推进。')).toBeVisible()
  await expect(dialog.getByRole('button', { name: '在会话中继续' })).toBeVisible()

  await page.getByRole('button', { name: '创建定时任务' }).click()
  await expect(dialog.getByText(/外部工具是否直接执行/)).toBeVisible()
  await dialog.getByPlaceholder('到点后助手会按这段任务内容在后台执行').fill('明早汇总待办')
  await page.getByRole('button', { name: '创建', exact: true }).click()
  await expect(dialog.getByText('明早汇总待办')).toBeVisible()
  // once 类型详情不重复展示「下次」（P1-3：下次即触发时间本身）
  await expect(dialog.getByText(/· 下次/)).toHaveCount(0)
})

test('定时任务弹窗：脏表单关闭需二次确认，继续编辑保留、放弃后清空', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await page.goto('/#/super-assistant')

  await page.getByRole('button', { name: '定时任务' }).click()
  const dialog = page.getByTestId('scheduled-tasks-dialog')
  await expect(dialog).toBeVisible()
  await page.getByRole('button', { name: '创建定时任务' }).click()
  const titleInput = dialog.getByPlaceholder('可留空，将用任务内容前几字作为标题')
  const instructionInput = dialog.getByPlaceholder('到点后助手会按这段任务内容在后台执行')
  await titleInput.fill('脏数据保护验证')
  await instructionInput.fill('汇总昨天的待办')
  // SelectTrigger 的 aria-label（对抗审查 a11y 修补）
  await expect(dialog.getByRole('combobox', { name: '触发方式' })).toBeVisible()
  const discard = page.getByRole('dialog').filter({ hasText: '放弃创建？' })

  // 遮罩点击（全新状态、无前置确认交互）：脏表单应弹确认，「继续编辑」保留草稿
  await page.mouse.click(5, 400)
  await expect(discard).toBeVisible()
  await discard.getByRole('button', { name: '继续编辑' }).click()
  await expect(discard).toHaveCount(0)
  await expect(titleInput).toHaveValue('脏数据保护验证')

  // Esc：脏表单不直接关闭，确认后「继续编辑」保留草稿
  await page.keyboard.press('Escape')
  await expect(discard).toBeVisible()
  await expect(dialog).toBeVisible()
  await discard.getByRole('button', { name: '继续编辑' }).click()
  await expect(discard).toHaveCount(0)
  await expect(titleInput).toHaveValue('脏数据保护验证')

  // 左栏任务切换：同样确认，「放弃」后落在该任务详情
  await dialog.getByRole('button', { name: /每日站会纪要/ }).click()
  await expect(discard).toBeVisible()
  await expect(titleInput).toHaveValue('脏数据保护验证')
  await discard.getByRole('button', { name: '继续编辑' }).click()
  await expect(discard).toHaveCount(0)
  await dialog.getByRole('button', { name: /每日站会纪要/ }).click()
  await discard.getByRole('button', { name: '放弃' }).click()
  await expect(discard).toHaveCount(0)
  await expect(dialog.getByText('执行记录')).toBeVisible()
  await expect(dialog.getByRole('heading', { name: '每日站会纪要' })).toBeVisible()

  // 关闭符路径同样确认；放弃后弹窗关闭
  await page.getByRole('button', { name: '创建定时任务' }).click()
  await titleInput.fill('脏数据保护验证')
  await page.getByRole('button', { name: '关闭' }).click()
  await expect(discard).toBeVisible()
  await discard.getByRole('button', { name: '放弃' }).click()
  await expect(dialog).toHaveCount(0)

  // 重开不残留创建态：先断言再点「创建定时任务」（其自带重置，先点会掩蔽残留缺陷）
  await page.getByRole('button', { name: '定时任务' }).click()
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText('执行记录')).toBeVisible()
  await expect(dialog.getByText('请输入任务内容')).toHaveCount(0)
  await page.getByRole('button', { name: '创建定时任务' }).click()
  await page.getByRole('button', { name: '创建', exact: true }).click()
  await expect(dialog.getByText('请输入任务内容')).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)

  await page.getByRole('button', { name: '定时任务' }).click()
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText('执行记录')).toBeVisible()
  await expect(dialog.getByText('请输入任务内容')).toHaveCount(0)
  await page.getByRole('button', { name: '创建定时任务' }).click()
  await expect(titleInput).toHaveValue('')
  await expect(instructionInput).toHaveValue('')

  // saving 中不静默锁死：悬挂 POST 期间创建按钮禁用、Esc 给 toast 反馈且弹窗保留
  await page.route('**/api/v2/super-assistant/scheduled-tasks', async route => {
    if (route.request().method() === 'POST') return
    return route.fallback()
  })
  await instructionInput.fill('慢创建验证')
  await page.getByRole('button', { name: '创建', exact: true }).click()
  await expect(page.getByRole('button', { name: '创建', exact: true })).toBeDisabled()
  await expect(dialog.getByRole('button', { name: '创建定时任务' })).toBeDisabled()
  await page.keyboard.press('Escape')
  await expect(page.getByText('正在创建定时任务，请稍候…')).toBeVisible()
  await expect(dialog).toBeVisible()
})

test('定时任务弹窗：触发对象可选流程模板，执行记录可跳转任务实例', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await mockTaskInstancesApis(page)
  await page.goto('/#/super-assistant')

  await page.getByRole('button', { name: '定时任务' }).click()
  const dialog = page.getByTestId('scheduled-tasks-dialog')
  await expect(dialog).toBeVisible()

  // 既有模板目标任务：详情展示触发对象，执行记录是「触发即完成」语义
  await dialog.getByRole('button', { name: /变更评审例会/ }).click()
  await expect(dialog.getByText(/触发对象：流程模板 · 代码变更评审/)).toBeVisible()
  await dialog.getByRole('button', { name: /\d{4}-\d{2}-\d{2}.*已完成/ }).click()
  await expect(dialog.getByText(/已按模板「代码变更评审」创建实例/)).toBeVisible()
  await expect(dialog.getByRole('button', { name: '在会话中继续' })).toHaveCount(0)
  await dialog.getByRole('button', { name: '查看实例' }).click()
  // 查看实例：定时任务弹窗关闭，任务实例弹窗深链选中该实例
  await expect(dialog).toHaveCount(0)
  await expect(page.getByTestId('task-instances-dialog')).toBeVisible()
  await expect(page.getByTestId('task-instance-lite')).toBeVisible()
  // 关闭再从侧栏重开：深链不残留、回到看板（TaskInstancesDialog 两个 effect 的顺序契约锚点）
  await page.keyboard.press('Escape')
  await expect(page.getByTestId('task-instances-dialog')).toHaveCount(0)
  await page.getByRole('button', { name: /任务实例/ }).click()
  await expect(page.getByTestId('task-instances-board')).toBeVisible()
  await expect(page.getByTestId('task-instance-lite')).toHaveCount(0)
  await page.keyboard.press('Escape')
  await expect(page.getByTestId('task-instances-dialog')).toHaveCount(0)

  // 创建流程模板目标的新任务：载荷真实带 target_kind/target_template_id
  await page.getByRole('button', { name: '定时任务' }).click()
  await expect(dialog).toBeVisible()
  await page.getByRole('button', { name: '创建定时任务' }).click()
  await dialog.getByRole('combobox', { name: '触发方式' }).click()
  await page.getByRole('option', { name: '每天' }).click()
  await dialog.getByRole('combobox', { name: '触发对象' }).click()
  await page.getByRole('option', { name: '流程模板' }).click()
  const templateSelect = dialog.getByRole('combobox', { name: '流程模板' })
  await expect(templateSelect).toBeVisible()
  // 未选模板时提交被字段级校验拦截（错误文案与下拉占位符同文，钉到错误节点断言）
  await dialog.getByPlaceholder('到点后会以这段内容作为任务目标').fill('例行跑一遍评审流程')
  await page.getByRole('button', { name: '创建', exact: true }).click()
  await expect(dialog.locator('#scheduled-template-error')).toBeVisible()
  await expect(dialog.locator('#scheduled-template-error')).toHaveText('请选择流程模板')
  await templateSelect.click()
  await page.getByRole('option', { name: '代码变更评审' }).click()
  // 模板目标的助手专属提示让位给实例语义提示
  await expect(dialog.getByText(/外部工具是否直接执行/)).toHaveCount(0)
  await expect(dialog.getByText(/需要人工把关的节点会停下等你处理/)).toBeVisible()

  const posted: Array<Record<string, unknown>> = []
  await page.route('**/api/v2/super-assistant/scheduled-tasks', route => {
    if (route.request().method() === 'POST') posted.push(route.request().postDataJSON())
    return route.fallback()
  })
  await page.getByRole('button', { name: '创建', exact: true }).click()
  await expect(dialog.getByText(/触发对象：流程模板 · 代码变更评审/)).toBeVisible()
  expect(posted.length).toBeGreaterThan(0)
  expect(posted[posted.length - 1].target_kind).toBe('task_template')
  expect(posted[posted.length - 1].target_template_id).toBe('tpl-1')
  expect(posted[posted.length - 1].instruction).toBe('例行跑一遍评审流程')
})

test('multica 命令提示：未配置/未启用时输入 / 也不提供任何命令', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await page.goto('/#/super-assistant')

  const composer = page.getByRole('textbox', { name: '向超级助手发送消息' })
  await composer.fill('/')
  await expect(page.getByTestId('multica-command-hints')).toHaveCount(0)
  await composer.fill('/multica')
  await expect(page.getByTestId('multica-command-hints')).toHaveCount(0)
  await composer.fill('/multica:list_agents')
  await expect(page.getByTestId('multica-command-hints')).toHaveCount(0)
})

test('multica 命令提示：输入 / 即列出全部，前缀收窄、点选填充、参数区收起', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page, { multicaConfig: multicaEnabledFixture })
  await page.goto('/#/super-assistant')

  const composer = page.getByRole('textbox', { name: '向超级助手发送消息' })
  const hints = page.getByTestId('multica-command-hints')

  // 仅输入 / 即列出全部可用命令（由已启用集成的命令目录驱动）
  await composer.fill('/')
  await expect(hints).toBeVisible()
  await expect(hints.getByRole('button')).toHaveCount(3)
  await expect(hints.getByText('需确认')).toBeVisible()

  await composer.fill('/multica:l')
  await expect(hints.getByRole('button')).toHaveCount(2)
  await composer.fill('/multica:list_a')
  await expect(hints.getByRole('button')).toHaveCount(1)
  await hints.getByRole('button', { name: /list_agents/ }).click()
  await expect(composer).toHaveValue('/multica:list_agents')

  // 选中进入参数输入（出现空白）后提示收起；未知前缀同样收起
  await composer.fill('/multica:list_agents 进行中的')
  await expect(page.getByTestId('multica-command-hints')).toHaveCount(0)
  await composer.fill('/multica:warp')
  await expect(page.getByTestId('multica-command-hints')).toHaveCount(0)
})

test('近期会话默认限量 10 条，展开全部后显示完整列表且可收起', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  const many = Array.from({ length: 13 }, (_, index) => ({
    id: `c-cap-${index}`, title: `批量会话 ${index + 1}`, model_config_id: 'model-1',
    status: 'active', created_at: at(0, 8), updated_at: at(0, 8),
  }))
  // 后注册的精确路由优先于 mockApis 的通用 /** 路由
  await page.route('**/api/v2/super-assistant/conversations', route => {
    if (route.request().method() === 'GET') return json(route, many)
    return route.fallback()
  })
  await page.goto('/#/super-assistant')

  const recent = page.locator('[data-workbench-group="recent"]')
  await expect(recent.locator('[data-workbench-conversation]')).toHaveCount(10)
  const toggle = page.locator('[data-workbench-group-toggle="recent"]')
  await expect(toggle).toHaveText('显示全部 13 个会话')

  await toggle.click()
  await expect(recent.locator('[data-workbench-conversation]')).toHaveCount(13)
  await expect(toggle).toHaveText('收起')

  await toggle.click()
  await expect(recent.locator('[data-workbench-conversation]')).toHaveCount(10)
})

test('会话时间按本地时区显示：naive UTC 串按 UTC 解析', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  // 后端实际返回形态：UTC 瞬间、无 Z 后缀；误按本地解析会慢 8 小时（上海）
  const naive = '2026-08-20T08:30:00'
  await page.route('**/api/v2/super-assistant/conversations', route => {
    if (route.request().method() === 'GET') {
      return json(route, [{
        id: 'c-naive', title: '时差验证', model_config_id: 'model-1',
        status: 'active', created_at: naive, updated_at: naive,
      }])
    }
    return route.fallback()
  })
  await page.goto('/#/super-assistant')

  const d = new Date(`${naive}Z`)
  const p2 = (n: number) => String(n).padStart(2, '0')
  // 窄列展示紧凑 MM-DD HH:mm，完整时间挂在时间戳 span 的 title 上
  const compact = `${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}`
  const full = `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${compact.slice(6)}`
  const row = page.locator('[data-workbench-conversation="c-naive"]')
  await expect(row).toContainText(compact)
  await expect(row.locator('span[title]')).toHaveAttribute('title', full)
})

test('会话行悬停时行高与相邻组位置不变（无抖动）', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await page.goto('/#/super-assistant')

  const row = page.locator('[data-workbench-conversation="c-today"]')
  const nextGroup = page.locator('[data-workbench-group="archived"]')
  const beforeBox = await row.boundingBox()
  const beforeY = (await nextGroup.boundingBox())?.y

  await row.hover()
  // 悬停后动作按钮可见（归档/删除浮出）
  await expect(row.getByRole('button', { name: '删除会话 今日需求梳理' })).toBeVisible()
  const hoverBox = await row.boundingBox()

  expectSameBoundingBox(beforeBox, hoverBox, '会话行')
  expect((await nextGroup.boundingBox())?.y).toBeCloseTo(beforeY ?? 0, 1)
})

test('会话附件：上传/展示/移除，且跨会话不可见', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page)
  await page.goto('/#/super-assistant?conversation=c-today')

  // c-today 预置 1 个附件 chip
  await expect(page.getByTestId('super-assistant-attachments')).toBeVisible()
  await expect(page.getByText('需求清单.md')).toBeVisible()

  // 附件 chips 展示在输入框上方
  const chipsBox = await page.getByTestId('super-assistant-attachments').boundingBox()
  const inputBox = await page.getByRole('textbox', { name: '向超级助手发送消息' }).boundingBox()
  expect(chipsBox && inputBox && chipsBox.y + chipsBox.height <= inputBox.y + 1).toBe(true)

  // 切到昨日会话：附件区不可见（跨会话隔离）
  await page.locator('[data-workbench-conversation="c-yesterday"] button').first().click()
  await expect(page.getByTestId('super-assistant-attachments')).toHaveCount(0)

  // 切回后重新出现
  await page.locator('[data-workbench-conversation="c-today"] button').first().click()
  await expect(page.getByTestId('super-assistant-attachments')).toBeVisible()

  // 上传新附件（隐藏 file input 驱动）
  await page.getByLabel('选择会话附件文件').setInputFiles({
    name: '会议纪要.md',
    mimeType: 'text/markdown',
    buffer: Buffer.from('# 纪要'),
  })
  await expect.poll(() => mocks.fileUploads).toEqual(['会议纪要.md'])
  await expect(page.getByText('会议纪要.md')).toBeVisible()

  // 移除附件
  await page.getByRole('button', { name: '移除附件 会议纪要.md' }).click()
  await expect.poll(() => mocks.fileDeletes.length).toBe(1)
  await expect(page.getByText('会议纪要.md')).toHaveCount(0)
})

test('流式生成跨会话隔离：A 生成中切到 B，B 输入区不受影响且不串内容', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page, { chatDelayMs: 1500 })
  await page.goto('/#/super-assistant?conversation=c-today')

  await page.getByRole('textbox', { name: '向超级助手发送消息' }).fill('你好')
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect(page.getByRole('button', { name: '停止生成' })).toBeVisible()

  // 生成中切到昨日会话：输入区不表现为发送中，可以正常输入与发送
  await page.locator('[data-workbench-conversation="c-yesterday"] button').first().click()
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0)
  await page.getByRole('textbox', { name: '向超级助手发送消息' }).fill('B 会话消息')
  await expect(page.getByRole('button', { name: '发送消息' })).toBeEnabled()
  // A 会话的流式内容不得串到 B
  await expect(page.getByText('你好，我是超级助手')).toHaveCount(0)

  // A 完成后切回：消息从服务端刷新，内容完整可见
  await expect.poll(mocks.isChatDone).toBe(true)
  await page.locator('[data-workbench-conversation="c-today"] button').first().click()
  await expect(page.getByText('你好，我是超级助手')).toBeVisible()
})

test('生成中可继续输入并将下一条排队', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page, { chatDelayMs: 1500 })
  await page.goto('/#/super-assistant?conversation=c-today')

  await page.getByRole('textbox', { name: '向超级助手发送消息' }).fill('你好')
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect(page.getByRole('button', { name: '停止生成' })).toBeVisible()

  await page.getByRole('textbox', { name: '向超级助手发送消息' }).fill('继续补充')
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect(page.getByTestId('super-assistant-queued-prompt')).toContainText('继续补充')
  await expect(page.getByRole('button', { name: '停止生成' })).toBeVisible()
  await expect.poll(() => mocks.chatCalls.length).toBe(2)
})

test('会话模型选择器为 ReUI Select，选择后 PATCH 持久化', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page)
  await page.goto('/#/super-assistant?conversation=c-today')

  // 触发器背景与左侧「上下文」胶囊同 token（浅色 rgba(240,253,250,0.8)），
  // 锁死以防 bg-brand-soft/80 这类 alpha 死类回潮（该写法不生成 CSS，视觉无背景）
  const trigger = page.getByRole('combobox', { name: '会话模型' })
  await expect(trigger).toHaveCSS('background-color', 'rgba(240, 253, 250, 0.8)')

  // 取消选择（Escape）后 Radix 把焦点程序化还给触发器并命中 :focus-visible：
  // 只允许 1px brand 细边（#059669）作焦点指示，基础组件的 2px 粗环必须被 ring-0 压掉
  //（ring-0 的 box-shadow 是 0 宽多段阴影而非字面量 none，故断言"不含 2px 环"）
  await trigger.click()
  await page.keyboard.press('Escape')
  await expect(trigger).not.toHaveCSS('box-shadow', /2px/)
  await expect(trigger).toHaveCSS('border-color', 'rgb(5, 150, 105)')

  // c-today 当前为 model-1（DeepSeek）；Radix Select 选中相同值不触发 onValueChange，切到 Qwen
  await trigger.click()
  await page.getByRole('option', { name: /Qwen/ }).click()
  await expect.poll(() => mocks.patchBodies.length).toBe(1)
  expect(JSON.parse(mocks.patchBodies[0])).toMatchObject({ model_config_id: 'model-2' })
})

test('模型下拉底部「管理模型」为跳转入口：进入模型配置页且不切换会话模型', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page)
  await page.goto('/#/super-assistant?conversation=c-today')

  await page.getByRole('combobox', { name: '会话模型' }).click()
  await page.getByRole('option', { name: '管理模型' }).click()
  await page.waitForURL('**/#/models')
  // 哨兵项只做跳转：不发起会话模型切换 PATCH
  expect(mocks.patchBodies).toHaveLength(0)
})

test('删除会话走 ReUI 确认弹窗（非 window.confirm）', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page)
  await page.goto('/#/super-assistant')

  const row = page.locator('[data-workbench-conversation="c-earlier"]')
  await row.hover()
  await row.getByRole('button', { name: '删除会话 上周数据摸底' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText(/确定删除会话「上周数据摸底」/)).toBeVisible()

  // 取消：弹窗关闭、会话仍在
  await dialog.getByRole('button', { name: '取消' }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.locator('[data-workbench-conversation="c-earlier"]')).toHaveCount(1)

  // 确认：DELETE 发出、会话移除
  await row.hover()
  await row.getByRole('button', { name: '删除会话 上周数据摸底' }).click()
  await page.getByRole('dialog').getByRole('button', { name: '删除', exact: true }).click()
  await expect.poll(() => mocks.deleteCalls).toEqual(['c-earlier'])
  await expect(page.locator('[data-workbench-conversation="c-earlier"]')).toHaveCount(0)
})

test('新建会话不落库：有消息会话中点击仅清空聊天区，发送首条消息才建会话', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page)
  await page.goto('/#/super-assistant?conversation=c-today')

  // c-today 是空会话：点击新建会话不创建
  await expect(page.getByTestId('super-assistant-composer')).toBeVisible()
  await page.getByRole('button', { name: '新建会话' }).click()
  await page.waitForTimeout(300)
  expect(mocks.createCalls).toHaveLength(0)

  // 切到有消息的 c-earlier：点击新建会话仅切到未落地的新会话视图，不建会话
  await page.locator('[data-workbench-conversation="c-earlier"] button').first().click()
  await expect(page.getByText('上周的答复')).toBeVisible()
  await page.getByRole('button', { name: '新建会话' }).click()
  await page.waitForTimeout(300)
  expect(mocks.createCalls).toHaveLength(0)
  await expect(page.locator('[data-workbench-conversation="c-new-1"]')).toHaveCount(0)
  // 聊天区已清空：历史消息不可见，回到品牌空态与标题占位，地址栏会话参数移除
  await expect(page.getByText('上周的答复')).toHaveCount(0)
  await expect(page.getByText('SuperAgent 工作空间 2.0', { exact: true })).toBeVisible()
  await expect(page.getByText('新的超级助手会话')).toBeVisible()
  await expect(page).not.toHaveURL(/conversation=/)

  // 未落地视图下再次点击：仍不创建
  await page.getByRole('button', { name: '新建会话' }).click()
  await page.waitForTimeout(300)
  expect(mocks.createCalls).toHaveLength(0)

  // 未落地视图下外部带同值深链（如悬浮窗跳回刚离开的会话）：不被已消费标记短路，正确切回
  await page.goto('/#/super-assistant?conversation=c-earlier')
  await expect(page.getByText('上周的答复')).toBeVisible()
  // 再次进入未落地视图，随后走发送路径
  await page.getByRole('button', { name: '新建会话' }).click()
  await page.waitForTimeout(300)
  expect(mocks.createCalls).toHaveLength(0)

  // 真正创建会话的时机是发送首条消息：懒建后消息投递进新会话并收到回复
  await page.getByRole('textbox', { name: '向超级助手发送消息' }).fill('第一条消息')
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect.poll(() => mocks.createCalls.length).toBe(1)
  await expect.poll(() => mocks.chatCalls).toEqual(['c-new-1'])
  await expect(page.getByText('你好，我是超级助手')).toBeVisible()
})

test('空态只保留品牌一句话，输入框占位符不混入用户输入', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await page.goto('/#/super-assistant?conversation=c-today')

  const hero = page.getByText('SuperAgent 工作空间 2.0', { exact: true })
  await expect(hero).toBeVisible()
  await expect(page.getByRole('heading', { name: '超级助手', exact: true })).toHaveCount(0)
  await expect(page.getByText('查资料、写文档、操作平台工具，或委派专业助手完成特定领域的任务')).toHaveCount(0)
  await expect(page.getByText('试试这样问')).toHaveCount(0)
  // 品牌一句话是页面视觉主角：字号不小于 text-3xl（30px）
  const heroFontSize = await hero.evaluate(el => parseFloat(getComputedStyle(el).fontSize))
  expect(heroFontSize).toBeGreaterThanOrEqual(30)

  const textbox = page.getByRole('textbox', { name: '向超级助手发送消息' })
  await expect(textbox).toHaveAttribute('placeholder', '输入消息；Shift + Enter 换行')
  await textbox.fill('帮我梳理需求')
  await expect(textbox).toHaveValue('帮我梳理需求')
})

test('助手配置面板为白色背景', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await page.goto('/#/super-assistant?conversation=c-today')

  // 配置面板默认收起：点击展开
  const configToggle = page.locator('button[title="助手配置"]')
  if ((await configToggle.getAttribute('aria-expanded')) !== 'true') await configToggle.click()
  const panel = page.locator('section[aria-label="助手配置"]')
  await expect(panel).toBeVisible()
  await expect.poll(() => panel.evaluate(el => getComputedStyle(el).backgroundColor)).toBe('rgb(255, 255, 255)')
})

test('工具标签页：查看内置工具目录、可用性标注与启停切换', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page)
  await page.goto('/#/super-assistant?conversation=c-today')

  const configToggle = page.locator('button[title="助手配置"]')
  if ((await configToggle.getAttribute('aria-expanded')) !== 'true') await configToggle.click()
  // 分段控件第 3 个标签：工具（含计数后缀，用正则消歧）
  await page.getByRole('button', { name: /^工具/ }).click()
  const toolsTab = page.getByTestId('tools-tab')
  await expect(toolsTab).toBeVisible()
  await expect(page.getByTestId('tool-card-memory_search')).toBeVisible()
  await expect(page.getByTestId('tool-card-multica_create_task')).toContainText('写操作 · 执行前审批')
  // 条件不可用如实标注，与用户启停是两个维度
  await expect(page.getByTestId('tool-card-web_search')).toContainText('平台未配置搜索后端')
  // 业务域分组：组头带数量（mock 夹具 memory 组仅 1 个），卡片归属正确组
  await expect(page.getByTestId('tool-group-memory')).toContainText('记忆')
  await expect(page.getByTestId('tool-group-memory')).toContainText('1 个')
  await expect(page.getByTestId('tool-group-web')).toContainText('web_search')
  await expect(page.getByTestId('tool-group-multica')).toContainText('multica_create_task')

  // 查看参数：展开后可见参数名与必填标记
  await page.getByTestId('tool-card-memory_search').getByText('查看参数').click()
  await expect(page.getByTestId('tool-card-memory_search').getByText('query')).toBeVisible()

  // 启停切换：PATCH 落库后目录刷新，「已禁用」徽章出现；组头禁用计数联动
  await page.getByRole('switch', { name: '禁用工具 memory_search' }).click()
  await expect.poll(() => mocks.toolPatchCalls).toEqual([
    { name: 'memory_search', body: { enabled: false } },
  ])
  await expect(page.getByTestId('tool-card-memory_search')).toContainText('已禁用')
  await expect(page.getByTestId('tool-group-memory')).toContainText('已禁用 1')
  // 再启用：徽章消失
  await page.getByRole('switch', { name: '启用工具 memory_search' }).click()
  await expect.poll(() => mocks.toolPatchCalls).toHaveLength(2)
  await expect(page.getByTestId('tool-card-memory_search')).not.toContainText('已禁用')
  await expect(page.getByTestId('tool-group-memory')).not.toContainText('已禁用')
})

test('重命名会话：点击其它处自动取消，Enter 仍可保存', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page)
  await page.goto('/#/super-assistant?conversation=c-today')

  // 点击表单外（聊天输入框）→ 自动取消编辑，不发 PATCH
  const titleButton = page.locator('section header').getByRole('button', { name: '今日需求梳理' })
  await titleButton.click()
  const renameInput = page.getByRole('textbox', { name: '编辑会话名称' })
  await expect(renameInput).toBeVisible()
  await renameInput.fill('改名尝试')
  await page.getByRole('textbox', { name: '向超级助手发送消息' }).click()
  await expect(renameInput).toHaveCount(0)
  expect(mocks.patchBodies).toHaveLength(0)
  await expect(page.locator('section header').getByRole('button', { name: '今日需求梳理' })).toBeVisible()

  // Enter 保存路径不受影响
  await page.locator('section header').getByRole('button', { name: '今日需求梳理' }).click()
  await page.getByRole('textbox', { name: '编辑会话名称' }).fill('新名称')
  await page.getByRole('textbox', { name: '编辑会话名称' }).press('Enter')
  await expect.poll(() => mocks.patchBodies.length).toBe(1)
  expect(JSON.parse(mocks.patchBodies[0])).toMatchObject({ title: '新名称' })
})

test('知识图谱：三栏工作台（文件树|内容|图谱），上传删除联动，外部集成打开 multica 配置弹窗', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page)
  await page.goto('/#/super-assistant')

  await page.getByRole('button', { name: '知识图谱' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  // level:2 消歧：弹窗标题 h2 与图谱面板 h3 同名「知识图谱」
  await expect(dialog.getByRole('heading', { name: '知识图谱', level: 2 })).toBeVisible()

  // 三栏同时可见：左树（含目录「设计图」）、中栏空态、右侧图谱画布
  const filesPane = dialog.getByTestId('super-assistant-palace-files')
  const contentPane = dialog.locator('section[aria-label="文档内容"]')
  const graphSection = dialog.getByTestId('super-assistant-palace-graph')
  await expect(filesPane).toBeVisible()
  await expect(filesPane.getByTestId('palace-file-tree')).toBeVisible()
  await expect(filesPane.getByText('设计图')).toBeVisible()
  await expect(filesPane.getByText('个人知识库.md')).toBeVisible()
  await expect(filesPane.getByText('架构图.png')).toBeVisible()
  await expect(contentPane.getByText(/在左侧选择一个文档/)).toBeVisible()
  await expect(graphSection.locator('canvas').first()).toBeVisible()
  const statsBar = graphSection.getByTestId('palace-graph-stats')
  await expect(statsBar).toContainText('3 实体')
  await expect(statsBar).toContainText('2 关系')
  await expect(statsBar).toContainText('已建图文档 2/3')

  // 上传：隐藏 input 接线到 palace 上传端点，树即时刷新并自动选中新文件
  await dialog.getByTestId('palace-file-input').setInputFiles({
    name: '新文档.md', mimeType: 'text/markdown', buffer: Buffer.from('# 新文档\n张三 任职 ACME'),
  })
  await expect.poll(() => mocks.palaceUploads.length).toBe(1)
  expect(mocks.palaceUploads[0]).toBe('新文档.md@')
  await expect(filesPane.getByText('新文档.md')).toBeVisible()
  // 文件名同时出现在中栏标题与预览正文，取首个（标题）
  await expect(contentPane.getByText('新文档.md').first()).toBeVisible()
  await expect(contentPane.getByText('待抽取')).toBeVisible()

  // 选中行业研究.pdf：中栏展示元信息（抽取中状态徽标），删除后从树中消失
  await filesPane.locator('[data-palace-file="pf-2"]').click()
  await expect(contentPane.getByText('行业研究.pdf')).toBeVisible()
  await expect(contentPane.getByText('抽取中')).toBeVisible()
  await contentPane.getByRole('button', { name: '删除 行业研究.pdf' }).click()
  await expect.poll(() => mocks.palaceDeletes.length).toBe(1)
  await expect(filesPane.getByText('行业研究.pdf')).toHaveCount(0)

  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)

  // 外部集成已是真实功能：左侧集成类型 tabs（multica / 远程助手），
  // multica 默认选中并走通「测试连接 → 选工作区 → 保存」
  await page.getByRole('button', { name: '外部集成' }).click()
  const integrationsDialog = page.getByRole('dialog')
  await expect(integrationsDialog.getByRole('heading', { name: '外部集成' })).toBeVisible()
  const multicaTab = integrationsDialog.locator('[data-integrations-tab="multica"]')
  await expect(multicaTab).toHaveAttribute('aria-selected', 'true')
  await expect(integrationsDialog.getByTestId('multica-config-card')).toBeVisible()
  // 文件夹同步已迁至知识图谱「远程同步」目录：左栏仅剩两个集成 tab
  await expect(integrationsDialog.locator('[data-integrations-tab]')).toHaveCount(2)
  await expect(integrationsDialog.locator('[data-integrations-tab="folder-sync"]')).toHaveCount(0)
  // 等入场缩放动画结束再量尺寸，避免动画中的包围盒参与比较
  await page.waitForTimeout(300)

  // 切到远程助手 tab 面板替换，切回 multica 表单仍在；弹窗固定宽高：切 tab 前后尺寸不变
  const shellBefore = await integrationsDialog.boundingBox()
  await integrationsDialog.locator('[data-integrations-tab="remote-agents"]').click()
  await expect(integrationsDialog.getByTestId('multica-config-card')).toHaveCount(0)
  const shellAfter = await integrationsDialog.boundingBox()
  expect(Math.abs(shellBefore!.height - shellAfter!.height)).toBeLessThanOrEqual(1)
  expect(Math.abs(shellBefore!.width - shellAfter!.width)).toBeLessThanOrEqual(1)
  const remoteAgentsLabel = integrationsDialog.locator('[data-integrations-tab="remote-agents"]').locator('span').first()
  expect(await remoteAgentsLabel.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true)
  await multicaTab.click()
  await expect(integrationsDialog.getByTestId('multica-config-card')).toBeVisible()

  await integrationsDialog.getByTestId('multica-base-url').fill('http://127.0.0.1:8080')
  await integrationsDialog.getByTestId('multica-token').fill('mul-e2e-token')
  await integrationsDialog.getByTestId('multica-test-button').click()
  await expect(integrationsDialog.getByTestId('multica-test-result')).toContainText('连接成功')
  await expect.poll(() => mocks.multicaTests.length).toBe(1)
  expect(mocks.multicaTests[0]).toMatchObject({ base_url: 'http://127.0.0.1:8080', token: 'mul-e2e-token' })

  await integrationsDialog.getByRole('combobox', { name: 'Multica 工作区' }).click()
  await page.getByRole('option', { name: 'E2E 工作区' }).click()
  await integrationsDialog.getByRole('checkbox', { name: '启用集成' }).check()
  await integrationsDialog.getByTestId('multica-save-button').click()
  await expect.poll(() => mocks.multicaPuts.length).toBe(1)
  expect(mocks.multicaPuts[0]).toMatchObject({
    base_url: 'http://127.0.0.1:8080',
    token: 'mul-e2e-token',
    workspace_id: 'ws-2',
    // 选中工作区的显示名随保存持久化（下拉兜底显示名称而非裸 UUID）
    workspace_name: 'E2E 工作区',
    enabled: true,
  })
  await expect(integrationsDialog).toHaveCount(0)
})

test('multica 配置弹窗：已配置时打开即拉取全量工作区，无需先点测试连接', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page, { multicaConfig: multicaEnabledFixture })
  await page.goto('/#/super-assistant')

  await page.getByRole('button', { name: '外部集成' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('heading', { name: '外部集成' })).toBeVisible()
  await expect(dialog.getByTestId('multica-config-card')).toBeVisible()

  // 打开弹窗即请求工作区列表（此前只有单条已保存兜底，掩盖其余可选工作区）；
  // StrictMode 开发态 effect 双触发，调用数按 ≥1 断言
  await expect.poll(() => mocks.multicaWorkspaceCalls.length).toBeGreaterThanOrEqual(1)
  await expect(mocks.multicaTests.length).toBe(0)
  await dialog.getByRole('combobox', { name: 'Multica 工作区' }).click()
  await expect(page.getByRole('option', { name: 'My Workspace' })).toBeVisible()
  await expect(page.getByRole('option', { name: 'E2E 工作区' })).toBeVisible()
})

test('知识图谱：选中即预览，md 在线编辑保存触发 PUT content，图片走原图预览', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page)
  await page.goto('/#/super-assistant')

  await page.getByRole('button', { name: '知识图谱' }).click()
  const dialog = page.getByRole('dialog')
  const filesPane = dialog.getByTestId('super-assistant-palace-files')
  const contentPane = dialog.locator('section[aria-label="文档内容"]')

  // 树中点击 md：中栏自动加载抽取文本预览
  await filesPane.locator('[data-palace-file="pf-1"]').click()
  await expect.poll(() => mocks.palacePreviews.length).toBe(1)
  expect(mocks.palacePreviews[0]).toBe('pf-1')
  const previewPanel = dialog.getByTestId('palace-file-preview')
  await expect(previewPanel).toBeVisible()
  await expect(previewPanel.getByText('张三 任职 ACME，正在研究知识图谱。')).toBeVisible()

  // pdf：previewable=false 时如实提示；且非 editable 文件不渲染编辑按钮
  await filesPane.locator('[data-palace-file="pf-2"]').click()
  await expect.poll(() => mocks.palacePreviews.length).toBe(2)
  await expect(dialog.getByTestId('palace-file-preview')).toContainText('该格式暂不支持文本预览')
  await expect(contentPane.getByRole('button', { name: '编辑 行业研究.pdf' })).toHaveCount(0)

  // 图片：经鉴权 raw 端点拉取 blob 并渲染 img（不参与图谱抽取）
  await filesPane.locator('[data-palace-file="pf-img"]').click()
  await expect.poll(() => mocks.palaceRaws.length).toBe(1)
  expect(mocks.palaceRaws[0]).toBe('pf-img')
  await expect(dialog.getByTestId('palace-file-image').locator('img')).toBeVisible()
  await expect(contentPane.getByText('已建图')).toBeVisible()

  // 编辑：md 经 preview 端点取初稿，改写后显式保存
  await filesPane.locator('[data-palace-file="pf-1"]').click()
  await contentPane.getByRole('button', { name: '编辑 个人知识库.md' }).click()
  const editorPanel = dialog.getByTestId('palace-file-editor')
  await expect(editorPanel).toBeVisible()
  const textarea = editorPanel.locator('textarea')
  await expect(textarea).toHaveValue(/张三 任职 ACME/)
  await textarea.fill('# 更新后的知识库\n\n张三 任职 ACME。')
  await editorPanel.getByRole('button', { name: '保存并重建图谱' }).click()
  await expect.poll(() => mocks.palaceContentPuts.length).toBe(1)
  expect(JSON.parse(mocks.palaceContentPuts[0])).toMatchObject({ content: '# 更新后的知识库\n\n张三 任职 ACME。' })
  // toast 提示（渲染在弹窗外层的全局 Toaster（sonner））+ 编辑器关闭回到预览
  await expect(page.getByText('已保存，图谱重建已排队')).toBeVisible()
  await expect(editorPanel).toHaveCount(0)
  await expect(dialog.getByTestId('palace-file-preview')).toBeVisible()
})

test('知识图谱：ZIP 导入按压缩包名建顶层目录并展示跳过原因', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page)
  await page.goto('/#/super-assistant')

  await page.getByRole('button', { name: '知识图谱' }).click()
  const dialog = page.getByRole('dialog')
  const filesPane = dialog.getByTestId('super-assistant-palace-files')

  // mock 端点不校验真实 zip 字节：普通 buffer + .zip 文件名即可断言 batch 请求
  await dialog.getByTestId('palace-zip-input').setInputFiles({
    name: '知识库备份.zip', mimeType: 'application/zip', buffer: Buffer.from('placeholder-zip'),
  })
  await expect.poll(() => mocks.palaceBatchImports.length).toBe(1)
  expect(mocks.palaceBatchImports[0]).toBe('知识库备份.zip')

  const banner = dialog.getByTestId('palace-import-result')
  await expect(banner).toBeVisible()
  await expect(banner.getByText('导入 1 个，跳过 1 个')).toBeVisible()
  await banner.getByRole('button', { name: '查看跳过原因' }).click()
  await expect(banner.getByText('重复笔记.md：同名文件已存在')).toBeVisible()

  // 新目录自动展开，导入的文件落在压缩包名目录下
  await expect(filesPane.getByText('知识库备份')).toBeVisible()
  await expect(filesPane.getByText('导入笔记.md')).toBeVisible()
  const tree = filesPane.getByTestId('palace-file-tree')
  await expect(tree.locator('[data-palace-file="pf-zip-1"]')).toBeVisible()
})

test('知识图谱：图谱过滤、节点详情、点节点定位来源文档与聚焦联动', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page)
  await page.goto('/#/super-assistant')

  await page.getByRole('button', { name: '知识图谱' }).click()
  const dialog = page.getByRole('dialog')
  const filesPane = dialog.getByTestId('super-assistant-palace-files')
  const contentPane = dialog.locator('section[aria-label="文档内容"]')
  const graphSection = dialog.getByTestId('super-assistant-palace-graph')
  await expect(graphSection.locator('canvas').first()).toBeVisible()

  // 过滤：300ms 防抖后只剩「张三」一个节点（计数文案，不做脆弱断言）
  await graphSection.getByTestId('palace-graph-filter').fill('张三')
  await expect(graphSection.getByTestId('palace-graph-filter-count')).toHaveText(/1 \/ 3 实体/)

  // 单节点力导向收敛于布局中心（series top 28 / bottom 8）：轮询点击画布中心命中节点
  const canvas = graphSection.locator('canvas').first()
  const box = await canvas.boundingBox()
  expect(box).not.toBeNull()
  const detail = graphSection.getByTestId('palace-node-detail')
  await expect.poll(async () => {
    await canvas.click({ position: { x: box!.width / 2, y: box!.height / 2 + 10 } })
    return detail.isVisible()
  }).toBe(true)
  await expect(detail.getByText('张三', { exact: true })).toBeVisible()
  await expect(detail.getByText(/人物 · 提及 5 次 · 被引用 2 次/)).toBeVisible()
  await expect(detail.getByText('ACME（任职）')).toBeVisible()

  // 节点↔文档联动：张三唯一来源 pf-1，点节点直接在中栏定位该文档
  await expect(contentPane.getByText('个人知识库.md').first()).toBeVisible()
  await expect(contentPane.getByText(/解析 1800 字符/)).toBeVisible()
  // 来源文档 chips 可再定位
  await expect(detail.getByTestId('palace-node-source-file').getByText('个人知识库.md')).toBeVisible()

  // 选中文件后图谱默认聚焦其贡献节点，可一键切回全图
  const focusChip = graphSection.getByTestId('palace-graph-file-focus')
  await expect(focusChip).toBeVisible()
  await expect(focusChip).toHaveAttribute('aria-pressed', 'true')
  await focusChip.click()
  await expect(focusChip).toHaveAttribute('aria-pressed', 'false')

  // 邻域检索：返回实体 id 集合成为高亮集，非命中节点静态降透明
  await detail.getByRole('button', { name: '在图谱中检索邻域' }).click()
  await expect.poll(() => mocks.palaceGraphSearches.length).toBe(1)
  expect(mocks.palaceGraphSearches[0]).toBe('张三')
  await expect(graphSection.getByTestId('palace-graph-highlight-count')).toContainText('2 个实体已高亮')

  // 树侧再切换到 pdf：中栏联动，图谱聚焦文案随之更新
  await filesPane.locator('[data-palace-file="pf-2"]').click()
  await expect(contentPane.getByText('行业研究.pdf')).toBeVisible()
  await expect(graphSection.getByTestId('palace-graph-file-focus')).toContainText('行业研究.pdf')
})

test('⌘K / Ctrl+K 唤起全局搜索面板', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await page.goto('/#/super-assistant')

  await page.keyboard.press('Control+k')
  await expect(page.getByRole('dialog')).toBeVisible()
  await expect(page.getByPlaceholder('搜索会话标题与消息内容…')).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).toHaveCount(0)
})

test('全局搜索：检索会话标题与消息内容，命中消息可跳转会话', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page)
  await page.goto('/#/super-assistant')

  await page.getByRole('button', { name: '全局搜索' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()

  // 检索请求悬挂 500ms：断言检索中只有一个「正在搜索…」且在列表区垂直居中（输入框 44px + 行 min-h 264px）
  await page.route('**/api/v2/super-assistant/search/conversations**', async route => {
    await new Promise(resolve => setTimeout(resolve, 500))
    return route.fallback()
  })

  // 输入关键词（防抖 300ms 后发出检索请求）
  await page.getByPlaceholder('搜索会话标题与消息内容…').fill('需求')
  const searching = page.getByText('正在搜索…')
  await expect(searching).toBeVisible()
  // 夹具把检索响应挂起 500ms：并行 worker 拖慢时，断言到取几何之间元素可能已卸载，
  // 句柄先行 + 空值跳过几何断言，消除竞态（几何检查存在时仍精确到 ±8px）
  const searchingBox = await searching.boundingBox()
  const dialogBox = await dialog.boundingBox()
  if (searchingBox && dialogBox) {
    expect(Math.abs((searchingBox.y + searchingBox.height / 2) - (dialogBox.y + 44 + 132))).toBeLessThan(8)
  }

  await expect.poll(() => mocks.searchQueries.length).toBeGreaterThan(0)
  await expect.poll(() => mocks.searchQueries.at(-1)).toBe('需求')

  // 标题命中与消息命中分组展示，关键词高亮
  await expect(page.getByTestId('global-search-title-hit')).toHaveCount(1)
  await expect(dialog.locator('mark').first()).toHaveText('需求')
  await expect(page.getByTestId('global-search-message-hit')).toHaveCount(1)

  // 选中消息命中：面板关闭并切到该会话
  await page.getByTestId('global-search-message-hit').click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.locator('[data-workbench-conversation="c-today"]')).toHaveClass(/bg-brand-soft/)

  // 无结果文案
  await page.keyboard.press('Control+k')
  await page.getByPlaceholder('搜索会话标题与消息内容…').fill('不存在的词')
  await expect(page.getByText('没有匹配的会话或消息')).toBeVisible()
})

test('输入草稿按会话缓存：切换会话不丢内容，发送后清空', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page)
  await page.goto('/#/super-assistant?conversation=c-today')

  const textbox = page.getByRole('textbox', { name: '向超级助手发送消息' })
  await textbox.fill('A 会话的草稿')

  await page.locator('[data-workbench-conversation="c-yesterday"] button').first().click()
  await expect(textbox).toHaveValue('')
  await textbox.fill('B 会话的草稿')

  await page.locator('[data-workbench-conversation="c-today"] button').first().click()
  await expect(textbox).toHaveValue('A 会话的草稿')
  await page.locator('[data-workbench-conversation="c-yesterday"] button').first().click()
  await expect(textbox).toHaveValue('B 会话的草稿')

  // A 发送后其草稿清空，且不影响 B 的草稿
  await page.locator('[data-workbench-conversation="c-today"] button').first().click()
  await expect(textbox).toHaveValue('A 会话的草稿')
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect.poll(mocks.isChatDone).toBe(true)
  await expect(textbox).toHaveValue('')
  await page.locator('[data-workbench-conversation="c-yesterday"] button').first().click()
  await expect(textbox).toHaveValue('B 会话的草稿')
  await page.locator('[data-workbench-conversation="c-today"] button').first().click()
  await expect(textbox).toHaveValue('')
})

test('知识图谱：弹窗全屏切换与图谱缩放控制', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await page.goto('/#/super-assistant')

  await page.getByRole('button', { name: '知识图谱' }).click()
  const dialog = page.getByRole('dialog')
  const filesPane = dialog.getByTestId('super-assistant-palace-files')
  const contentPane = dialog.locator('section[aria-label="文档内容"]')
  const graphSection = dialog.getByTestId('super-assistant-palace-graph')

  // 选中一个文档作为联动基线
  await filesPane.locator('[data-palace-file="pf-1"]').click()
  await expect(dialog.getByTestId('palace-file-preview')).toBeVisible()

  // 缩放控制簇：放大/缩小/复位可用
  const zoomIn = graphSection.getByTestId('palace-zoom-in')
  const zoomOut = graphSection.getByTestId('palace-zoom-out')
  const zoomReset = graphSection.getByTestId('palace-zoom-reset')
  await zoomIn.click()
  await zoomIn.click()
  await zoomOut.click()
  await zoomReset.click()

  // 全屏切换：aria-pressed 翻转，三栏与选中态在两种形态下都保持
  const fullscreen = dialog.getByTestId('palace-fullscreen')
  await fullscreen.click()
  await expect(fullscreen).toHaveAttribute('aria-pressed', 'true')
  await expect(filesPane.getByTestId('palace-file-tree')).toBeVisible()
  await expect(graphSection.locator('canvas').first()).toBeVisible()
  await expect(contentPane.getByText('个人知识库.md').first()).toBeVisible()

  // 全屏下退出：状态复位
  await fullscreen.click()
  await expect(fullscreen).toHaveAttribute('aria-pressed', 'false')
  await expect(contentPane.getByText('个人知识库.md').first()).toBeVisible()

  // 图谱视图保持：放大后刷新（同一数据签名）不重置用户视角 —— 通过
  // 缩放按钮放大后触发刷新按钮，画布不报错且聚焦 chip 仍在
  await zoomIn.click()
  await graphSection.getByRole('button', { name: '刷新知识图谱' }).click()
  await expect(graphSection.locator('canvas').first()).toBeVisible()
  const statsBar = graphSection.getByTestId('palace-graph-stats')
  await expect(statsBar).toContainText('3 实体')
  await expect(statsBar).toContainText('2 关系')
  await expect(statsBar).toContainText('已建图文档 2/3')
})

test('知识图谱：目录一等公民——新建目录/笔记、重命名与空目录删除', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page)
  await page.goto('/#/super-assistant')

  await page.getByRole('button', { name: '知识图谱' }).click()
  const dialog = page.getByRole('dialog')
  const filesPane = dialog.getByTestId('super-assistant-palace-files')
  const contentPane = dialog.locator('section[aria-label="文档内容"]')
  const toolbar = dialog.getByTestId('palace-dir-toolbar')
  await expect(toolbar).toContainText('当前目录：/')

  // 新建子目录（根目录下）：内联输入行出现在树顶，Enter 提交 POST folders
  await toolbar.getByTestId('palace-new-folder').click()
  const inlineInput = filesPane.getByTestId('palace-inline-input')
  await expect(inlineInput).toBeVisible()
  await inlineInput.fill('项目资料')
  await inlineInput.press('Enter')
  await expect.poll(() => mocks.palaceFolderCreates.length).toBe(1)
  expect(mocks.palaceFolderCreates[0]).toBe('项目资料')
  await expect(filesPane.locator('[data-palace-dir="项目资料"]')).toBeVisible()

  // 在新建目录下新建 md 笔记：POST notes（draft），自动进入编辑态。
  // 只填笔记名不带后缀：前端归一化补 .md 后提交
  await filesPane.locator('[data-palace-dir="项目资料"]').click()
  await expect(toolbar).toContainText('当前目录：/项目资料')
  await toolbar.getByTestId('palace-new-note').click()
  const noteInput = filesPane.getByTestId('palace-inline-input')
  await expect(noteInput).toHaveAttribute('placeholder', '笔记名（自动存为 .md）')
  await noteInput.fill('会议纪要')
  await noteInput.press('Enter')
  await expect.poll(() => mocks.palaceNotes.length).toBe(1)
  expect(mocks.palaceNotes[0]).toBe('会议纪要.md@项目资料')
  const editorPanel = dialog.getByTestId('palace-file-editor')
  await expect(editorPanel).toBeVisible()
  // 空 draft 笔记内容为空也可编辑（previewable 不随空内容翻转）：编辑器给出空文本域
  await expect(editorPanel.locator('textarea')).toHaveValue('')
  await expect(filesPane.locator('[data-palace-file="pf-note-1"]')).toBeVisible()

  // 草稿保存内容 → PUT content → pending（进入既有抽取链路）
  await editorPanel.locator('textarea').fill('# 会议纪要\n张三 任职 ACME')
  await editorPanel.getByRole('button', { name: '保存并重建图谱' }).click()
  await expect.poll(() => mocks.palaceContentPuts.length).toBe(1)
  await expect(contentPane.getByText('待抽取')).toBeVisible()

  // 重命名目录（笔记选中后当前目录已是其归属目录）
  await toolbar.getByTestId('palace-rename-folder').click()
  const renameInput = filesPane.getByTestId('palace-inline-input')
  await expect(renameInput).toHaveValue('项目资料')
  await renameInput.fill('项目档案')
  await renameInput.press('Enter')
  await expect.poll(() => mocks.palaceFolderRenames.length).toBe(1)
  expect(mocks.palaceFolderRenames[0]).toBe('项目资料->项目档案')

  // 空目录删除：先删文件（清空目录），标准确认弹窗确认后 DELETE
  await expect(toolbar).toContainText('当前目录：/项目档案')
  await expect(filesPane.locator('[data-palace-dir="项目档案"]')).toBeVisible()
  await filesPane.locator('[data-palace-file="pf-note-1"]').click()
  const deleteNoteButton = contentPane.getByRole('button', { name: /删除 会议纪要/ })
  await expect(deleteNoteButton).toBeEnabled()
  await deleteNoteButton.click()
  await expect.poll(() => mocks.palaceDeletes.length).toBe(1)
  await filesPane.locator('[data-palace-dir="项目档案"]').click()
  await toolbar.getByTestId('palace-delete-folder').click()
  await page.getByRole('dialog', { name: /删除目录/ }).getByRole('button', { name: '删除', exact: true }).click()
  await expect.poll(() => mocks.palaceFolderDeletes.length).toBe(1)
  expect(mocks.palaceFolderDeletes[0]).toBe('项目档案')
  await expect(filesPane.locator('[data-palace-dir="项目档案"]')).toHaveCount(0)
})

/** HTML5 拖拽事件序列（dragTo 的鼠标模拟不触发原生 dnd，headless-tree 用原生 dnd） */
const dragByDnd = async (page: Page, source: Locator, target: Locator) => {
  const dataTransfer = await page.evaluateHandle(() => new DataTransfer())
  await source.dispatchEvent('dragstart', { dataTransfer })
  await target.dispatchEvent('dragover', { dataTransfer })
  await target.dispatchEvent('drop', { dataTransfer })
  await source.dispatchEvent('dragend', { dataTransfer })
}

test('知识图谱：拖拽文件至目录归位与画布下统计条', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page)
  await page.goto('/#/super-assistant')

  await page.getByRole('button', { name: '知识图谱' }).click()
  const dialog = page.getByRole('dialog')
  const filesPane = dialog.getByTestId('super-assistant-palace-files')
  const graphSection = dialog.getByTestId('super-assistant-palace-graph')

  // 统计条在画布下方：实体/关系/已建图文档/上次更新时间
  const statsBar = graphSection.getByTestId('palace-graph-stats')
  await expect(statsBar).toContainText('3 实体')
  await expect(statsBar).toContainText('2 关系')
  await expect(statsBar).toContainText('已建图文档 2/3')
  await expect(statsBar).toContainText(/上次更新：\d{4}-\d{2}-\d{2}/)
  // 标题行不再重复统计小字
  await expect(graphSection.getByText(/3 实体 \/ 2 关系/)).toHaveCount(0)

  // HTML5 拖拽（headless-tree 原生 dnd）：个人知识库.md → 目录「设计图」，
  // PATCH 携带目标目录
  const source = filesPane.locator('[data-palace-file="pf-1"]')
  await dragByDnd(page, source, filesPane.locator('[data-palace-dir="设计图"]'))
  await expect.poll(() => mocks.palaceMoves.length).toBe(1)
  expect(mocks.palaceMoves[0]).toBe('pf-1:->设计图')

  // 拖到树容器空白处 → 根目录 drop → folderPath 空串
  await dragByDnd(page, source, filesPane.getByTestId('palace-file-tree'))
  await expect.poll(() => mocks.palaceMoves.length).toBe(2)
  expect(mocks.palaceMoves[1]).toBe('pf-1:设计图->')
})

test('知识图谱：本体文档目录——发布文档只读展示、目录计数徽章与待同步状态', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page, { withOntologyDocs: true })
  await page.goto('/#/super-assistant')

  await page.getByRole('button', { name: '知识图谱' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  const filesPane = dialog.getByTestId('super-assistant-palace-files')
  const contentPane = dialog.locator('section[aria-label="文档内容"]')

  // 「本体文档」固定根级首位；目录行右侧直接子节点计数徽章（含子目录与文件）
  const ontoDirRow = filesPane.locator('[data-palace-dir="__ontology_docs__"]')
  await expect(ontoDirRow).toBeVisible()
  await expect(ontoDirRow).toContainText('本体文档')
  await expect(ontoDirRow.getByLabel('本体文档 下共 2 项')).toHaveText('2')
  // 用户目录同样有计数徽章
  await expect(filesPane.locator('[data-palace-dir="设计图"]').getByLabel('设计图 下共 1 项')).toHaveText('1')

  // 点击已建图文档：中栏只读预览（无编辑/替换/删除动作）；渲染/源码切换可用
  await filesPane.getByText('供应链业务文档').click()
  await expect(contentPane.getByText('供应链业务文档').first()).toBeVisible()
  await expect(contentPane.getByText(/只读，随本体发布自动更新/)).toBeVisible()
  await expect(contentPane.getByTestId('palace-ontology-doc-preview')).toContainText('张三 任职 ACME')
  await expect(contentPane.getByTestId('palace-ontology-doc-preview').getByRole('button', { name: /编辑|替换|删除/ })).toHaveCount(0)
  await expect(contentPane.getByTestId('palace-ontology-preview-mode')).toBeVisible()

  // 权威清单有而镜像未达（发布事件在途）：占位行标记「待同步」
  await filesPane.getByText('合规本体 业务文档').click()
  await expect(contentPane.getByText(/尚未同步到知识图谱/)).toBeVisible()

  // 最后点目录行（单击会折叠目录）：工具栏切换为只读说明，上传/新建被禁用
  await ontoDirRow.click()
  await expect(filesPane.getByTestId('palace-dir-toolbar')).toContainText('本体文档（只读')
  await expect(filesPane.getByRole('button', { name: '上传', exact: true })).toBeDisabled()
  await expect(filesPane.getByTestId('palace-new-folder')).toBeDisabled()
  await expect(filesPane.getByTestId('palace-new-note')).toBeDisabled()
})



test('远程助手：邀请函自助接入主路径，手动配置为高级路径，双模式展示', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page)
  await page.goto('/#/super-assistant')

  await page.getByRole('button', { name: '外部集成' }).click()
  const integrationsDialog = page.getByRole('dialog')
  await expect(integrationsDialog.getByRole('heading', { name: '外部集成' })).toBeVisible()
  await integrationsDialog.locator('[data-integrations-tab="remote-agents"]').click()

  // 目录：直连行展示端点，回连行展示模式徽标与在线状态；已有助手时仍可再邀请
  const list = integrationsDialog.getByTestId('remote-agent-list')
  await expect(integrationsDialog.getByText('可同时接入多个远程助手')).toBeVisible()
  await expect(integrationsDialog.getByTestId('remote-agent-count')).toHaveText('已接入 2 个')
  await expect(integrationsDialog.getByRole('button', { name: '再邀请一个' })).toBeVisible()
  await expect(list.getByText('客服知识库助手')).toBeVisible()
  await expect(list.getByText('https://kb.example.com/turn')).toBeVisible()
  await expect(list.getByText('内网文档助手')).toBeVisible()
  await expect(list.getByText('回连', { exact: true })).toBeVisible()
  await expect(list.getByText('直连', { exact: true })).toBeVisible()
  await expect(list.getByText('在线').first()).toBeVisible()
  await expect(list.getByText('上次委派 3 小时前')).toBeVisible()

  // 主路径：已有助手时再邀请 → 邀请函弹层常驻全文（复制失败也有手动复制/下载兜底）
  await integrationsDialog.getByTestId('remote-agent-invite-create').click()
  const promptBox = integrationsDialog.getByTestId('remote-agent-invite-prompt')
  await expect(promptBox).toBeVisible()
  await expect(promptBox).toHaveValue(/rai_e2e_fixture_invite_token_1234/)
  await expect(promptBox).toHaveValue(/方式 B：回连/)
  await integrationsDialog.getByTestId('remote-agent-invite-copy').click()
  await expect(
    page.getByText('已尝试写入剪贴板').or(page.getByText('自动复制不可用')),
  ).toBeVisible()
  await page.keyboard.press('Escape')

  // 邀请卡：待使用状态 + 撤销流转
  const invites = integrationsDialog.getByTestId('remote-agent-invite-list')
  await expect(invites.getByText('待使用')).toBeVisible()
  await invites.getByRole('button', { name: '撤销' }).click()
  await expect(invites.getByText('已撤销', { exact: true })).toBeVisible()

  // 高级路径：手动配置（key 留空自动生成）仍可用
  await integrationsDialog.getByRole('button', { name: '手动配置' }).click()
  const form = integrationsDialog.getByTestId('remote-agent-form')
  await expect(form).toBeVisible()
  await form.getByTestId('remote-agent-label').fill('数据查询助手')
  await form.getByTestId('remote-agent-endpoint').fill('https://data.example.com/turn')
  await integrationsDialog.getByTestId('remote-agent-save-button').click()
  await expect.poll(() => mocks.remoteAgentCreates.length).toBe(1)
  expect(mocks.remoteAgentCreates[0].endpoint).toBe('https://data.example.com/turn')
  expect(mocks.remoteAgentCreates[0].key).toBeUndefined()
  await expect(list.getByText('客服知识库助手')).toBeVisible()
  await expect(list.getByText('内网文档助手')).toBeVisible()
  await expect(list.getByText('数据查询助手')).toBeVisible()
  await expect(integrationsDialog.getByTestId('remote-agent-count')).toHaveText('已接入 3 个')
})

test('远程助手：空目录展示首次邀请文案，加载完成前不闪成单槽 CTA', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page, {
    remoteAgents: [],
    remoteAgentsDelayMs: 1200,
  })
  await page.goto('/#/super-assistant')
  await page.getByRole('button', { name: '外部集成' }).click()
  const integrationsDialog = page.getByRole('dialog')
  await integrationsDialog.locator('[data-integrations-tab="remote-agents"]').click()

  await expect(integrationsDialog.getByRole('button', { name: '邀请 AI 助手接入' })).toHaveCount(0)
  await expect(integrationsDialog.getByRole('button', { name: '再邀请一个' })).toHaveCount(0)
  await expect(integrationsDialog.getByRole('button', { name: '正在加载远程助手' })).toBeVisible()

  await expect(integrationsDialog.getByRole('button', { name: '邀请 AI 助手接入' })).toBeVisible()
  await expect(integrationsDialog.getByText('还没有接入远程助手')).toBeVisible()
  await expect(integrationsDialog.getByText('之后还能继续添加')).toBeVisible()
  await expect(integrationsDialog.getByTestId('remote-agent-count')).toHaveCount(0)
})

test('会话模型下拉过滤后台用途配置：usage_tags 标记不出现，偏好标记保留', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await page.goto('/#/super-assistant?conversation=c-today')

  await page.getByRole('combobox', { name: '会话模型' }).click()
  // 无标记与仅 super_assistant 偏好标记的对话模型可选
  await expect(page.getByRole('option', { name: /Qwen/ })).toBeVisible()
  await expect(page.getByRole('option', { name: /GLM 对话专用/ })).toBeVisible()
  // 记忆宫殿抽取等后台管线配置不出现在会话模型列表
  await expect(page.getByRole('option', { name: /记忆宫殿抽取/ })).toHaveCount(0)
})

test('长会话进入即贴底，上滚出现「回到最新」，点击回到底部', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  const longMessages = Array.from({ length: 30 }, (_, index) => ({
    id: `m-long-${index}`, conversation_id: 'c-long',
    role: index % 2 === 0 ? 'user' : 'assistant',
    content: `第 ${index + 1} 条消息：${'内容'.repeat(30)}`,
    status: 'complete', steps: [], token_usage: {}, created_at: at(0, 9),
  }))
  await page.route('**/api/v2/super-assistant/conversations', route => {
    if (route.request().method() === 'GET') {
      return json(route, [{
        id: 'c-long', title: '长会话', model_config_id: 'model-1',
        status: 'active', created_at: at(0, 9), updated_at: at(0, 9),
      }])
    }
    return route.fallback()
  })
  await page.route('**/api/v2/super-assistant/conversations/c-long/messages', route => json(route, longMessages))

  await page.goto('/#/super-assistant?conversation=c-long')
  const scroller = page.locator('main div.h-full.overflow-y-auto')
  await expect(scroller.getByText('第 30 条消息')).toBeVisible()

  // 进入长会话直接看到最新消息：滚动位置贴近底部
  await expect.poll(async () => {
    const distance = await scroller.evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight)
    return Math.round(distance)
  }).toBeLessThan(16)

  // 上滚离开底部后出现「回到最新」
  await scroller.evaluate(el => { el.scrollTop = 0 })
  const jumpButton = page.getByRole('button', { name: '回到最新消息' })
  await expect(jumpButton).toBeVisible()

  await jumpButton.click()
  await expect.poll(async () => {
    const distance = await scroller.evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight)
    return Math.round(distance)
  }).toBeLessThan(16)
  await expect(jumpButton).toBeHidden()
})

test('会话模型下拉空回退：全部配置带流水线标签时仍显示全集', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  // 后注册的路由优先：把全部 llm 配置都打上流水线专用标签（且都非默认）
  await page.route('**/api/v1/models', route => json(route, [{
    id: 'model-p1', name: 'Palace-A', config_type: 'llm', provider: 'a',
    api_base: '', has_api_key: true, enabled: true, is_default: false,
    last_test_status: 'success', last_tested_at: at(0, 8), last_test_message: 'ok',
    models: ['a-1'], options: { usage_tags: ['super_assistant_palace'] }, created_by: 'admin',
    created_at: at(0, 8), updated_at: at(0, 8),
  }, {
    id: 'model-p2', name: 'VLM-B', config_type: 'llm', provider: 'b',
    api_base: '', has_api_key: true, enabled: true, is_default: false,
    last_test_status: 'success', last_tested_at: at(0, 8), last_test_message: 'ok',
    models: ['b-1'], options: { usage_tags: ['VLM提取'] }, created_by: 'admin',
    created_at: at(0, 8), updated_at: at(0, 8),
  }]))
  await page.goto('/#/super-assistant?conversation=c-today')

  // 会话保存的 model-1 不在（回退后的）模型集内：触发器按未选择处理、回落占位符，不渲染空白
  const trigger = page.getByRole('combobox', { name: '会话模型' })
  await expect(trigger).toHaveText(/选择模型/)

  // 回退全集：不能因为 tagging 不全而变成「无可用模型」
  await trigger.click()
  await expect(page.getByRole('option', { name: /Palace-A/ })).toBeVisible()
  await expect(page.getByRole('option', { name: /VLM-B/ })).toBeVisible()
})

test('过程卡连续同名工具折叠为 ×N，展开可见逐条子步骤', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await page.route('**/api/v2/super-assistant/conversations/c-earlier/messages', route => json(route, [{
    id: 'm-s1', conversation_id: 'c-earlier', role: 'user', content: '查接口来源', status: 'complete', steps: [], token_usage: {}, created_at: at(5, 10),
  }, {
    id: 'm-s2', conversation_id: 'c-earlier', role: 'assistant', status: 'complete',
    content: '# 结论\n\n正文答复',
    steps: [
      { toolName: 'todo_write', status: 'success', preview: '计划' },
      { toolName: 'browser_network_requests', status: 'success', preview: '请求 1' },
      { toolName: 'browser_network_requests', status: 'success', preview: '请求 2' },
      { toolName: 'browser_network_requests', status: 'error', preview: '请求 3' },
    ],
    token_usage: {}, created_at: at(5, 11),
  }]))
  await page.goto('/#/super-assistant?conversation=c-earlier')

  const process = page.getByTestId('super-assistant-turn-process')
  await process.getByRole('button', { name: '4 个工具' }).click()
  // 连续三条同名折叠为一行 ×3，聚合状态取失败（断言收在 summary 行，
  // 折叠态下子步骤的「失败」也命中 getByText，严格模式会判多元素）
  const group = process.locator('details', { hasText: 'browser_network_requests' })
  await expect(group.locator('summary')).toContainText('browser_network_requests')
  await expect(group.locator('summary')).toContainText('×3')
  // 内置工具在等宽原名旁渲染中文友好名（MCP/未知名不翻译）
  await expect(group.locator('summary')).toContainText('查看网络请求')
  await expect(group.locator('summary')).toContainText('失败')
  // 展开组：三条子步骤逐条可见
  await group.locator('summary').click()
  await expect(process.getByText('#1')).toBeVisible()
  await expect(process.getByText('#2')).toBeVisible()
  await expect(process.getByText('#3')).toBeVisible()
  await expect(process.getByText('请求 3')).toBeVisible()
})

test('助手消息元信息行：显示时间与输出 tokens，缺 outputTokens 时仍显示时间', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await page.route('**/api/v2/super-assistant/conversations/c-earlier/messages', route => json(route, [{
    id: 'm-t1', conversation_id: 'c-earlier', role: 'user', content: '问', status: 'complete', steps: [], token_usage: {}, created_at: at(5, 10),
  }, {
    id: 'm-t2', conversation_id: 'c-earlier', role: 'assistant', content: '带用量的答复', status: 'complete',
    steps: [], token_usage: { inputTokens: 1000, outputTokens: 1234 }, created_at: at(5, 11),
  }, {
    id: 'm-t3', conversation_id: 'c-earlier', role: 'user', content: '再问', status: 'complete', steps: [], token_usage: {}, created_at: at(5, 12),
  }, {
    id: 'm-t4', conversation_id: 'c-earlier', role: 'assistant', content: '无输出用量的旧答复', status: 'complete',
    steps: [], token_usage: {}, created_at: at(5, 13),
  }]))
  await page.goto('/#/super-assistant?conversation=c-earlier')

  const metas = page.getByTestId('super-assistant-message-meta')
  await expect(metas).toHaveCount(2)
  await expect(metas.first()).toContainText('输出 1.2k tokens')
  await expect(metas.first()).toContainText(/-/) // 时间存在（MM-DD HH:mm）
  // 旧记录缺 outputTokens：时间仍在，token 段不显示
  await expect(metas.nth(1)).not.toContainText('tokens')
})

test('失败消息提供重试：无已成功工具的轮次以同一提示词重发', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await page.route('**/api/v2/super-assistant/conversations', route => {
    if (route.request().method() === 'GET') {
      return json(route, [{
        id: 'c-err', title: '失败会话', model_config_id: 'model-1',
        status: 'active', created_at: at(0, 9), updated_at: at(0, 9),
      }])
    }
    return route.fallback()
  })
  // 重试完成后消息列表对齐为成功形态（send 的 finally 会重新拉取消息）
  let retried = false
  await page.route('**/api/v2/super-assistant/conversations/c-err/messages', route => json(route, retried
    ? [
      { id: 'm-err-1', conversation_id: 'c-err', role: 'user', content: '帮我算 1+1', status: 'complete', steps: [], token_usage: {}, created_at: at(0, 9) },
      { id: 'm-err-3', conversation_id: 'c-err', role: 'assistant', content: '重试后的答复', status: 'complete', steps: [], token_usage: {}, created_at: at(0, 9) },
    ]
    : [
      { id: 'm-err-1', conversation_id: 'c-err', role: 'user', content: '帮我算 1+1', status: 'complete', steps: [], token_usage: {}, created_at: at(0, 9) },
      { id: 'm-err-2', conversation_id: 'c-err', role: 'assistant', content: '上游模型超时', status: 'error', steps: [], token_usage: {}, created_at: at(0, 9) },
    ]))
  const chatBodies: string[] = []
  await page.route('**/api/v2/super-assistant/conversations/c-err/chat', route => {
    retried = true
    chatBodies.push(route.request().postData() || '')
    return route.fulfill({ status: 200, contentType: 'text/event-stream', body: sseBody('重试后的答复') })
  })

  await page.goto('/#/super-assistant?conversation=c-err')
  await expect(page.getByText('生成失败')).toBeVisible()
  await page.getByRole('button', { name: '重试' }).click()

  await expect.poll(() => chatBodies.length).toBe(1)
  expect(JSON.parse(chatBodies[0])).toMatchObject({ message: '帮我算 1+1' })
  await expect(page.getByText('重试后的答复')).toBeVisible()
})

test('失败且已有成功工具步骤的轮次不提供重试入口', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await page.route('**/api/v2/super-assistant/conversations', route => {
    if (route.request().method() === 'GET') {
      return json(route, [{
        id: 'c-err-2', title: '带工具失败会话', model_config_id: 'model-1',
        status: 'active', created_at: at(0, 9), updated_at: at(0, 9),
      }])
    }
    return route.fallback()
  })
  await page.route('**/api/v2/super-assistant/conversations/c-err-2/messages', route => json(route, [
    { id: 'm-et-1', conversation_id: 'c-err-2', role: 'user', content: '改一下标题', status: 'complete', steps: [], token_usage: {}, created_at: at(0, 9) },
    { id: 'm-et-2', conversation_id: 'c-err-2', role: 'assistant', content: '执行工具后中断', status: 'error',
      steps: [{ toolName: 'update_interface', status: 'success', arguments: {} }], token_usage: {}, created_at: at(0, 9) },
  ]))

  await page.goto('/#/super-assistant?conversation=c-err-2')
  await expect(page.getByText('生成失败')).toBeVisible()
  // 已成功执行过（可能含写操作的）工具：不提供一键重试，由用户自行决定是否重发
  await expect(page.getByRole('button', { name: '重试' })).toHaveCount(0)
})

test.describe('窄屏顶栏与会话操作（390px）', () => {
  test.use({ viewport: { width: 390, height: 844 } })

  test('顶栏保留标题与模型选择器，实时浏览器/助手配置收进溢出菜单', async ({ page }) => {
    await seedAuth(page)
    await mockApis(page)
    await page.goto('/#/super-assistant?conversation=c-earlier')

    // 标题优先占位：可见且未被压成 0 宽（按编辑按钮的 title 精确定位，避开侧栏同名会话行）
    const title = page.getByTitle('点击编辑会话名称')
    await expect(title).toBeVisible()
    await expect(title).toHaveText(/上周数据摸底/)
    expect((await title.boundingBox())?.width ?? 0).toBeGreaterThan(24)

    // 模型选择器仍在顶栏，两个图标按钮让位给「⋯」溢出菜单
    await expect(page.getByRole('combobox', { name: '会话模型' })).toBeVisible()
    await expect(page.getByRole('button', { name: '打开实时浏览器' })).toBeHidden()
    await expect(page.getByRole('button', { name: '打开助手配置' })).toBeHidden()
    const overflow = page.getByRole('button', { name: '更多操作' })
    await expect(overflow).toBeVisible()

    // 触屏无悬停：助手消息的复制按钮不依赖 hover，常显可用
    const copyButton = page.getByRole('button', { name: '复制', exact: true }).first()
    await expect(copyButton).toBeVisible()
    await expect(copyButton).toHaveCSS('opacity', '1')

    await overflow.click()
    const menu = page.getByRole('dialog')
    await expect(menu.getByRole('button', { name: '打开助手配置' })).toBeVisible()
    await menu.getByRole('button', { name: '打开助手配置' }).click()
    // 移动端配置面板为卡内覆盖抽屉：出现关闭入口即视为打开
    await expect(page.getByRole('button', { name: '关闭助手配置' })).toBeVisible()
  })

  test('触屏无悬停：会话行归档/删除常显可达', async ({ page }) => {
    await seedAuth(page)
    await mockApis(page)
    await page.goto('/#/super-assistant')

    await page.getByRole('button', { name: '打开工作台导航' }).click()
    const row = page.locator('[data-workbench-conversation="c-today"]')
    await expect(row).toBeVisible()
    // 不 hover，直接断言操作按钮可见且可点（时间戳窄屏让位）
    const remove = row.getByRole('button', { name: '删除会话 今日需求梳理' })
    await expect(remove).toBeVisible()
    await remove.click()
    await expect(page.getByRole('dialog', { name: '删除会话' })).toBeVisible()
  })
})

test('代码块复制失败如实反馈：显示失败提示并选中代码留出手动复制路径', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await page.route('**/api/v2/super-assistant/conversations/c-earlier/messages', route => json(route, [
    { id: 'm-c1', conversation_id: 'c-earlier', role: 'user', content: '给个示例', status: 'complete', steps: [], token_usage: {}, created_at: at(5, 10) },
    { id: 'm-c2', conversation_id: 'c-earlier', role: 'assistant', status: 'complete',
      content: '示例：\n\n```json\n{"k": "v"}\n```\n', steps: [], token_usage: { outputTokens: 42 }, created_at: at(5, 11) },
  ]))
  // 双路径剪贴板全部失败：Clipboard API 不可用 + execCommand 返回 false
  await page.addInitScript(() => {
    Object.defineProperty(Navigator.prototype, 'clipboard', { get: () => undefined })
    Document.prototype.execCommand = () => false
  })

  await page.goto('/#/super-assistant?conversation=c-earlier')
  await page.getByRole('button', { name: '复制', exact: true }).first().click()
  await expect(page.getByText('复制失败，可手动复制')).toBeVisible()
  // 失败兜底：代码块被选中，用户可直接 Cmd+C / Ctrl+C
  const selected = await page.evaluate(() => String(window.getSelection()))
  expect(selected).toContain('"k": "v"')
})

test('助手完成但无正文时如实展示「助手未返回文本」', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await page.route('**/api/v2/super-assistant/conversations/c-earlier/messages', route => json(route, [
    { id: 'm-n1', conversation_id: 'c-earlier', role: 'user', content: '在吗', status: 'complete', steps: [], token_usage: {}, created_at: at(5, 10) },
    { id: 'm-n2', conversation_id: 'c-earlier', role: 'assistant', content: '', status: 'complete', steps: [], token_usage: {}, created_at: at(5, 11) },
  ]))

  await page.goto('/#/super-assistant?conversation=c-earlier')
  await expect(page.getByText('助手未返回文本')).toBeVisible()
  // 旧文案不应残留
  await expect(page.getByText('（无文本）')).toHaveCount(0)
})

test('会话右键菜单：快捷操作齐备且无分组标题', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page)
  await page.goto('/#/super-assistant')

  // —— 菜单结构：右键近期会话行弹出，分组标题已移除、十一项齐备 ——
  await page.locator('[data-workbench-conversation="c-today"]').click({ button: 'right' })
  const menu = page.locator('[data-ctx-menu="c-today"]')
  await expect(menu).toBeVisible()
  // 分组标签（会话操作/助手配置/会话审计）已按需求移除，不应再渲染
  for (const label of ['会话操作', '助手配置', '会话审计']) {
    await expect(menu.getByText(label, { exact: true })).toHaveCount(0)
  }
  for (const key of [
    'rename', 'archive', 'delete',
    'config-skills', 'config-mcp', 'config-tools', 'config-approval', 'config-memory', 'config-voice',
    'export', 'feedback',
  ]) {
    await expect(menu.locator(`[data-ctx-item="${key}"]`)).toBeVisible()
  }

  // —— 助手配置快捷：配置 MCP → 面板打开并一次性落位到 MCP tab ——
  await menu.locator('[data-ctx-item="config-mcp"]').click()
  const configPanel = page.getByRole('region', { name: '助手配置' })
  await expect(configPanel).toBeVisible()
  // MCP tab 特征：面板 footer 出现「添加 MCP」入口（skills tab 为「新建 Skill」，approval/memory 无 footer）
  await expect(page.getByRole('button', { name: '添加 MCP' })).toBeVisible()

  // P1 回归：面板已打开时手动切走 tab，再次右键同 tab 快捷方式仍应重新落位
  // （一次性 initialTab 消费即清空；若不清空则同值右键静默失效）
  await configPanel.getByRole('button', { name: '记忆', exact: true }).click()
  await expect(page.getByRole('button', { name: '添加 MCP' })).toBeHidden()
  await page.locator('[data-workbench-conversation="c-today"]').click({ button: 'right' })
  await page.locator('[data-ctx-menu="c-today"] [data-ctx-item="config-mcp"]').click()
  await expect(page.getByRole('button', { name: '添加 MCP' })).toBeVisible()
  await configPanel.getByLabel('关闭助手配置').click()
  await expect(configPanel).toBeHidden()

  // —— 归档：右键归档 → PATCH status，行真实移出近期组（写回持久，轮询不洗回）——
  await page.locator('[data-workbench-conversation="c-today"]').click({ button: 'right' })
  await page.locator('[data-ctx-menu="c-today"] [data-ctx-item="archive"]').click()
  await expect.poll(() => mocks.patchBodies.at(-1) ?? '').toContain('"status":"archived"')
  await expect(page.locator('[data-workbench-group="recent"] [data-workbench-conversation="c-today"]')).toHaveCount(0)

  // —— 重命名：右键行 → 弹窗改名 → PATCH title，侧栏行即时回显 ——
  await page.locator('[data-workbench-conversation="c-yesterday"]').click({ button: 'right' })
  await page.locator('[data-ctx-menu="c-yesterday"] [data-ctx-item="rename"]').click()
  const renameDialog = page.getByRole('dialog').filter({ hasText: '重命名会话' })
  await expect(renameDialog).toBeVisible()
  await renameDialog.locator('[data-rename-input]').fill('改名后的会话')
  await renameDialog.getByRole('button', { name: '保存' }).click()
  await expect.poll(() => mocks.patchBodies.at(-1) ?? '').toContain('"title":"改名后的会话"')
  await expect(page.locator('[data-workbench-conversation="c-yesterday"]')).toContainText('改名后的会话')

  // —— 导出：右键 → 下载完整审计 JSON，文件名带标题与会话 id 前缀 ——
  const downloadPromise = page.waitForEvent('download')
  await page.locator('[data-workbench-conversation="c-earlier"]').click({ button: 'right' })
  await page.locator('[data-ctx-menu="c-earlier"] [data-ctx-item="export"]').click()
  await expect.poll(() => mocks.exportCalls.length).toBe(1)
  const download = await downloadPromise
  expect(mocks.exportCalls).toContain('c-earlier')
  expect(await download.suggestedFilename()).toBe('上周数据摸底-c-earlie.json')
  // 下载内容断言（steward 导出同范式）：前端透传完整审计载荷，而非会话列表对象
  const exportPath = test.info().outputPath('conversation-export.json')
  await download.saveAs(exportPath)
  const exported = JSON.parse(await import('node:fs/promises').then(fs => fs.readFile(exportPath, 'utf8')))
  expect(exported.format).toBe('openontology.super-assistant.conversation')
  expect(exported.conversation.messageCount).toBe(2)
  expect(exported.conversation.messages[1].steps[0].tool).toBe('query')
  expect(exported.summary).toEqual({ messageCount: 2, toolStepCount: 1, totalTokens: 42 })

  // —— 反馈问题：右键 → 打开全站工单提交弹窗（提交链路由 tickets.spec 覆盖）——
  // 用 c-yesterday：归档段把 c-today 持久移入归档组（mock 写回后不再洗回）
  await page.locator('[data-workbench-conversation="c-yesterday"]').click({ button: 'right' })
  await page.locator('[data-ctx-menu="c-yesterday"] [data-ctx-item="feedback"]').click()
  const ticketDialog = page.getByRole('dialog').filter({ hasText: '反馈内容' })
  await expect(ticketDialog).toBeVisible()
  await ticketDialog.getByRole('button', { name: /关闭|取消/ }).first().click()

  // —— 归档区行右键对称：第一组切换为「恢复会话」，删除仍走确认弹窗 ——
  await page.getByRole('button', { name: '归档会话', exact: true }).click()
  await page.locator('[data-workbench-conversation="c-archived"]').click({ button: 'right' })
  const archivedMenu = page.locator('[data-ctx-menu="c-archived"]')
  await expect(archivedMenu.locator('[data-ctx-item="archive"]')).toContainText('恢复会话')
  await archivedMenu.locator('[data-ctx-item="delete"]').click()
  const confirmDelete = page.getByRole('dialog').filter({ hasText: '确定删除会话' })
  await expect(confirmDelete).toBeVisible()
  await confirmDelete.getByRole('button', { name: '取消' }).click()
})

// ═══ 任务实例弹窗改版：看板卡片→轻量详情（状态图+审批直达）、激活表单内嵌
// 只读流程结构预览、新建流程归位模板区、导航高亮随视图域同步 ═══

const tiSpecCompiled = {
  api_version: 'openontology.task/v1',
  kind: 'Workflow',
  metadata: { name: '代码变更评审', description: null },
  contracts: {},
  nodes: {
    analyze: { kind: 'agent' },
    gate: { kind: 'approval' },
    end_ok: { kind: 'terminal' },
  },
  edges: [
    { from: 'analyze.done', to: 'gate', rework: false },
    { from: 'gate.approved', to: 'end_ok', rework: false },
  ],
}

const tiSpecYaml = `api_version: openontology.task/v1
kind: Workflow
metadata:
  name: 代码变更评审
nodes:
  analyze: { kind: agent, system: 分析变更 }
  gate: { kind: approval, approvers: [admin] }
  end_ok: { kind: terminal, outcome: success }
edges:
  - { from: analyze.done, to: gate }
  - { from: gate.approved, to: end_ok }
`

const tiTemplatesFixture = [
  { id: 'tpl-1', name: '代码变更评审', description: '条件分流 + 关口审批',
    is_preset: true,
    latest_revision_no: 3, latest_revision_id: 'rev-3',
    canonical_hash: 'a'.repeat(64), instance_count: 5,
    created_at: '2026-10-11T02:00:00Z', updated_at: '2026-10-11T03:00:00Z' },
  { id: 'tpl-2', name: '我的冒烟流程', description: '条件分流 + 关口审批（个人副本）',
    is_preset: false,
    latest_revision_no: 1, latest_revision_id: 'rev-9',
    canonical_hash: 'b'.repeat(64), instance_count: 0,
    created_at: '2026-10-12T02:00:00Z', updated_at: '2026-10-12T03:00:00Z' },
]

const tiInstancesFixture = {
  total: 2, page: 1, size: 50,
  items: [
    { id: 'ins-1', name: '登录页改版评审', goal: '评审登录页改版',
      status: 'active', needs_attention: true, template_revision_id: 'rev-3',
      created_by: 'u-1', created_at: '2026-10-11T03:10:00Z', finished_at: null,
      fail_reason: null, cancel_reason: null,
      nodes: [{ node_run_id: 'nr-list-1', node_id: 'gate', attempt_no: 1,
        status: 'waiting_approval', waiting: true, error: null,
        correction_count: 0, rework_count: 0, output: null,
        dispatched_at: null, started_at: null, finished_at: null,
        created_at: '2026-10-11T03:12:00Z' }] },
    { id: 'ins-2', name: '缺陷修复流水', goal: '修复登录崩溃',
      status: 'completed', needs_attention: false, template_revision_id: 'rev-3',
      created_by: 'u-1', created_at: '2026-10-10T08:00:00Z',
      finished_at: '2026-10-10T09:00:00Z', fail_reason: null,
      cancel_reason: null,
      nodes: [{ node_run_id: 'nr-list-2', node_id: 'review', attempt_no: 1,
        status: 'waiting_human', waiting: true, error: null,
        correction_count: 0, rework_count: 0, output: null,
        dispatched_at: null, started_at: null, finished_at: null,
        created_at: '2026-10-10T08:30:00Z' }] },
  ],
}

const tiInstanceDetailFixture = {
  ...tiInstancesFixture.items[0],
  nodes: [
    { node_run_id: 'nr-1', node_id: 'analyze', attempt_no: 1,
      status: 'completed', waiting: false, error: null, correction_count: 0,
      rework_count: 0, output: { summary: '涉及登录页' },
      dispatched_at: '2026-10-11T03:11:00Z', started_at: null,
      finished_at: '2026-10-11T03:12:00Z', created_at: '2026-10-11T03:11:00Z' },
    { node_run_id: 'nr-2', node_id: 'gate', attempt_no: 1,
      status: 'waiting_approval', waiting: true, error: null,
      correction_count: 0, rework_count: 0, output: null,
      dispatched_at: null, started_at: null, finished_at: null,
      created_at: '2026-10-11T03:12:00Z' },
  ],
  approvals: [
    { id: 'apr-1', node_run_id: 'nr-2', status: 'pending', reason: null,
      proposal: { inputs: { analyze: { summary: '涉及登录页' } } },
      expires_at: '2026-10-14T03:12:00Z', decided_by: null, decided_at: null },
  ],
  artifacts: [],
  spec_snapshot: tiSpecCompiled,
}

/** 任务实例域路由 mock（在 mockApis 之后注册，优先于其 catch-all） */
async function mockTaskInstancesApis(page: Page) {
  await page.route('**/api/v2/task-instances/templates**', route => {
    if (route.request().method() === 'GET') return json(route, tiTemplatesFixture)
    return json(route, {})
  })
  await page.route('**/api/v2/task-instances/templates/tpl-1', route =>
    json(route, { ...tiTemplatesFixture[0],
      spec_yaml: tiSpecYaml, spec_compiled: tiSpecCompiled }))
  await page.route('**/api/v2/task-instances/templates/tpl-2', route =>
    json(route, { ...tiTemplatesFixture[1],
      spec_yaml: tiSpecYaml, spec_compiled: tiSpecCompiled }))
  await page.route('**/api/v2/task-instances/templates/tpl-2/revisions', route =>
    json(route, [
      { id: 'rev-9', revision_no: 1, canonical_hash: 'b'.repeat(64),
        note: null, created_by: 'u-1', created_at: '2026-10-12T03:00:00Z',
        spec_yaml: tiSpecYaml },
    ]))
  await page.route('**/api/v2/task-instances/instances**', route => {
    const url = route.request().url()
    if (url.includes('/events')) {
      return route.fulfill({
        status: 200, contentType: 'text/event-stream',
        body: [
          ['event: instance.snapshot',
            'data: ' + JSON.stringify({ instance: { id: 'ins-1' } })].join('\n'),
          '',
        ].join('\n\n'),
      })
    }
    if (url.match(/instances\/ins-1$/)) return json(route, tiInstanceDetailFixture)
    if (route.request().method() === 'POST') return json(route, tiInstanceDetailFixture, 202)
    return json(route, tiInstancesFixture)
  })
}

test('任务实例弹窗：看板卡片可点进轻量详情（状态图+审批直达），完整详情跳独立页', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await mockTaskInstancesApis(page)
  await page.goto('/#/super-assistant')

  await page.getByRole('button', { name: /任务实例/ }).click()
  const dialog = page.getByTestId('task-instances-dialog')
  await expect(dialog).toBeVisible()
  await expect(page.getByText('需介入').first()).toBeVisible()
  // 需介入卡片的原因徽章：等待审批（来自列表项 nodes 状态投影）
  await expect(page.getByTestId('task-instance-card')
    .filter({ hasText: '登录页改版评审' }).getByText('待审批')).toBeVisible()
  await expect(page.getByTestId('task-instance-card')
    .filter({ hasText: '缺陷修复流水' }).getByText('待人工')).toBeVisible()

  // 卡片点击 → 弹窗内轻量详情（修复前卡片无 onClick，点击零反应）
  const attentionCard = page.getByTestId('task-instance-card').filter({ hasText: '登录页改版评审' })
  await expect(attentionCard).toBeVisible()
  await attentionCard.click()
  const lite = page.getByTestId('task-instance-lite')
  await expect(lite).toBeVisible()
  await expect(lite.getByRole('button', { name: '返回看板' })).toBeVisible()
  await expect(lite.getByText('流程快照')).toBeVisible()
  // 状态 DAG 按 spec_snapshot 投影渲染
  await expect(lite.locator('.react-flow')).toBeVisible()
  await expect(lite.getByText('analyze').first()).toBeVisible()
  // 需介入实例的审批关口直达操作
  await expect(lite.getByText('审批关口').first()).toBeVisible()
  await expect(lite.getByRole('button', { name: '批准' })).toBeVisible()
  await expect(lite.getByRole('button', { name: '退回重做' })).toBeVisible()
  // 审批卡片：结构化摘要可见，原始 JSON 默认折叠、可展开
  await expect(lite.getByText('涉及登录页').first()).toBeVisible()
  await lite.getByRole('button', { name: '查看原始数据' }).click()
  await expect(lite.locator('pre').filter({ hasText: 'summary' })).toBeVisible()

  // 批准动作走通完整链路（decision POST 命中正确端点与载荷；
  // handler 内只记录不抛错，避免断言失败把 route 挂起成假绿）
  let decided: { method: string; decision: string } | null = null
  await page.route('**/task-instances/instances/ins-1/nodes/gate/approvals/apr-1/decision', route => {
    const body = route.request().postDataJSON() as { decision: string }
    decided = { method: route.request().method(), decision: body.decision }
    return json(route, tiInstanceDetailFixture)
  })
  await lite.getByRole('button', { name: '批准' }).click()
  await expect.poll(() => decided).toEqual({ method: 'POST', decision: 'approved' })

  // 返回看板可回退，再次进入轻量详情
  await lite.getByRole('button', { name: '返回看板' }).click()
  await expect(page.getByTestId('task-instances-board')).toBeVisible()
  await attentionCard.click()
  await expect(page.getByTestId('task-instance-lite')).toBeVisible()

  // 在完整页面打开 → 关闭弹窗并跳转独立实例详情页
  await page.getByTestId('task-instance-lite').getByRole('button', { name: /在完整页面打开/ }).click()
  await expect(page).toHaveURL(/#\/task-instances\/instances\/ins-1/)
  await expect(page.getByText('流程快照')).toBeVisible()
  await expect(page.getByTestId('task-instances-dialog')).toHaveCount(0)
})

test('任务实例弹窗：激活表单内嵌只读流程结构，新建流程归位模板区，导航高亮随视图域同步', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await mockTaskInstancesApis(page)
  await page.goto('/#/super-assistant')

  await page.getByRole('button', { name: /任务实例/ }).click()
  const nav = page.locator('nav[aria-label="任务实例视图"]')

  // 初始看板视图：实例看板高亮
  await expect(nav.getByRole('button', { name: '实例看板' })).toHaveAttribute('aria-pressed', 'true')

  // 模板区：头部有「新建流程」且全弹窗唯一（左下角入口已移除）
  await nav.getByRole('button', { name: '流程模板' }).click()
  await expect(page.getByTestId('new-flow-button')).toBeVisible()
  await expect(page.getByRole('button', { name: '新建流程' })).toHaveCount(1)
  await expect(page.getByText('代码变更评审').first()).toBeVisible()

  // 卡片主体点击 → 激活表单携带只读流程结构预览（预览数据来自模板详情接口）
  await page.getByTestId('template-card').filter({ hasText: '代码变更评审' }).click()
  await expect(page.getByText('激活「代码变更评审」')).toBeVisible()
  const preview = page.getByTestId('template-graph-preview')
  await expect(preview).toBeVisible()
  await expect(preview.locator('.react-flow')).toBeVisible()
  await expect(preview.getByText('analyze').first()).toBeVisible()
  await expect(preview.getByText('gate').first()).toBeVisible()

  // 导航高亮修复：激活表单属模板域（修复前高亮跳回实例看板）
  await expect(nav.getByRole('button', { name: '流程模板' })).toHaveAttribute('aria-pressed', 'true')
  await expect(nav.getByRole('button', { name: '实例看板' })).toHaveAttribute('aria-pressed', 'false')

  // 返回模板列表 → 新建流程进入画布编辑器（高亮仍在模板域）
  await page.getByRole('button', { name: '返回模板', exact: true }).click()
  await page.getByTestId('new-flow-button').click()
  await expect(page.getByRole('button', { name: '校验' })).toBeVisible()
  await expect(nav.getByRole('button', { name: '流程模板' })).toHaveAttribute('aria-pressed', 'true')
})

test('任务实例弹窗：零实例看板渲染引导空态并可直达流程模板', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await mockTaskInstancesApis(page)
  // 覆盖实例列表为空（后注册的 route 优先）
  await page.route('**/api/v2/task-instances/instances**', route =>
    json(route, { total: 0, page: 1, size: 50, items: [] }))

  await page.goto('/#/super-assistant')
  await page.getByRole('button', { name: /任务实例/ }).click()
  const board = page.getByTestId('task-instances-board')
  await expect(board).toBeVisible()
  await expect(board.getByText('还没有任务实例')).toBeVisible()
  await expect(board.getByText(/从流程模板激活第一个任务/)).toBeVisible()

  await board.getByRole('button', { name: '去流程模板' }).click()
  await expect(page.getByTestId('task-instances-templates')).toBeVisible()
})

test('任务实例弹窗：失败实例展示人话失败原因，可展开技术详情并用相同目标重新激活', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await mockTaskInstancesApis(page)
  const failedSummary = {
    id: 'ins-fail', name: 'E2E失败实例', goal: '重跑冒烟目标',
    status: 'failed', needs_attention: false, template_revision_id: 'rev-3',
    created_by: 'u-1', created_at: '2026-10-11T03:10:00Z',
    finished_at: '2026-10-11T03:20:00Z',
    fail_reason: "node:outline:容器执行失败: runner 异常退出 code=None output=''",
    cancel_reason: null, nodes: [],
  }
  const failedDetail = {
    ...failedSummary,
    approvals: [], artifacts: [], spec_snapshot: tiSpecCompiled,
  }
  await page.route('**/api/v2/task-instances/instances**', route => {
    const url = route.request().url()
    if (url.includes('/events')) {
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' })
    }
    if (url.match(/instances\/ins-fail$/)) return json(route, failedDetail)
    return json(route, { total: 1, page: 1, size: 50, items: [failedSummary] })
  })

  await page.goto('/#/super-assistant')
  await page.getByRole('button', { name: /任务实例/ }).click()
  await page.getByTestId('task-instance-card').filter({ hasText: 'E2E失败实例' }).click()
  const lite = page.getByTestId('task-instance-lite')
  await expect(lite).toBeVisible()

  // FailureNotice：人话首行 + 技术详情折叠展开
  await expect(lite.getByText('执行环境异常退出，任务未能完成')).toBeVisible()
  await lite.getByRole('button', { name: '技术详情' }).click()
  await expect(lite.getByText(/runner 异常退出/)).toBeVisible()
  await expect(lite.getByRole('button', { name: '复制' })).toBeVisible()

  // 用相同目标重新激活 → 进入激活表单，名称/目标预填
  await lite.getByRole('button', { name: '用相同目标重新激活' }).click()
  await expect(page.getByText('激活「代码变更评审」')).toBeVisible()
  await expect(page.getByLabel(/实例名称/)).toHaveValue(/E2E失败实例·重试/)
  await expect(page.getByLabel(/任务目标/)).toHaveValue('重跑冒烟目标')
})

test('任务实例弹窗：画布保存门禁——名称必填且写入 metadata（画布与 YAML 双模式）', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await mockTaskInstancesApis(page)
  let createdBody: { spec_yaml?: string } | null = null
  await page.route('**/api/v2/task-instances/templates', route => {
    if (route.request().method() === 'POST') {
      createdBody = route.request().postDataJSON() as { spec_yaml?: string }
      return json(route, { id: 'tpl-new', name: 'x' }, 201)
    }
    return json(route, tiTemplatesFixture)
  })

  await page.goto('/#/super-assistant')
  await page.getByRole('button', { name: /任务实例/ }).click()
  await page.getByRole('navigation', { name: '任务实例视图' })
    .getByRole('button', { name: '流程模板' }).click()
  await page.getByTestId('new-flow-button').click()
  await expect(page.getByRole('button', { name: '校验' })).toBeVisible()

  // 画布模式：保存必经名称表单；空名禁用
  await page.getByRole('button', { name: '保存模板' }).click()
  const nameInput = page.getByLabel(/模板名称/)
  await expect(nameInput).toBeVisible()
  await expect(page.getByRole('button', { name: '保存', exact: true })).toBeDisabled()
  await nameInput.fill('冒烟流程')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect.poll(() => createdBody?.spec_yaml ?? '').toContain('name: 冒烟流程')
  // 保存成功后回到模板列表
  await expect(page.getByTestId('task-instances-templates')).toBeVisible()

  // YAML 模式：直接点保存同样被表单拦截，名称从 metadata 预填并回写
  await page.getByTestId('new-flow-button').click()
  await page.getByRole('button', { name: 'YAML 模式' }).click()
  await page.getByRole('button', { name: '保存模板' }).click()
  await expect(nameInput).toBeVisible()
  await expect(nameInput).toHaveValue('我的流程') // 默认画布序列化的占位名被解析预填
  await nameInput.fill('YAML命名流程')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect.poll(() => createdBody?.spec_yaml ?? '').toContain('name: YAML命名流程')
})

test('任务实例弹窗：激活表单必填校验与提交链路（POST 载荷 + 成功回看板 + 流程结构折叠）', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await mockTaskInstancesApis(page)
  let activated: { name?: string; goal?: string } | null = null
  await page.route('**/task-instances/templates/tpl-1/instances', route => {
    activated = route.request().postDataJSON() as { name?: string; goal?: string }
    return json(route, tiInstanceDetailFixture, 202)
  })

  await page.goto('/#/super-assistant')
  await page.getByRole('button', { name: /任务实例/ }).click()
  await page.getByRole('navigation', { name: '任务实例视图' })
    .getByRole('button', { name: '流程模板' }).click()
  await page.getByTestId('template-card').filter({ hasText: '代码变更评审' }).click()
  await expect(page.getByText('激活「代码变更评审」')).toBeVisible()

  // 未填名称/目标时提交禁用；流程结构默认展开可折叠
  const submit = page.getByRole('button', { name: '激活实例', exact: true })
  await expect(submit).toBeDisabled()
  const graphToggle = page.getByRole('button', { name: /流程结构/ })
  await expect(page.getByTestId('template-graph-preview')).toBeVisible()
  await graphToggle.click()
  await expect(page.getByTestId('template-graph-preview')).toBeHidden()

  await page.getByLabel(/实例名称/).fill('冒烟实例')
  await page.getByLabel(/任务目标/).fill('跑通激活链路')
  await submit.click()
  await expect.poll(() => activated).toEqual({ name: '冒烟实例', goal: '跑通激活链路' })
  // 激活成功回看板
  await expect(page.getByTestId('task-instances-board')).toBeVisible()
})

test('任务实例弹窗：模板搜索仅匹配名称，来源单选与关键词取交集，空态三态可区分', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await mockTaskInstancesApis(page)

  await page.goto('/#/super-assistant')
  await page.getByRole('button', { name: /任务实例/ }).click()
  await page.getByRole('navigation', { name: '任务实例视图' })
    .getByRole('button', { name: '流程模板' }).click()
  const panel = page.getByTestId('task-instances-templates')
  await expect(panel).toBeVisible()

  // 默认「预置」：预置模板可见、个人模板不可见
  const sourceGroup = page.getByTestId('template-source-group')
  await expect(sourceGroup.getByRole('radio', { name: '预置' })).toHaveAttribute('aria-checked', 'true')
  await expect(page.getByTestId('template-card').filter({ hasText: '代码变更评审' })).toBeVisible()
  await expect(page.getByTestId('template-card').filter({ hasText: '我的冒烟流程' })).toHaveCount(0)

  // 搜索仅匹配名称：关键词命中 tpl-1 描述（条件分流）不命中任何预置名称 → 无匹配空态
  await page.getByTestId('template-search-input').fill('条件分流')
  await expect(page.getByTestId('template-empty-state')).toBeVisible()
  await expect(page.getByText('没有匹配的模板')).toBeVisible()
  await expect(page.getByText('未找到名称包含「条件分流」的预置模板')).toBeVisible()

  // 切个人：关键词仍在（条件分流只在描述中出现）→ 交集语义继续无匹配
  await sourceGroup.getByRole('radio', { name: '个人' }).click()
  await expect(sourceGroup.getByRole('radio', { name: '个人' })).toHaveAttribute('aria-checked', 'true')
  await expect(page.getByText('未找到名称包含「条件分流」的个人模板')).toBeVisible()
  // 个人来源下用预置名称搜索 → 无匹配（关键词与来源取交集，不是并集）
  await page.getByTestId('template-search-input').fill('代码变更评审')
  await expect(page.getByTestId('template-empty-state')).toBeVisible()
  await expect(page.getByText('未找到名称包含「代码变更评审」的个人模板')).toBeVisible()

  // 清空关键词保留「个人」来源
  await page.getByTestId('template-search-input').fill('')
  await expect(page.getByTestId('template-card').filter({ hasText: '我的冒烟流程' })).toBeVisible()
  await expect(page.getByTestId('template-card').filter({ hasText: '代码变更评审' })).toHaveCount(0)
})

test('任务实例弹窗：个人分类空态与全局空态区分', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await mockTaskInstancesApis(page)
  // 只保留预置模板（后注册 route 优先）
  await page.route('**/api/v2/task-instances/templates**', route => {
    if (route.request().method() === 'GET') return json(route, [tiTemplatesFixture[0]])
    return json(route, {})
  })

  await page.goto('/#/super-assistant')
  await page.getByRole('button', { name: /任务实例/ }).click()
  await page.getByRole('navigation', { name: '任务实例视图' })
    .getByRole('button', { name: '流程模板' }).click()

  // 个人分类下没有任何模板 → 分类空态（不是「从未创建过模板」的全局空态）
  await page.getByTestId('template-source-group').getByRole('radio', { name: '个人' }).click()
  await expect(page.getByTestId('template-empty-state')).toBeVisible()
  await expect(page.getByText('还没有个人模板')).toBeVisible()

  // 全局无模板 → 全局空态
  await page.route('**/api/v2/task-instances/templates**', route => {
    if (route.request().method() === 'GET') return json(route, [])
    return json(route, {})
  })
  await page.getByTestId('template-source-group').getByRole('radio', { name: '预置' }).click()
  await page.reload()
  await page.getByRole('button', { name: /任务实例/ }).click()
  await page.getByRole('navigation', { name: '任务实例视图' })
    .getByRole('button', { name: '流程模板' }).click()
  await expect(page.getByText('还没有流程模板')).toBeVisible()
})

test('任务实例弹窗：模板卡四操作——预置编辑/删除禁用不冒泡，查看/激活可用', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await mockTaskInstancesApis(page)

  await page.goto('/#/super-assistant')
  await page.getByRole('button', { name: /任务实例/ }).click()
  await page.getByRole('navigation', { name: '任务实例视图' })
    .getByRole('button', { name: '流程模板' }).click()

  const presetCard = page.getByTestId('template-card').filter({ hasText: '代码变更评审' })
  await expect(presetCard.getByRole('button', { name: '查看' })).toBeEnabled()
  await expect(presetCard.getByRole('button', { name: '激活模板 代码变更评审' })).toBeEnabled()
  // 预置编辑/删除始终可见但禁用，且说明原因
  await expect(presetCard.getByRole('button', { name: /编辑模板 代码变更评审（预置模板不允许编辑）/ })).toBeDisabled()
  await expect(presetCard.getByRole('button', { name: /删除模板 代码变更评审（预置模板不允许删除）/ })).toBeDisabled()

  // 禁用区域点击不得冒泡触发激活
  await presetCard.getByRole('button', { name: /编辑模板/ }).click({ force: true })
  await expect(page.getByText(/激活「代码变更评审」/)).toHaveCount(0)

  // 先落关键词，再进「查看」→ 弹窗内只读详情（结构图 + 可折叠 YAML 原文），无保存/提交入口
  await page.getByTestId('template-search-input').fill('评审')
  await presetCard.getByRole('button', { name: '查看' }).click()
  const detail = page.getByTestId('template-detail-view')
  await expect(detail).toBeVisible()
  await expect(detail.getByText('预置', { exact: true })).toBeVisible()
  await expect(detail.getByTestId('template-detail-graph').locator('.react-flow')).toBeVisible()
  await expect(detail.getByRole('button', { name: /保存|提交|激活/ })).toHaveCount(0)
  await detail.getByRole('button', { name: 'YAML 原文' }).click()
  await expect(detail.getByText('api_version: openontology.task/v1').first()).toBeVisible()
  // 返回列表保留筛选（预置 + 关键词）；返回按钮在详情头部（testid 容器之外）
  await page.getByTestId('task-instances-dialog')
    .getByRole('button', { name: '返回模板' }).click()
  await expect(page.getByTestId('task-instances-templates')).toBeVisible()
  await expect(page.getByTestId('template-search-input')).toHaveValue('评审')
  await expect(page.getByTestId('template-card').filter({ hasText: '代码变更评审' })).toBeVisible()
})

test('任务实例弹窗：个人模板查看/编辑/激活可用，编辑载入既有 ID 的 YAML', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await mockTaskInstancesApis(page)
  const updated: { value?: { method: string; url: string; body?: { spec_yaml?: string } } } = {}
  await page.route('**/api/v2/task-instances/templates/tpl-2', route => {
    if (route.request().method() === 'PUT') {
      updated.value = {
        method: route.request().method(),
        url: route.request().url(),
        body: route.request().postDataJSON() as { spec_yaml?: string },
      }
      return json(route, { ...tiTemplatesFixture[1], latest_revision_no: 2,
        spec_yaml: tiSpecYaml, spec_compiled: tiSpecCompiled })
    }
    return json(route, { ...tiTemplatesFixture[1],
      spec_yaml: tiSpecYaml, spec_compiled: tiSpecCompiled })
  })

  await page.goto('/#/super-assistant')
  await page.getByRole('button', { name: /任务实例/ }).click()
  await page.getByRole('navigation', { name: '任务实例视图' })
    .getByRole('button', { name: '流程模板' }).click()
  await page.getByTestId('template-source-group').getByRole('radio', { name: '个人' }).click()

  const personalCard = page.getByTestId('template-card').filter({ hasText: '我的冒烟流程' })
  await expect(personalCard.getByRole('button', { name: '查看' })).toBeEnabled()
  await expect(personalCard.getByRole('button', { name: '编辑模板 我的冒烟流程' })).toBeEnabled()
  await expect(personalCard.getByRole('button', { name: '激活模板 我的冒烟流程' })).toBeEnabled()
  await expect(personalCard.getByRole('button', { name: '删除模板 我的冒烟流程' })).toBeEnabled()

  // 弹窗内编辑：载入既有模板 YAML，真实修改草稿（含未知字段），保存 PUT
  // 同一 template ID 的完整修改后原文（不丢字段、不误建新模板）
  await personalCard.getByRole('button', { name: '编辑模板 我的冒烟流程' }).click()
  const editView = page.getByTestId('template-edit-view')
  await expect(editView).toBeVisible()
  await expect(editView.getByText('保存新版本')).toBeVisible()
  const editor = editView.locator('.cm-content')
  await expect(editor).toContainText('kind: Workflow')
  await editor.click()
  await page.keyboard.press('ControlOrMeta+End')
  await page.keyboard.type('\nx-custom-unknown-field: 保留未知字段\n')
  await editView.getByRole('button', { name: '保存新版本' }).click()
  await expect.poll(() => updated.value?.url ?? '').toContain('/task-instances/templates/tpl-2')
  expect(updated.value?.method).toBe('PUT')
  // 提交的是完整修改后草稿：原文每行原样保留 + 追加的未知字段行逐字可见，
  // 不是未修改原文的转发（Ctrl+End 落在原文末尾空行，Enter 再开一行）
  const submitted = updated.value?.body?.spec_yaml ?? ''
  const expected = tiSpecYaml + '\nx-custom-unknown-field: 保留未知字段\n'
  expect(submitted).toBe(expected)
  expect(submitted).not.toBe(tiSpecYaml)
  // 保存成功留在编辑器（已保存 toast），返回列表保留「个人」筛选
  await editView.getByRole('button', { name: '返回模板', exact: true }).click()
  await expect(page.getByTestId('template-card').filter({ hasText: '我的冒烟流程' })).toBeVisible()
})

test('任务实例弹窗：编辑脏稿出口守卫——Esc/左栏切视图/右上关闭统一确认', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await mockTaskInstancesApis(page)
  let putCount = 0
  await page.route('**/api/v2/task-instances/templates/tpl-2', route => {
    if (route.request().method() === 'PUT') {
      putCount += 1
      return json(route, { ...tiTemplatesFixture[1], latest_revision_no: 2,
        spec_yaml: tiSpecYaml, spec_compiled: tiSpecCompiled })
    }
    return json(route, { ...tiTemplatesFixture[1],
      spec_yaml: tiSpecYaml, spec_compiled: tiSpecCompiled })
  })

  const enterEditorFromList = async () => {
    // 弹窗已开且在模板列表：直接从个人卡进入编辑（工作台导航在弹窗遮罩后不可点）
    await page.getByTestId('template-card').filter({ hasText: '我的冒烟流程' })
      .getByRole('button', { name: '编辑模板 我的冒烟流程' }).click()
    const editView = page.getByTestId('template-edit-view')
    await expect(editView).toBeVisible()
    await expect(editView.locator('.cm-content')).toContainText('kind: Workflow')
    await editView.locator('.cm-content').click()
    await page.keyboard.press('ControlOrMeta+End')
    await page.keyboard.type('\n# 脏稿标记')
    await expect(editView.getByText('未保存')).toBeVisible()
    return editView
  }

  const openDirtyEditor = async () => {
    await page.getByRole('button', { name: /任务实例/ }).click()
    await page.getByRole('navigation', { name: '任务实例视图' })
      .getByRole('button', { name: '流程模板' }).click()
    await page.getByTestId('template-source-group').getByRole('radio', { name: '个人' }).click()
    return enterEditorFromList()
  }

  await page.goto('/#/super-assistant')
  const discard = page.getByRole('dialog').filter({ hasText: '放弃未保存修改？' })

  // Esc：脏稿不直接丢，确认后「继续编辑」保留草稿且不执行关闭
  const editView = await openDirtyEditor()
  await page.keyboard.press('Escape')
  await expect(discard).toBeVisible()
  await discard.getByRole('button', { name: '继续编辑' }).click()
  await expect(discard).toHaveCount(0)
  await expect(editView).toBeVisible()
  await expect(editView.locator('.cm-content')).toContainText('# 脏稿标记')
  await expect(page.getByTestId('task-instances-dialog')).toBeVisible()

  // 左栏切视图：同样确认；「放弃」才执行原去向（落在流程模板列表）
  await page.getByRole('navigation', { name: '任务实例视图' })
    .getByRole('button', { name: '流程模板' }).click()
  await expect(discard).toBeVisible()
  await expect(editView.locator('.cm-content')).toContainText('# 脏稿标记')
  await discard.getByRole('button', { name: '继续编辑' }).click()
  await expect(discard).toHaveCount(0)
  await expect(editView).toBeVisible()
  await page.getByRole('navigation', { name: '任务实例视图' })
    .getByRole('button', { name: '流程模板' }).click()
  await discard.getByRole('button', { name: '放弃' }).click()
  await expect(discard).toHaveCount(0)
  await expect(page.getByTestId('task-instances-templates')).toBeVisible()

  // 右上关闭：同样确认；「放弃」后弹窗关闭（弹窗仍开，从列表重进编辑）
  await enterEditorFromList()
  await page.getByRole('button', { name: '关闭' }).click()
  await expect(discard).toBeVisible()
  await discard.getByRole('button', { name: '放弃' }).click()
  await expect(discard).toHaveCount(0)
  await expect(page.getByTestId('task-instances-dialog')).toHaveCount(0)

  // 全程守卫确认不发 PUT（「继续编辑」与「放弃」都不触发保存）
  await expect.poll(() => putCount, { timeout: 500 }).toBe(0)
})

test('任务实例弹窗：保存失败保留草稿，确认后才退出', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await mockTaskInstancesApis(page)
  await page.route('**/api/v2/task-instances/templates/tpl-2', route => {
    if (route.request().method() === 'PUT') {
      return route.fulfill({
        status: 400, contentType: 'application/json',
        body: JSON.stringify({ detail: { code: 'TEMPLATE_INVALID',
          message: 'edges 引用了不存在的节点' } }),
      })
    }
    return json(route, { ...tiTemplatesFixture[1],
      spec_yaml: tiSpecYaml, spec_compiled: tiSpecCompiled })
  })

  await page.goto('/#/super-assistant')
  await page.getByRole('button', { name: /任务实例/ }).click()
  await page.getByRole('navigation', { name: '任务实例视图' })
    .getByRole('button', { name: '流程模板' }).click()
  await page.getByTestId('template-source-group').getByRole('radio', { name: '个人' }).click()
  await page.getByTestId('template-card').filter({ hasText: '我的冒烟流程' })
    .getByRole('button', { name: '编辑模板 我的冒烟流程' }).click()
  const editView = page.getByTestId('template-edit-view')
  await expect(editView.locator('.cm-content')).toContainText('kind: Workflow')
  await editView.locator('.cm-content').click()
  await page.keyboard.press('ControlOrMeta+End')
  await page.keyboard.type('\n# 失败保留草稿')

  // 保存失败：错误可见、草稿保留在编辑器
  await editView.getByRole('button', { name: '保存新版本' }).click()
  await expect(editView.getByText('edges 引用了不存在的节点')).toBeVisible()
  await expect(editView.locator('.cm-content')).toContainText('# 失败保留草稿')
  await expect(editView.getByText('未保存')).toBeVisible()

  // 失败后退出仍走确认：面板自身返回按钮 → 面板确认「放弃修改并退出」
  await editView.getByRole('button', { name: '返回模板', exact: true }).click()
  const panelConfirm = page.getByRole('dialog').filter({ hasText: '放弃未保存的修改' })
  await expect(panelConfirm).toBeVisible()
  await panelConfirm.getByRole('button', { name: '放弃修改并退出' }).click()
  await expect(page.getByTestId('template-card').filter({ hasText: '我的冒烟流程' })).toBeVisible()
})

test('任务实例弹窗：个人删除走确认框——取消不发请求，成功后列表刷新', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await mockTaskInstancesApis(page)
  const deleted: string[] = []
  let remaining = [...tiTemplatesFixture]
  await page.route('**/api/v2/task-instances/templates**', route => {
    if (route.request().method() === 'DELETE') {
      deleted.push(route.request().url())
      remaining = remaining.filter(t => !route.request().url().includes(t.id))
      return json(route, { status: 'deleted' })
    }
    if (route.request().method() === 'GET') return json(route, remaining)
    return json(route, {})
  })

  await page.goto('/#/super-assistant')
  await page.getByRole('button', { name: /任务实例/ }).click()
  await page.getByRole('navigation', { name: '任务实例视图' })
    .getByRole('button', { name: '流程模板' }).click()
  await page.getByTestId('template-source-group').getByRole('radio', { name: '个人' }).click()

  const personalCard = page.getByTestId('template-card').filter({ hasText: '我的冒烟流程' })
  await personalCard.getByRole('button', { name: '删除模板 我的冒烟流程' }).click()
  await expect(page.getByText('删除模板')).toBeVisible()
  // 取消：不发删除请求，卡片仍在
  await page.getByRole('button', { name: '取消', exact: true }).click()
  await expect(personalCard).toBeVisible()
  await expect.poll(() => deleted.length, { timeout: 500 }).toBe(0)

  // 确认：发 DELETE，成功后列表刷新、卡片消失
  await personalCard.getByRole('button', { name: '删除模板 我的冒烟流程' }).click()
  await page.getByRole('button', { name: '删除', exact: true }).click()
  await expect.poll(() => deleted.length).toBe(1)
  expect(deleted[0]).toContain('/task-instances/templates/tpl-2')
  await expect(page.getByTestId('template-card').filter({ hasText: '我的冒烟流程' })).toHaveCount(0)
})

test('任务实例弹窗：新建保存成功归位「个人」并清空关键词', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await mockTaskInstancesApis(page)
  const listedWithNew = [
    ...tiTemplatesFixture,
    { id: 'tpl-new', name: '刚创建的流程', description: '',
      is_preset: false, latest_revision_no: 1, latest_revision_id: 'rev-new',
      canonical_hash: 'c'.repeat(64), instance_count: 0,
      created_at: '2026-10-17T02:00:00Z', updated_at: '2026-10-17T02:00:00Z' },
  ]
  await page.route('**/api/v2/task-instances/templates**', route => {
    const url = route.request().url()
    if (route.request().method() === 'POST' && url.endsWith('/templates')) {
      return json(route, { id: 'tpl-new', name: '刚创建的流程' }, 201)
    }
    if (route.request().method() === 'GET' && !/\/templates\/[^/]+/.test(
      url.replace(/([?]).*$/, ''))) {
      return json(route, listedWithNew)
    }
    return json(route, {})
  })

  await page.goto('/#/super-assistant')
  await page.getByRole('button', { name: /任务实例/ }).click()
  await page.getByRole('navigation', { name: '任务实例视图' })
    .getByRole('button', { name: '流程模板' }).click()
  // 预置来源 + 无关关键词：新建成功后必须切换到「个人」并清空关键词
  await page.getByTestId('template-search-input').fill('评审')

  await page.getByTestId('new-flow-button').click()
  await page.getByRole('button', { name: '保存模板' }).click()
  await page.getByLabel(/模板名称/).fill('刚创建的流程')
  await page.getByRole('button', { name: '保存', exact: true }).click()

  await expect(page.getByTestId('task-instances-templates')).toBeVisible()
  await expect(page.getByTestId('template-source-group')
    .getByRole('radio', { name: '个人' })).toHaveAttribute('aria-checked', 'true')
  await expect(page.getByTestId('template-search-input')).toHaveValue('')
  await expect(page.getByTestId('template-card').filter({ hasText: '刚创建的流程' })).toBeVisible()
})

test('对话问题导航竖条：长会话展示每条提问刻度，点击定位并高亮当前视口轮次', async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 1080 })
  await seedAuth(page)
  await mockApis(page)
  const longMessages = Array.from({ length: 30 }, (_, index) => ({
    id: `m-long-${index}`, conversation_id: 'c-long',
    role: index % 2 === 0 ? 'user' : 'assistant',
    content: `第 ${index + 1} 条消息：${'内容'.repeat(30)}`,
    status: 'complete', steps: [], token_usage: {}, created_at: at(0, 9),
  }))
  await page.route('**/api/v2/super-assistant/conversations', route => {
    if (route.request().method() === 'GET') {
      return json(route, [{
        id: 'c-long', title: '长会话', model_config_id: 'model-1',
        status: 'active', created_at: at(0, 9), updated_at: at(0, 9),
      }])
    }
    return route.fallback()
  })
  await page.route('**/api/v2/super-assistant/conversations/c-long/messages', route => json(route, longMessages))

  await page.goto('/#/super-assistant?conversation=c-long')
  const scroller = page.locator('main div.h-full.overflow-y-auto')
  await expect(scroller.getByText('第 30 条消息')).toBeVisible()

  // 宽视口 + 多轮会话：竖条渲染，刻度数与用户提问数一致（30 条消息 15 轮）
  const rail = page.getByTestId('super-assistant-turn-rail')
  await expect(rail).toBeVisible()
  await expect(rail.locator('[data-turn-rail-item]')).toHaveCount(15)
  // 助手消息锚点补齐：导航与全局搜索共用 super-assistant-msg-<id> 锚点体系
  await expect(page.locator('#super-assistant-msg-m-long-1')).toBeAttached()

  // 滚到顶：第 1 条提问为当前视口轮次
  await scroller.evaluate(el => { el.scrollTop = 0 })
  await expect(rail.locator('[data-turn-rail-item][data-index="0"]')).toHaveAttribute('data-active', 'true')

  // 点击第 8 根刻度：平滑滚动定位到第 8 条提问（锚点顶到滚动区顶缘附近）
  await rail.locator('[data-turn-rail-item][data-index="7"]').click()
  await expect.poll(async () => {
    return await page.evaluate(() => {
      const host = document.querySelector('main div.h-full.overflow-y-auto')
      const anchor = document.getElementById('super-assistant-msg-m-long-14')
      if (!host || !anchor) return 9999
      return Math.round(anchor.getBoundingClientRect().top - host.getBoundingClientRect().top)
    })
  }).toBeLessThan(48)
  await expect(rail.locator('[data-turn-rail-item][data-index="7"]')).toHaveAttribute('data-active', 'true')

  // 悬停刻度弹出两段式预览：用户提问 + 助手回复摘要
  await rail.locator('[data-turn-rail-item][data-index="0"]').hover()
  const preview = page.getByTestId('super-assistant-turn-rail-preview')
  await expect(preview).toBeVisible()
  await expect(preview).toContainText('第 1 条消息')
  await expect(preview).toContainText('第 2 条消息')
})

test('对话问题导航竖条：单轮会话与窄聊天区不渲染', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  const shortMessages = [
    { id: 'm-s-0', conversation_id: 'c-short', role: 'user', content: '唯一一问', status: 'complete', steps: [], token_usage: {}, created_at: at(0, 9) },
    { id: 'm-s-1', conversation_id: 'c-short', role: 'assistant', content: '唯一一答', status: 'complete', steps: [], token_usage: {}, created_at: at(0, 8) },
  ]
  const longMessages = Array.from({ length: 30 }, (_, index) => ({
    id: `m-long-${index}`, conversation_id: 'c-long',
    role: index % 2 === 0 ? 'user' : 'assistant',
    content: `第 ${index + 1} 条消息：${'内容'.repeat(30)}`,
    status: 'complete', steps: [], token_usage: {}, created_at: at(0, 9),
  }))
  await page.route('**/api/v2/super-assistant/conversations', route => {
    if (route.request().method() === 'GET') {
      return json(route, [
        { id: 'c-short', title: '单轮会话', model_config_id: 'model-1', status: 'active', created_at: at(0, 9), updated_at: at(0, 9) },
        { id: 'c-long', title: '长会话', model_config_id: 'model-1', status: 'active', created_at: at(0, 9), updated_at: at(0, 9) },
      ])
    }
    return route.fallback()
  })
  await page.route('**/api/v2/super-assistant/conversations/c-short/messages', route => json(route, shortMessages))
  await page.route('**/api/v2/super-assistant/conversations/c-long/messages', route => json(route, longMessages))

  // 宽视口但只有一轮提问：刻度不足 2 根，竖条不渲染
  await page.setViewportSize({ width: 1920, height: 1080 })
  await page.goto('/#/super-assistant?conversation=c-short')
  await expect(page.getByText('唯一一答')).toBeVisible()
  await expect(page.getByTestId('super-assistant-turn-rail')).toHaveCount(0)

  // 多轮会话但聊天区过窄：左缘没有留白，竖条让位不渲染
  await page.setViewportSize({ width: 1024, height: 768 })
  await page.goto('/#/super-assistant?conversation=c-long')
  await expect(page.getByText('第 30 条消息').first()).toBeVisible()
  await expect(page.getByTestId('super-assistant-turn-rail')).toHaveCount(0)
})

test('对话问题导航竖条：配置接口慢于消息返回（loading 后收口）时仍渲染', async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 1080 })
  await seedAuth(page)
  await mockApis(page)
  const longMessages = Array.from({ length: 30 }, (_, index) => ({
    id: `m-long-${index}`, conversation_id: 'c-long',
    role: index % 2 === 0 ? 'user' : 'assistant',
    content: `第 ${index + 1} 条消息：${'内容'.repeat(30)}`,
    status: 'complete', steps: [], token_usage: {}, created_at: at(0, 9),
  }))
  await page.route('**/api/v2/super-assistant/conversations', route => {
    if (route.request().method() === 'GET') {
      return json(route, [{
        id: 'c-long', title: '长会话', model_config_id: 'model-1',
        status: 'active', created_at: at(0, 9), updated_at: at(0, 9),
      }])
    }
    return route.fallback()
  })
  await page.route('**/api/v2/super-assistant/conversations/c-long/messages', route => json(route, longMessages))
  // 消息接口即时返回，而页面初始 loading 等待的模型配置接口延迟返回：
  // 复现「消息先到、loading 后收口」的时序——竖条挂载必须与滚动容器同帧，
  // 否则初测读到空的 scrollHostRef 后将永不渲染
  await page.route('**/api/v1/models', async route => {
    await page.waitForTimeout(900)
    await route.fallback()
  })

  await page.goto('/#/super-assistant?conversation=c-long')
  await expect(page.locator('main div.h-full.overflow-y-auto').getByText('第 30 条消息')).toBeVisible()
  const rail = page.getByTestId('super-assistant-turn-rail')
  await expect(rail).toBeVisible()
  await expect(rail.locator('[data-turn-rail-item]')).toHaveCount(15)
})

// 能力地图是业务说明入口，不读取新的后台数据，也不触发业务操作。
test('能力地图：上下文左侧入口、分层与连接边界、关闭后聊天可用', async ({ page }) => {
  await seedAuth(page)
  const api = await mockApis(page)
  await page.goto('/#/super-assistant?conversation=c-today')
  const entry = page.getByRole('button', { name: '查看能力地图' })
  const context = page.getByTestId('super-assistant-context-usage')
  await expect(context).toBeVisible()
  const entryBox = await entry.boundingBox()
  const contextBox = await context.boundingBox()
  expect(entryBox).not.toBeNull()
  expect(contextBox).not.toBeNull()
  expect(entryBox!.x + entryBox!.width).toBeLessThanOrEqual(contextBox!.x)

  const failures: string[] = []
  page.on('pageerror', error => failures.push(error.message))
  await entry.click()
  const dialog = page.getByRole('dialog', { name: '超级助手能力地图' })
  await expect(dialog).toBeVisible()
  for (const title of ['01 · 信息与业务背景', '02 · 理解与协调', '03 · 执行与协作']) {
    await expect(dialog.getByRole('heading', { name: title })).toBeVisible()
  }
  for (const title of ['消息通知', '知识图谱', '本体治理', '定时任务', '任务实例', '外部集成']) {
    await expect(dialog.getByRole('heading', { name: title, exact: true })).toBeVisible()
  }
  await expect(dialog.locator('[data-capability-scenario="从消息到任务"]')).toContainText('待打通')
  await expect(dialog.locator('[data-capability-scenario="从知识到业务操作"]')).toContainText('待打通')
  await expect(dialog.locator('[data-capability-scenario="从助手到外部协作"]')).toContainText('按配置接入')
  await expect(dialog).not.toContainText(/Skill|MCP|记忆/)
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
  await expect(entry).toBeFocused()
  expect(api.chatCalls).toHaveLength(0)
  expect(api.createCalls).toHaveLength(0)

  // 空会话也能认识助手边界；开关弹窗不改变输入草稿或触发懒建会话。
  await page.getByRole('button', { name: '新建会话' }).click()
  const input = page.getByRole('textbox', { name: '向超级助手发送消息' })
  await input.fill('能力地图关闭后的草稿')
  await entry.focus()
  await page.keyboard.press('Enter')
  await expect(dialog).toBeVisible()
  const close = dialog.getByRole('button', { name: '关闭', exact: true })
  await expect(close).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(close).toBeFocused()
  await close.click()
  await expect(entry).toBeFocused()
  await expect(input).toHaveValue('能力地图关闭后的草稿')
  await input.fill('你好')
  await input.press('Enter')
  await expect.poll(() => api.chatCalls.length).toBe(1)
  await expect(page.getByText('你好，我是超级助手', { exact: true })).toBeVisible()
  expect(failures).toEqual([])
})

for (const viewport of [{ width: 390, height: 844 }, { width: 1024, height: 768 }]) {
  test(`能力地图：${viewport.width}px 入口可达、视口内滚动、遮罩关闭`, async ({ page }) => {
    await page.setViewportSize(viewport)
    await seedAuth(page)
    await mockApis(page)
    await page.goto('/#/super-assistant?conversation=c-today')
    if (viewport.width >= 768) {
      await page.getByRole('button', { name: '打开助手配置' }).click()
      await expect(page.getByTestId('super-assistant-context-usage')).toBeHidden()
    }
    const entry = page.getByRole('button', { name: '查看能力地图' })
    await expect(entry).toBeVisible()
    await expect(page.getByRole('combobox', { name: '会话模型' })).toBeVisible()
    await entry.click()
    const dialog = page.getByRole('dialog', { name: '超级助手能力地图' })
    await expect(dialog).toBeVisible()
    const box = await dialog.boundingBox()
    expect(box).not.toBeNull()
    expect(box!.x).toBeGreaterThanOrEqual(0)
    expect(box!.y).toBeGreaterThanOrEqual(0)
    expect(box!.x + box!.width).toBeLessThanOrEqual(viewport.width)
    expect(box!.y + box!.height).toBeLessThanOrEqual(viewport.height)
    const body = dialog.locator('[data-capability-map-body]')
    expect(await body.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true)
    await body.evaluate(el => { el.scrollTop = el.scrollHeight })
    await expect(dialog.getByRole('heading', { name: '典型场景与连接进度' })).toBeVisible()
    const close = dialog.getByRole('button', { name: '关闭', exact: true })
    expect(await close.evaluate(el => {
      const r = el.getBoundingClientRect()
      return el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2))
    })).toBe(true)
    await page.mouse.click(2, 2)
    await expect(dialog).toHaveCount(0)
    await expect(entry).toBeFocused()
  })
}

test('能力地图：暗色令牌、无导航授权账号可读说明且不增加操作权限', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await page.addInitScript(() => {
    localStorage.setItem('auth-store', JSON.stringify({ state: {
      token: 'e2e-token', user: { id: 'u-1', username: 'viewer', role: 'viewer', menu_permissions: ['super_assistant'] },
    }, version: 0 }))
    localStorage.setItem('theme', JSON.stringify({ state: { theme: 'dark' }, version: 0 }))
  })
  await page.goto('/#/super-assistant?conversation=c-today')
  await expect(page.getByRole('link', { name: '本体治理' })).toHaveCount(0)
  await page.getByRole('button', { name: '查看能力地图' }).click()
  const dialog = page.getByRole('dialog', { name: '超级助手能力地图' })
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText('账号权限、配置与授权约束')
  await expect(dialog.getByRole('button')).toHaveCount(1)
  await expect(dialog.getByRole('link')).toHaveCount(0)
  expect(await dialog.evaluate(el => {
    const probe = document.createElement('span')
    probe.style.backgroundColor = 'var(--popover)'
    el.appendChild(probe)
    const expected = getComputedStyle(probe).backgroundColor
    probe.remove()
    return document.documentElement.classList.contains('dark')
      && getComputedStyle(el).backgroundColor === expected
  })).toBe(true)
})

