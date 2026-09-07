import { expect, test, type Page, type Route } from '@playwright/test'

const now = '2026-08-12T08:00:00+00:00'

const json = (route: Route, data: unknown, status = 200) => route.fulfill({
  status,
  contentType: 'application/json',
  body: JSON.stringify({ data }),
})

const conversationOne = {
  id: 'conversation-1',
  title: '会话一',
  model_config_id: 'model-1',
  status: 'active',
  created_at: now,
  updated_at: now,
}
const conversationTwo = {
  id: 'conversation-2',
  title: '会话二',
  model_config_id: 'model-1',
  status: 'active',
  created_at: now,
  updated_at: now,
}

const messageOf = (conversationId: string, id: string, role: 'user' | 'assistant', content: string) => ({
  id,
  conversation_id: conversationId,
  role,
  content,
  status: 'complete',
  steps: [],
  token_usage: {},
  created_at: now,
})

const sseBody = [
  'event: meta\ndata: {"conversationId":"conversation-1","assistantMessageId":"assistant-live"}',
  'event: thinking\ndata: {"round":1}',
  'event: tool_start\ndata: {"toolRunId":"run-1","toolName":"use_skill","arguments":{"skill":"demo"}}',
  'event: tool_result\ndata: {"toolRunId":"run-1","status":"success","preview":"读取成功"}',
  'event: text_delta\ndata: {"delta":"流式最终答复"}',
  'event: message_end\ndata: {"message":{"id":"assistant-live","content":"流式最终答复","steps":[{"toolName":"use_skill","status":"success","arguments":{"skill":"demo"},"preview":"读取成功"}],"tokenUsage":{"inputTokens":10}}}',
  'event: done\ndata: {}',
  '',
].join('\n\n')

async function seedAuth(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: { id: 'u-widget', username: 'widget-tester', role: 'admin' },
      },
      version: 0,
    }))
  })
}

/** 平台外壳兜底：未显式声明的接口一律返回空集合，避免触达真实后端 */
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

interface WidgetMockOptions { denyMenu?: boolean; hiddenMenuKeys?: string[] }

async function mockSuperAssistant(page: Page, options: WidgetMockOptions = {}) {
  let chatCompleted = false
  const savedConfig = { hidden_menu_keys: options.hiddenMenuKeys ?? [], updated_at: now }
  await page.route('**/api/v2/super-assistant/**', route => {
    const request = route.request()
    const path = new URL(request.url()).pathname

    if (path === '/api/v2/super-assistant/widget-config') {
      if (request.method() === 'PUT') {
        savedConfig.hidden_menu_keys = (request.postDataJSON() as { hidden_menu_keys?: string[] })?.hidden_menu_keys ?? []
      }
      return json(route, savedConfig)
    }
    if (path === '/api/v2/super-assistant/conversations' && request.method() === 'GET') {
      if (options.denyMenu) {
        return route.fulfill({
          status: 403,
          contentType: 'application/json',
          body: JSON.stringify({
            detail: { code: 'MENU_ACCESS_DENIED', message: '当前角色无权访问此功能', menu_key: 'super_assistant' },
          }),
        })
      }
      return json(route, [conversationOne, conversationTwo])
    }
    if (path === '/api/v2/super-assistant/conversations' && request.method() === 'POST') {
      return json(route, { ...conversationOne, id: 'conversation-new', title: '新会话' }, 201)
    }
    if (path === '/api/v2/super-assistant/conversations/conversation-1/messages') {
      return json(route, chatCompleted
        ? [
            messageOf('conversation-1', 'user-live', 'user', '介绍一下平台'),
            {
              ...messageOf('conversation-1', 'assistant-live', 'assistant', '流式最终答复'),
              steps: [{ toolName: 'use_skill', status: 'success', arguments: { skill: 'demo' }, preview: '读取成功' }],
              token_usage: { inputTokens: 10 },
            },
          ]
        : [
            messageOf('conversation-1', 'user-1', 'user', '你好'),
            messageOf('conversation-1', 'assistant-1', 'assistant', '这是**会话一**的历史答复'),
          ])
    }
    if (path === '/api/v2/super-assistant/conversations/conversation-2/messages') {
      return json(route, [
        messageOf('conversation-2', 'user-2', 'user', '第二个问题'),
        messageOf('conversation-2', 'assistant-2', 'assistant', '第二个会话的内容'),
      ])
    }
    if (path === '/api/v2/super-assistant/conversations/conversation-1/chat' && request.method() === 'POST') {
      chatCompleted = true
      return route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
        body: sseBody,
      })
    }
    if (path.endsWith('/cancel')) return json(route, {}, 202)
    return json(route, [])
  })
}

test('悬浮入口在所有业务页面常驻且可开合', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)
  await mockSuperAssistant(page)
  await page.setViewportSize({ width: 1280, height: 900 })

  await page.goto('/#/overview')
  const fab = page.getByTestId('assistant-widget-fab')
  await expect(fab).toBeVisible()

  await page.goto('/#/models')
  await expect(fab).toBeVisible()

  await fab.click()
  const panel = page.getByTestId('assistant-widget-panel')
  await expect(panel).toBeVisible()
  await panel.getByRole('button', { name: '关闭 AI 助手' }).click()
  await expect(page.getByTestId('assistant-widget-panel')).toHaveCount(0)
  await expect(fab).toBeVisible()
})

test('面板加载历史消息并以 Markdown 渲染', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)
  await mockSuperAssistant(page)
  await page.setViewportSize({ width: 1280, height: 900 })

  await page.goto('/#/overview')
  await page.getByTestId('assistant-widget-fab').click()

  const panel = page.getByTestId('assistant-widget-panel')
  await expect(panel.getByText('你好', { exact: true })).toBeVisible()
  await expect(panel.locator('strong').filter({ hasText: '会话一' })).toBeVisible()
})

test('发送消息后渲染思考链与流式答复', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)
  await mockSuperAssistant(page)
  await page.setViewportSize({ width: 1280, height: 900 })

  await page.goto('/#/overview')
  await page.getByTestId('assistant-widget-fab').click()
  const panel = page.getByTestId('assistant-widget-panel')
  await expect(panel.getByText('这是会话一的历史答复')).toBeVisible()

  await panel.getByPlaceholder('输入消息，Enter 发送 / Shift+Enter 换行').fill('介绍一下平台')
  await panel.getByPlaceholder('输入消息，Enter 发送 / Shift+Enter 换行').press('Enter')

  // 思考链：工具调用步骤可见；流式结束后展示最终答复（含服务端回刷的持久化结果）
  await expect(panel.getByText('use_skill')).toBeVisible()
  await expect(panel.getByText('流式最终答复').first()).toBeVisible()
  await expect(panel.getByText('介绍一下平台').first()).toBeVisible()
})

test('历史会话浮层可切换会话', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)
  await mockSuperAssistant(page)
  await page.setViewportSize({ width: 1280, height: 900 })

  await page.goto('/#/overview')
  await page.getByTestId('assistant-widget-fab').click()
  const panel = page.getByTestId('assistant-widget-panel')
  await expect(panel.getByText('这是会话一的历史答复')).toBeVisible()

  await panel.getByRole('button', { name: '历史会话' }).click()
  await page.getByTestId('assistant-widget-history').getByText('会话二').click()

  await expect(panel.getByText('第二个会话的内容')).toBeVisible()
})

test('跳转完整页携带当前会话并直达同一会话', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)
  await mockSuperAssistant(page)
  await page.setViewportSize({ width: 1280, height: 900 })

  await page.goto('/#/overview')
  await page.getByTestId('assistant-widget-fab').click()
  const panel = page.getByTestId('assistant-widget-panel')
  await expect(panel.getByText('这是会话一的历史答复')).toBeVisible()

  await panel.getByRole('button', { name: '历史会话' }).click()
  await page.getByTestId('assistant-widget-history').getByText('会话二').click()
  await expect(panel.getByText('第二个会话的内容')).toBeVisible()

  await panel.getByTestId('assistant-widget-open-full').click()
  await expect(page).toHaveURL(/#\/super-assistant\?conversation=conversation-2/)
  // 完整页头部标题按钮显示目标会话，且历史消息一致（侧栏会话行同名，需限定在头部）
  await expect(page.locator('header').getByRole('button', { name: /会话二/ })).toBeVisible()
  await expect(page.getByText('第二个会话的内容')).toBeVisible()
  // 超级助手页即 AI 工作台前台，不再挂载悬浮入口（页面本身就是助手）
  await expect(page.getByTestId('assistant-widget-fab')).toHaveCount(0)
})

test('流式进行中点击停止可立即取消生成', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)

  let cancelCalled = false
  let releaseChat: () => void = () => undefined
  await page.route('**/api/v2/super-assistant/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path === '/api/v2/super-assistant/conversations' && request.method() === 'GET') {
      return json(route, [conversationOne])
    }
    if (path === '/api/v2/super-assistant/conversations/conversation-1/messages') {
      return json(route, [messageOf('conversation-1', 'user-1', 'user', '你好')])
    }
    if (path === '/api/v2/super-assistant/conversations/conversation-1/chat' && request.method() === 'POST') {
      // 保持连接不闭合，模拟长生成过程，直到测试显式释放
      await new Promise<void>(resolve => { releaseChat = resolve })
      return json(route, {})
    }
    if (path.endsWith('/cancel')) {
      cancelCalled = true
      return json(route, {}, 202)
    }
    return json(route, [])
  })

  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto('/#/overview')
  await page.getByTestId('assistant-widget-fab').click()
  const panel = page.getByTestId('assistant-widget-panel')
  const composer = panel.getByPlaceholder('输入消息，Enter 发送 / Shift+Enter 换行')
  await expect(composer).toBeVisible()

  await composer.fill('讲一个很长很长的故事')
  await composer.press('Enter')

  // 生成中：出现停止按钮；点击后本地立即取消，不等后端轮询感知
  const stopButton = panel.locator('button[class*="loading-button"]')
  await expect(stopButton).toBeVisible()
  await stopButton.click()

  await expect(panel.getByText('已停止生成')).toBeVisible()
  await expect(stopButton).toHaveCount(0)
  await expect(composer).toHaveValue('')
  expect(cancelCalled).toBe(true)

  releaseChat()
})

test('无菜单权限时面板展示不可用提示且输入被禁用', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)
  await mockSuperAssistant(page, { denyMenu: true })
  await page.setViewportSize({ width: 1280, height: 900 })

  await page.goto('/#/overview')
  await page.getByTestId('assistant-widget-fab').click()

  const panel = page.getByTestId('assistant-widget-panel')
  await expect(panel.getByText('当前账号暂无 AI 助手使用权限，请联系管理员开通。')).toBeVisible()
  await expect(panel.getByPlaceholder('输入消息，Enter 发送 / Shift+Enter 换行')).toBeDisabled()
})

test('本体助手未选本体时悬浮窗盖过拓扑卡片轮播（层级回归）', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)
  await mockSuperAssistant(page)
  // MYW-51 左右互换后：未选本体时卡片轮播随本体拓扑卡位于左列（宽侧），
  // 全局悬浮窗固定在视口右缘；两者仅在窄视口下水平相交（左列最小宽 560 +
  // 右列最小宽 420 使更宽视口必然把悬浮窗挤进独立区域）。因此在 720px 宽度下
  // 取交叠区域中心，断言该点最上层元素属于悬浮窗而非轮播卡（z-index 回归守卫）。
  await page.route(/\/api\/v1\/ontologies(\?|$)/, route => json(route, {
    items: [
      {
        id: 'ontology-1', name: '供应链本体', domain: '供应链', description: '供应链本体描述',
        status: 'draft', version: 'v1', current_release_id: 'release-ontology-1',
        current_release_version: 'v1', assistant_card_clicks: 7,
        entity_count: 2, relation_count: 1, action_count: 1, sentinel_count: 1,
        created_at: now, updated_at: now,
      },
      {
        id: 'ontology-2', name: '医疗健康本体', domain: '医疗', description: '医疗健康本体描述',
        status: 'draft', version: 'v1', current_release_id: 'release-ontology-2',
        current_release_version: 'v1', assistant_card_clicks: 3,
        entity_count: 2, relation_count: 1, action_count: 1, sentinel_count: 0,
        created_at: now, updated_at: now,
      },
    ],
    total: 2,
    page: 1,
    page_size: 1000,
  }))
  await page.setViewportSize({ width: 720, height: 900 })

  await page.goto('/#/agent')
  await expect(page.getByTestId('ontology-card-carousel')).toBeVisible()

  await page.getByTestId('assistant-widget-fab').click()
  const panel = page.getByTestId('assistant-widget-panel')
  await expect(panel).toBeVisible()

  // 取面板与居中卡片的交叠区域中心，断言该点最上层元素属于悬浮窗而非卡片
  const panelBox = await panel.boundingBox()
  const cardBox = await page.locator('[data-card-index="0"]').boundingBox()
  if (!panelBox || !cardBox) throw new Error('bounding box missing')
  const overlapX = Math.min(panelBox.x + panelBox.width, cardBox.x + cardBox.width) - Math.max(panelBox.x, cardBox.x)
  const overlapY = Math.min(panelBox.y + panelBox.height, cardBox.y + cardBox.height) - Math.max(panelBox.y, cardBox.y)
  expect(overlapX).toBeGreaterThan(0)
  expect(overlapY).toBeGreaterThan(0)
  const x = Math.max(panelBox.x, cardBox.x) + overlapX / 2
  const y = Math.max(panelBox.y, cardBox.y) + overlapY / 2

  const topmost = await page.evaluate(({ x, y }) => {
    const stack = document.elementsFromPoint(x, y)
    for (const el of stack) {
      const node = el as HTMLElement
      if (node.closest?.('[data-card-index]')) return 'card'
      if (node.closest?.('[data-testid="assistant-widget-panel"]')) return 'panel'
    }
    return 'other'
  }, { x, y })
  expect(topmost).toBe('panel')
})

test('管理员配置的隐藏目录不再渲染悬浮入口', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)
  // 平台级配置：事件登记（一级）、模型配置（一级）、领域设置（二级）隐藏悬浮助手
  await mockSuperAssistant(page, { hiddenMenuKeys: ['events', 'models', 'settings.domains'] })
  await page.setViewportSize({ width: 1280, height: 900 })

  const fab = page.getByTestId('assistant-widget-fab')

  // 先在未配置隐藏的页面确认悬浮入口已正常挂载
  await page.goto('/#/overview')
  await expect(fab).toBeVisible()

  // 隐藏的一级目录下悬浮入口随路由切换被移除
  await page.goto('/#/events')
  await expect(fab).toHaveCount(0)
  await page.goto('/#/models')
  await expect(fab).toHaveCount(0)

  // 隐藏的二级目录同样生效（其余系统设置页不受影响）
  await page.goto('/#/settings/domains')
  await expect(fab).toHaveCount(0)

  // 超级助手页为 AI 工作台前台（裸布局），不挂载悬浮入口
  await page.goto('/#/super-assistant')
  await expect(fab).toHaveCount(0)

  // 切回未隐藏页面时恢复显示
  await page.goto('/#/overview')
  await expect(fab).toBeVisible()
})

test('系统设置-超级助手页可勾选目录并保存显示范围', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)
  await mockSuperAssistant(page)
  await page.setViewportSize({ width: 1280, height: 900 })

  await page.goto('/#/settings/assistant-widget')
  await expect(page.getByText('悬浮 AI 助手的页面显示范围')).toBeVisible()

  const tree = page.getByTestId('assistant-widget-visibility-tree')
  await expect(tree).toBeVisible()
  // 目录树与左侧导航一致：一级目录及其二级菜单都渲染出来
  await expect(tree.getByRole('checkbox', { name: /事件登记/ })).toBeChecked()
  await expect(tree.getByRole('checkbox', { name: '数据流水线' })).toBeChecked()

  // 取消勾选 事件登记（叶子目录），保存后 PUT 携带隐藏名单
  await tree.getByRole('checkbox', { name: /事件登记/ }).click()
  const putRequest = page.waitForRequest(request =>
    request.url().endsWith('/api/v2/super-assistant/widget-config') && request.method() === 'PUT')
  await page.getByRole('button', { name: '保存配置' }).click()
  const body = (await putRequest).postDataJSON() as { hidden_menu_keys: string[] }
  expect(body.hidden_menu_keys).toEqual(['events'])
  await expect(page.getByRole('status')).toContainText('已保存')

  // 一级目录整体取消勾选 → 其全部二级菜单进入隐藏名单
  await tree.getByRole('checkbox', { name: /数据集成/ }).click()
  await expect(tree.getByRole('checkbox', { name: '数据流水线' })).not.toBeChecked()
  await expect(tree.getByRole('checkbox', { name: '数据任务池' })).not.toBeChecked()
  await expect(tree.getByRole('checkbox', { name: '数据资产湖' })).not.toBeChecked()

  const putRequest2 = page.waitForRequest(request =>
    request.url().endsWith('/api/v2/super-assistant/widget-config') && request.method() === 'PUT')
  await page.getByRole('button', { name: '保存配置' }).click()
  const body2 = (await putRequest2).postDataJSON() as { hidden_menu_keys: string[] }
  expect(body2.hidden_menu_keys).toEqual(['events', 'data.pipelines', 'data.sync_tasks', 'data.structured'])
})
