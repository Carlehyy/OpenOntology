import { expect, test, type Page, type Route } from '@playwright/test'

// 任务实例（独立运行时功能域）：一级导航入口、模板列表、YAML 编排页
// （校验面板/片段插入）、实例看板分组、实例详情（SSE 时间线 + 审批/交活/
// 插话面板）。全部接口本地 mock，不触真实后端。

const json = (route: Route, data: unknown, status = 200) => route.fulfill({
  status,
  contentType: 'application/json',
  body: JSON.stringify({ data }),
})

const SPEC_YAML = `api_version: openontology.task/v1
kind: Workflow
metadata:
  name: 代码变更评审
nodes:
  analyze: { kind: agent, system: 分析变更 }
  gate: { kind: approval, approvers: [admin] }
  end_ok: { kind: terminal, outcome: success }
edges:
  - { from: analyze.done, to: gate }
  - { from: gate.approved, to: end_ok }
`

const SPEC_COMPILED = {
  api_version: 'openontology.task/v1',
  kind: 'Workflow',
  metadata: { name: '代码变更评审', description: null },
  contracts: {},
  nodes: {
    analyze: { kind: 'agent', system: '分析变更', model: null,
      timeout_minutes: 30, outputs: {} },
    gate: { kind: 'approval', approvers: ['admin'], expires_hours: 72 },
    end_ok: { kind: 'terminal', outcome: 'success' },
  },
  edges: [
    { from: 'analyze.done', to: 'gate', rework: false },
    { from: 'gate.approved', to: 'end_ok', rework: false },
  ],
  policies: { max_parallelism: 8, corrections_per_node: 2,
    rework_per_edge: 3, event_budget: 10000 },
}

const templatesFixture = [
  { id: 'tpl-1', name: '代码变更评审', description: '条件分流 + 关口审批',
    latest_revision_no: 3, latest_revision_id: 'rev-3',
    canonical_hash: 'a'.repeat(64), instance_count: 5,
    created_at: '2026-10-11T02:00:00Z', updated_at: '2026-10-11T03:00:00Z' },
]

const instancesFixture = {
  total: 3, page: 1, size: 100,
  items: [
    { id: 'ins-1', name: '登录页改版评审', goal: '评审登录页改版',
      status: 'active', needs_attention: true, template_revision_id: 'rev-3',
      created_by: 'u-1', created_at: '2026-10-11T03:10:00Z', finished_at: null,
      fail_reason: null, cancel_reason: null, nodes: [] },
    { id: 'ins-2', name: '缺陷修复流水', goal: '修复登录崩溃',
      status: 'active', needs_attention: false, template_revision_id: 'rev-3',
      created_by: 'u-1', created_at: '2026-10-11T03:20:00Z', finished_at: null,
      fail_reason: null, cancel_reason: null, nodes: [] },
    { id: 'ins-3', name: '历史评审', goal: '上周评审',
      status: 'completed', needs_attention: false, template_revision_id: 'rev-2',
      created_by: 'u-1', created_at: '2026-10-10T08:00:00Z',
      finished_at: '2026-10-10T09:00:00Z', fail_reason: null,
      cancel_reason: null, nodes: [] },
  ],
}

const instanceDetailFixture = {
  ...instancesFixture.items[0],
  nodes: [
    { node_run_id: 'nr-1', node_id: 'analyze', attempt_no: 1,
      status: 'completed', waiting: false, error: null, correction_count: 0,
      rework_count: 0, output: { summary: '涉及登录页' },
      dispatched_at: '2026-10-11T03:11:00Z', started_at: null,
      finished_at: '2026-10-11T03:12:00Z', created_at: '2026-10-11T03:11:00Z' },
    { node_run_id: 'nr-2', node_id: 'gate', attempt_no: 1,
      status: 'waiting_approval', waiting: true, error: null,
      correction_count: 0, rework_count: 0, output: null,
      dispatched_at: null, started_at: null, finished_at: null,
      created_at: '2026-10-11T03:12:00Z' },
  ],
  approvals: [
    { id: 'apr-1', node_run_id: 'nr-2', status: 'pending', reason: null,
      proposal: { inputs: { analyze: { summary: '涉及登录页' } } },
      expires_at: '2026-10-14T03:12:00Z', decided_by: null, decided_at: null },
  ],
  artifacts: [
    { id: 'art-1', node_run_id: 'nr-1', name: 'work-report.md',
      mime_type: 'text/markdown', size_bytes: 454,
      sha256: 'b'.repeat(64), created_at: '2026-10-11T03:12:00Z' },
  ],
  spec_snapshot: SPEC_COMPILED,
}

async function seedAuth(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: { id: 'u-1', username: 'taskinst-tester', role: 'admin' },
      },
      version: 0,
    }))
  })
}

async function mockApis(page: Page) {
  await page.route('**/api/v2/task-instances/templates**', route => {
    if (route.request().method() === 'GET') return json(route, templatesFixture)
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON() as { spec_yaml?: string }
      if (body.spec_yaml?.includes('INVALID_MARKER')) {
        return route.fulfill({
          status: 400, contentType: 'application/json',
          body: JSON.stringify({ detail: {
            code: 'TEMPLATE_INVALID', message: '语义校验未通过',
            errors: [{ code: 'CYCLE', message: '检测到环: a -> b -> a',
              ref: 'edges[1]' }],
          } }),
        })
      }
      return json(route, { ...templatesFixture[0], id: 'tpl-new',
        spec_yaml: body.spec_yaml, spec_compiled: SPEC_COMPILED })
    }
    return json(route, {})
  })
  await page.route('**/api/v2/task-instances/templates/validate', route =>
    json(route, { valid: true }))
  await page.route('**/api/v2/task-instances/templates/tpl-1', route =>
    json(route, { ...templatesFixture[0], spec_yaml: SPEC_YAML,
      spec_compiled: SPEC_COMPILED }))
  await page.route('**/api/v2/task-instances/templates/tpl-1/revisions', route =>
    json(route, [
      { id: 'rev-3', revision_no: 3, canonical_hash: 'a'.repeat(64),
        note: null, created_by: 'u-1', created_at: '2026-10-11T03:00:00Z',
        spec_yaml: SPEC_YAML },
    ]))
  await page.route('**/api/v2/task-instances/instances**', route => {
    const url = route.request().url()
    if (url.includes('/events')) {
      // SSE：快照首帧 + 若干事件后终态断流（块内 \n、块间 \n\n）
      const body = [
        [
          'event: instance.snapshot',
          'data: ' + JSON.stringify({ instance: {
            id: 'ins-1', name: '登录页改版评审', status: 'active',
            goal: '评审登录页改版' } }),
        ].join('\n'),
        [
          'id: ins-1:1',
          'event: node.dispatched',
          'data: ' + JSON.stringify({ node_id: 'analyze', attempt_no: 1 }),
        ].join('\n'),
        [
          'id: ins-1:2',
          'event: approval.requested',
          'data: ' + JSON.stringify({ node_id: 'gate', approval_id: 'apr-1' }),
        ].join('\n'),
        '',
      ].join('\n\n')
      return route.fulfill({
        status: 200, contentType: 'text/event-stream', body,
      })
    }
    if (url.match(/instances\/ins-1$/)) {
      return json(route, instanceDetailFixture)
    }
    if (route.request().method() === 'POST') {
      return json(route, instanceDetailFixture, 202)
    }
    return json(route, instancesFixture)
  })
}

test.describe('任务实例（mocked）', () => {
  test.beforeEach(async ({ page }) => {
    await seedAuth(page)
    await mockApis(page)
  })

  test('后台导航含任务实例入口，页面骨架与模板列表渲染', async ({ page }) => {
    await page.goto('/#/task-instances')
    await expect(page.getByRole('heading', { name: '任务实例' })).toBeVisible()
    await expect(page.getByText('代码变更评审').first()).toBeVisible()
    await expect(page.getByText('v3 · 5 次实例')).toBeVisible()
  })

  test('切换实例看板：四阶分组与需介入卡片', async ({ page }) => {
    await page.goto('/#/task-instances')
    await page.getByRole('button', { name: '实例看板' }).click()
    await expect(page.getByText('需介入').first()).toBeVisible()
    await expect(page.getByText('登录页改版评审').first()).toBeVisible()
    await expect(page.getByText('缺陷修复流水').first()).toBeVisible()
    await expect(page.getByText('历史评审').first()).toBeVisible()
  })

  test('编排页：编辑器 + 校验面板 + 版本历史 + 预览图', async ({ page }) => {
    await page.goto('/#/task-instances/templates/tpl-1')
    await expect(page.getByText('代码变更评审').first()).toBeVisible()
    await expect(page.getByRole('button', { name: '校验' })).toBeVisible()
    await expect(page.getByRole('button', { name: '保存新版本' })).toBeVisible()
    // 片段插入栏
    await expect(page.getByRole('button', { name: '+ condition 节点' })).toBeVisible()
    // 校验通过流
    await page.getByRole('button', { name: '校验' }).click()
    await expect(page.getByText('校验通过，可以保存')).toBeVisible()
    // 版本历史
    await page.getByRole('button', { name: '版本历史' }).click()
    await expect(page.getByText('v3').first()).toBeVisible()
    // 编译预览（xyflow 画布）
    await expect(page.getByText('编译预览')).toBeVisible()
  })

  test('实例详情：状态图 + 审批面板 + SSE 时间线 + 产物', async ({ page }) => {
    await page.goto('/#/task-instances/instances/ins-1')
    await expect(page.getByText('登录页改版评审').first()).toBeVisible()
    await expect(page.getByText('流程快照')).toBeVisible()
    // 审批关口面板（pending 审批 + 提案 JSON）
    await expect(page.getByText('审批关口 gate').first()).toBeVisible()
    await expect(page.getByRole('button', { name: '批准' })).toBeVisible()
    await expect(page.getByRole('button', { name: '驳回并打回' })).toBeVisible()
    // 产物清单
    await expect(page.getByText('work-report.md')).toBeVisible()
    // SSE 时间线事件（断流前送达的两条）
    await expect(page.getByText('节点派发 · analyze').first()).toBeVisible({
      timeout: 8000 })
    await expect(page.getByText('等待审批 · gate').first()).toBeVisible()
  })

  test('审批驳回交互：必填理由内联提交', async ({ page }) => {
    await page.goto('/#/task-instances/instances/ins-1')
    await expect(page.getByText('审批关口 gate').first()).toBeVisible()
    await page.getByLabel('驳回理由（驳回时必填）')
      .fill('补丁不完整，缺少回归测试')
    let decided = false
    await page.route('**/approvals/apr-1/decision', route => {
      decided = true
      expect(route.request().method()).toBe('POST')
      const body = route.request().postDataJSON() as {
        decision: string; reason: string }
      expect(body.decision).toBe('rejected')
      expect(body.reason).toContain('回归测试')
      return json(route, { ...instanceDetailFixture, status: 'active' })
    })
    await page.getByRole('button', { name: '驳回并打回' }).click()
    await expect.poll(() => decided).toBe(true)
  })
})
