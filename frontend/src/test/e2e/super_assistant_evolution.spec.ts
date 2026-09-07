import { expect, test, type Page, type Route } from '@playwright/test'

const now = '2026-08-12T08:00:00+00:00'

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
        user: { id: 'u-evolution', username: 'evolution-tester', role: 'admin' },
      },
      version: 0,
    }))
  })
}

test('超级助手：待审批与记忆面板全链路', async ({ page }) => {
  await seedAuth(page)

  let autoAccept = true
  let pendingCount = 1
  let candidatePending = true
  const decisions: Array<{ decision: string }> = []
  const memories = [
    {
      id: 'mem-1', content: '用户偏好简洁的中文回答', zone: 'core', pinned: true,
      confidence: 'high', source: 'user', tags: ['偏好'], supersedes: [], superseded: false,
      match_count: 3, reference_count: 2, last_accessed_at: null,
      created_at: now, updated_at: now,
    },
    {
      id: 'mem-2', content: '正在推进本体发布流程', zone: 'work', pinned: false,
      confidence: 'medium', source: 'reflection', tags: [], supersedes: [], superseded: false,
      match_count: 1, reference_count: 0, last_accessed_at: null,
      created_at: now, updated_at: now,
    },
  ]

  await page.route('**/api/v1/**', route => {
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
    const request = route.request()
    const path = new URL(request.url()).pathname
    const method = request.method()
    if (path === '/api/v2/super-assistant/conversations') {
      return json(route, [{
        id: 'conv-1', title: '测试会话', model_config_id: 'model-1', status: 'active',
        created_at: now, updated_at: now,
      }])
    }
    if (path === '/api/v2/super-assistant/mcp-servers') return json(route, [])
    if (path === '/api/v2/super-assistant/skills') return json(route, [])
    if (path === '/api/v2/super-assistant/memories' && method === 'GET') {
      return json(route, memories)
    }
    if (path === '/api/v2/super-assistant/memories' && method === 'POST') {
      const body = request.postDataJSON() as { content: string }
      if (body.content.includes('简洁')) {
        return route.fulfill({
          status: 409,
          contentType: 'application/json',
          body: JSON.stringify({
            detail: '与现有记忆过于相似',
            existing: { id: 'mem-1', content: '用户偏好简洁的中文回答', similarity: 0.87 },
          }),
        })
      }
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({ data: { ...memories[0], id: 'mem-new', content: body.content } }),
      })
    }
    if (path === '/api/v2/super-assistant/reflection/settings') {
      if (method === 'PUT') {
        const body = request.postDataJSON() as { auto_accept_enabled: boolean }
        autoAccept = body.auto_accept_enabled
      }
      return json(route, {
        auto_accept_enabled: autoAccept,
        palace_index: null,
        profile: null,
        memory_count: memories.length,
        pending_count: pendingCount,
      })
    }
    if (path === '/api/v2/super-assistant/reflection/candidates') {
      return json(route, candidatePending ? [{
        id: 'cand-1', run_id: 'run-1', conversation_id: 'conv-1',
        kind: 'memory', status: 'pending', confidence: 'medium',
        payload: { content: '用户正在评估知识库收敛方案', zone: 'work', tags: [], pinned: false, supersedes: [] },
        decision: null, created_at: now, decided_at: null,
      }] : [])
    }
    if (path.startsWith('/api/v2/super-assistant/reflection/candidates/') && method === 'POST') {
      const body = request.postDataJSON() as { decision: string }
      decisions.push(body)
      candidatePending = false
      pendingCount = 0
      return json(route, {
        id: 'cand-1', run_id: 'run-1', conversation_id: 'conv-1',
        kind: 'memory', status: 'accepted', confidence: 'medium',
        payload: {}, decision: body.decision, created_at: now, decided_at: now,
      })
    }
    return route.fulfill({ status: 404, body: '{}' })
  })

  await page.goto('/#/super-assistant')
  // 配置面板默认收起：点击展开
  const configToggle = page.locator('button[title="助手配置"]')
  if ((await configToggle.getAttribute('aria-expanded')) !== 'true') await configToggle.click()

  // 待审批：接受一条 memory 候选
  await page.getByRole('button', { name: '待审批' }).click()
  await expect(page.getByTestId('candidate-memory')).toBeVisible()
  await expect(page.getByText('用户正在评估知识库收敛方案')).toBeVisible()
  await page.getByRole('button', { name: '接受', exact: true }).click()
  await expect(page.getByText('没有待审批的候选')).toBeVisible()
  expect(decisions).toEqual([{ decision: 'accept' }])

  // 记忆：列表、搜索过滤、auto-accept 开关、409 冲突提示
  await page.getByRole('button', { name: '记忆', exact: true }).click()
  await expect(page.getByTestId('memory-item')).toHaveCount(2)
  await expect(page.getByText('用户偏好简洁的中文回答')).toBeVisible()

  const autoAcceptSwitch = page.getByTestId('auto-accept-switch')
  await expect(autoAcceptSwitch).toHaveAttribute('aria-checked', 'true')
  await autoAcceptSwitch.click()
  await expect(autoAcceptSwitch).toHaveAttribute('aria-checked', 'false')

  await page.getByPlaceholder('搜索记忆内容或标签…').fill('本体')
  await expect(page.getByTestId('memory-item')).toHaveCount(1)
  await page.getByPlaceholder('搜索记忆内容或标签…').fill('')

  await page.getByRole('button', { name: '新增记忆' }).click()
  await page.getByPlaceholder(/要记住的事实/).fill('用户偏好简洁的回答风格')
  await page.getByRole('button', { name: '保存记忆' }).click()
  await expect(page.getByText('与现有记忆过于相似')).toBeVisible()
  await expect(page.getByText(/相似度 87%/)).toBeVisible()
})

test('超级助手：蒸馏收敛与 Skill 常驻', async ({ page }) => {
  await seedAuth(page)

  let alwaysActive = false
  let distilled = false
  const skillPatches: Array<Record<string, unknown>> = []
  const distillBodies: Array<Record<string, unknown>> = []

  const skill = {
    id: 'skill-1', name: 'research-helper', description: '检索助手',
    manifest: [{ path: 'SKILL.md', size: 128, editable: true }],
    enabled: true, always_active: false, use_count: 0, last_used_at: null,
    revision: 1, created_at: now, updated_at: now,
  }
  const memA = {
    id: 'mem-a', content: '用户偏好简洁的中文回答', zone: 'general', pinned: false,
    confidence: 'medium', source: 'user', tags: [], supersedes: [], superseded: false,
    match_count: 2, reference_count: 1, last_accessed_at: null,
    created_at: now, updated_at: now,
  }
  const memB = { ...memA, id: 'mem-b', content: '用户喜欢简短中文回复', match_count: 0, reference_count: 0 }
  const merged = { ...memA, id: 'mem-merged', content: '用户偏好简洁的中文回答（合并）' }
  const cluster = {
    cluster_key: 'cluster-1',
    members: [
      { id: 'mem-a', content: memA.content, zone: 'general', pinned: false, match_count: 2, reference_count: 1, created_at: now },
      { id: 'mem-b', content: memB.content, zone: 'general', pinned: false, match_count: 0, reference_count: 0, created_at: now },
    ],
    survivor_id: 'mem-a',
    protected: false,
  }

  await page.route('**/api/v1/**', route => {
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
    const request = route.request()
    const path = new URL(request.url()).pathname
    const method = request.method()
    if (path === '/api/v2/super-assistant/conversations' && method === 'GET') {
      return json(route, [{
        id: 'conv-1', title: '测试会话', model_config_id: 'model-1', status: 'active',
        created_at: now, updated_at: now,
      }])
    }
    if (path === '/api/v2/super-assistant/conversations/conv-1/messages') return json(route, [])
    if (path === '/api/v2/super-assistant/mcp-servers') return json(route, [])
    if (path === '/api/v2/super-assistant/skills' && method === 'GET') {
      return json(route, [{ ...skill, always_active: alwaysActive }])
    }
    if (path === '/api/v2/super-assistant/skills/skill-1' && method === 'PATCH') {
      const body = request.postDataJSON() as Record<string, unknown>
      skillPatches.push(body)
      if (typeof body.always_active === 'boolean') alwaysActive = body.always_active
      return json(route, { ...skill, always_active: alwaysActive })
    }
    if (path === '/api/v2/super-assistant/memories' && method === 'GET') {
      return json(route, distilled ? [merged] : [memA, memB])
    }
    if (path === '/api/v2/super-assistant/memories/distill-report' && method === 'GET') {
      return json(route, { clusters: distilled ? [] : [cluster] })
    }
    if (path === '/api/v2/super-assistant/memories/distill' && method === 'POST') {
      const body = request.postDataJSON() as Record<string, unknown>
      distillBodies.push(body)
      distilled = true
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({ data: merged }),
      })
    }
    if (path === '/api/v2/super-assistant/reflection/settings') {
      return json(route, {
        auto_accept_enabled: true,
        palace_index: null,
        profile: null,
        memory_count: 2,
        pending_count: 0,
      })
    }
    if (path === '/api/v2/super-assistant/reflection/candidates') return json(route, [])
    return route.fulfill({ status: 404, body: '{}' })
  })

  await page.goto('/#/super-assistant')

  // 自主模式已内置：输入区不再提供切换开关
  await expect(page.getByTestId('agent-mode-toggle')).toHaveCount(0)

  // 配置面板默认收起：点击展开
  const configToggle = page.locator('button[title="助手配置"]')
  if ((await configToggle.getAttribute('aria-expanded')) !== 'true') await configToggle.click()

  // Skill 治理：未使用标记 + 「常驻」开关触发 PATCH 并刷新卡片
  await expect(page.getByText('未使用')).toBeVisible()
  await page.getByRole('switch', { name: '设为常驻 Skill research-helper' }).click()
  await expect.poll(() => skillPatches).toEqual([{ always_active: true }])
  await expect(page.getByRole('switch', { name: '取消常驻 Skill research-helper' })).toHaveAttribute('aria-checked', 'true')

  // 蒸馏收敛：打开报告、查看簇并「直接合并」，列表与报告同步刷新
  await page.getByRole('button', { name: '记忆', exact: true }).click()
  await expect(page.getByTestId('memory-item')).toHaveCount(2)
  await page.getByTestId('distill-open').click()
  const dialog = page.getByRole('dialog', { name: '记忆蒸馏收敛' })
  await expect(dialog.getByTestId('distill-cluster')).toHaveCount(1)
  await expect(dialog.getByText('相似记忆 2 条')).toBeVisible()
  await expect(dialog.getByText('建议保留')).toHaveCount(1)
  await expect(dialog.getByText('用户喜欢简短中文回复')).toBeVisible()
  await dialog.getByTestId('distill-merge').click()
  await expect.poll(() => distillBodies).toEqual([{ member_ids: ['mem-a', 'mem-b'], use_llm: false }])
  await expect(page.getByText('记忆已合并')).toBeVisible()
  await expect(dialog.getByText('没有可收敛的相似记忆簇')).toBeVisible()
  await expect(page.getByTestId('memory-item')).toHaveCount(1)
  await expect(page.getByText('用户偏好简洁的中文回答（合并）')).toBeVisible()
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
      return json(route, { auto_accept_enabled: true, palace_index: null, profile: null, memory_count: 0, pending_count: 0 })
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
