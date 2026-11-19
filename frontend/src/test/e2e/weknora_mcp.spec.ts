import { expect, test, type Locator, type Page, type Route } from '@playwright/test'

import type { SuperMcpServer, WeKnoraDirectory, WeKnoraEndpoint } from '../../api/superAssistant'

// 仅模拟 HTTP；断言真实组件请求、凭据弹窗、确认取消、启停和错误反馈。
const now = '2026-11-19T08:00:00Z'
const base = '/api/v2/super-assistant'
const tools = ['list_knowledge_bases', 'search_knowledge', 'read_document']
const token = 'mcp_synthetic_fixture_authorization_123456'

function endpoint(id = 'endpoint-1', overrides: Partial<WeKnoraEndpoint> = {}): WeKnoraEndpoint {
  return {
    id, name: `知识端点 ${id}`, enabled: true, knowledge_base_ids: ['kb-1'],
    knowledge_bases: [{ id: 'kb-1', name: 'OpenOntology' }], tools: [...tools],
    readonly: true, server_id: null, connection_status: 'not_connected',
    message: null, missing: false, ...overrides,
  }
}

function server(item: WeKnoraEndpoint, overrides: Partial<SuperMcpServer> = {}): SuperMcpServer {
  return {
    id: `server-${item.id}`, name: 'platform_weknora_aabb0011', display_name: item.name,
    description: 'Synthetic read-only endpoint', builtin_key: null, dev_project_id: null,
    transport: 'streamable_http', url: `http://weknora-app:8080/mcp/${item.id}`,
    header_names: ['Authorization'], command: null, args: [], env_names: [],
    enabled: true, require_confirmation: true, constrained: true,
    tool_manifest: tools.map(name => ({ name, description: name, input_schema: { type: 'object' } })),
    last_test_status: 'success', last_test_message: '连接已验证', last_tested_at: now,
    weknora_endpoint_id: item.id, weknora_scope: { knowledge_base_ids: ['kb-1'] },
    created_at: now, updated_at: now, ...overrides,
  }
}

const json = (route: Route, data: unknown) => route.fulfill({
  status: 200, contentType: 'application/json', body: JSON.stringify({ data }),
})

type State = {
  directory: WeKnoraDirectory
  servers: SuperMcpServer[]
  calls: Array<{ method: string; path: string; body: Record<string, unknown> }>
  initializeError?: string
  verificationError?: string
  directoryError?: string
}

async function setup(page: Page, directory: WeKnoraDirectory, initialServers: SuperMcpServer[] = [], role = 'admin') {
  const state: State = { directory, servers: initialServers, calls: [] }
  page.on('pageerror', error => console.error(`Page error: ${error.message}`))
  await page.addInitScript(({ role }) => {
    localStorage.setItem('token', 'synthetic-session')
    localStorage.setItem('auth-store', JSON.stringify({
      state: { token: 'synthetic-session', user: { id: 'u-1', username: 'fixture-admin', role, menu_permissions: ['super_assistant'] } },
      version: 0,
    }))
  }, { role })
  await page.route('**/api/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (!path.startsWith('/api/')) return route.continue()
    const method = request.method()
    const body = request.postData() ? request.postDataJSON() as Record<string, unknown> : {}
    if (path.includes('/weknora/mcp-')) state.calls.push({ method, path, body })
    if (path === `${base}/weknora/mcp-endpoints`) {
      if (state.directoryError) return route.fulfill({
        status: 503, contentType: 'application/json',
        body: JSON.stringify({ detail: { code: 'WEKNORA_UNAVAILABLE', message: state.directoryError } }),
      })
      return json(route, state.directory)
    }
    if (path === `${base}/weknora/mcp-initialize`) {
      if (state.initializeError) return route.fulfill({
        status: 409, contentType: 'application/json',
        body: JSON.stringify({ detail: { code: 'WEKNORA_MODELS_REQUIRED', message: state.initializeError } }),
      })
      if (!state.directory.endpoints.some(item => !item.missing) && (!state.directory.initialized || body.recreate)) {
        const item = endpoint('default')
        const row = server(item)
        state.servers.push(row)
        state.directory.endpoints = [{ ...item, server_id: row.id, connection_status: 'ready' }]
      }
      state.directory.initialized = true
      return json(route, state.directory)
    }
    const connect = path.match(/\/weknora\/mcp-endpoints\/([^/]+)\/(connect|verify)$/)
    if (connect && method === 'POST') {
      const item = state.directory.endpoints.find(candidate => candidate.id === connect[1])!
      const existing = state.servers.find(row => row.weknora_endpoint_id === item.id)
      const row = server(item, {
        ...existing,
        ...(state.verificationError ? { constrained: false, last_test_status: 'error', last_test_message: state.verificationError } : {}),
      })
      state.servers = [...state.servers.filter(candidate => candidate.id !== row.id), row]
      item.server_id = row.id
      item.connection_status = state.verificationError ? 'needs_verification' : row.enabled ? 'ready' : 'disabled'
      item.message = state.verificationError ?? null
      return json(route, row)
    }
    if (path === `${base}/mcp-servers`) return json(route, state.servers)
    const managed = path.match(/\/mcp-servers\/([^/]+)$/)
    if (managed && method === 'PATCH') {
      state.calls.push({ method, path, body })
      const row = state.servers.find(item => item.id === managed[1])!
      Object.assign(row, body)
      return json(route, row)
    }
    if (managed && method === 'DELETE') {
      state.calls.push({ method, path, body })
      const row = state.servers.find(item => item.id === managed[1])!
      state.servers = state.servers.filter(item => item.id !== row.id)
      const item = state.directory.endpoints.find(item => item.id === row.weknora_endpoint_id)!
      item.server_id = null
      item.connection_status = 'not_connected'
      return route.fulfill({ status: 204 })
    }
    if (path === '/api/v1/models') return json(route, [{
      id: 'model-1', name: 'Synthetic', config_type: 'llm', provider: 'openai',
      api_base: 'https://example.invalid', has_api_key: true, enabled: true, is_default: true,
      last_test_status: 'success', last_tested_at: now, last_test_message: 'ok',
      models: ['synthetic-model'], options: {}, created_by: 'u-1', created_at: now, updated_at: now,
    }])
    if (path === `${base}/weknora`) return json(route, { web_url: 'https://weknora.example.invalid' })
    if (path === `${base}/kaneo`) return json(route, { web_url: 'https://kaneo.example.invalid' })
    if (path === '/api/v1/auth/profile') return json(route, { id: 'u-1', username: 'fixture-admin', role, menu_permissions: ['super_assistant'] })
    if (path.endsWith('/unread-count')) return json(route, { count: 0 })
    if (path.endsWith('/stats')) return json(route, { unread: 0, total: 0 })
    return json(route, [])
  })
  await page.goto('/#/super-assistant')
  await expect(page.getByRole('textbox', { name: '向超级助手发送消息' })).toBeVisible()
  return state
}

async function openMcp(page: Page) {
  if ((page.viewportSize()?.width ?? 1280) < 768) {
    await page.getByRole('button', { name: '更多操作', exact: true }).click()
    await page.getByRole('button', { name: '打开助手配置', exact: true }).click()
  } else {
    await page.getByRole('button', { name: '打开助手配置' }).click()
  }
  await page.getByRole('button', { name: /^MCP \d+$/ }).click()
  return page.getByTestId('weknora-mcp-section')
}

async function expectHit(locator: Locator) {
  await locator.scrollIntoViewIfNeeded()
  await expect.poll(() => locator.evaluate(element => {
    const box = element.getBoundingClientRect()
    const target = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2)
    return target !== null && (target === element || element.contains(target))
  })).toBe(true)
}

test('仅打开 MCP 标签触发首次初始化，重开不重复创建', async ({ page }) => {
  const state = await setup(page, { initialized: false, endpoints: [], message: null })
  await page.getByRole('button', { name: '打开助手配置' }).click()
  await expect(page.getByText('暂无 Skill', { exact: true })).toBeVisible()
  expect(state.calls).toEqual([])
  await page.getByRole('button', { name: /^MCP \d+$/ }).click()
  await expect(page.getByTestId('weknora-endpoint-default')).toContainText('已连接')
  expect(state.calls.filter(call => call.method === 'POST')).toEqual([
    { method: 'POST', path: `${base}/weknora/mcp-initialize`, body: { recreate: false } },
  ])
  await page.getByRole('region', { name: '助手配置', exact: true }).getByRole('button', { name: '关闭助手配置' }).click()
  await openMcp(page)
  await expect.poll(() => state.calls.filter(call => call.method === 'GET').length).toBe(2)
  expect(state.calls.filter(call => call.method === 'POST')).toHaveLength(1)
})

test('已有多个端点只确认目录；输入 token 后挂载对应端点并保留其他端点', async ({ page }) => {
  const state = await setup(page, { initialized: false, endpoints: [endpoint(), endpoint('endpoint-2')], message: null })
  const section = await openMcp(page)
  await expect(section.getByTestId('weknora-endpoint-endpoint-2')).toContainText('未接入')
  await expect.poll(() => state.calls.filter(call => call.method === 'POST').length).toBe(1)
  expect(state.servers).toEqual([])
  const card = section.getByTestId('weknora-endpoint-endpoint-1')
  await card.getByRole('button', { name: '输入 Token 接入' }).click()
  const dialog = page.getByRole('dialog', { name: '接入 WeKnora 端点：知识端点 endpoint-1' })
  const input = dialog.getByLabel('WeKnora 端点 知识端点 endpoint-1 的访问 Token')
  await expectHit(input)
  await expect(input).toHaveAttribute('type', 'password')
  await input.fill(token)
  await dialog.getByRole('button', { name: '接入', exact: true }).click()
  await expect(dialog).toHaveCount(0)
  await expect(card).toContainText('已连接')
  await expect(page.getByRole('button', { name: 'MCP 1', exact: true })).toBeVisible()
  expect(state.calls.find(call => call.path.endsWith('/endpoint-1/connect'))?.body).toEqual({ token })
  expect(state.calls.some(call => call.body.rotate_token === true)).toBe(false)
  await expect(section.getByTestId('weknora-endpoint-endpoint-2')).toContainText('未接入')
  const stored = await page.evaluate(() => JSON.stringify(localStorage))
  expect(stored).not.toContain(token)
  await expect(page.getByText(token, { exact: true })).toHaveCount(0)
})

test('轮换必须确认，取消不发送请求；窄屏确认按钮实际可点击', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 })
  const state = await setup(page, { initialized: true, endpoints: [endpoint()], message: null })
  const section = await openMcp(page)
  await section.getByRole('button', { name: '轮换 Token', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '轮换 Token 并接入' })
  await expect(dialog).toContainText('原有 Token 立即失效')
  await expectHit(dialog.getByRole('button', { name: '取消', exact: true }))
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  expect(state.calls.filter(call => call.method === 'POST')).toEqual([])
  await section.getByRole('button', { name: '轮换 Token', exact: true }).click()
  await expectHit(dialog.getByRole('button', { name: '轮换并接入' }))
  await dialog.getByRole('button', { name: '轮换并接入' }).click()
  await expect(section.getByTestId('weknora-endpoint-endpoint-1')).toContainText('已连接')
  expect(state.calls.filter(call => call.method === 'POST').map(call => call.body)).toEqual([{ rotate_token: true }])
})

test('启停和确认开关复用托管行，断开保留上游端点', async ({ page }) => {
  const item = endpoint()
  const row = server(item)
  item.server_id = row.id
  item.connection_status = 'ready'
  const state = await setup(page, { initialized: true, endpoints: [item], message: null }, [row])
  const section = await openMcp(page)
  await section.getByRole('switch', { name: '停用 WeKnora 端点 知识端点 endpoint-1' }).click()
  await expect(section.getByRole('switch', { name: '启用 WeKnora 端点 知识端点 endpoint-1' })).not.toBeChecked()
  await expect(section.getByText('已停用', { exact: true })).toBeVisible()
  await section.getByRole('switch', { name: '启用 WeKnora 端点 知识端点 endpoint-1' }).click()
  await section.getByRole('switch', { name: '开启 知识端点 endpoint-1 自动执行' }).click()
  await expect(section.getByRole('switch', { name: '关闭 知识端点 endpoint-1 自动执行' })).toBeChecked()
  await section.getByRole('button', { name: '断开 WeKnora 端点 知识端点 endpoint-1' }).click()
  const dialog = page.getByRole('dialog', { name: '断开 WeKnora 端点' })
  await expect(dialog).toContainText('不会删除 WeKnora 侧的端点')
  await expectHit(dialog.getByRole('button', { name: '断开', exact: true }))
  await dialog.getByRole('button', { name: '断开', exact: true }).click()
  await expect(section.getByTestId('weknora-endpoint-endpoint-1')).toContainText('未接入')
  expect(state.calls.filter(call => call.method === 'PATCH').map(call => call.body)).toEqual([
    { enabled: false }, { enabled: true }, { require_confirmation: false },
  ])
  expect(state.calls.filter(call => call.method === 'DELETE').map(call => call.path)).toEqual([`${base}/mcp-servers/${row.id}`])
  expect(state.directory.endpoints).toHaveLength(1)
})

test('无原生模型时如实提示并可重试初始化', async ({ page }) => {
  const state = await setup(page, { initialized: false, endpoints: [], message: null })
  state.initializeError = '请先在领域知识原生页面配置默认 Embedding 与聊天模型'
  const section = await openMcp(page)
  await expect(section.getByTestId('weknora-init-error')).toContainText('配置默认 Embedding')
  await expect(section.getByText('已连接', { exact: true })).toHaveCount(0)
  state.initializeError = undefined
  await section.getByRole('button', { name: '重试初始化' }).click()
  await expect(section.getByTestId('weknora-endpoint-default')).toContainText('已连接')
})

test('被删端点只显示失效；点击创建才带 recreate 标记重建', async ({ page }) => {
  const item = endpoint('deleted', { enabled: false, missing: true, connection_status: 'unavailable', message: '此端点已在 WeKnora 中删除' })
  const row = server(item)
  item.server_id = row.id
  const state = await setup(page, { initialized: true, endpoints: [item], message: null }, [row])
  const section = await openMcp(page)
  await expect(section.getByTestId('weknora-endpoint-deleted')).toContainText('上游缺失')
  await expect(section.getByTestId('weknora-empty-directory')).toBeVisible()
  expect(state.calls.filter(call => call.method === 'POST')).toEqual([])
  await section.getByRole('button', { name: '创建默认端点' }).click()
  await expect(section.getByTestId('weknora-endpoint-default')).toContainText('已连接')
  expect(state.calls.filter(call => call.method === 'POST').map(call => call.body)).toEqual([{ recreate: true }])
})

test('HTTP 200 的验证失败不宣称成功，允许重新提供 token', async ({ page }) => {
  const state = await setup(page, { initialized: true, endpoints: [endpoint()], message: null })
  state.verificationError = 'WeKnora MCP 连接失败，请检查端点 token'
  const section = await openMcp(page)
  await section.getByRole('button', { name: '输入 Token 接入' }).click()
  const dialog = page.getByRole('dialog', { name: '接入 WeKnora 端点：知识端点 endpoint-1' })
  await dialog.getByLabel('WeKnora 端点 知识端点 endpoint-1 的访问 Token').fill(token)
  await dialog.getByRole('button', { name: '接入', exact: true }).click()
  await expect(page.getByText('端点「知识端点 endpoint-1」凭据已保存，验证失败', { exact: true })).toBeVisible()
  await expect(section.getByTestId('weknora-endpoint-endpoint-1')).toContainText('待校验')
  await expect(section.getByRole('button', { name: '输入 Token', exact: true })).toBeVisible()
  await expect(page.getByText(/已接入并通过验证/)).toHaveCount(0)
  state.verificationError = undefined
  await section.getByRole('button', { name: '校验', exact: true }).click()
  await expect(section.getByTestId('weknora-endpoint-endpoint-1')).toContainText('已连接')
})

test('包含 ask 的端点拒绝接入；非管理员不挂载管理目录', async ({ page }) => {
  const state = await setup(page, { initialized: true, endpoints: [endpoint('unsafe', { tools: [...tools, 'ask'], readonly: false, connection_status: 'unavailable' })], message: null })
  const section = await openMcp(page)
  await expect(section).toContainText('包含平台不允许的工具（ask）')
  await expect(section.getByRole('button', { name: '输入 Token 接入' })).toHaveCount(0)
  expect(state.calls.filter(call => call.method === 'POST')).toEqual([])
})

test('目录不可达显示可重试的失败，编辑角色不访问管理员目录', async ({ page }) => {
  const state = await setup(page, { initialized: true, endpoints: [], message: null })
  state.directoryError = 'WeKnora 暂时无法连接，请稍后重试'
  const section = await openMcp(page)
  await expect(section.getByRole('alert')).toContainText('WeKnora 暂时无法连接')
  state.directoryError = undefined
  await section.getByRole('button', { name: '重试', exact: true }).click()
  await expect(section.getByTestId('weknora-empty-directory')).toBeVisible()
})

test('编辑角色不挂载 WeKnora 管理区', async ({ page }) => {
  const state = await setup(page, { initialized: false, endpoints: [], message: null }, [], 'editor')
  await page.getByRole('button', { name: '打开助手配置' }).click()
  await page.getByRole('button', { name: /^MCP \d+$/ }).click()
  await expect(page.getByText('暂无 MCP Server', { exact: true })).toBeVisible()
  await expect(page.getByTestId('weknora-mcp-section')).toHaveCount(0)
  expect(state.calls).toEqual([])
})
