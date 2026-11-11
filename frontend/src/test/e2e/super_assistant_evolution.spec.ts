import { expect, test, type Page, type Route } from '@playwright/test'

const now = '2026-08-12T08:00:00+00:00'

const json = (route: Route, data: unknown) => route.fulfill({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify({ data }),
})

async function seedAuth(page: Page, role = 'admin') {
  await page.addInitScript(role => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: { id: 'u-evolution', username: 'evolution-tester', role },
      },
      version: 0,
    }))
  }, role)
}

async function openConfigPanel(page: Page) {
  const configToggle = page.locator('button[title="助手配置"]')
  if ((await configToggle.getAttribute('aria-expanded')) !== 'true') await configToggle.click()
}

test('超级助手：技能审批保留（旧记忆/冲突卡隐藏）与 Hindsight 记忆面板全链路', async ({ page }) => {
  await seedAuth(page)

  let pendingCount = 1
  const decisions: Array<{ decision: string }> = []
  const patches: Array<Record<string, unknown>> = []
  const createdSources: Array<Record<string, unknown>> = []
  const retriedIds: string[] = []
  const forgottenIds: string[] = []
  const searchBodies: Array<Record<string, unknown>> = []

  let memoryEnabled = false
  let memoryModelId: string | null = null
  let memoryState = 'unconfigured'

  const source = (overrides: Record<string, unknown>) => ({
    id: 'src-1',
    role: 'user',
    content: '',
    occurred_at: now,
    conversation_id: 'conv-1',
    message_id: 'msg-1',
    explicit: false,
    correction_of: null,
    status: 'synced',
    error: null,
    created_at: now,
    updated_at: now,
    ...overrides,
  })
  let sources: Array<ReturnType<typeof source>> = [
    source({ id: 'src-1', role: 'user', content: '用户偏好简洁的中文回答' }),
    source({ id: 'src-2', role: 'assistant', content: '已整理好本周的部署安排' }),
    source({
      id: 'src-3', role: 'user', content: '旧的座机号是 010-1234',
      status: 'failed', error: '记忆模型超时',
    }),
  ]

  await page.route('**/api/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/models') return json(route, [
      {
        id: 'model-1', name: 'Fake model', config_type: 'llm', provider: 'openai',
        api_base: 'https://example.com', has_api_key: true, enabled: true, is_default: true,
        last_test_status: 'success', last_tested_at: now, last_test_message: 'ok',
        models: ['fake-model'], options: {}, created_by: 'admin',
        created_at: now, updated_at: now,
      },
      {
        id: 'model-2', name: '兼容模型', config_type: 'llm', provider: 'compatible',
        api_base: 'https://example.com', has_api_key: true, enabled: true, is_default: false,
        last_test_status: 'success', last_tested_at: now, last_test_message: 'ok',
        models: ['compatible-model'], options: {}, created_by: 'admin',
        created_at: now, updated_at: now,
      },
    ])
    return route.continue()
  })

  await page.route('**/api/v2/super-assistant/**', async route => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    const method = request.method()
    if (path === '/api/v2/super-assistant/conversations') {
      return json(route, [{
        id: 'conv-1', title: '测试会话', model_config_id: 'model-1', status: 'active',
        created_at: now, updated_at: now,
      }])
    }
    if (path === '/api/v2/super-assistant/conversations/conv-1/messages') return json(route, [])
    if (path === '/api/v2/super-assistant/mcp-servers') return json(route, [])
    if (path === '/api/v2/super-assistant/tools') return json(route, [])
    if (path === '/api/v2/super-assistant/skills') return json(route, [])
    if (path === '/api/v2/super-assistant/reflection/settings') {
      return json(route, { pending_count: pendingCount })
    }
    if (path === '/api/v2/super-assistant/reflection/candidates') {
      return json(route, pendingCount > 0 ? [
        {
          id: 'cand-skill', run_id: 'run-1', conversation_id: 'conv-1',
          kind: 'skill', status: 'pending', confidence: 'medium',
          payload: { name: 'weekly-report', description: '按固定结构生成周报', skill_md: '# 周报技能' },
          decision: null, created_at: now, decided_at: null,
        },
        {
          id: 'cand-memory', run_id: 'run-1', conversation_id: 'conv-1',
          kind: 'memory', status: 'pending', confidence: 'medium',
          payload: { content: '旧记忆候选不应再出现', zone: 'work', tags: [], pinned: false, supersedes: [] },
          decision: null, created_at: now, decided_at: null,
        },
        {
          id: 'cand-conflict', run_id: 'run-1', conversation_id: 'conv-1',
          kind: 'conflict', status: 'pending', confidence: 'medium',
          payload: { explain: '旧冲突候选不应再出现', conflict_kind: 'zone' },
          decision: null, created_at: now, decided_at: null,
        },
      ] : [])
    }
    if (path.startsWith('/api/v2/super-assistant/reflection/candidates/') && method === 'POST') {
      const body = request.postDataJSON() as { decision: string }
      decisions.push(body)
      // 人为延迟响应：给 busy 粒度断言留出观察窗口
      await new Promise(resolve => setTimeout(resolve, 600))
      pendingCount = 0
      return json(route, {
        id: 'cand-skill', run_id: 'run-1', conversation_id: 'conv-1',
        kind: 'skill', status: 'accepted', confidence: 'medium',
        payload: {}, decision: body.decision, created_at: now, decided_at: now,
      })
    }
    // Hindsight 记忆：设置 / 状态 / 来源 / 召回
    if (path === '/api/v2/super-assistant/memory/settings') {
      if (method === 'PATCH') {
        const body = request.postDataJSON() as { enabled: boolean; model_config_id?: string | null }
        patches.push(body)
        memoryEnabled = body.enabled
        memoryModelId = body.model_config_id ?? memoryModelId
        if (memoryEnabled) memoryState = 'ready'
      }
      return json(route, {
        enabled: memoryEnabled, model_config_id: memoryModelId,
        web_url: 'https://memory.example.invalid:9443',
      })
    }
    if (path === '/api/v2/super-assistant/memory/status') {
      return json(route, {
        state: memoryState,
        message: null,
        pending_count: 2,
        failed_count: 1,
        last_synced_at: memoryState === 'ready' ? now : null,
      })
    }
    if (path === '/api/v2/super-assistant/memory/sources' && method === 'GET') {
      const query = url.searchParams.get('query') || ''
      const offset = Number(url.searchParams.get('offset') || 0)
      const limit = Number(url.searchParams.get('limit') || 20)
      const filtered = query
        ? sources.filter(item => String(item.content).includes(query))
        : sources
      return json(route, { items: filtered.slice(offset, offset + limit), total: filtered.length })
    }
    if (path === '/api/v2/super-assistant/memory/sources' && method === 'POST') {
      const body = request.postDataJSON() as { content: string; correction_of?: string }
      createdSources.push(body)
      const created = source({
        id: `src-new-${createdSources.length}`,
        role: 'user',
        content: body.content,
        explicit: true,
        correction_of: body.correction_of ?? null,
        status: 'pending',
      })
      sources = [created, ...sources]
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({ data: created }),
      })
    }
    const retryMatch = path.match(/^\/api\/v2\/super-assistant\/memory\/sources\/([^/]+)\/retry$/)
    if (retryMatch && method === 'POST') {
      retriedIds.push(retryMatch[1])
      sources = sources.map(item => item.id === retryMatch[1] ? { ...item, status: 'retry_wait', error: null } : item)
      return json(route, sources.find(item => item.id === retryMatch[1]))
    }
    const deleteMatch = path.match(/^\/api\/v2\/super-assistant\/memory\/sources\/([^/]+)$/)
    if (deleteMatch && method === 'DELETE') {
      forgottenIds.push(deleteMatch[1])
      // 遗忘异步：DELETE 返回的仍是 forgetting 中间态，绝不立刻宣称已忘记
      sources = sources.map(item => item.id === deleteMatch[1] ? { ...item, status: 'forgetting' } : item)
      return json(route, sources.find(item => item.id === deleteMatch[1]))
    }
    if (path === '/api/v2/super-assistant/memory/search' && method === 'POST') {
      const body = request.postDataJSON() as { query: string }
      searchBodies.push(body)
      return json(route, {
        items: [{
          id: 'recall-1', text: '部署评审安排在每周四下午', type: 'episode',
          document_id: 'doc-1', occurred_start: now, occurred_end: now, source_ids: ['src-new-1'],
        }],
      })
    }
    return route.fulfill({ status: 404, body: '{}' })
  })

  await page.goto('/#/super-assistant')
  await openConfigPanel(page)

  // 待审批：技能候选保留；旧记忆/冲突候选随 Hindsight 上线隐藏不渲染
  await page.getByRole('button', { name: '待审批' }).click()
  await expect(page.getByTestId('candidate-skill')).toBeVisible()
  await expect(page.getByText('weekly-report')).toBeVisible()
  await expect(page.getByText('旧记忆候选不应再出现')).toHaveCount(0)
  await expect(page.getByText('旧冲突候选不应再出现')).toHaveCount(0)

  await page.getByRole('button', { name: '接受', exact: true }).click()

  // busy 粒度按 `${候选id}:${decision}`：请求进行中仅被点击的按钮转圈，
  // 同卡另一按钮禁用但不转圈
  const rejectButton = page.getByRole('button', { name: '拒绝', exact: true })
  await expect(rejectButton).toBeDisabled()
  await expect(rejectButton.locator('.animate-spin')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '接受', exact: true }).locator('.animate-spin')).toBeVisible()

  await expect(page.getByText('暂无需要你批准的技能')).toBeVisible()
  expect(decisions).toEqual([{ decision: 'accept' }])

  // 记忆面板：未配置状态可见（不静默）
  await page.getByRole('button', { name: '记忆', exact: true }).click()
  await expect(page.getByTestId('memory-status-unconfigured')).toBeVisible()

  const consoleLink = page.getByRole('link', { name: '打开 Hindsight' })
  await expect(consoleLink).toHaveAttribute('href', 'https://memory.example.invalid:9443')
  await expect(consoleLink).toHaveAttribute('target', '_blank')
  await expect(consoleLink).toHaveAttribute('rel', 'noopener noreferrer')
  await page.context().route('https://memory.example.invalid:9443/**', route => route.fulfill({
    contentType: 'text/html', body: '<title>Hindsight test console</title><h1>Hindsight</h1>',
  }))
  const popupPromise = page.waitForEvent('popup')
  await consoleLink.click()
  const popup = await popupPromise
  await popup.waitForLoadState('domcontentloaded')
  await expect(popup).toHaveURL('https://memory.example.invalid:9443/')
  await expect(popup.getByRole('heading', { name: 'Hindsight', exact: true })).toBeVisible()
  expect(await popup.evaluate(() => window.opener)).toBeNull()
  await popup.close()

  // 设置：启用 + 选择 openai 提供方的模型并保存（保存后由后端能力校验）
  await page.getByRole('switch', { name: '启用长期记忆' }).click()
  await page.getByTestId('memory-settings').getByRole('combobox', { name: '记忆模型' }).click()
  await page.getByRole('option', { name: 'Fake model' }).click()
  const patchRequest = page.waitForRequest(request =>
    request.url().endsWith('/api/v2/super-assistant/memory/settings') && request.method() === 'PATCH')
  await page.getByTestId('memory-settings-save').click()
  expect((await patchRequest).postDataJSON()).toEqual({ enabled: true, model_config_id: 'model-1' })
  await expect(page.getByText('记忆设置已保存')).toBeVisible()

  // 保存后状态轮询刷新为 ready：状态条与计数明确可见
  await expect(page.getByTestId('memory-status-ready')).toBeVisible()
  await expect(page.getByText(/待处理 2 · 失败 1/)).toBeVisible()

  // 来源列表：role / status 可见
  await expect(page.getByTestId('memory-source-item')).toHaveCount(3)
  const failedCard = page.locator('article[data-testid="memory-source-item"]', { hasText: '旧的座机号是 010-1234' })
  await expect(failedCard.getByTestId('memory-source-status-failed')).toBeVisible()
  await expect(failedCard.getByText('记忆模型超时')).toBeVisible()

  // 失败来源重试：返回 retry_wait 中间态
  await failedCard.getByTestId('memory-source-retry').click()
  await expect(failedCard.getByTestId('memory-source-status-retry_wait')).toBeVisible()
  expect(retriedIds).toEqual(['src-3'])

  // 手动记住：显式来源入列（手动 + pending，等待沉淀）
  await page.getByTestId('memory-remember-open').click()
  await page.getByRole('textbox', { name: '要记住的内容' }).fill('部署评审安排在每周四下午')
  await page.getByTestId('memory-submit-source').click()
  const manualCard = page.locator('article[data-testid="memory-source-item"]', { hasText: '部署评审安排在每周四下午' })
  await expect(manualCard.getByTestId('memory-source-status-pending')).toBeVisible()
  await expect(manualCard.getByText('手动', { exact: true })).toBeVisible()
  expect(createdSources).toEqual([{ content: '部署评审安排在每周四下午' }])

  // 追加更正：携带 correction_of
  page.locator('article[data-testid="memory-source-item"]', { hasText: '用户偏好简洁的中文回答' }).getByTestId('memory-source-correct').click()
  await page.getByRole('textbox', { name: '更正内容' }).fill('更正：用户偏好详尽带示例的中文回答')
  await page.getByTestId('memory-submit-source').click()
  const correctionCard = page.locator('article[data-testid="memory-source-item"]', { hasText: '更正：用户偏好详尽带示例的中文回答' })
  await expect(correctionCard.getByTestId('memory-source-status-pending')).toBeVisible()
  await expect(correctionCard.locator('span[title^="更正于来源"]')).toBeVisible()
  expect(createdSources[1]).toEqual({ content: '更正：用户偏好详尽带示例的中文回答', correction_of: 'src-1' })

  // Hindsight 召回
  await page.getByRole('textbox', { name: '记忆召回查询' }).fill('部署评审')
  await page.getByTestId('memory-recall-run').click()
  await expect(page.getByTestId('memory-recall-item')).toBeVisible()
  await expect(page.getByTestId('memory-recall-item').getByText('部署评审安排在每周四下午')).toBeVisible()
  expect(searchBodies).toEqual([{ query: '部署评审' }])

  // 按来源忘记：确认文案说明相关记忆一并清理；提交后仅受理（遗忘中），不宣称已忘记
  const forgetCard = page.locator('article[data-testid="memory-source-item"]', { hasText: '已整理好本周的部署安排' })
  await forgetCard.getByTestId('memory-source-forget').click()
  const confirmDialog = page.getByRole('dialog', { name: '按来源忘记' })
  await expect(confirmDialog.getByText(/一并清理/)).toBeVisible()
  await confirmDialog.getByRole('button', { name: '忘记' }).click()
  await expect(page.getByText('已提交忘记请求')).toBeVisible()
  await expect(forgetCard.getByTestId('memory-source-status-forgetting')).toBeVisible()
  expect(forgottenIds).toEqual(['src-2'])
})

for (const scenario of [
  { role: 'admin', webUrl: null, label: '未配置地址时禁用并说明原因' },
  { role: 'admin', webUrl: 'javascript:alert(1)', label: '非法地址不可跳转' },
  { role: 'editor', webUrl: 'https://memory.example.invalid', label: '普通用户不显示管理入口' },
]) {
  test(`Hindsight Web 入口：${scenario.label}`, async ({ page }) => {
    await seedAuth(page, scenario.role)
    await page.route('**/api/v1/models', route => json(route, []))
    await page.route('**/api/v2/super-assistant/**', route => {
      const path = new URL(route.request().url()).pathname
      if (path.endsWith('/memory/settings')) {
        return json(route, { enabled: true, model_config_id: null, web_url: scenario.webUrl })
      }
      if (path.endsWith('/memory/status')) {
        return json(route, {
          state: 'unconfigured', message: '', pending_count: 0,
          failed_count: 0, last_synced_at: null,
        })
      }
      if (path.endsWith('/memory/sources')) return json(route, { items: [], total: 0 })
      if (path.endsWith('/reflection/settings')) return json(route, { pending_count: 0 })
      if (['conversations', 'skills', 'mcp-servers', 'tools', 'reflection/candidates']
        .some(endpoint => path.endsWith(`/${endpoint}`))) return json(route, [])
      return route.fulfill({ status: 404, body: '{}' })
    })
    await page.goto('/#/super-assistant')
    await openConfigPanel(page)
    await page.getByRole('button', { name: '记忆', exact: true }).click()
    await expect(page.getByTestId('memory-status-unconfigured')).toBeVisible()
    await expect(page.getByRole('link', { name: '打开 Hindsight' })).toHaveCount(0)
    if (scenario.role === 'admin') {
      await expect(page.getByRole('button', { name: '打开 Hindsight' })).toBeDisabled()
      await expect(page.getByText('尚未配置 Hindsight Web 地址', { exact: false })).toBeVisible()
    } else {
      await expect(page.getByTestId('memory-web-entry')).toHaveCount(0)
    }
  })
}

test('超级助手：记忆故障不影响最终回复 + Skill 常驻', async ({ page }) => {
  await seedAuth(page)

  let alwaysActive = false
  const skillPatches: Array<Record<string, unknown>> = []
  const chatBodies: Array<Record<string, unknown>> = []
  let chatCompleted = false

  const messageOf = (id: string, role: 'user' | 'assistant', content: string) => ({
    id,
    conversation_id: 'conv-1',
    role,
    content,
    status: 'complete',
    steps: [],
    token_usage: {},
    created_at: now,
  })

  const sseBody = [
    'event: meta\ndata: {"conversationId":"conv-1","assistantMessageId":"assistant-live"}',
    // 记忆状态事件穿插在正文流中：syncing → error，但不得影响 text/finish
    'event: memory_status\ndata: {"state":"syncing","pending_count":1,"failed_count":0}',
    'event: text_delta\ndata: {"delta":"记忆故障时依然完整的"}',
    'event: memory_status\ndata: {"state":"error","message":"记忆模型暂不可用","pending_count":1,"failed_count":1}',
    'event: text_delta\ndata: {"delta":"最终答复"}',
    'event: message_end\ndata: {"message":{"id":"assistant-live","content":"记忆故障时依然完整的最终答复","steps":[],"tokenUsage":{"inputTokens":8}}}',
    'event: done\ndata: {}',
    '',
  ].join('\n\n')

  await page.route('**/api/v1/models', route => json(route, [{
    id: 'model-1', name: 'Fake model', config_type: 'llm', provider: 'openai',
    api_base: 'https://example.com', has_api_key: true, enabled: true, is_default: true,
    last_test_status: 'success', last_tested_at: now, last_test_message: 'ok',
    models: ['fake-model'], options: {}, created_by: 'admin',
    created_at: now, updated_at: now,
  }]))

  await page.route('**/api/v2/super-assistant/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    const method = request.method()
    if (path === '/api/v2/super-assistant/conversations' && method === 'GET') {
      return json(route, [{
        id: 'conv-1', title: '测试会话', model_config_id: 'model-1', status: 'active',
        created_at: now, updated_at: now,
      }])
    }
    if (path === '/api/v2/super-assistant/conversations/conv-1/messages') {
      return json(route, chatCompleted
        ? [
            messageOf('user-live', 'user', '介绍一下部署安排'),
            messageOf('assistant-live', 'assistant', '记忆故障时依然完整的最终答复'),
          ]
        : [])
    }
    if (path === '/api/v2/super-assistant/conversations/conv-1/chat' && method === 'POST') {
      chatBodies.push(request.postDataJSON() as Record<string, unknown>)
      chatCompleted = true
      return route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
        body: sseBody,
      })
    }
    if (path === '/api/v2/super-assistant/mcp-servers') return json(route, [])
    if (path === '/api/v2/super-assistant/tools') return json(route, [])
    if (path === '/api/v2/super-assistant/skills' && method === 'GET') {
      return json(route, [{
        id: 'skill-1', name: 'research-helper', description: '检索助手',
        manifest: [{ path: 'SKILL.md', size: 128, editable: true }],
        enabled: true, always_active: alwaysActive, use_count: 0, last_used_at: null,
        revision: 1, created_at: now, updated_at: now,
      }])
    }
    if (path === '/api/v2/super-assistant/skills/skill-1' && method === 'PATCH') {
      const body = request.postDataJSON() as Record<string, unknown>
      skillPatches.push(body)
      if (typeof body.always_active === 'boolean') alwaysActive = body.always_active
      return json(route, {
        id: 'skill-1', name: 'research-helper', description: '检索助手',
        manifest: [{ path: 'SKILL.md', size: 128, editable: true }],
        enabled: true, always_active: alwaysActive, use_count: 0, last_used_at: null,
        revision: 1, created_at: now, updated_at: now,
      })
    }
    if (path === '/api/v2/super-assistant/reflection/candidates') return json(route, [])
    if (path === '/api/v2/super-assistant/reflection/settings') {
      return json(route, { pending_count: 0 })
    }
    return route.fulfill({ status: 404, body: '{}' })
  })

  await page.goto('/#/super-assistant')

  // 发送消息：SSE 中穿插记忆 syncing→error 事件
  const textbox = page.getByRole('textbox', { name: '向超级助手发送消息' })
  await textbox.fill('介绍一下部署安排')
  await textbox.press('Enter')
  await expect.poll(() => chatBodies.length).toBe(1)

  // 最终回复完整呈现：memory_status 与 text/message_end 完全独立
  await expect(page.getByText('记忆故障时依然完整的最终答复').first()).toBeVisible()

  // 记忆故障以独立状态条明确可见，绝不渲染为聊天错误
  await expect(page.getByTestId('memory-status-error')).toBeVisible()
  await expect(page.getByText('记忆同步失败')).toBeVisible()
  await expect(page.getByText('记忆模型暂不可用').first()).toBeVisible()
  await expect(page.getByText('生成失败')).toHaveCount(0)

  // Skill 治理保留：「常驻」开关触发 PATCH 并刷新卡片
  await openConfigPanel(page)
  await expect(page.getByText('未使用')).toBeVisible()
  await page.getByRole('switch', { name: '设为常驻 Skill research-helper' }).click()
  await expect.poll(() => skillPatches).toEqual([{ always_active: true }])
  await expect(page.getByRole('switch', { name: '取消常驻 Skill research-helper' })).toHaveAttribute('aria-checked', 'true')
})

test('MCP 开关按卡片粒度 busy：任一开关保存中不连坐禁用其它卡片', async ({ page }) => {
  await seedAuth(page)

  const server = (id: string, name: string) => ({
    id, name, builtin_key: null, transport: 'streamable_http',
    url: `https://${id}.example.com/mcp`, command: null, args: [],
    headers: {}, header_names: [], env: {}, env_names: [],
    tool_manifest: [], enabled: true, require_confirmation: true,
    last_test_status: 'success', last_test_message: 'ok',
    created_at: now, updated_at: now,
  })
  const alpha = server('alpha', 'alpha')
  const beta = server('beta', 'beta')
  const patches: Array<{ id: string; body: Record<string, unknown> }> = []

  await page.route('**/api/v1/models', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/models') return json(route, [{
      id: 'model-1', name: 'Fake model', config_type: 'llm', provider: 'openai',
      api_base: 'https://example.com', has_api_key: true, enabled: true, is_default: true,
      last_test_status: 'success', last_tested_at: now, last_test_message: 'ok',
      models: ['fake-model'], options: {}, created_by: 'admin',
      created_at: now, updated_at: now,
    }])
    return route.continue()
  })

  await page.route('**/api/v2/super-assistant/**', async route => {
    const path = new URL(route.request().url()).pathname
    const method = route.request().method()
    if (path === '/api/v2/super-assistant/conversations') return json(route, [])
    if (path === '/api/v2/super-assistant/mcp-servers' && method === 'GET') return json(route, [alpha, beta])
    if (path === '/api/v2/super-assistant/tools') return json(route, [])
    if (path === '/api/v2/super-assistant/mcp-servers/alpha' && method === 'PATCH') {
      // alpha 的保存悬挂 800ms：期间 beta 的开关必须保持可点
      await new Promise(resolve => setTimeout(resolve, 800))
      const body = route.request().postDataJSON() as Record<string, unknown>
      patches.push({ id: 'alpha', body })
      if (typeof body.enabled === 'boolean') alpha.enabled = body.enabled
      return json(route, alpha)
    }
    if (path === '/api/v2/super-assistant/mcp-servers/beta' && method === 'PATCH') {
      const body = route.request().postDataJSON() as Record<string, unknown>
      patches.push({ id: 'beta', body })
      if (typeof body.enabled === 'boolean') beta.enabled = body.enabled
      return json(route, beta)
    }
    if (path === '/api/v2/super-assistant/skills') return json(route, [])
    if (path === '/api/v2/super-assistant/reflection/candidates') return json(route, [])
    if (path === '/api/v2/super-assistant/reflection/settings') {
      return json(route, { pending_count: 0 })
    }
    return route.fulfill({ status: 404, body: '{}' })
  })

  await page.goto('/#/super-assistant')
  // 配置面板默认收起：点击展开
  const configToggle = page.locator('button[title="助手配置"]')
  if ((await configToggle.getAttribute('aria-expanded')) !== 'true') await configToggle.click()
  await page.getByRole('button', { name: /^MCP/ }).click()

  const betaSwitch = page.getByRole('switch', { name: '停用 MCP beta' })
  await expect(betaSwitch).toBeVisible()

  // 点击 alpha 启用开关（进入 800ms 保存中）：beta 开关不禁用、不忙碌，且可立即点击
  await page.getByRole('switch', { name: '停用 MCP alpha' }).click()
  await expect(betaSwitch).not.toBeDisabled()
  await expect(betaSwitch).not.toHaveAttribute('aria-busy', 'true')
  await betaSwitch.click()

  // 两个 PATCH 都到达（beta 未被吞掉；beta 无延迟先返回），刷新后两卡开关同步为停用态
  await expect.poll(() => patches.map(item => item.id).sort()).toEqual(['alpha', 'beta'])
  expect(patches[0].body).toMatchObject({ enabled: false })
  expect(patches[1].body).toMatchObject({ enabled: false })
  await expect(page.getByRole('switch', { name: '启用 MCP beta' })).toBeVisible()
})
