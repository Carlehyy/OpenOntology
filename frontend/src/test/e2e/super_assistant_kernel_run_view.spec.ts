import { expect, test, type Page, type Route } from '@playwright/test'

// YIYA-36 kernel run 视图回归：审批项展示服务端持久摘要（目标/参数/范围），
// call 层按 confirmation.state 如实呈现——unknown=结果待核实、
// compensation_required=需人工处理，不再恒显「等待外部助手」；
// 审批拒绝只产生一次 decision=denied，绝不触发 approved。
const now = '2026-10-18T08:00:00+00:00'

const json = (route: Route, data: unknown) => route.fulfill({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify({ data }),
})

const confirmationFor = (state: string) => ({
  confirmationId: `conf-${state}`,
  state,
  display: {
    ontology: { id: 'onto-1', name: '供应商域' },
    action: { id: 'action-1', name: '下调供应商等级' },
    target: { id: 'inst-1', label: 'SUP-0042 华南金属' },
    parameters: [{ name: 'level', value: 'C', source: 'user_input' }],
    release: { id: 'rel-1', version: '7' },
    requiresApproval: false,
  },
})

const runView = {
  run_id: 'run-1',
  conversation_id: 'c-1',
  status: 'waiting_approval',
  wait_reason: 'approval_required',
  version: 3,
  execution_version: 'kernel.v1',
  goal: '下调华南金属供应商等级',
  deadline: null,
  binding_snapshot: {},
  current_inbox: [
    {
      inbox_id: 'approval:ap-1',
      kind: 'approval_decision',
      question_id: null,
      approval_id: 'ap-1',
      expires_at: '2026-10-19T00:00:00Z',
      approval: {
        approval_id: 'ap-1', status: 'pending', call_id: 'call-1',
        target_summary: '供应商 SUP-0042 华南金属',
        parameter_summary: '{"level":"C"}',
        scope_summary: '供应商域 v7 写权限',
        expires_at: '2026-10-19T00:00:00Z', decided_at: null,
      },
    },
  ],
  calls: [
    {
      call_id: 'call-1', status: 'waiting_external', outcome: 'remote_running',
      capability_key: 'internal.opaque.uuid',
      confirmation: confirmationFor('executing'),
    },
    {
      call_id: 'call-2', status: 'reconciling', outcome: 'outcome_unknown',
      capability_key: 'internal.opaque.uuid', manual_attention: true,
      confirmation: confirmationFor('unknown'),
    },
    {
      call_id: 'call-3', status: 'reconciling', outcome: 'outcome_unknown',
      capability_key: 'internal.opaque.uuid', manual_attention: true,
      confirmation: confirmationFor('compensation_required'),
    },
    {
      call_id: 'call-4', status: 'closed', outcome: 'completed',
      capability_key: 'remote.report.fetch',
    },
  ],
  artifacts: [],
}

async function seedAuth(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: { id: 'u-1', username: 'kernel-view-tester', role: 'admin' },
      },
      version: 0,
    }))
  })
}

test.describe('kernel run 视图（服务端 display 与 call 层如实状态）', () => {
  test('审批摘要完整可见；unknown/compensation call 显示待核实/需人工处理而非等待外部助手', async ({ page }) => {
    await seedAuth(page)
    const approvalDecisions: Array<{ approvalId: string; body: unknown }> = []
    let runSnapshot: Omit<typeof runView, 'wait_reason' | 'status'> & { wait_reason: string | null; status: string } = { ...runView }

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
          id: 'c-1', title: '长任务会话', model_config_id: 'model-1', status: 'active',
          created_at: now, updated_at: now,
        }])
      }
      if (path === '/api/v2/super-assistant/runs/run-1') return json(route, runSnapshot)
      if (path === '/api/v2/super-assistant/runs/run-1/events') {
        // 心跳节流：SSE 立即关闭会让重连循环打转；1.2s 心跳稳定循环节奏
        await new Promise(resolve => setTimeout(resolve, 1200))
        return route.fulfill({ status: 200, contentType: 'text/event-stream', body: ': heartbeat\n\n' })
      }
      const approvalMatch = path.match(/^\/api\/v2\/super-assistant\/runs\/run-1\/approvals\/([^/]+)\/decision$/)
      if (approvalMatch && method === 'POST') {
        approvalDecisions.push({ approvalId: approvalMatch[1], body: route.request().postDataJSON() })
        // 决定后 run 进入终态，卡片循环退出
        runSnapshot = { ...runSnapshot, status: 'cancelled', wait_reason: null, current_inbox: [] }
        return json(route, { command_id: 'cmd-1', status: 'denied', version: 4 })
      }
      if (path === '/api/v2/super-assistant/conversations/c-1/messages') return json(route, [])
      if (path === '/api/v2/super-assistant/conversations/c-1/files') return json(route, [])
      if (path === '/api/v2/super-assistant/conversations/c-1/pending-confirmation') return json(route, null)
      if (path === '/api/v2/super-assistant/skills') return json(route, [])
      if (path === '/api/v2/super-assistant/mcp-servers') return json(route, [])
      if (path === '/api/v2/super-assistant/tools') return json(route, [])
      if (path === '/api/v2/super-assistant/multica/config') return json(route, { enabled: false, commands: [] })
      if (path === '/api/v2/super-assistant/runs') return json(route, [])
      return route.fulfill({ status: 404, body: '{}' })
    })

    await page.goto('/#/super-assistant?run=run-1')
    const card = page.getByTestId('kernel-run-task-card')
    await expect(card).toBeVisible()

    // 审批摘要：服务端持久的目标/范围/参数如实展示，不只剩一行「需要审批」
    const approval = page.getByTestId('kernel-run-approval')
    await expect(approval).toBeVisible()
    await expect(approval.getByText('供应商 SUP-0042 华南金属')).toBeVisible()
    await expect(approval.getByText('供应商域 v7 写权限')).toBeVisible()
    await expect(approval.getByText('{"level":"C"}')).toBeVisible()

    // call 层：确认状态优先呈现，readable 动作名代替内部 capability uuid
    const calls = page.getByTestId('kernel-run-calls')
    await expect(calls).toBeVisible()
    const callRows = calls.getByTestId('kernel-run-call')
    await expect(callRows).toHaveCount(4)
    await expect(callRows.filter({ hasText: '执行结果待核实' })).toHaveCount(1)
    await expect(callRows.filter({ hasText: '需人工处理' })).toHaveCount(1)
    // 补偿完整文案（徽章与确认详情摘要各出现一次，同一行内）
    await expect(callRows.filter({ hasText: '外部效果可能已发生，需人工处理' })).toHaveCount(1)
    // 不伪完成：任何 call 都不显示成功；opaque 内部 ID 不作为产品标题
    await expect(callRows.filter({ hasText: '已执行成功' })).toHaveCount(0)
    await expect(callRows.filter({ hasText: '已完成' })).toHaveCount(1) // 无确认的普通外部 call
    await expect(calls.getByText('internal.opaque.uuid')).toHaveCount(0)
    await expect(calls.getByText('下调供应商等级').first()).toBeVisible()

    // 拒绝审批：仅一条 decision=denied，无 approved
    await approval.getByRole('button', { name: '拒绝' }).click()
    await expect.poll(() => approvalDecisions.length).toBe(1)
    expect(approvalDecisions[0].approvalId).toBe('ap-1')
    expect((approvalDecisions[0].body as { decision: string }).decision).toBe('denied')
    // 决定后审批项随终态快照消失
    await expect(approval).toHaveCount(0)
    expect(approvalDecisions.filter(item => (item.body as { decision: string }).decision === 'approved')).toHaveLength(0)
  })
})
