import { expect, test, type Page, type Route } from '@playwright/test'


const now = '2026-09-13T08:00:00+00:00'

const json = (route: Route, data: unknown, status = 200) => route.fulfill({
  status,
  contentType: 'application/json',
  body: JSON.stringify({ data }),
})

const errorJson = (route: Route, status: number, detail: string) => route.fulfill({
  status,
  contentType: 'application/json',
  body: JSON.stringify({ detail }),
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

const PROJECT_ID = 'dev-project-1'

const script = `@mcp_tool(description="把两个数字相加")
def add_numbers(a: int, b: int = 1) -> dict:
    return {"sum": a + b}
`

const manifest = [{
  name: 'add_numbers',
  description: '把两个数字相加',
  input_schema: {
    type: 'object',
    properties: { a: { type: 'integer' }, b: { type: 'integer', default: 1 } },
    required: ['a'],
  },
}]

const developedServer = {
  id: 'server-1',
  name: 'my_tools',
  display_name: '我的工具集',
  description: 'e2e 自研 MCP',
  builtin_key: null,
  dev_project_id: PROJECT_ID,
  transport: 'developed',
  url: `developed://${PROJECT_ID}`,
  header_names: [],
  command: null,
  args: [],
  env_names: [],
  enabled: false,
  require_confirmation: true,
  tool_manifest: manifest,
  last_test_status: 'success',
  last_test_message: '发布校验通过：1 个工具全部执行成功',
  last_tested_at: now,
  created_at: now,
  updated_at: now,
}

test('开发 MCP：新建项目 → 解析 → 试跑 → 保存 → 发布 → 清单自研徽标', async ({ page }) => {
  await authenticate(page)
  let executeBody: Record<string, unknown> | null = null
  let saveBody: Record<string, unknown> | null = null
  let publishBody: Record<string, unknown> | null = null
  let published = false

  await page.route('**/api/v2/community/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path === '/api/v2/community/mcp-servers' && request.method() === 'GET') {
      return json(route, published ? [developedServer] : [])
    }
    if (path === '/api/v2/community/mcp-dev/projects' && request.method() === 'POST') {
      const body = request.postDataJSON() as Record<string, unknown>
      expect(body.name).toBe('my_tools')
      return json(route, {
        id: PROJECT_ID,
        name: 'my_tools',
        display_name: '我的工具集',
        description: '',
        status: 'draft',
        tool_count: 0,
        version_count: 0,
        published_version_no: null,
        script,
        tool_samples: {},
        created_at: now,
        updated_at: now,
      }, 201)
    }
    if (path === `/api/v2/community/mcp-dev/projects/${PROJECT_ID}` && request.method() === 'GET') {
      return json(route, {
        id: PROJECT_ID,
        name: 'my_tools',
        display_name: '我的工具集',
        description: '',
        status: published ? 'published' : 'draft',
        tool_count: 1,
        version_count: published ? 1 : 0,
        published_version_no: published ? 1 : null,
        script,
        tool_samples: { add_numbers: { a: 2, b: 3 } },
        created_at: now,
        updated_at: now,
      })
    }
    if (path === `/api/v2/community/mcp-dev/projects/${PROJECT_ID}/versions` && request.method() === 'GET') {
      return json(route, [{
        id: 'version-1', version_no: 1, tool_count: 1, duration_ms: 12, created_at: now,
      }])
    }
    if (path === `/api/v2/community/mcp-dev/projects/${PROJECT_ID}/versions/1` && request.method() === 'GET') {
      return json(route, {
        id: 'version-1', version_no: 1, tool_count: 1, duration_ms: 12, created_at: now,
        script, tool_manifest: manifest, tool_samples: { add_numbers: { a: 2, b: 3 } }, tool_gates: null,
      })
    }
    if (path === `/api/v2/community/mcp-dev/projects/${PROJECT_ID}/execute` && request.method() === 'POST') {
      executeBody = request.postDataJSON() as Record<string, unknown>
      if (executeBody.tool_name) {
        return json(route, {
          ok: true, tools: null, payload: { ok: true, payload: { sum: 5 }, duration_ms: 8 },
          stdout: '', error: null, traceback: '', duration_ms: 8,
        })
      }
      return json(route, {
        ok: true, tools: manifest, payload: null,
        stdout: '', error: null, traceback: '', duration_ms: 6,
      })
    }
    if (path === `/api/v2/community/mcp-dev/projects/${PROJECT_ID}/save` && request.method() === 'POST') {
      saveBody = request.postDataJSON() as Record<string, unknown>
      return json(route, { ok: true, version_no: 1, tools: manifest, error: null, traceback: '', duration_ms: 10 })
    }
    if (path === `/api/v2/community/mcp-dev/projects/${PROJECT_ID}/publish` && request.method() === 'POST') {
      publishBody = request.postDataJSON() as Record<string, unknown>
      published = true
      return json(route, {
        server_id: 'server-1', version_no: 1, tools: manifest,
        gates: [{ name: 'add_numbers', ok: true, error: '', duration_ms: 9 }],
      })
    }
    return route.fulfill({ status: 404, body: '{}' })
  })
  await page.route('**/api/v2/inbox/summary', route => json(route, { unread_count: 0 }))

  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/community/plugins')

  // 列表页：开发 MCP 按钮位于添加 MCP 左侧，空态引导可见
  const addButton = page.getByRole('button', { name: '添加 MCP' })
  const devButton = page.getByRole('button', { name: '开发 MCP' })
  await expect(devButton).toBeVisible()
  const devBox = await devButton.boundingBox()
  const addBox = await addButton.boundingBox()
  expect(devBox && addBox && devBox.x < addBox.x).toBe(true)

  // 新建开发项目
  await devButton.click()
  await expect(page.getByRole('heading', { name: '开发 MCP' })).toBeVisible()
  await page.getByLabel('开发项目标识').fill('my_tools')
  await page.getByLabel('开发项目显示名称').fill('我的工具集')
  await page.getByRole('button', { name: '创建并开发' }).click()

  // 开发页：左编辑器 + 右工具面板（编辑一处内容进入未保存态，保存门槛才可能解锁）
  await expect(page).toHaveURL(new RegExp(`/community/plugins/develop/${PROJECT_ID}`))
  await expect(page.getByText('MCP 工具脚本（Python）')).toBeVisible()
  await expect(page.locator('.cm-content')).toContainText('add_numbers')
  await page.locator('.cm-content').click()
  await page.keyboard.press('ControlOrMeta+End')
  await page.keyboard.type('\n# e2e edit')
  await expect(page.getByText('未保存', { exact: true })).toBeVisible()

  // 解析工具 → 工具清单出现
  await page.getByRole('button', { name: '解析工具' }).click()
  await expect(page.getByText('工具清单（1 个）')).toBeVisible()
  await expect(page.getByRole('button', { name: /add_numbers/ })).toBeVisible()

  // 试跑 → 执行结果展示返回值
  await page.getByRole('button', { name: '试跑', exact: true }).click()
  await expect(page.getByText('"sum": 5')).toBeVisible()

  // 保存（解析通过后解锁）→ 冻结版本
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByText('已保存为版本 v1')).toBeVisible()

  // 发布为 MCP
  await page.getByRole('button', { name: '发布为 MCP' }).click()
  await expect(page.getByText('发布版本')).toBeVisible()
  await page.getByRole('button', { name: '发布', exact: true }).click()
  await expect(page.getByText(/已发布为 MCP（v1，1 个工具）/)).toBeVisible()

  const bodyOf = (value: Record<string, unknown> | null) => value ?? {}
  expect(bodyOf(executeBody).script).toContain('add_numbers')
  expect(bodyOf(saveBody).script).toContain('add_numbers')
  expect(bodyOf(publishBody).version_no).toBe(1)

  // 返回插件社区：自研 MCP 与导入的同场展示
  await page.getByRole('button', { name: '返回插件社区' }).click()
  await expect(page.getByText('我的工具集').first()).toBeVisible()
  await expect(page.getByText('自研', { exact: true }).first()).toBeVisible()
})

test('开发 MCP：发布闸门失败时提示明确原因，不写入清单', async ({ page }) => {
  await authenticate(page)

  await page.route('**/api/v2/community/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path === '/api/v2/community/mcp-servers' && request.method() === 'GET') {
      return json(route, [])
    }
    if (path === `/api/v2/community/mcp-dev/projects/${PROJECT_ID}` && request.method() === 'GET') {
      return json(route, {
        id: PROJECT_ID, name: 'my_tools', display_name: '我的工具集', description: '',
        status: 'draft', tool_count: 1, version_count: 1, published_version_no: null,
        script, tool_samples: {}, created_at: now, updated_at: now,
      })
    }
    if (path === `/api/v2/community/mcp-dev/projects/${PROJECT_ID}/versions` && request.method() === 'GET') {
      return json(route, [{ id: 'version-1', version_no: 1, tool_count: 1, duration_ms: 12, created_at: now }])
    }
    if (path === `/api/v2/community/mcp-dev/projects/${PROJECT_ID}/versions/1` && request.method() === 'GET') {
      return json(route, {
        id: 'version-1', version_no: 1, tool_count: 1, duration_ms: 12, created_at: now,
        script, tool_manifest: manifest, tool_samples: {}, tool_gates: null,
      })
    }
    if (path === `/api/v2/community/mcp-dev/projects/${PROJECT_ID}/publish` && request.method() === 'POST') {
      return errorJson(route, 400, '以下工具还没有样例参数（请在开发页用真实入参成功试跑一次，发布校验将按样例参数执行）：add_numbers')
    }
    return route.fulfill({ status: 404, body: '{}' })
  })
  await page.route('**/api/v2/inbox/summary', route => json(route, { unread_count: 0 }))

  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(`/#/community/plugins/develop/${PROJECT_ID}`)

  await page.getByRole('button', { name: '发布为 MCP' }).click()
  await page.getByRole('button', { name: '发布', exact: true }).click()
  await expect(page.getByText('发布未通过')).toBeVisible()
  await expect(page.getByText(/还没有样例参数/)).toBeVisible()

  // 关闭发布对话框（失败路径保持打开），再返回清单页：没有写入任何 MCP
  await page.getByRole('button', { name: '取消' }).click()
  await page.getByRole('button', { name: '返回插件社区' }).click()
  await expect(page.getByText('从第一个 MCP Server 开始')).toBeVisible()
})
