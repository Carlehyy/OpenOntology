import { expect, test, type Page, type Route } from '@playwright/test'

const ONTOLOGY_ID = 'ontology-hitl-reject'
const RELEASE_ID = 'release-hitl-reject'
const ACTION_LOG_ID = 'action-log-hitl-reject'

const pendingAction = {
  id: ACTION_LOG_ID,
  actionId: 'action-suspend-purchase',
  actionName: '暂停高风险采购单',
  objectInstanceId: 'purchase-order-instance-1001',
  parameters: {
    orderNumber: 'PO-1001',
    riskLevel: 'high',
  },
  actorId: null,
  executedAt: '2026-07-28T02:30:00Z',
  ontologyVersion: 'v20',
  objectTypeName: '采购订单',
  objectInstanceLabel: 'PO-1001',
  triggerSource: 'sentinel',
}

async function mockGovernance(
  page: Page,
  initialMode: 'success' | 'failure' = 'success',
  pendingOverrides: Partial<typeof pendingAction> = {},
) {
  let mode = initialMode
  const pending = { ...pendingAction, ...pendingOverrides }
  let pendingVisible = true
  let failureRelease: (() => void) | null = null
  let failureGate = new Promise<void>(resolve => {
    failureRelease = resolve
  })
  const decisionBodies: Array<Record<string, unknown>> = []

  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: {
          id: 'u-governance',
          username: 'governance-tester',
          role: 'admin',
        },
      },
      version: 0,
    }))
  })

  const json = (route: Route, data: unknown) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ data }),
  })

  await page.route('**/api/**', async route => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    if (!path.startsWith('/api/')) return route.continue()

    if (path === `/api/v1/ontologies/${ONTOLOGY_ID}`) {
      return json(route, {
        id: ONTOLOGY_ID,
        name: 'HITL 拒绝测试本体',
        domain: '供应链',
        description: '验证人工拒绝决策事实',
        status: 'published',
        version: 'v20',
        current_release_id: RELEASE_ID,
        current_release_version: 'v20',
        entity_count: 1,
        relation_count: 0,
        action_count: 1,
        sentinel_count: 1,
        created_by: 'u-governance',
        created_at: '2026-07-28T02:00:00Z',
        updated_at: '2026-07-28T02:00:00Z',
      })
    }
    if (path === `/api/v2/formal/ontologies/${ONTOLOGY_ID}/pending-actions`) {
      return json(route, pendingVisible ? [pending] : [])
    }
    if (
      path === `/api/v2/formal/ontologies/${ONTOLOGY_ID}/action-logs/${ACTION_LOG_ID}/decide`
      && request.method() === 'POST'
    ) {
      decisionBodies.push(request.postDataJSON() as Record<string, unknown>)
      if (mode === 'failure') {
        await failureGate
        return route.fulfill({
          status: 409,
          contentType: 'application/json',
          body: JSON.stringify({
            detail: { message: '待办已被其他审批人处理' },
          }),
        })
      }
      pendingVisible = false
      return json(route, { id: ACTION_LOG_ID, status: 'rejected' })
    }
    if (path === `/api/v2/formal/ontologies/${ONTOLOGY_ID}/autonomy`) {
      return json(route, [{
        actionId: 'action-suspend-purchase',
        actionName: '暂停高风险采购单',
        requiresApproval: true,
        level: 'L1',
        shadow: false,
        sentinels: [{ id: 'sent-1', name: '高风险采购哨兵', muted: false, enabled: true }],
        decisions: { approved: 0, rejected: 0, total: 0, recentCount: 0, recentApprovalRate: null },
        autoRuns: { total: 0, failed: 0 },
        pending: 1,
        recommendation: null,
        recommendationReason: null,
        thresholds: { promoteMinDecisions: 10, promoteRate: 0.95 },
      }])
    }
    if (path === `/api/v2/formal/ontologies/${ONTOLOGY_ID}/facts/recent`) {
      return json(route, [])
    }
    if (path === `/api/v1/ontologies/${ONTOLOGY_ID}/sentinels/`) {
      return json(route, [])
    }
    if (path === `/api/v1/ontologies/${ONTOLOGY_ID}/sentinels/firings`) {
      return json(route, [])
    }
    if (path === '/api/v2/inbox/summary') {
      return json(route, { unread_count: 0 })
    }
    return json(route, [])
  })

  return {
    decisionBodies,
    releaseFailure: () => failureRelease?.(),
    useSuccess: () => {
      mode = 'success'
      failureGate = Promise.resolve()
    },
  }
}

test('HITL 拒绝弹窗不会重复展示相同的对象类型与实例标签', async ({ page }) => {
  await mockGovernance(page, 'success', { objectInstanceLabel: '采购订单' })
  await page.goto(`/#/ontologies/${ONTOLOGY_ID}?tab=governance`, {
    waitUntil: 'domcontentloaded',
  })

  await page.getByRole('button', { name: '查看待审批详情:暂停高风险采购单' }).click()
  await page.getByRole('dialog', { name: '待审批详情：暂停高风险采购单' })
    .getByRole('button', { name: '拒绝', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '拒绝动作：暂停高风险采购单' })
  await expect(dialog).toBeVisible()
  await expect(dialog).not.toContainText('采购订单 · 采购订单')
  await expect(dialog.getByText('采购订单', { exact: true })).toBeVisible()
})

test('HITL 拒绝弹窗取消不提交，确认时携带原因并展示成功反馈', async ({ page }) => {
  const mock = await mockGovernance(page)
  await page.goto(`/#/ontologies/${ONTOLOGY_ID}?tab=governance`, {
    waitUntil: 'domcontentloaded',
  })

  await page.getByRole('button', { name: '查看待审批详情:暂停高风险采购单' }).click()
  await page.getByRole('dialog', { name: '待审批详情：暂停高风险采购单' })
    .getByRole('button', { name: '拒绝', exact: true }).click()
  let dialog = page.getByRole('dialog', { name: '拒绝动作：暂停高风险采购单' })
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText('只会写入人工拒绝的决策事实')
  await expect(dialog).toContainText('不会执行动作，也不会修改目标对象')
  await expect(dialog.getByText('目标摘要', { exact: true })).toBeVisible()
  await expect(dialog).toContainText('采购订单 · PO-1001')
  await expect(dialog).toContainText('purchase-order-instance-1001')
  await expect(dialog).toContainText('orderNumber=PO-1001')

  const reason = dialog.getByLabel('拒绝原因')
  await expect(reason).toBeFocused()
  await expect(dialog.getByText(/可留空.*与拒绝结果一起记录到决策事实/)).toBeVisible()
  await reason.fill('风险证据不完整，请补充供应商审查记录')
  await reason.press('Escape')
  await expect(dialog).toHaveCount(0)
  expect(mock.decisionBodies).toHaveLength(0)

  await page.getByRole('button', { name: '查看待审批详情:暂停高风险采购单' }).click()
  await page.getByRole('dialog', { name: '待审批详情：暂停高风险采购单' })
    .getByRole('button', { name: '拒绝', exact: true }).click()
  dialog = page.getByRole('dialog', { name: '拒绝动作：暂停高风险采购单' })
  await dialog.getByLabel('拒绝原因').fill('风险证据不完整，请补充供应商审查记录')
  await dialog.getByRole('button', { name: '取消' }).click()
  await expect(dialog).toHaveCount(0)
  expect(mock.decisionBodies).toHaveLength(0)

  await page.getByRole('button', { name: '查看待审批详情:暂停高风险采购单' }).click()
  await page.getByRole('dialog', { name: '待审批详情：暂停高风险采购单' })
    .getByRole('button', { name: '拒绝', exact: true }).click()
  dialog = page.getByRole('dialog', { name: '拒绝动作：暂停高风险采购单' })
  await dialog.getByLabel('拒绝原因').fill('风险证据不完整，请补充供应商审查记录')
  await dialog.getByRole('button', { name: '确认拒绝' }).click()

  await expect.poll(() => mock.decisionBodies).toHaveLength(1)
  expect(mock.decisionBodies[0]).toEqual({
    decision: 'rejected',
    reason: '风险证据不完整，请补充供应商审查记录',
    releaseId: RELEASE_ID,
  })
  await expect(dialog).toHaveCount(0)
  await expect(
    page.locator('[data-sonner-toast]').filter({ hasText: '已拒绝。可在事实流查看记录。' }).first(),
  ).toBeVisible()
})

test('HITL 拒绝 API 失败时保留弹窗和输入，并恢复可重试状态', async ({ page }) => {
  const mock = await mockGovernance(page, 'failure')
  await page.goto(`/#/ontologies/${ONTOLOGY_ID}?tab=governance`, {
    waitUntil: 'domcontentloaded',
  })

  await page.getByRole('button', { name: '查看待审批详情:暂停高风险采购单' }).click()
  await page.getByRole('dialog', { name: '待审批详情：暂停高风险采购单' })
    .getByRole('button', { name: '拒绝', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '拒绝动作：暂停高风险采购单' })
  const reason = dialog.getByLabel('拒绝原因')
  const confirm = dialog.getByRole('button', { name: '确认拒绝' })
  await reason.fill('当前证据链不完整')
  await confirm.click()

  await expect.poll(() => mock.decisionBodies).toHaveLength(1)
  await expect(confirm).toBeDisabled()
  await expect(reason).toBeDisabled()
  mock.releaseFailure()

  await expect(dialog.getByRole('alert')).toContainText(
    '拒绝提交失败：待办已被其他审批人处理。请核对待办状态后重试。',
  )
  await expect(reason).toHaveValue('当前证据链不完整')
  await expect(reason).toBeEnabled()
  await expect(confirm).toBeEnabled()

  mock.useSuccess()
  await confirm.click()
  await expect.poll(() => mock.decisionBodies).toHaveLength(2)
  await expect(dialog).toHaveCount(0)
  await expect(
    page.locator('[data-sonner-toast]').filter({ hasText: '已拒绝。可在事实流查看记录。' }).first(),
  ).toBeVisible()
})
