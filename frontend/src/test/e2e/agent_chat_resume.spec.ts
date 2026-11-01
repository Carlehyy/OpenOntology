import { expect, test, type Page, type Route } from '@playwright/test'

/**
 * 本体助手 · 后台回合恢复（MYW-71）：
 * 发送消息后离开页面，回合在后端继续执行并落库；回到页面后空会话态出现
 * 「仍在后台处理」横幅，点击恢复后先看到「正在处理」占位，终态后自动
 * 装载落库的完整回答。SSE 与后端执行解耦的前端恢复链路全程走 HTTP 轮询。
 */

const now = '2026-07-19T08:00:00+00:00'
const runId = 'run-resume-e2e'
const conversationId = '12345678-1234-4321-8765-123456789abc'

const conversation = {
  id: conversationId,
  title: '查询订单数量',
  ontologyReleaseId: 'release-1',
  createdAt: now,
  updatedAt: now,
}

const message = (id: string, role: 'user' | 'assistant', content: string) => ({
  id,
  role,
  content,
  steps: [],
  citations: [],
  proposals: [],
  model: role === 'assistant' ? 'deepseek-flash' : null,
  tokenUsage: null,
  createdAt: now,
})

/**
 * 落库回答带行内代码与围栏代码块：聊天正文属浅色阅读面（DESIGN.md §2.3），
 * 断言颜色可防止弹窗遮罩色（overlay）回归成 AI 响应里的暗底块。
 */
const assistantAnswer = [
  '当前共有 **18** 个订单实例，主键为 `order_no`。',
  '',
  '```json',
  '{"order_no":"SO-1","status":"paid"}',
  '```',
].join('\n')

async function mockPlatform(page: Page, states: {
  runStatus: () => 'running' | 'succeeded'
  answered: () => boolean
}) {  await page.addInitScript(([rid, cid]) => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: { id: 'admin', username: 'admin', email: 'admin@example.com', role: 'admin' },
      },
      version: 0,
    }))
    // 模拟「发送后离开页面」留下的后台回合登记
    sessionStorage.setItem('ontoagent:active-chat-run:v1', JSON.stringify({
      runId: rid,
      ontologyId: 'ontology-1',
      conversationId: cid,
      question: '查询订单数量',
      startedAt: '2026-07-19T08:00:00+00:00',
    }))
  }, [runId, conversationId])

  const json = (route: Route, data: unknown) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ data }),
  })

  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/ontologies') return json(route, {
      items: [{
        id: 'ontology-1',
        name: '供应链本体',
        domain: '供应链',
        description: '后台回合恢复验证',
        status: 'draft',
        version: 'v1',
        current_release_id: 'release-1',
        current_release_version: 'v1',
        created_at: now,
        updated_at: now,
      }],
      total: 1,
      page: 1,
      page_size: 20,
    })
    if (path === '/api/v1/domains') return json(route, [])
    if (path === '/api/v1/models') return json(route, [
      { id: 'model-1', name: 'deepseek-flash', config_type: 'llm' },
    ])
    return route.fallback()
  })

  await page.route('**/api/v2/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v2/inbox/summary') return json(route, { unread_count: 0 })
    if (path === '/api/v2/ontologies/ontology-1/versions/release-1/workspace'
      || path === '/api/v2/formal/ontologies/ontology-1/full') return json(route, {
      id: 'ontology-1',
      name: '供应链本体',
      version: 'v1',
      workspaceMode: 'release',
      objectTypes: [{
        id: 'ot-order', name: 'Order', displayName: '订单', primaryKey: 'order_no',
        properties: [
          { id: 'p1', name: 'order_no', displayName: '订单号', type: 'string', required: true },
          { id: 'p2', name: 'status', displayName: '状态', type: 'string', required: false },
        ], positionX: 0, positionY: 0,
      }],
      linkTypes: [],
      actions: [],
      functions: [],
      instances: [],
      linkInstances: [],
      executionLogs: [],
    })
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/capabilities') return json(route, {
      enabled: true,
      objectTypes: [{ id: 'ot-order', name: 'Order', displayName: '订单', instanceCount: 1 }],
      linkTypes: [],
      actions: [],
      allowActionProposals: true,
      maxRowsPerQuery: 50,
      maxSteps: 8,
      skillCard: '',
      releaseId: 'release-1',
      releaseVersion: 'v1',
    })
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/profile') return json(route, {
      id: 'profile-1',
      ontologyId: 'ontology-1',
      enabled: true,
      allowedObjectTypeIds: null,
      allowedLinkTypeIds: null,
      allowedActionIds: [],
      allowActionProposals: true,
      maxRowsPerQuery: 50,
      maxSteps: 8,
      systemPromptExtra: '',
      defaultModelId: null,
      updatedAt: now,
    })
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/conversations') {
      return json(route, [conversation])
    }
    if (path === `/api/v2/formal/ontologies/ontology-1/agent/conversations/${conversationId}`) {
      // 恢复期间：只有用户提问；终态后：回答已落库
      return json(route, {
        ...conversation,
        ontologyId: 'ontology-1',
        userId: 'admin',
        messages: states.answered()
          ? [message('message-1', 'user', '查询订单数量'), message('message-2', 'assistant', assistantAnswer)]
          : [message('message-1', 'user', '查询订单数量')],
      })
    }
    if (path === `/api/v2/formal/ontologies/ontology-1/agent/chat/runs/${runId}`) {
      const status = states.runStatus()
      return json(route, {
        runId,
        conversationId,
        status,
        startedAt: now,
        finishedAt: status === 'running' ? null : now,
      })
    }
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/decision-simulations') {
      return json(route, [])
    }
    return route.fallback()
  })
}

test('后台回合恢复：横幅 → 正在处理 → 自动装载落库回答', async ({ page }) => {
  const states: { runStatus: () => 'running' | 'succeeded'; answered: () => boolean } = {
    runStatus: () => 'running',
    answered: () => false,
  }
  await mockPlatform(page, states)

  await page.goto('/#/agent?ontology_id=ontology-1')

  // 空会话态出现恢复横幅（含原提问），且登记仍存在
  const banner = page.getByTestId('agent-resume-banner')
  await expect(banner).toBeVisible()
  await expect(banner).toContainText('查询订单数量')
  await expect(banner).toContainText('仍在后台处理')

  // 点击恢复：会话装载（提问已落库），展示「正在处理」占位
  await banner.click()
  await expect(page.getByTestId('agent-resume-pending')).toBeVisible()
  await expect(page.getByTestId('agent-resume-pending')).toContainText('仍在后台处理')

  // 后端回合到达终态：占位被落库的完整回答替换，登记被清除
  await page.waitForTimeout(2300) // 等一次轮询周期拿到 running
  states.runStatus = () => 'succeeded'
  states.answered = () => true

  await expect(page.getByText('当前共有 18 个订单实例，主键为')).toBeVisible()
  await expect(page.getByTestId('agent-resume-pending')).toHaveCount(0)
  await expect(page.getByTestId('agent-resume-banner')).toHaveCount(0)

  // AI 响应正文的 Markdown 阅读面保持浅色（DESIGN.md §2.3）：
  // 行内代码 bg-hover #f1f3f5，围栏块 bg-elevated #f5f7fa，文字 text-primary #1a1a2e
  const chatRegion = page.getByTestId('agent-chat-region')
  const inlineCode = chatRegion.locator('p code').filter({ hasText: 'order_no' })
  await expect(inlineCode).toHaveCSS('background-color', 'rgb(241, 243, 245)')
  await expect(inlineCode).toHaveCSS('color', 'rgb(26, 26, 46)')
  const codeBlock = chatRegion.locator('pre')
  await expect(codeBlock).toContainText('SO-1')
  await expect(codeBlock).toHaveCSS('background-color', 'rgb(245, 247, 250)')
  await expect(codeBlock).toHaveCSS('color', 'rgb(26, 26, 46)')

  const stored = await page.evaluate(() => sessionStorage.getItem('ontoagent:active-chat-run:v1'))
  expect(stored).toBeNull()
})
