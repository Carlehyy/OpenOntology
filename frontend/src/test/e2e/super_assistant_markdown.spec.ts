import { expect, test, type Page, type Route } from '@playwright/test'

import {
  STACK_ADMIN_PASSWORD,
  STACK_ADMIN_USERNAME,
} from './support/stack-credentials'

const now = '2026-07-19T08:00:00+00:00'

const wrappedMarkdown = `\`\`\`\`markdown
### 三级标题

**粗体文本**、*斜体文本*以及 ~~删除线~~。

> 这是一段引用文字。

- 无序项
- [x] 已完成
- [ ] 待办事项

| 名称 | 类型 | 备注 |
| --- | --- | --- |
| 项目 A | 工具 | 开源免费 |

\`\`\`python
def hello():
    print("Hello, World!")
\`\`\`

[OpenOntology](https://openontology.org)
\`\`\`\`

围栏外的补充说明。`

async function loginAsAdmin(page: Page) {
  await page.goto('/#/login')
  await page.getByLabel('用户名', { exact: true }).fill(STACK_ADMIN_USERNAME)
  await page.getByLabel('密码', { exact: true }).fill(STACK_ADMIN_PASSWORD)
  await page.locator('button[type="submit"]').click()
  await page.waitForURL('**/#/super-assistant')
}

async function mockSuperAssistant(page: Page) {
  await loginAsAdmin(page)

  const json = (route: Route, data: unknown) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ data }),
  })

  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/models') return json(route, [{
      id: 'model-1',
      name: 'DeepSeek',
      config_type: 'llm',
      provider: 'deepseek',
      api_base: 'https://api.deepseek.com',
      has_api_key: true,
      enabled: true,
      is_default: true,
      last_test_status: 'success',
      last_tested_at: now,
      last_test_message: 'ok',
      models: ['deepseek-v4-pro'],
      options: { max_context_tokens: 100_000 },
      created_by: 'admin',
      created_at: now,
      updated_at: now,
    }])
    return route.continue()
  })

  await page.route('**/api/v2/super-assistant/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v2/super-assistant/conversations') return json(route, [{
      id: 'conversation-1',
      title: 'Markdown 渲染验证',
      model_config_id: 'model-1',
      status: 'active',
      created_at: now,
      updated_at: now,
    }])
    if (path === '/api/v2/super-assistant/conversations/conversation-1/messages') return json(route, [
      {
        id: 'user-1',
        conversation_id: 'conversation-1',
        role: 'user',
        content: '展示 Markdown',
        status: 'complete',
        steps: [],
        token_usage: {},
        created_at: now,
      },
      {
        id: 'assistant-1',
        conversation_id: 'conversation-1',
        role: 'assistant',
        content: wrappedMarkdown,
        status: 'complete',
        steps: [],
        token_usage: { inputTokens: 974, contextTokens: 974, contextLimit: 100_000 },
        created_at: now,
      },
    ])
    if (path === '/api/v2/super-assistant/skills') return json(route, [])
    if (path === '/api/v2/super-assistant/mcp-servers') return json(route, [])
    return route.fulfill({ status: 404, body: '{}' })
  })
}

test('渲染 Markdown 主体并在顶栏展示上下文用量', async ({ page }) => {
  await mockSuperAssistant(page)
  // 上下文胶囊仅在面板展开时的 ≥2xl 视口展示，用宽视口验证
  await page.setViewportSize({ width: 1600, height: 900 })
  await page.goto('/#/super-assistant')

  await expect(page.getByRole('heading', { name: '三级标题' })).toBeVisible()
  await expect(page.locator('strong').filter({ hasText: '粗体文本' })).toBeVisible()
  await expect(page.getByRole('columnheader', { name: '名称' })).toBeVisible()
  const codeBlock = page.locator('pre').filter({ hasText: 'def hello()' })
  await expect(codeBlock).toBeVisible()
  await expect(codeBlock).toHaveCSS('background-color', 'rgb(248, 250, 252)')
  await expect(codeBlock).toHaveCSS('color', 'rgb(51, 65, 85)')
  await expect(page.getByRole('link', { name: 'OpenOntology' })).toHaveAttribute('target', '_blank')
  await expect(page.getByText('围栏外的补充说明。')).toBeVisible()
  await expect(page.getByText('### 三级标题', { exact: true })).toHaveCount(0)

  await expect(page.getByTestId('super-assistant-context-usage')).toHaveAttribute(
    'aria-label',
    '上下文占比 1.0%，974 / 100k',
  )
})

test('标题编辑与顶部工具默认使用可识别的状态色', async ({ page }) => {
  await mockSuperAssistant(page)
  // 上下文胶囊仅在面板展开时的 ≥2xl 视口展示，用宽视口验证
  await page.setViewportSize({ width: 1600, height: 900 })
  await page.goto('/#/super-assistant')

  const contextUsage = page.getByTestId('super-assistant-context-usage')
  // 配置面板桌面端默认展开，按钮呈选中态（brand-soft 浅绿底），以静态 title 定位
  const configButton = page.locator('button[title="助手配置"]')

  await expect(contextUsage).toHaveCSS('background-color', 'rgba(240, 253, 250, 0.8)')
  await expect(configButton).toHaveCSS('background-color', 'rgb(236, 253, 245)')

  const contextBox = await contextUsage.boundingBox()
  const configBox = await configButton.boundingBox()
  expect(contextBox).not.toBeNull()
  expect(configBox).not.toBeNull()
  expect(Math.abs(contextBox!.y - configBox!.y)).toBeLessThan(2)

  // 侧栏会话行与头栏标题按钮同名，标题编辑入口需限定在头部
  await page.locator('header').getByRole('button', { name: /Markdown 渲染验证/ }).click()
  const cancelButton = page.getByRole('button', { name: '取消编辑会话名称' })
  await expect(page.getByRole('textbox', { name: '编辑会话名称' })).toBeVisible()
  await expect(cancelButton).toHaveCSS('background-color', 'rgb(255, 241, 242)')
  await expect(cancelButton).toHaveCSS('color', 'rgb(225, 29, 72)')
})

test('打开助手配置时工作台侧栏保持可用', async ({ page }) => {
  await mockSuperAssistant(page)
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto('/#/super-assistant')

  // 配置面板桌面端默认展开：仅在收起时点击展开
  const configToggle = page.locator('button[title="助手配置"]')
  if ((await configToggle.getAttribute('aria-expanded')) !== 'true') await configToggle.click()
  await expect(page.getByRole('heading', { name: '助手配置' })).toBeVisible()

  // 历史会话已迁入左侧工作台常驻时间线，不再是顶栏浮层
  await expect(page.getByRole('button', { name: '查看会话记录' })).toHaveCount(0)
  await expect(page.locator('[data-workbench-conversation="conversation-1"]')).toBeVisible()
  await expect(page.locator('button[title="助手配置"]')).toHaveAttribute('aria-expanded', 'true')
})

test('消息输入框默认获得焦点并展示绿色边框', async ({ page }) => {
  await mockSuperAssistant(page)
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto('/#/super-assistant')

  await expect(page.getByRole('textbox', { name: '向超级助手发送消息' })).toBeFocused()
  await expect(page.getByTestId('super-assistant-composer')).toHaveCSS('border-color', 'rgb(20, 184, 166)')
})

test('消息跳转浮层与输入区保留间距', async ({ page }) => {
  await mockSuperAssistant(page)
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto('/#/super-assistant')

  await page.getByRole('button', { name: '查看我发送的消息' }).click()
  const [messageHistoryBox, composerBox] = await Promise.all([
    page.getByTestId('super-assistant-message-history').boundingBox(),
    page.getByTestId('super-assistant-composer').boundingBox(),
  ])
  expect(messageHistoryBox).not.toBeNull()
  expect(composerBox).not.toBeNull()
  expect(composerBox!.y - (messageHistoryBox!.y + messageHistoryBox!.height)).toBeGreaterThanOrEqual(10)
})
