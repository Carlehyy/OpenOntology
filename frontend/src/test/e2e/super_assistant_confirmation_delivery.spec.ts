import { expect, test, type Page, type Route } from '@playwright/test'

// YIYA-36 direct 二次确认交付回归：
// 1) reload 恢复：GET pending-confirmation 同形载荷（含 ontologyConfirmation
//    服务端持久 display）让确认卡在刷新后原样回归，用户能看清本体/动作/
//    目标/最终参数/发布版本/审批要求；
// 2) deny 只产生一次 decision=deny 请求，无 approve、无执行类写调用；
// 3) 对端结案/TTL 后轮询撤卡；
// 4) SSE 流内 tool_result status=unknown 的步骤如实显示「结果待核实」，
//    不伪完成。
const now = '2026-10-18T08:00:00+00:00'

const ontologyConfirmation = {
  confirmationId: 'conf-1',
  state: 'prepared',
  payloadHash: 'a'.repeat(64),
  expiresAt: '2026-10-19T00:00:00Z',
  requiresApproval: true,
  display: {
    ontology: { id: 'onto-1', name: '供应商域' },
    action: { id: 'action-1', name: '下调供应商等级' },
    target: { id: 'inst-1', label: 'SUP-0042 华南金属' },
    parameters: [
      { name: 'level', value: 'C', source: 'user_input' },
      { name: 'reason', value: '连续三次交付逾期', source: 'trusted_query' },
    ],
    release: { id: 'rel-1', version: '7' },
    requiresApproval: true,
  },
}

const pendingConfirmationPayload = {
  toolRunId: 'tr-1',
  toolName: 'ontology_execute_action',
  serverName: 'multica',
  arguments: { title: '供应商等级调整' },
  ontologyConfirmation,
}

/** 确认卡只在会话已有消息时渲染：基线 = 一轮进行中的对话（助手步骤待确认） */
const baselineMessages = [
  {
    id: 'm-user-1', conversation_id: 'c-1', role: 'user', content: '帮我下调华南金属的供应商等级',
    status: 'complete', steps: [], token_usage: {}, created_at: now,
  },
  {
    id: 'm-assistant-1', conversation_id: 'c-1', role: 'assistant', content: '',
    status: 'streaming',
    steps: [{ toolRunId: 'tr-1', toolName: 'ontology_execute_action', status: 'awaiting_confirmation' }],
    token_usage: {}, created_at: now,
  },
]

const json = (route: Route, data: unknown) => route.fulfill({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify({ data }),
})

async function seedAuth(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: { id: 'u-1', username: 'confirmation-tester', role: 'admin' },
      },
      version: 0,
    }))
  })
}

/** 记录全部写请求（decision）与 pending-confirmation 轮询的 mock 台架 */
async function mockWorkbench(page: Page, options: {
  pending?: (() => unknown) | null
  messages?: () => unknown[]
} = {}) {
  const decisions: Array<{ toolRunId: string; body: unknown }> = []
  let pendingResponses = 0
  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/models') {
      return json(route, [{
        id: 'model-1', name: 'DeepSeek', config_type: 'llm', provider: 'deepseek',
        api_base: 'https://api.deepseek.com', has_api_key: true, enabled: true, is_default: true,
        last_test_status: 'success', last_tested_at: now, last_test_message: 'ok',
        models: ['deepseek-v4-pro'], options: {}, created_by: 'admin',
        created_at: now, updated_at: now,
      }])
    }
    return route.continue()
  })
  await page.route('**/api/v2/super-assistant/**', async route => {
    const path = new URL(route.request().url()).pathname
    const method = route.request().method()
    if (path === '/api/v2/super-assistant/conversations' && method === 'GET') {
      return json(route, [{
        id: 'c-1', title: '交付会话', model_config_id: 'model-1', status: 'active',
        created_at: now, updated_at: now,
      }])
    }
    if (path === '/api/v2/super-assistant/conversations/c-1/messages') {
      return json(route, options.messages ? options.messages() : baselineMessages)
    }
    if (path === '/api/v2/super-assistant/conversations/c-1/files') return json(route, [])
    if (path === '/api/v2/super-assistant/conversations/c-1/pending-confirmation') {
      pendingResponses += 1
      const value = options.pending ? options.pending() : null
      return json(route, value)
    }
    if (path === '/api/v2/super-assistant/skills') return json(route, [])
    if (path === '/api/v2/super-assistant/mcp-servers') return json(route, [])
    if (path === '/api/v2/super-assistant/tools') return json(route, [])
    if (path === '/api/v2/super-assistant/multica/config') {
      return json(route, { enabled: false, commands: [] })
    }
    if (path === '/api/v2/super-assistant/runs') return json(route, [])
    const decisionMatch = path.match(/^\/api\/v2\/super-assistant\/tool-runs\/([^/]+)\/decision$/)
    if (decisionMatch && method === 'POST') {
      decisions.push({ toolRunId: decisionMatch[1], body: route.request().postDataJSON() })
      return json(route, { ok: true })
    }
    return route.fulfill({ status: 404, body: '{}' })
  })
  return { decisions, pendingCallCount: () => pendingResponses }
}

test.describe('direct 二次确认（YIYA-36 持久 display）', () => {
  test('reload 后恢复确认卡：服务端字段完整可见，拒绝只产生一次 deny 且无其它写调用', async ({ page }) => {
    await seedAuth(page)
    const { decisions } = await mockWorkbench(page, { pending: () => pendingConfirmationPayload })

    await page.goto('/#/super-assistant')
    const card = page.getByTestId('super-assistant-confirmation-card')
    await expect(card).toBeVisible()

    // 用户必须看清的内容全部来自服务端持久 display（不是内部 ID 或 MCP 名）
    await expect(card.getByText('供应商域')).toBeVisible()
    await expect(card.getByText('下调供应商等级')).toBeVisible()
    await expect(card.getByText('SUP-0042 华南金属')).toBeVisible()
    await expect(card.getByText('发布版本', { exact: false })).toBeVisible()
    await expect(card.getByText('7').first()).toBeVisible()
    await expect(card.getByText('需审批')).toBeVisible()
    // 最终参数只读展示 + 来源标签
    await expect(card.getByText('连续三次交付逾期')).toBeVisible()
    await expect(card.getByText('查询带入')).toBeVisible()
    // 技术详情默认收起：MCP 来源与原始参数不作为产品主详情
    await expect(card.getByText('技术详情', { exact: false })).toBeVisible()
    await expect(card.getByText('ontology_execute_action')).toBeHidden()

    // 拒绝：仅一条 decision=deny，绝不触发 approve 或其它执行路径
    await card.getByRole('button', { name: '拒绝' }).click()
    await expect(card).toBeHidden()
    expect(decisions).toHaveLength(1)
    expect(decisions[0].toolRunId).toBe('tr-1')
    expect(decisions[0].body).toEqual({ decision: 'deny' })
  })

  test('确认执行提交 decision=approve；批准与拒绝互斥（单次点击单一决定）', async ({ page }) => {
    await seedAuth(page)
    const { decisions } = await mockWorkbench(page, { pending: () => pendingConfirmationPayload })

    await page.goto('/#/super-assistant')
    const card = page.getByTestId('super-assistant-confirmation-card')
    await expect(card).toBeVisible()
    await card.getByRole('button', { name: '确认执行' }).click()
    await expect(card).toBeHidden()

    expect(decisions).toHaveLength(1)
    expect(decisions[0].body).toEqual({ decision: 'approve' })
  })

  test('对端已决定/超时结案后，轮询在同步周期内撤卡', async ({ page }) => {
    await seedAuth(page)
    let pendingAlive = true
    const { decisions } = await mockWorkbench(page, {
      pending: () => (pendingAlive ? pendingConfirmationPayload : null),
    })

    await page.goto('/#/super-assistant')
    const card = page.getByTestId('super-assistant-confirmation-card')
    await expect(card).toBeVisible()

    // 模拟对端决定（或 TTL 结案）：pending-confirmation 翻转为 null
    pendingAlive = false
    await expect(card).toBeHidden({ timeout: 6000 })
    // 本端从未发过决定请求：撤卡只读，不产生写副作用
    expect(decisions).toHaveLength(0)
  })

  test('关联声明确认：呈现两端键与业务含义，approve 文案为「确认关联」，deny 无副作用', async ({ page }) => {
    await seedAuth(page)
    const joinAssertionPayload = {
      toolRunId: 'tr-join-1',
      toolName: 'ontology_register_join_assertion',
      serverName: 'multica',
      arguments: {
        leftOntologyId: 'supply', leftObjectTypeId: 'Supplier', leftProperty: 'tax_id',
        rightOntologyId: 'finance', rightObjectTypeId: 'Vendor', rightProperty: 'tax_no',
        keyType: 'string',
        businessMeaning: '两端的税号指向同一纳税主体',
      },
    }
    const { decisions } = await mockWorkbench(page, { pending: () => joinAssertionPayload })

    await page.goto('/#/super-assistant')
    const card = page.getByTestId('super-assistant-confirmation-card')
    await expect(card).toBeVisible()

    // 用户必须看懂两端键 + 业务含义；不得出现「MCP multica」作为主文案
    await expect(card.getByText('确认业务关联')).toBeVisible()
    await expect(card.getByText('supply · Supplier · tax_id')).toBeVisible()
    await expect(card.getByText('finance · Vendor · tax_no')).toBeVisible()
    // 业务含义同时出现在主详情与折叠的技术详情 JSON 中，主详情取第一个
    await expect(card.getByText('两端的税号指向同一纳税主体').first()).toBeVisible()
    await expect(card.getByText(/MCP「multica」/)).toHaveCount(0)
    await expect(card.getByRole('button', { name: '确认关联' })).toBeVisible()
    await expect(card.getByRole('button', { name: '确认执行' })).toHaveCount(0)

    // 拒绝仍然只是 deny，无其它写调用
    await card.getByRole('button', { name: '拒绝' }).click()
    await expect(card).toBeHidden()
    expect(decisions).toHaveLength(1)
    expect(decisions[0].toolRunId).toBe('tr-join-1')
    expect(decisions[0].body).toEqual({ decision: 'deny' })
  })

  test('SSE 流内 unknown 工具结果如实显示「结果待核实」，不伪完成、无确认残留', async ({ page }) => {
    await seedAuth(page)
    let chatCompleted = false
    const { decisions } = await mockWorkbench(page, {
      messages: () => (chatCompleted ? [
        {
          id: 'm-user-1', conversation_id: 'c-1', role: 'user', content: '下调华南金属等级',
          status: 'complete', steps: [], token_usage: {}, created_at: now,
        },
        {
          id: 'm-assistant-1', conversation_id: 'c-1', role: 'assistant', content: '动作已提交，结果待核实',
          status: 'complete',
          steps: [{ toolRunId: 'tr-2', toolName: 'ontology_execute_action', status: 'unknown', preview: '外部效果未确认' }],
          token_usage: {}, created_at: now,
        },
      ] : []),
    })

    await page.route('**/api/v2/super-assistant/conversations/c-1/chat', route => {
      chatCompleted = true
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: [
          'event: tool_start',
          `data: ${JSON.stringify({ toolRunId: 'tr-2', toolName: 'ontology_execute_action', arguments: { title: 'x' } })}`,
          '',
          'event: tool_confirmation_required',
          `data: ${JSON.stringify({ toolRunId: 'tr-2', toolName: 'ontology_execute_action', serverName: 'multica', arguments: {}, ontologyConfirmation })}`,
          '',
          'event: tool_result',
          `data: ${JSON.stringify({ toolRunId: 'tr-2', status: 'unknown', preview: '外部效果未确认' })}`,
          '',
          'event: message_end',
          `data: ${JSON.stringify({
            message: {
              content: '动作已提交，结果待核实',
              steps: [{ toolRunId: 'tr-2', toolName: 'ontology_execute_action', status: 'unknown', preview: '外部效果未确认' }],
            },
          })}`,
          '',
          '',
        ].join('\n'),
      })
    })

    await page.goto('/#/super-assistant')
    const textbox = page.getByRole('textbox', { name: '向超级助手发送消息' })
    await textbox.fill('下调华南金属等级')
    await textbox.press('Enter')

    // 回合结束后：确认卡不残留（流内已被 tool_result 解除）
    await expect(page.getByTestId('super-assistant-confirmation-card')).toHaveCount(0)
    await expect(page.getByText('动作已提交，结果待核实').first()).toBeVisible()
    // 工具步骤的状态标注如实：展开过程区，状态为「结果待核实」而非「完成」
    const process = page.getByTestId('super-assistant-turn-process')
    await process.getByRole('button').click()
    await expect(process.getByText('结果待核实')).toBeVisible()
    await expect(process.getByText('完成')).toHaveCount(0)
    // 全程无决定请求：未确认不产生写副作用
    expect(decisions).toHaveLength(0)
  })
})
