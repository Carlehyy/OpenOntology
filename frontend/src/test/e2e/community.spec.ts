import { expect, test, type Page, type Route } from '@playwright/test'


const now = '2026-07-21T08:00:00+00:00'

const json = (route: Route, data: unknown, status = 200) => route.fulfill({
  status,
  contentType: 'application/json',
  body: JSON.stringify({ data }),
})

async function authenticate(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: { id: 'admin', username: 'admin', email: 'admin@example.com', role: 'admin' },
      },
      version: 0,
    }))
  })
}

  test('开放社区导航、技能占位页与 MCP 完整生命周期可用', async ({ page }) => {
  await authenticate(page)
  let createBody: Record<string, unknown> | null = null
  let patchBody: Record<string, unknown> | null = null
  let exportBody: Record<string, unknown> | null = null
  let stdioExportBody: Record<string, unknown> | null = null
  let server = {
    id: 'mcp-1',
    name: 'weather_tools',
    display_name: '天气工具集',
    description: '提供城市天气查询能力',
    builtin_key: null,
    transport: 'streamable_http',
    url: 'https://mcp.example.com/mcp',
    header_names: [],
    command: null,
    args: [],
    env_names: [],
    enabled: false,
    require_confirmation: true,
    tool_manifest: [] as Array<{ name: string; description: string; input_schema: Record<string, unknown> }>,
    last_test_status: null as 'success' | 'error' | null,
    last_test_message: null as string | null,
    last_tested_at: null as string | null,
    created_at: now,
    updated_at: now,
  }
  const stdioServer = {
    ...server,
    id: 'mcp-stdio-1',
    name: 'worldbank_mcp',
    display_name: '世界银行',
    description: '世界银行开放数据',
    transport: 'stdio',
    url: '',
    command: 'npx',
    args: ['-y', 'worldbank-mcp'],
    env_names: ['WORLDBANK_API_KEY'],
    tool_manifest: [{ name: 'search_indicators', description: '检索指标', input_schema: { type: 'object' } }],
    last_test_status: 'success' as const,
    last_test_message: '连接成功，发现 1 个工具',
  }
  const builtinMinio = {
    ...server,
    id: 'builtin-minio',
    name: 'platform_minio',
    builtin_key: 'minio',
    url: 'builtin://minio',
    enabled: true,
  }
  const servers = () => [builtinMinio, server, stdioServer]

  await page.route('**/api/v2/community/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path === '/api/v2/community/mcp-servers' && request.method() === 'GET') {
      return json(route, servers())
    }
    if (path === '/api/v2/community/mcp-servers' && request.method() === 'POST') {
      createBody = request.postDataJSON() as Record<string, unknown>
      return json(route, {
        ...server,
        id: 'mcp-2',
        name: createBody.name,
        url: createBody.url,
        enabled: createBody.enabled,
      }, 201)
    }
    if (path === '/api/v2/community/mcp-servers/mcp-1/test' && request.method() === 'POST') {
      server = {
        ...server,
        last_test_status: 'success',
        last_test_message: '连接成功，发现 1 个工具',
        last_tested_at: now,
        tool_manifest: [{ name: 'get_forecast', description: '读取城市天气预报', input_schema: { type: 'object' } }],
      }
      return json(route, { ok: true, message: server.last_test_message, tools: server.tool_manifest })
    }
    if (path === '/api/v2/community/mcp-servers/mcp-1' && request.method() === 'PATCH') {
      const body = request.postDataJSON() as Record<string, unknown>
      patchBody = body
      server = { ...server, ...body }
      return json(route, server)
    }
    if (path === '/api/v2/community/mcp-servers/mcp-1/export-interfaces' && request.method() === 'POST') {
      exportBody = request.postDataJSON() as Record<string, unknown>
      return json(route, {
        created: [{ id: 9, name: '天气工具集 · get_forecast', tool: 'get_forecast' }],
        skipped: [],
      })
    }
    if (path === '/api/v2/community/mcp-servers/mcp-stdio-1/export-interfaces' && request.method() === 'POST') {
      stdioExportBody = request.postDataJSON() as Record<string, unknown>
      return json(route, {
        created: [{ id: 10, name: '世界银行 · search_indicators', tool: 'search_indicators' }],
        skipped: [],
      })
    }
    return route.fulfill({ status: 404, body: '{}' })
  })
  await page.route('**/api/v2/inbox/summary', route => json(route, { unread_count: 0 }))

  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/community/skills')
  await expect(page.getByRole('heading', { name: '技能社区即将上线' })).toBeVisible()

  const apiHub = page.getByRole('button', { name: '接口代理', exact: true })
  const community = page.getByRole('button', { name: '开放社区', exact: true })
  const models = page.getByRole('link', { name: '模型配置' })
  const [apiHubBox, communityBox, modelsBox] = await Promise.all([
    apiHub.boundingBox(),
    community.boundingBox(),
    models.boundingBox(),
  ])
  expect(apiHubBox).not.toBeNull()
  expect(communityBox).not.toBeNull()
  expect(modelsBox).not.toBeNull()
  expect(apiHubBox!.y).toBeLessThan(communityBox!.y)
  expect(communityBox!.y).toBeLessThan(modelsBox!.y)

  await expect(community).toHaveAttribute('aria-expanded', 'true')
  await community.click()
  await expect(community).toHaveAttribute('aria-expanded', 'false')
  await expect(page.getByRole('link', { name: '插件社区' })).toHaveCount(0)
  await community.click()
  await expect(community).toHaveAttribute('aria-expanded', 'true')

  await page.getByRole('link', { name: '插件社区' }).click()
  await expect(page).toHaveURL(/#\/community\/plugins$/)
  await expect(page.getByRole('heading', { name: '插件社区' })).toBeVisible()
  await expect(page.locator('table').getByText('天气工具集', { exact: true })).toBeVisible()
  await expect(page.locator('table').getByText('weather_tools', { exact: true })).toBeVisible()
  await expect(page.locator('table').getByText('提供城市天气查询能力')).toBeVisible()
  await expect(page.getByText('platform_minio', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '添加平台 MinIO' })).toHaveCount(0)
  await expect(page.getByRole('columnheader', { name: '开放状态' })).toHaveCount(0)
  await expect(page.getByRole('columnheader', { name: '执行策略' })).toHaveCount(0)

  const serverList = page.getByRole('region', { name: 'MCP Server 清单' })
  const serverListBox = await serverList.boundingBox()
  const viewportHeight = await page.evaluate(() => window.innerHeight)
  expect(serverListBox).not.toBeNull()
  expect(serverListBox!.height).toBeGreaterThan(500)
  expect(viewportHeight - serverListBox!.y - serverListBox!.height).toBeGreaterThanOrEqual(20)
  expect(viewportHeight - serverListBox!.y - serverListBox!.height).toBeLessThanOrEqual(28)
  // beUI 统计卡组：三张卡（MCP/已通过/已发现工具）在筛选区上方渲染
  const statsRegion = page.getByTestId('mcp-server-stats')
  await expect(statsRegion).toBeVisible()
  await expect(statsRegion.getByText('MCP Server', { exact: true })).toBeVisible()
  await expect(statsRegion.getByText('已通过', { exact: true })).toBeVisible()
  await expect(statsRegion.getByText('已发现工具', { exact: true })).toBeVisible()

  await page.getByRole('button', { name: '测试 MCP 天气工具集' }).click()
  // 列表含多条已测试数据（stdio 桥接用例），状态/工具数断言须限定在目标行内
  const weatherRow = page.getByRole('row', { name: /天气工具集/ })
  await expect(weatherRow.getByText('已通过', { exact: true })).toBeVisible()
  await expect(weatherRow.getByText('共 1 个', { exact: true })).toBeVisible()
  // 行内启用开关：本页切换「对超级助手启用」，测试成功但未启用时 toast 提供就地启用
  const enableSwitch = page.getByRole('switch', { name: '对超级助手启用 天气工具集' })
  await expect(enableSwitch).toBeVisible()
  await expect(enableSwitch).toHaveAttribute('aria-checked', 'false')
  await page.getByRole('button', { name: '就地启用' }).click()
  await expect.poll(() => patchBody).toMatchObject({ enabled: true })
  await expect(enableSwitch).toHaveAttribute('aria-checked', 'true')

  await page.getByRole('button', { name: '查看 天气工具集 的工具清单' }).click()
  await expect(page.getByText('调用方式', { exact: true })).toBeVisible()
  await expect(page.getByText(/tools\/call/).first()).toBeVisible()
  await expect(page.getByText('https://mcp.example.com/mcp').first()).toBeVisible()
  await page.getByText('输入参数（JSON Schema）').first().click()
  await page.getByText('请求示例（tools/call）').first().click()
  await expect(page.locator('pre').filter({ hasText: '"method": "tools/call"' }).first()).toBeVisible()
  await page.getByRole('button', { name: '关闭弹窗' }).click()

  await page.getByRole('button', { name: '转接口 天气工具集' }).click()
  await expect(page.getByText('单发 JSON-RPC（tools/call）POST').first()).toBeVisible()
  await page.getByRole('button', { name: '生成接口' }).click()
  await expect.poll(() => exportBody).not.toBeNull()
  expect(exportBody).toMatchObject({ tool_names: ['get_forecast'] })
  await expect(page.getByText('工具已导出至接口代理')).toBeVisible()

  // stdio 传输经平台桥接导出：按钮可点、弹窗展示桥接说明、导出请求正常发出
  const stdioExportButton = page.getByRole('button', { name: '转接口 世界银行' })
  await expect(stdioExportButton).toBeEnabled()
  await stdioExportButton.click()
  await expect(page.getByText('平台桥接调用').first()).toBeVisible()
  await page.getByRole('button', { name: '生成接口' }).click()
  await expect.poll(() => stdioExportBody).not.toBeNull()
  expect(stdioExportBody).toMatchObject({ tool_names: ['search_indicators'] })
  // 前一次导出的 toast 可能仍在栈中未消退，断言最新一条即可
  await expect(page.getByText('工具已导出至接口代理').last()).toBeVisible()

  await page.getByRole('button', { name: '添加 MCP', exact: true }).click()
  await expect(page.getByText('开放到超级助手', { exact: true })).toHaveCount(0)
  await expect(page.getByText('每次工具调用前要求确认', { exact: true })).toHaveCount(0)
  await page.getByLabel('MCP 客户端 JSON').fill(`{
    // VS Code mcp.json
    "servers": {
      "Remote Docs": {
        "type": "http",
        "url": "https://docs.example.com/mcp",
      },
      "local-tools": {
        "type": "stdio",
        "command": "uvx",
        "args": ["mcp-server-fetch"],
      },
    },
  }`)
  await page.getByRole('button', { name: '解析并填入下方表单' }).click()
  await expect(page.getByLabel('选择要填入的 MCP Server')).toBeVisible()
  await expect(page.getByLabel(/标识/)).toHaveValue('Remote-Docs')
  await expect(page.getByLabel(/名称/)).toHaveValue('')
  await expect(page.getByLabel(/描述/)).toHaveValue('')
  await expect(page.getByLabel(/传输方式/)).toContainText('Streamable HTTP（推荐）')
  await expect(page.getByLabel(/MCP URL/)).toHaveValue('https://docs.example.com/mcp')
  await page.getByLabel('选择要填入的 MCP Server').click()
  await page.getByRole('option', { name: 'local-tools' }).click()
  await expect(page.getByLabel(/传输方式/)).toContainText('stdio（启动本地进程）')
  await expect(page.getByLabel(/^command/)).toHaveValue('uvx')
  await page.getByLabel('选择要填入的 MCP Server').click()
  await page.getByRole('option', { name: 'Remote-Docs' }).click()
  await expect(page.getByLabel(/标识/)).toHaveValue('Remote-Docs')
  await expect(page.getByRole('button', { name: '保存' })).toBeDisabled()
  await page.getByLabel(/名称/).fill('知识检索服务')
  await expect(page.getByRole('button', { name: '保存' })).toBeDisabled()
  await page.getByLabel(/描述/).fill('企业知识库检索工具')
  await expect(page.getByRole('button', { name: '保存' })).toBeEnabled()
  await page.getByLabel(/标识/).fill('knowledge_search')
  await page.getByLabel(/MCP URL/).fill('https://knowledge.example.com/mcp')
  await page.getByRole('button', { name: '保存' }).click()
  await expect.poll(() => createBody).not.toBeNull()
  expect(createBody).toMatchObject({
    name: 'knowledge_search',
    display_name: '知识检索服务',
    description: '企业知识库检索工具',
    transport: 'streamable_http',
    url: 'https://knowledge.example.com/mcp',
    enabled: false,
    require_confirmation: true,
  })
})

test('插件社区在窄屏下不产生页面横向溢出', async ({ page }) => {
  await authenticate(page)
  await page.route('**/api/v2/community/mcp-servers', route => json(route, []))
  await page.route('**/api/v2/inbox/summary', route => json(route, { unread_count: 0 }))
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('/#/community/plugins')
  await expect(page.getByRole('heading', { name: '插件社区' })).toBeVisible()
  await expect(page.getByRole('button', { name: '添加 MCP', exact: true })).toBeVisible()
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  expect(overflow).toBeLessThanOrEqual(1)
})

test('MCP 状态筛选支持多选并集与 chip 移除恢复', async ({ page }) => {
  await authenticate(page)
  const mk = (id: string, name: string, status: 'success' | 'error' | null) => ({
    id,
    name,
    display_name: name,
    description: `${name} 描述`,
    builtin_key: null,
    transport: 'streamable_http',
    url: `https://${id}.example.com/mcp`,
    header_names: [],
    command: null,
    args: [],
    env_names: [],
    enabled: true,
    require_confirmation: true,
    tool_manifest: [{ name: `${id}_tool`, description: '示例工具', input_schema: { type: 'object' } }],
    last_test_status: status,
    last_test_message: null,
    last_tested_at: null,
    created_at: now,
    updated_at: now,
  })
  await page.route('**/api/v2/community/mcp-servers', route => json(route, [
    mk('mcp-ok', '已通过服务', 'success'),
    mk('mcp-bad', '异常服务', 'error'),
    mk('mcp-new', '未测试服务', null),
  ]))
  await page.route('**/api/v2/inbox/summary', route => json(route, { unread_count: 0 }))
  await page.goto('/#/community/plugins')

  // 未勾选 = 不过滤：三行齐全
  await expect(page.getByRole('row', { name: /已通过服务/ })).toBeVisible()
  await expect(page.getByRole('row', { name: /异常服务/ })).toBeVisible()
  await expect(page.getByRole('row', { name: /未测试服务/ })).toBeVisible()

  // 勾选「已通过 + 未测试」：异常行隐藏，其余保留；chip 成对展示
  const filterInput = page.getByRole('combobox', { name: '筛选 MCP 状态' })
  await filterInput.click()
  await page.getByRole('option', { name: '已通过', exact: true }).click()
  await page.getByRole('option', { name: '未测试', exact: true }).click()
  await expect(page.getByRole('row', { name: /异常服务/ })).toBeHidden()
  await expect(page.getByRole('row', { name: /已通过服务/ })).toBeVisible()
  await expect(page.getByRole('row', { name: /未测试服务/ })).toBeVisible()
  await expect(page.getByRole('button', { name: '移除 已通过' })).toBeVisible()

  // chip 移除「未测试」：仅剩已通过
  await page.getByRole('button', { name: '移除 未测试' }).click()
  await expect(page.getByRole('row', { name: /未测试服务/ })).toBeHidden()
  await expect(page.getByRole('row', { name: /已通过服务/ })).toBeVisible()

  // 移除最后一个 chip：回到全部
  await page.getByRole('button', { name: '移除 已通过' }).click()
  await expect(page.getByRole('row', { name: /异常服务/ })).toBeVisible()
  await expect(page.getByRole('row', { name: /未测试服务/ })).toBeVisible()
})
