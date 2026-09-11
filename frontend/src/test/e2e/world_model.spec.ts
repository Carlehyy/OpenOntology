import { expect, test, type Page, type Route } from '@playwright/test'

const now = '2026-08-13T08:00:00+00:00'

const json = (route: Route, data: unknown, status = 200) => route.fulfill({
  status,
  contentType: 'application/json',
  body: JSON.stringify({ data }),
})

const projectRow = {
  id: 'wm-project-1',
  name: '台区负荷短期推演',
  description: '基于历史负荷曲线做 24 步外推',
  engine_type: 'statistical',
  status: 'published',
  service_status: 'online',
  version_count: 1,
  created_at: now,
  updated_at: now,
}

const projectDetail = {
  ...projectRow,
  script: 'def simulate(context, actions, horizon):\n    return {"trajectory": [1, 2, 3]}\n',
}

async function seedAuth(page: Page, role = 'admin', menuPermissions?: string[]) {
  await page.addInitScript(([userRole, menus]) => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: {
          id: 'u-wm',
          username: 'wm-tester',
          role: userRole,
          ...(menus ? { menu_permissions: menus } : {}),
        },
      },
      version: 0,
    }))
  }, [role, menuPermissions])
}

async function mockPlatformShell(page: Page) {
  await page.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname
    if (!path.startsWith('/api/')) return route.continue()
    if (path === '/api/v2/inbox/summary') {
      return json(route, { openAlertCount: 0, actionableCount: 0, unreadCount: 0, resolvedCount: 0 })
    }
    if (path === '/api/v2/inbox') return json(route, { items: [], nextCursor: null, hasMore: false })
    return json(route, [])
  })
}

async function mockWorldModel(page: Page) {
  await page.route('**/api/v2/world-model/**', route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path === '/api/v2/world-model/projects' && request.method() === 'GET') {
      // 服务端分页/筛选口径：按 keyword / engine_type / page / size 过滤
      const url = new URL(request.url())
      const keywordParam = (url.searchParams.get('keyword') || '').toLowerCase()
      const engineParam = url.searchParams.get('engine_type') || ''
      const pageParam = Number(url.searchParams.get('page') || '1')
      const sizeParam = Number(url.searchParams.get('size') || '12')
      const matched = [projectRow].filter(row =>
        (!keywordParam || `${row.name} ${row.description}`.toLowerCase().includes(keywordParam))
        && (!engineParam || row.engine_type === engineParam))
      const start = (pageParam - 1) * sizeParam
      return json(route, { items: matched.slice(start, start + sizeParam), total: matched.length })
    }
    if (path === '/api/v2/world-model/projects' && request.method() === 'POST') {
      return json(route, { ...projectRow, id: 'wm-project-new', name: '新建推演模型' }, 201)
    }
    if (path === `/api/v2/world-model/projects/${projectRow.id}` && request.method() === 'GET') {
      return json(route, projectDetail)
    }
    if (path.endsWith('/execute') && request.method() === 'POST') {
      return json(route, {
        ok: true,
        payload: { trajectory: [100, 102, 105], confidence: 0.8, boundary: '仅适用于居民用户' },
        stdout: 'done\n',
        error: null,
        traceback: '',
        duration_ms: 42,
        kernel_id: 'kernel-e2e',
      })
    }
    if (path.endsWith('/save') && request.method() === 'POST') {
      return json(route, { ok: true, version_no: 2, execution: null })
    }
    if (path.endsWith('/versions') && request.method() === 'GET') {
      return json(route, [{ id: 'v1', version_no: 1, duration_ms: 40, created_at: now }])
    }
    if (path === `/api/v2/world-model/projects/${projectRow.id}/versions/v1` && request.method() === 'GET') {
      return json(route, {
        id: 'v1',
        version_no: 1,
        duration_ms: 40,
        created_at: now,
        test_input: { context: { current_value: 100 }, actions: [], horizon: 3 },
        script: 'def simulate(context, actions, horizon):\n    return {"trajectory": [9, 9]}\n',
      })
    }
    if (path === '/api/v2/world-model/calls/overview') {
      return json(route, { total: 1, failed: 0, avg_duration_ms: 120 })
    }
    if (path === '/api/v2/world-model/calls/daily') {
      return json(route, [
        { date: '2026-08-12', total: 0, failed: 0, avg_duration_ms: 0 },
        { date: '2026-08-13', total: 1, failed: 0, avg_duration_ms: 120 },
      ])
    }
    if (path === '/api/v2/world-model/calls' && request.method() === 'GET') {
      return json(route, {
        items: [{
          id: 'call-1',
          project_id: projectRow.id,
          service_name: '台区负荷短期推演',
          caller: 'agent-session-1',
          ok: true,
          duration_ms: 120,
          created_at: now,
        }],
        total: 1,
        page: 1,
        size: 20,
      })
    }
    if (path === '/api/v2/world-model/calls/call-1') {
      return json(route, {
        id: 'call-1',
        project_id: projectRow.id,
        service_name: '台区负荷短期推演',
        caller: 'agent-session-1',
        ok: true,
        duration_ms: 120,
        request_payload: { context: { current_value: 100 } },
        response_payload: { trajectory: [100, 102, 105] },
        error: null,
        created_at: now,
      })
    }
    if (path === '/api/v2/world-model/templates/time-series') {
      return json(route, {
        key: 'time-series',
        name: '时序推演示例（ARIMA / SARIMA）',
        description: 'ITSM 式统计时序建模',
        script: 'import statsmodels\ndef simulate(context, actions, horizon):\n    return {"trajectory": [1, 2, 3]}\n',
        test_input: {
          context: { series: Array.from({ length: 36 }, (_, i) => 100 + i), period: 12 },
          actions: [],
          horizon: 6,
        },
      })
    }
    return json(route, [])
  })
}

/** 推演服务注册表页的 mock：可变状态（上线/下线切换后持久到本次测试会话）。 */
async function mockWorldModelServices(page: Page) {
  let serviceStatus = 'online'
  const serviceRow = {
    id: 'svc-registry-1',
    project_id: projectRow.id,
    project_name: '台区负荷短期推演',
    version_id: 'v1',
    version_no: 1,
    name: '台区负荷短期推演服务',
    description: '基于历史负荷曲线做 24 步外推',
    status: 'online',
    endpoint_path: '/api/v2/world-model/services/svc-registry-1/invoke',
    applicable_object_types: { ontology_id: 'ontology-1', object_type_ids: ['ot-line', 'ot-user'] },
    preconditions: [{ object_type_id: 'ot-line', min_count: 12 }],
    call_count: 3,
    failed_count: 0,
    created_at: now,
    updated_at: now,
  }
  await page.route('**/api/v2/world-model/services**', route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path === '/api/v2/world-model/services' && request.method() === 'GET') {
      const url = new URL(request.url())
      const statusParam = url.searchParams.get('status') || ''
      const keywordParam = url.searchParams.get('keyword') || ''
      const visible = serviceStatus === statusParam || !statusParam
      const matched = visible && (!keywordParam || serviceRow.name.includes(keywordParam))
      return json(route, {
        items: matched ? [{ ...serviceRow, status: serviceStatus }] : [],
        total: matched ? 1 : 0,
      })
    }
    if (path === '/api/v2/world-model/services/overview') {
      return json(route, {
        total: 1,
        online: serviceStatus === 'online' ? 1 : 0,
        offline: serviceStatus === 'offline' ? 1 : 0,
        call_total: 3,
        call_failed: 0,
        avg_duration_ms: 18,
      })
    }
    if (path === '/api/v2/world-model/services/svc-registry-1' && request.method() === 'GET') {
      return json(route, { ...serviceRow, status: serviceStatus })
    }
    if (path === '/api/v2/world-model/services/svc-registry-1/status' && request.method() === 'POST') {
      serviceStatus = (request.postDataJSON() as { status: string }).status
      return json(route, { ...serviceRow, status: serviceStatus })
    }
    if (path === '/api/v2/world-model/services/svc-registry-1/invoke' && request.method() === 'POST') {
      // horizon >= 3 视为有效预测；小 horizon 模拟脚本边界拒绝（调用成功但轨迹为空）
      const body = request.postDataJSON() as { horizon?: number }
      const validPrediction = (body.horizon ?? 0) >= 3
      return json(route, {
        ok: true,
        payload: validPrediction
          ? { trajectory: [100, 101, 102] }
          : { trajectory: [], confidence: 0, boundary: 'context.series 必须是长度 >= 12 的数值列表。' },
        error: null,
        duration_ms: 18,
        call_id: 'call-registry-1',
      })
    }
    return json(route, [])
  })
}

test('世界模型与本体模型为一级导航分组，本体管理为本体模型子项', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)
  await mockWorldModel(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/overview')

  const sidebar = page.locator('aside')
  // 本体模型是一级分组按钮，未展开时子项不可见；点击后自动导航到首个子项「本体管理」
  const ontologyModelGroup = sidebar.getByRole('button', { name: '本体模型' })
  await expect(ontologyModelGroup).toBeVisible()
  await expect(sidebar.getByRole('link', { name: '本体管理', exact: true })).toHaveCount(0)
  await ontologyModelGroup.click()
  await expect(page).toHaveURL(/#\/ontologies$/)
  await expect(sidebar.getByRole('link', { name: '本体管理', exact: true })).toBeVisible()
  // 业务澄清已改为详情页「在线配置」直达入口，不再出现在导航中
  await expect(sidebar.getByRole('link', { name: '业务澄清', exact: true })).toHaveCount(0)
  await expect(sidebar.getByRole('link', { name: '本体网络', exact: true })).toBeVisible()
  // 世界模型是一级分组按钮，展开后出现三个子项并自动导航到推演模型
  const worldModelGroup = sidebar.getByRole('button', { name: '世界模型' })
  await expect(worldModelGroup).toBeVisible()
  await worldModelGroup.click()
  await expect(page).toHaveURL(/#\/world-model\/models$/)
  await expect(sidebar.getByRole('link', { name: '推演模型' })).toBeVisible()
  await expect(sidebar.getByRole('link', { name: '推演服务' })).toBeVisible()
  await expect(sidebar.getByRole('link', { name: '调用记录' })).toBeVisible()
})

test('旧 /ontologies/world-model 路径重定向到一级路由', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)
  await mockWorldModel(page)
  await page.setViewportSize({ width: 1440, height: 900 })

  await page.goto('/#/ontologies/world-model')
  await expect(page).toHaveURL(/#\/world-model\/models$/)

  await page.goto('/#/ontologies/world-model/calls')
  await expect(page).toHaveURL(/#\/world-model\/calls$/)
})

test('推演模型与调用记录为独立页面（无页内 Tab 与共享标题）', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)
  await mockWorldModel(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/world-model/models')

  // 共享标题与页内 Tab 已移除
  await expect(page.getByRole('heading', { name: '世界模型' })).toHaveCount(0)
  await expect(page.getByText('演化层能力', { exact: false })).toHaveCount(0)
  await expect(page.getByRole('navigation', { name: '世界模型子功能' })).toHaveCount(0)
  // 列表页内容正常；已发布服务徽标必须显示「在线」（回归：schema 缺字段
  // 导致 service_status 被静默丢弃、徽标永远显示「草稿」）
  await expect(page.getByText('台区负荷短期推演')).toBeVisible()
  await expect(page.getByText('在线', { exact: true })).toBeVisible()

  // 列表筛选（服务端过滤：keyword 随请求发出，mock 按筛选口径返回空结果）
  await page.getByLabel('按模型名称或描述筛选').fill('不存在的模型')
  await expect(page.getByText('台区负荷短期推演')).toHaveCount(0)
  await page.getByLabel('按模型名称或描述筛选').fill('')
  await expect(page.getByText('台区负荷短期推演')).toBeVisible()

  // 经侧边栏子项进入独立的调用记录页
  // 回归：未设置时间筛选时，请求不得携带空的 start=/end=（后端 datetime 校验会 422）
  const callsRequests: string[] = []
  page.on('request', req => {
    if (req.url().includes('/world-model/calls?')) callsRequests.push(req.url())
  })
  const sidebar = page.locator('aside')
  // 当前已在世界模型域内，分组处于激活展开态，子项直接可见（点击分组按钮反而会收起）
  await sidebar.getByRole('link', { name: '调用记录' }).click()
  await expect(page).toHaveURL(/#\/world-model\/calls$/)
  await expect(page.getByText('agent-session-1')).toBeVisible()
  expect(callsRequests.length).toBeGreaterThan(0)
  expect(callsRequests[0]).not.toContain('start=')
  expect(callsRequests[0]).not.toContain('end=')
})

test('在线模型删除保护：服务快捷入口与先下线引导，离线模型可删除', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)

  // 可变状态：在线模型（含服务摘要）+ 已下线模型各一
  let rows = [
    {
      ...projectRow,
      service_name: '台区负荷短期推演服务',
      service_endpoint: '/api/v2/world-model/services/svc-1/invoke',
      service_version_no: 1,
    },
    {
      ...projectRow,
      id: 'wm-project-2',
      name: '潮流离线模型',
      service_status: 'offline',
      service_name: '潮流离线服务',
      service_endpoint: '/api/v2/world-model/services/svc-2/invoke',
      service_version_no: 3,
    },
  ]
  const deleteRequests: string[] = []
  await page.route('**/api/v2/world-model/**', route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path === '/api/v2/world-model/projects' && request.method() === 'GET') {
      return json(route, { items: rows, total: rows.length })
    }
    if (path === '/api/v2/world-model/projects/wm-project-2' && request.method() === 'DELETE') {
      deleteRequests.push(path)
      rows = rows.filter(row => row.id !== 'wm-project-2')
      return json(route, { status: 'deleted', id: 'wm-project-2' })
    }
    return json(route, [])
  })
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/world-model/models')

  // 在线模型卡片：服务快捷入口展示名称/版本，点击进入推演服务页
  const serviceChip = page.getByRole('button', { name: /台区负荷短期推演服务 · v1/ })
  await expect(serviceChip).toBeVisible()
  await serviceChip.click()
  await expect(page).toHaveURL(/#\/world-model\/services$/)
  await page.goto('/#/world-model/models')

  // 在线模型删除：弹窗提示先下线，不开放删除按钮，可直达推演服务页
  await page.getByRole('button', { name: '删除 台区负荷短期推演' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByText(/当前在线/)).toBeVisible()
  await expect(dialog.getByRole('button', { name: '删除模型' })).toHaveCount(0)
  await dialog.getByRole('button', { name: '前往推演服务' }).click()
  await expect(page).toHaveURL(/#\/world-model\/services$/)
  expect(deleteRequests).toHaveLength(0)

  // 已下线模型：确认弹窗提示服务一并移除，确认后真正发出删除请求
  await page.goto('/#/world-model/models')
  await page.getByRole('button', { name: '删除 潮流离线模型' }).click()
  await expect(page.getByText(/已下线的推演服务「潮流离线服务」/)).toBeVisible()
  await page.getByRole('button', { name: '删除模型' }).click()
  await expect(page.getByText('潮流离线模型')).toHaveCount(0)
  expect(deleteRequests).toHaveLength(1)
})

test('推演模型列表服务端分页：翻页请求携带页码', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)

  const allRows = Array.from({ length: 13 }, (_, index) => ({
    ...projectRow,
    id: `wm-p-${index + 1}`,
    name: `模型-${String(index + 1).padStart(2, '0')}`,
    service_status: null,
    service_name: null,
    service_endpoint: null,
    service_version_no: null,
  }))
  const requestedPages: number[] = []
  await page.route('**/api/v2/world-model/**', route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path === '/api/v2/world-model/projects' && request.method() === 'GET') {
      const url = new URL(request.url())
      const pageParam = Number(url.searchParams.get('page') || '1')
      const sizeParam = Number(url.searchParams.get('size') || '12')
      requestedPages.push(pageParam)
      const start = (pageParam - 1) * sizeParam
      return json(route, { items: allRows.slice(start, start + sizeParam), total: allRows.length })
    }
    return json(route, [])
  })
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/world-model/models')

  await expect(page.getByText('共 13 个模型')).toBeVisible()
  await expect(page.getByText('1 / 2')).toBeVisible()
  await expect(page.getByText('模型-13')).toHaveCount(0)

  await page.getByRole('button', { name: '下一页' }).click()
  await expect(page.getByText('2 / 2')).toBeVisible()
  await expect(page.getByText('模型-13')).toBeVisible()
  expect(requestedPages).toContain(2)
})

test('开发页：执行通过才可保存，版本恢复需二次确认', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)
  await mockWorldModel(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(`/#/world-model/models/${projectRow.id}/develop`)

  // 初始：未修改，保存不可用
  const saveButton = page.getByRole('button', { name: '保存', exact: true })
  await expect(saveButton).toBeDisabled()

  // 修改测试入参（含 JSON 布尔/null，回归场景）
  await page.getByLabel('测试入参 JSON').fill(JSON.stringify({
    context: { current_value: 100, online: true, note: null },
    actions: [],
    horizon: 3,
  }))
  // 修改脚本（dirty=true）；任何修改都会让执行凭证失效
  await page.locator('.cm-content').first().click()
  await page.keyboard.press('End')
  await page.keyboard.type('# tweak')
  await expect(saveButton).toBeDisabled()

  // 执行通过后保存才可用
  await page.getByRole('button', { name: '执行', exact: true }).click()
  await expect(page.getByText('simulate 返回值')).toBeVisible()
  await expect(page.getByText('适用边界：仅适用于居民用户')).toBeVisible()
  await expect(saveButton).toBeEnabled()

  // 保存成功出现版本号提示
  await saveButton.click()
  await expect(page.getByText('已保存为版本 v2')).toBeVisible()

  // 版本恢复弹二次确认，取消不覆盖编辑器
  await page.getByRole('button', { name: '历史版本' }).click()
  await page.getByRole('button', { name: '恢复' }).first().click()
  await expect(page.getByText('恢复到该历史版本？')).toBeVisible()
  await page.getByRole('button', { name: '取消' }).click()
  await expect(page.getByText('恢复到该历史版本？')).toHaveCount(0)
})

test('开发页：执行结果自动绘制轨迹折线，原始 JSON 折叠保留', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)
  await mockWorldModel(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(`/#/world-model/models/${projectRow.id}/develop`)

  // 契约返回数值 trajectory + confidence + boundary 时：折线预览 + 摘要行
  await page.getByRole('button', { name: '执行', exact: true }).click()
  await expect(page.getByText('simulate 返回值 · 轨迹预览')).toBeVisible()
  await expect(page.getByText('轨迹 3 点')).toBeVisible()
  await expect(page.getByText('置信度 0.8')).toBeVisible()
  await expect(page.getByText('适用边界：仅适用于居民用户')).toBeVisible()
  await expect(page.locator('section', { hasText: '执行结果' }).locator('canvas').first()).toBeVisible()

  // 原始 JSON 默认折叠，展开后可核对
  const rawDetails = page.locator('details', { hasText: '原始返回值（JSON）' })
  await expect(rawDetails.locator('pre')).toBeHidden()
  await rawDetails.locator('summary').click()
  await expect(rawDetails.locator('pre')).toBeVisible()
  await expect(rawDetails.locator('pre')).toContainText('"trajectory"')
})

test('开发页：历史版本可先查看脚本与入参，恢复时一并回退测试入参', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)
  await mockWorldModel(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(`/#/world-model/models/${projectRow.id}/develop`)

  // 展开查看：懒加载该版脚本与当时测试入参
  await page.getByRole('button', { name: '历史版本' }).click()
  await page.getByRole('button', { name: '查看', exact: true }).click()
  await expect(page.getByText('该版脚本')).toBeVisible()
  await expect(page.locator('pre', { hasText: 'return {"trajectory": [9, 9]}' })).toBeVisible()
  await expect(page.getByText('该版测试入参')).toBeVisible()

  // 确认恢复：编辑器与测试入参一并回退到该版
  await page.getByRole('button', { name: '恢复' }).first().click()
  const restoreConfirm = page.getByRole('dialog', { name: '恢复到该历史版本？' })
  await expect(restoreConfirm).toBeVisible()
  // 标准确认弹窗内的「恢复」（抽屉列表项同名按钮仍在 DOM 中，需按弹窗收敛）
  await restoreConfirm.getByRole('button', { name: '恢复', exact: true }).click()
  await expect(page.getByText('已恢复 v1 的脚本内容')).toBeVisible()
  const editor = page.locator('.cm-content').first()
  await expect(editor).toContainText('return {"trajectory": [9, 9]}')
  await expect(page.getByLabel('测试入参 JSON')).toHaveValue(/"horizon": 3/)
})

test('开发页：测试入参即时校验定位错误，放大编辑区一键切换', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)
  await mockWorldModel(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(`/#/world-model/models/${projectRow.id}/develop`)

  const inputArea = page.getByLabel('测试入参 JSON')

  // 非法 JSON：输入过程即时报错（不等点击执行）
  await inputArea.fill('{ "context": }')
  await expect(page.getByText(/测试入参 JSON 无效：/)).toBeVisible()
  await expect(page.getByText('JSON 无效', { exact: true })).toBeVisible()

  // 顶层为数组同样被拦截
  await inputArea.fill('[1, 2]')
  await expect(page.getByText('顶层必须是 JSON 对象（context / actions / horizon）')).toBeVisible()

  // 合法化后行内错误消失
  await inputArea.fill('{ "context": { "current_value": 1 }, "actions": [], "horizon": 3 }')
  await expect(page.getByText(/测试入参 JSON 无效：/)).toHaveCount(0)

  // 放大编辑区：整列让给入参、结果面板隐藏；执行时自动收回展示结果
  await page.getByRole('button', { name: '放大入参编辑区' }).click()
  await expect(page.getByRole('button', { name: '收起入参编辑区' })).toBeVisible()
  await expect(page.getByText('点击「执行」在内核中试运行')).toHaveCount(0)
  await page.getByRole('button', { name: '执行', exact: true }).click()
  await expect(page.getByText('simulate 返回值 · 轨迹预览')).toBeVisible()
  await expect(page.getByRole('button', { name: '放大入参编辑区' })).toBeVisible()
})

test('保存首个版本后发布按钮立即可用（无需刷新页面）', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)
  // 场景回归：项目 version_count=0 时发布按钮禁用；
  // 保存成功必须立即解锁，不能要求用户刷新页面。
  await page.route('**/api/v2/world-model/**', route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path === `/api/v2/world-model/projects/${projectRow.id}` && request.method() === 'GET') {
      return json(route, { ...projectDetail, version_count: 0 })
    }
    if (path.endsWith('/execute') && request.method() === 'POST') {
      return json(route, { ok: true, payload: { trajectory: [1] }, stdout: '', error: null, traceback: '', duration_ms: 10, kernel_id: 'k' })
    }
    if (path.endsWith('/save') && request.method() === 'POST') {
      return json(route, { ok: true, version_no: 1, execution: null })
    }
    if (path.endsWith('/service') && request.method() === 'GET') {
      return json(route, null)
    }
    return json(route, [])
  })
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(`/#/world-model/models/${projectRow.id}/develop`)

  const publishButton = page.getByRole('button', { name: '发布', exact: true })
  await expect(publishButton).toBeDisabled()

  // 修改脚本后执行通过并保存
  await page.locator('.cm-content').first().click()
  await page.keyboard.press('End')
  await page.keyboard.type('# v1')
  await page.getByRole('button', { name: '执行', exact: true }).click()
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByText('已保存为版本 v1')).toBeVisible()

  // 缺陷修复点：保存成功后版本数刷新，发布按钮无需刷新即可点击
  await expect(publishButton).toBeEnabled()
})

test('时序示例：一键插入官方 ARIMA 模板并替换测试入参', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)
  await mockWorldModel(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(`/#/world-model/models/${projectRow.id}/develop`)

  const insertButton = page.getByRole('button', { name: '时序示例', exact: true })
  await expect(insertButton).toBeVisible()

  // 编辑器干净（无未保存修改）时直接插入，不弹确认
  await insertButton.click()
  const editor = page.locator('.cm-content').first()
  await expect(editor).toContainText('statsmodels')
  const inputArea = page.getByLabel('测试入参 JSON')
  await expect(inputArea).toHaveValue(/"period": 12/)

  // 插入后需重新执行通过才能保存（与保存纪律一致）
  const saveButton = page.getByRole('button', { name: '保存', exact: true })
  await expect(saveButton).toBeDisabled()
  await page.getByRole('button', { name: '执行', exact: true }).click()
  await expect(saveButton).toBeEnabled()

  // 编辑器与已保存脚本不同（dirty）时先弹二次确认；取消不改动内容
  await editor.click()
  await page.keyboard.press('End')
  await page.keyboard.type('# dirty')
  await insertButton.click()
  await expect(page.getByText('插入时序推演示例？')).toBeVisible()
  await page.getByRole('button', { name: '取消' }).click()
  await expect(page.getByText('插入时序推演示例？')).toHaveCount(0)
  await expect(editor).toContainText('# dirty')

  // 再次插入并确认后，示例模板覆盖当前内容
  await insertButton.click()
  await expect(page.getByText('插入时序推演示例？')).toBeVisible()
  await page.getByRole('button', { name: '插入示例' }).click()
  await expect(page.getByText('插入时序推演示例？')).toHaveCount(0)
  await expect(editor).toContainText('statsmodels')
  await expect(editor).not.toContainText('# dirty')
  // 内容替换后执行凭证失效，需重新执行
  await expect(saveButton).toBeDisabled()
})

test('发布为推演服务：语义注册、服务面板与状态切换', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)

  let published: Record<string, unknown> | null = null
  const serviceOut = (status: string) => ({
    id: 'svc-1',
    project_id: projectRow.id,
    version_id: 'v1',
    version_no: 1,
    name: '台区负荷短期推演服务',
    description: '',
    status,
    endpoint_path: '/api/v2/world-model/services/svc-1/invoke',
    applicable_object_types: { ontology_id: 'ontology-1', object_type_ids: ['ot-line'] },
    preconditions: [],
    created_at: now,
    updated_at: now,
  })

  await page.route('**/api/v1/ontologies**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/ontologies') {
      return json(route, {
        items: [{ id: 'ontology-1', name: '供应链本体', current_release_id: 'release-1' }],
        total: 1, page: 1, page_size: 200,
      })
    }
    if (path === '/api/v1/ontologies/ontology-1/entities') {
      return json(route, [{ id: 'ot-line', name_cn: '线路' }, { id: 'ot-user', name_cn: '用户' }])
    }
    return json(route, [])
  })
  await page.route('**/api/v2/world-model/**', route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path === `/api/v2/world-model/projects/${projectRow.id}` && request.method() === 'GET') {
      return json(route, {
        ...projectDetail,
        version_count: 1,
        service_status: published ? (published as { status: string }).status : null,
      })
    }
    if (path === `/api/v2/world-model/projects/${projectRow.id}/versions`) {
      return json(route, [{ id: 'v1', version_no: 1, duration_ms: 40, created_at: now }])
    }
    if (path === `/api/v2/world-model/projects/${projectRow.id}/service` && request.method() === 'GET') {
      return json(route, published)
    }
    // 多本体发布：开发页服务面板从复数端点拉取项目全部已发布服务
    if (path === `/api/v2/world-model/projects/${projectRow.id}/services` && request.method() === 'GET') {
      return json(route, published ? [published] : [])
    }
    if (path === `/api/v2/world-model/projects/${projectRow.id}/publish` && request.method() === 'POST') {
      published = serviceOut('online')
      return json(route, published, 201)
    }
    if (path === `/api/v2/world-model/projects/${projectRow.id}/service/status`) {
      const body = request.postDataJSON() as { status: string }
      published = serviceOut(body.status)
      return json(route, published)
    }
    // 面板内逐服务上下线走服务级端点
    if (path === '/api/v2/world-model/services/svc-1/status' && request.method() === 'POST') {
      const body = request.postDataJSON() as { status: string }
      published = serviceOut(body.status)
      return json(route, published)
    }
    return json(route, [])
  })

  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(`/#/world-model/models/${projectRow.id}/develop`)

  // 打开发布对话框，完成语义注册
  await page.getByRole('button', { name: '发布', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByText('发布为推演服务')).toBeVisible()
  await dialog.getByLabel('服务名称').fill('台区负荷短期推演服务')
  await dialog.getByLabel('选择所属本体').click()
  await page.getByRole('option', { name: '供应链本体' }).click()
  await dialog.getByRole('checkbox', { name: '线路' }).click()
  await dialog.getByRole('button', { name: '发布并上线' }).click()

  // 服务面板：端点、状态、切换
  const panel = page.getByTestId('world-model-service-panel')
  await expect(panel).toBeVisible()
  await expect(panel.getByText('在线')).toBeVisible()
  await expect(panel.getByText('/api/v2/world-model/services/svc-1/invoke')).toBeVisible()
  await panel.getByRole('button', { name: '下线' }).click()
  await expect(panel.getByText('已下线')).toBeVisible()
  // 重新发布入口存在
  await expect(page.getByRole('button', { name: '重新发布' })).toBeVisible()
})

test('推演服务页：注册表、状态切换、试调用与详情', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)
  await mockWorldModel(page)
  await mockWorldModelServices(page)
  // 详情抽屉需要解析本体名与对象类型名
  await page.route('**/api/v1/ontologies**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/ontologies') {
      return json(route, {
        items: [{ id: 'ontology-1', name: '供应链本体', current_release_id: 'release-1' }],
        total: 1, page: 1, page_size: 200,
      })
    }
    if (path === '/api/v1/ontologies/ontology-1/entities') {
      return json(route, [{ id: 'ot-line', name_cn: '线路' }, { id: 'ot-user', name_cn: '用户' }])
    }
    return json(route, [])
  })
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/world-model/services')

  // 注册表渲染：名称 / 所属模型 / 版本 / 状态 / 调用统计 / 端点动作
  const table = page.getByRole('table')
  await expect(page.getByText('台区负荷短期推演服务')).toBeVisible()
  await expect(table.getByText('v1')).toBeVisible()
  await expect(table.getByText('在线')).toBeVisible()
  await expect(page.getByText('共 1 个推演服务')).toBeVisible()
  // 概览统计条：服务总数/在线/总调用/成功率/平均耗时
  await expect(page.getByText('服务总数')).toBeVisible()
  await expect(page.getByText('全局成功率')).toBeVisible()
  await expect(page.getByText('平均耗时')).toBeVisible()

  // 下线切换：先弹二次确认（端点立即不可访问的提示），确认后状态徽标随之更新，试调用按钮禁用
  const powerButton = page.getByRole('button', { name: '下线 台区负荷短期推演服务' })
  await powerButton.click()
  const offlineDialog = page.getByRole('dialog')
  await expect(offlineDialog.getByText(/调用端点将立即不可访问/)).toBeVisible()
  await offlineDialog.getByRole('button', { name: '确认下线' }).click()
  await expect(table.getByText('已下线')).toBeVisible()
  await expect(page.getByRole('button', { name: '试调用 台区负荷短期推演服务' })).toBeDisabled()

  // 重新上线后试调用：入参编辑 → 调用 → 轨迹与耗时展示
  await page.getByRole('button', { name: '上线 台区负荷短期推演服务' }).click()
  await expect(table.getByText('在线')).toBeVisible()
  await page.getByRole('button', { name: '试调用 台区负荷短期推演服务' }).click()
  const invokeDialog = page.getByRole('dialog')
  await expect(invokeDialog.getByText('试调用推演服务')).toBeVisible()
  // curl 外部调用示例：完整 URL（协议+主机）与鉴权头，可直接粘贴执行
  await expect(invokeDialog.getByText(/curl -X POST "http:\/\//)).toBeVisible()
  await expect(invokeDialog.getByText(/Authorization: Bearer/)).toBeVisible()
  await expect(invokeDialog.getByRole('button', { name: '复制 curl 示例' })).toBeVisible()
  // 非法 JSON：输入即时校验并提示语法错误，无需提交才发现
  await invokeDialog.getByLabel('试调用测试入参 JSON').fill('{"context": broken}')
  await expect(invokeDialog.getByText(/JSON 语法错误/)).toBeVisible()
  await invokeDialog.getByLabel('试调用测试入参 JSON').fill('{"context": {"current_value": 100}, "actions": [], "horizon": 3}')
  await invokeDialog.getByRole('button', { name: '调用' }).click()
  await expect(invokeDialog.getByText('调用成功 · 18 ms')).toBeVisible()
  await expect(invokeDialog.getByText('trajectory')).toBeVisible()
  // 空轨迹（被边界拒绝）：调用成功但明确警示并给出边界说明，不再显示为纯绿色成功
  await invokeDialog.getByLabel('试调用测试入参 JSON').fill('{"context": {"series": [1]}, "actions": [], "horizon": 1}')
  await invokeDialog.getByRole('button', { name: '调用' }).click()
  await expect(invokeDialog.getByText(/未产生预测输出/)).toBeVisible()
  await expect(invokeDialog.getByText(/边界说明：context\.series 必须是长度 >= 12 的数值列表。/)).toBeVisible()
  await invokeDialog.getByRole('button', { name: '关闭', exact: true }).click()

  // 详情抽屉：语义注册 + 该服务调用记录 + 端点完整展示（可复制）
  await page.getByRole('button', { name: '查看 台区负荷短期推演服务 详情' }).click()
  const detailDialog = page.getByRole('dialog', { name: '推演服务详情' })
  await expect(detailDialog.getByText('本体语义注册')).toBeVisible()
  await expect(detailDialog.getByText('线路 ≥ 12')).toBeVisible()
  await expect(detailDialog.getByText('最近调用（共 1 条）')).toBeVisible()
  await expect(detailDialog.getByRole('button', { name: '复制调用端点' })).toBeVisible()
  // 下钻：一键进入按本服务过滤的调用记录页
  await detailDialog.getByRole('button', { name: '查看全部' }).click()
  await expect(page).toHaveURL(/#\/world-model\/calls\?service_id=svc-registry-1/)
  await expect(page.getByText('服务：台区负荷短期推演服务')).toBeVisible()
})

test('无 world_model 菜单权限的用户不可见且直达被拒', async ({ page }) => {
  await seedAuth(page, 'custom', ['overview'])
  await mockPlatformShell(page)
  await mockWorldModel(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/overview')

  const sidebar = page.locator('aside')
  await expect(sidebar.getByText('世界模型', { exact: true })).toHaveCount(0)
  // 本体管理单项对该用户也不可见（未授权），但不影响世界模型断言
  await page.goto('/#/world-model/models')
  await expect(page.getByText('当前页面无法访问')).toBeVisible()
})
