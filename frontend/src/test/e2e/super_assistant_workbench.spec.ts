import { expect, test, type Locator, type Page, type Route } from '@playwright/test'

import { expectSameBoundingBox } from './support/geometry'

// AI 原生工作台（前台）：登录默认落地、七项入口、近期会话单列表、归档流转、
// 本体治理跳后台并返回。全部接口本地 mock，不触真实后端。
// 本 spec 另覆盖：分组限量展开、naive UTC 时区显示、行悬停不抖动、
// 会话附件上传/移除/位于输入框上方与跨会话隔离、流式生成跨会话隔离、ReUI 模型选择器、
// 删除确认弹窗、新建任务空会话去重、空态品牌文案与占位符、配置面板白底、
// 重命名 blur 取消、知识图谱页签弹窗（文件库上传/删除/预览/在线编辑/ZIP 导入 +
// 知识图谱过滤/节点详情/邻域检索高亮）、外部集成（multica 配置弹窗 + /multica:
// 命令提示的配置门控）、⌘K/Ctrl+K 唤起全局搜索、输入草稿按会话缓存、
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
  /** GET /super-assistant/multica/config 的返回（默认未配置态） */
  multicaConfig?: Record<string, unknown>
}

async function mockApis(page: Page, options: MockOptions = {}) {
  const patchBodies: string[] = []
  const deleteCalls: string[] = []
  const createCalls: string[] = []
  const fileUploads: string[] = []
  const fileDeletes: string[] = []
  const chatCalls: string[] = []
  const searchQueries: string[] = []
  const createdConvs: Array<Record<string, unknown>> = []
  const multicaPuts: Array<Record<string, unknown>> = []
  const remoteAgentCreates: Array<Record<string, unknown>> = []
  const remoteInviteCreates: string[] = []
  // 远程助手目录夹具：直连（带端点）+ 回连（在线，仅邀请函路径接入的形态）
  const remoteAgents: Array<Record<string, unknown>> = [
    { id: 'ra-direct', key: 'remote.kb', label: '客服知识库助手', description: '擅长客服问答', endpoint: 'https://kb.example.com/turn', token_set: true, enabled: true, timeout_seconds: 120, mode: 'direct', last_seen_at: null, last_turn_at: new Date(Date.now() - 3 * 3600 * 1000).toISOString() },
    { id: 'ra-pull', key: 'remote.lan-helper', label: '内网文档助手', description: '内网文档检索', endpoint: '', token_set: false, enabled: true, timeout_seconds: 60, mode: 'pull', last_seen_at: new Date().toISOString(), last_turn_at: null },
  ]
  const remoteInvites: Array<Record<string, unknown>> = []
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
      const reply = '你好，我是超级助手'
      const fulfill = () => {
        chatDone = true
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
    if (path.startsWith('/api/v2/super-assistant/conversations/') && request.method() === 'PATCH') {
      const id = path.split('/')[5]
      const body = JSON.parse(request.postData() || '{}')
      patchBodies.push(request.postData() || '')
      const source = conversationsFixture.find(item => item.id === id)
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
      return json(route, [...createdConvs, ...conversationsFixture])
    }
    if (/^\/api\/v2\/super-assistant\/conversations\/[^/]+\/messages$/.test(path)) {
      const id = path.split('/')[5]
      if (id === 'c-today' && chatDone) {
        return json(route, [
          { id: 'm-1', conversation_id: 'c-today', role: 'user', content: '你好', status: 'complete', steps: [], token_usage: {}, created_at: at(0, 9) },
          { id: 'm-2', conversation_id: 'c-today', role: 'assistant', content: '你好，我是超级助手', status: 'complete', steps: [], token_usage: {}, created_at: at(0, 9) },
        ])
      }
      // c-earlier 是「有消息的历史会话」：新建任务去重、底部输入框等场景以此为夹具
      if (id === 'c-earlier') {
        return json(route, [
          { id: 'm-e1', conversation_id: 'c-earlier', role: 'user', content: '上周的问题', status: 'complete', steps: [], token_usage: {}, created_at: at(5, 10) },
          { id: 'm-e2', conversation_id: 'c-earlier', role: 'assistant', content: '上周的答复', status: 'complete', steps: [], token_usage: {}, created_at: at(5, 10) },
        ])
      }
      return json(route, [])
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
    searchQueries,
    multicaPuts,
    multicaTests,
    remoteAgentCreates,
    remoteInviteCreates,
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

test('工作台骨架：七项入口齐备，近期会话单列表，归档折叠', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await page.goto('/#/super-assistant')

  await expect(page.getByRole('button', { name: '新建任务' })).toBeVisible()
  await expect(page.getByRole('button', { name: /全局搜索/ })).toBeVisible()
  await expect(page.getByRole('button', { name: '定时任务' })).toBeVisible()
  await expect(page.getByRole('button', { name: '知识图谱' })).toBeVisible()
  await expect(page.getByRole('link', { name: '本体治理' })).toBeVisible()
  await expect(page.getByRole('button', { name: '外部集成' })).toBeVisible()
  await expect(page.getByRole('button', { name: /退出登录/ })).toBeVisible()

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
  await expect(page.getByRole('button', { name: '新建任务' })).toBeVisible()

  // 右下角悬浮助手仍是第二条返回路径
  await page.getByRole('link', { name: '本体治理' }).click()
  await page.waitForURL('**/#/ontologies')
  await page.getByTestId('assistant-widget-fab').click()
  await page.getByTestId('assistant-widget-open-full').click()
  await page.waitForURL(/#\/super-assistant/)
  await expect(page.getByRole('button', { name: '新建任务' })).toBeVisible()
})

test('定时任务为如实占位的即将上线弹窗，全局搜索打开真实检索面板', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await page.goto('/#/super-assistant')

  // 全局搜索已是真实功能：打开 ReUI Command 检索面板
  await page.getByRole('button', { name: '全局搜索' }).click()
  await expect(page.getByPlaceholder('搜索会话标题与消息内容…')).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).toHaveCount(0)

  await page.getByRole('button', { name: '定时任务' }).click()
  await expect(page.getByText(/即将上线/).first()).toBeVisible()
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
  await expect(toggle).toHaveText('展开全部（还有 3 条）')

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

  const expected = new Date(`${naive}Z`).toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  })
  await expect(page.locator('[data-workbench-conversation="c-naive"]')).toContainText(expected)
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

test('会话模型选择器为 ReUI Select，选择后 PATCH 持久化', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page)
  await page.goto('/#/super-assistant?conversation=c-today')

  // c-today 当前为 model-1（DeepSeek）；Radix Select 选中相同值不触发 onValueChange，切到 Qwen
  await page.getByRole('combobox', { name: '会话模型' }).click()
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

test('新建任务去重：空会话或全新视图下点击不再创建新会话', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page)
  await page.goto('/#/super-assistant?conversation=c-today')

  // c-today 是空会话：点击新建任务不创建
  await expect(page.getByTestId('super-assistant-composer')).toBeVisible()
  await page.getByRole('button', { name: '新建任务' }).click()
  await page.waitForTimeout(300)
  expect(mocks.createCalls).toHaveLength(0)

  // 切到有消息的 c-earlier：点击新建任务创建新会话并选中
  await page.locator('[data-workbench-conversation="c-earlier"] button').first().click()
  await expect(page.getByText('上周的答复')).toBeVisible()
  await page.getByRole('button', { name: '新建任务' }).click()
  await expect.poll(() => mocks.createCalls.length).toBe(1)
  await expect(page.locator('[data-workbench-conversation="c-new-1"]')).toHaveCount(1)

  // 新会话仍是空会话：再次点击不再创建
  await page.getByRole('button', { name: '新建任务' }).click()
  await page.waitForTimeout(300)
  expect(mocks.createCalls).toHaveLength(1)
})

test('空态只保留品牌一句话，输入框占位符不混入用户输入', async ({ page }) => {
  await seedAuth(page)
  await mockApis(page)
  await page.goto('/#/super-assistant?conversation=c-today')

  const hero = page.getByText('SuperAgent 工作空间 2.0')
  await expect(hero).toBeVisible()
  await expect(page.getByText('有什么可以帮你？')).toHaveCount(0)
  await expect(page.getByText('试试这样问')).toHaveCount(0)
  // 品牌一句话是页面视觉主角：字号不小于 text-3xl（30px）
  const heroFontSize = await hero.evaluate(el => parseFloat(getComputedStyle(el).fontSize))
  expect(heroFontSize).toBeGreaterThanOrEqual(30)

  const textbox = page.getByRole('textbox', { name: '向超级助手发送消息' })
  await expect(textbox).toHaveAttribute('placeholder', '咨询任何问题，创造任何事物')
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

  // 外部集成已是真实功能：左侧集成类型 tabs（multica / GitHub 占位），
  // multica 默认选中并走通「测试连接 → 选工作区 → 保存」
  await page.getByRole('button', { name: '外部集成' }).click()
  const integrationsDialog = page.getByRole('dialog')
  await expect(integrationsDialog.getByRole('heading', { name: '外部集成' })).toBeVisible()
  const multicaTab = integrationsDialog.locator('[data-integrations-tab="multica"]')
  await expect(multicaTab).toHaveAttribute('aria-selected', 'true')
  await expect(integrationsDialog.getByTestId('multica-config-card')).toBeVisible()
  // 等入场缩放动画结束再量尺寸，避免动画中的包围盒参与比较
  await page.waitForTimeout(300)

  // GitHub tab 为结构化占位：切过去显示规划中，切回 multica 表单仍在
  const shellBefore = await integrationsDialog.boundingBox()
  await integrationsDialog.locator('[data-integrations-tab="github"]').click()
  await expect(integrationsDialog.getByTestId('integrations-github-placeholder')).toBeVisible()
  await expect(integrationsDialog.getByTestId('multica-config-card')).toHaveCount(0)
  // 弹窗固定宽高：切 tab 前后尺寸不变；左栏加宽后 GitHub 全名不截断
  const shellAfter = await integrationsDialog.boundingBox()
  expect(Math.abs(shellBefore!.height - shellAfter!.height)).toBeLessThanOrEqual(1)
  expect(Math.abs(shellBefore!.width - shellAfter!.width)).toBeLessThanOrEqual(1)
  const githubLabel = integrationsDialog.locator('[data-integrations-tab="github"]').locator('span').first()
  expect(await githubLabel.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true)
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
  await expect(page.getByText('正在搜索…')).toHaveCount(1)
  const searchingBox = await page.getByText('正在搜索…').boundingBox()
  const dialogBox = await dialog.boundingBox()
  expect(searchingBox && dialogBox).toBeTruthy()
  expect(Math.abs((searchingBox!.y + searchingBox!.height / 2) - (dialogBox!.y + 44 + 132))).toBeLessThan(8)

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
  await filesPane.locator('[data-palace-file="pf-note-1"]').click()
  await contentPane.getByRole('button', { name: /删除 会议纪要/ }).click()
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
  await expect(statsBar).toContainText(/上次更新：\d{4}\//)
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


test('远程助手：邀请函自助接入主路径，手动配置为高级路径，双模式展示', async ({ page }) => {
  await seedAuth(page)
  const mocks = await mockApis(page)
  await page.goto('/#/super-assistant')

  await page.getByRole('button', { name: '外部集成' }).click()
  const integrationsDialog = page.getByRole('dialog')
  await expect(integrationsDialog.getByRole('heading', { name: '外部集成' })).toBeVisible()
  await integrationsDialog.locator('[data-integrations-tab="remote-agents"]').click()

  // 目录：直连行展示端点，回连行展示模式徽标与在线状态
  const list = integrationsDialog.getByTestId('remote-agent-list')
  await expect(list.getByText('客服知识库助手')).toBeVisible()
  await expect(list.getByText('https://kb.example.com/turn')).toBeVisible()
  await expect(list.getByText('内网文档助手')).toBeVisible()
  await expect(list.getByText('回连', { exact: true })).toBeVisible()
  await expect(list.getByText('直连', { exact: true })).toBeVisible()
  await expect(list.getByText('在线').first()).toBeVisible()
  await expect(list.getByText('上次委派 3 小时前')).toBeVisible()

  // 主路径：生成邀请 → 邀请函弹层常驻全文（复制失败也有手动复制/下载兜底）
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
  await expect(list.getByText('数据查询助手')).toBeVisible()
})
