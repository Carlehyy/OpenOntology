import { expect, test, type Page, type Route } from '@playwright/test'

// D-016 回归：本体助手输入框的 Enter 提交语义与发送按钮可访问性。
// 中文输入法按 Enter 确认候选词时 keydown 带 isComposing=true——旧实现
// 直接调用 send()，WebKit 系受控状态尚为空导致静默吞掉，用户感知
// 「Enter 不发送」。修复对齐超级助手平台语义：组合期/修饰键不发送。

const now = '2026-07-19T08:00:00+00:00'
const conversation = {
  id: 'conv-1',
  title: '会话一',
  ontologyReleaseId: 'release-1',
  createdAt: now,
  updatedAt: now,
}

async function mockPlatform(page: Page, chatBodies: string[]) {
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
        description: '',
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
    if (path === '/api/v1/models') return json(route, [])
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
        ], positionX: 0, positionY: 0,
      }],
      linkTypes: [], actions: [], functions: [],
      instances: [], linkInstances: [], executionLogs: [],
    })
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/capabilities') return json(route, {
      enabled: true,
      objectTypes: [{ id: 'ot-order', name: 'Order', displayName: '订单', instanceCount: 1 }],
      linkTypes: [], actions: [], allowActionProposals: true,
      maxRowsPerQuery: 50, maxSteps: 8, skillCard: '',
      releaseId: 'release-1', releaseVersion: 'v1',
    })
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/profile') return json(route, {
      id: 'profile-1', ontologyId: 'ontology-1', enabled: true,
      allowedObjectTypeIds: null, allowedLinkTypeIds: null, allowedActionIds: [],
      allowActionProposals: true, maxRowsPerQuery: 50, maxSteps: 8,
      systemPromptExtra: '', defaultModelId: null, updatedAt: now,
    })
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/conversations') {
      return json(route, [])
    }
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/chat') {
      chatBodies.push(route.request().postData() || '')
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: [
          'data: {"type":"meta","conversationId":"conv-1"}',
          '',
          'data: {"type":"answer","content":"LIVE_AGENT_ANSWER"}',
          '',
          'data: {"type":"done"}',
          '',
          '',
        ].join('\n'),
      })
    }
    if (path === '/api/v2/formal/ontologies/ontology-1/agent/decision-simulations') {
      return json(route, [])
    }
    return route.fallback()
  })
}

test.describe('本体助手输入框 Enter 语义（D-016）', () => {
  test('组合期 Enter 不发送、普通 Enter 发送、按钮有可访问名称', async ({ page }) => {
    const chatBodies: string[] = []
    await mockPlatform(page, chatBodies)

    await page.goto('/#/agent?ontology_id=ontology-1')
    await expect(page.getByTestId('agent-input-bar')).toBeVisible()

    // 发送按钮具备可访问名称（读屏/自动化可达，不再是纯图标小目标）
    await expect(page.getByRole('button', { name: '发送消息' })).toBeAttached()

    const composer = page.getByTestId('agent-composer')

    // 输入法组合期的 Enter（确认候选词）：不得触发发送
    await composer.fill('当前有多少订单')
    await composer.evaluate(element => {
      element.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'Enter', bubbles: true, cancelable: true, isComposing: true,
      }))
    })
    await page.waitForTimeout(400)
    expect(chatBodies).toHaveLength(0)

    // 普通 Enter：立即发送
    await composer.press('Enter')
    await expect.poll(() => chatBodies.length).toBe(1)
    expect(chatBodies[0]).toContain('当前有多少订单')
    await expect(page.getByText('LIVE_AGENT_ANSWER')).toBeVisible()
  })
})
