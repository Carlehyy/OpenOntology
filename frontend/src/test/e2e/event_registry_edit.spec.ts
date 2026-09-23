import { expect, test, type Page, type Route } from '@playwright/test'

const eventItem = {
  id: 'event-1',
  eventNo: 'EVT-20260728-test01',
  title: '系统故障',
  description: '系统故障需要回溯',
  eventType: '系统告警',
  severity: 'critical',
  tags: [],
  payload: {},
  occurredAt: '2026-07-28T07:03:00',
  recordedAt: '2026-07-28T07:03:00',
  sourceType: 'platform',
  sourceLabel: '平台录入',
  sourceSystem: null,
  sourceRef: null,
  reporterType: 'user',
  reporterName: 'admin',
  ingestKeyId: null,
  clientIp: null,
  confidence: null,
  ontologyId: null,
  subjectRef: null,
  supersedesId: null,
  status: 'active',
  createdAt: '2026-07-28T07:03:00',
  updatedAt: '2026-07-28T07:03:00',
  attachmentCount: 1,
}

async function mockEventRegistry(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: { token: 'e2e-token', user: { id: 'u1', username: 'admin', role: 'admin' } },
      version: 0,
    }))
  })

  const calls = {
    deletedAttachmentIds: [] as string[],
    uploadedFilenames: [] as string[],
    updatedBodies: [] as Record<string, unknown>[],
    createdBodies: [] as Record<string, unknown>[],
  }

  await page.route(/^https?:\/\/[^/]+\/api\/v[12]\//, async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    const ok = (data: unknown, status = 200) => route.fulfill({
      status,
      contentType: 'application/json',
      body: JSON.stringify({ data, message: 'ok' }),
    })

    if (url.pathname === '/api/v2/events/stats/summary') {
      return ok({
        total: 1,
        active: 1,
        archived: 0,
        platform: 1,
        api: 0,
        today: 1,
        bySeverity: { critical: 1, high: 0, medium: 0, low: 0, info: 0 },
        trend7d: [],
      })
    }
    if (url.pathname === '/api/v2/events' && request.method() === 'GET') {
      return ok({ items: [eventItem], total: 1, page: 1, pageSize: 8 })
    }
    if (url.pathname === '/api/v2/events/event-1' && request.method() === 'GET') {
      return ok({
        ...eventItem,
        attachments: [{
          id: 'attachment-1',
          eventId: 'event-1',
          filename: '故障报告.pdf',
          fileSize: 4096,
          mimeType: 'application/pdf',
          sha256: 'test-sha',
          uploadedBy: 'admin',
          createdAt: '2026-07-28T07:04:00',
        }],
        auditTrail: [],
      })
    }
    if (url.pathname === '/api/v2/events' && request.method() === 'POST') {
      calls.createdBodies.push(request.postDataJSON())
      return ok({
        ...eventItem,
        id: 'event-created',
        eventNo: 'EVT-20260822-new01',
        attachmentCount: 0,
      }, 201)
    }
    if (url.pathname === '/api/v2/events/event-1' && request.method() === 'PATCH') {
      calls.updatedBodies.push(request.postDataJSON())
      return ok(eventItem)
    }
    if (url.pathname === '/api/v2/events/event-1/attachments/attachment-1' && request.method() === 'DELETE') {
      calls.deletedAttachmentIds.push('attachment-1')
      return ok({ status: 'deleted', id: 'attachment-1' })
    }
    if (url.pathname === '/api/v2/events/event-1/attachments' && request.method() === 'POST') {
      calls.uploadedFilenames.push(request.postDataBuffer()?.toString('utf8').includes('补充说明.txt') ? '补充说明.txt' : '')
      return ok({
        id: 'attachment-2',
        eventId: 'event-1',
        filename: '补充说明.txt',
        fileSize: 12,
        mimeType: 'text/plain',
        sha256: 'new-sha',
        uploadedBy: 'admin',
        createdAt: '2026-07-28T08:00:00',
      }, 201)
    }
    if (url.pathname === '/api/v1/ontologies') {
      return ok({ items: [], total: 0, page: 1, page_size: 100 })
    }
    if (url.pathname === '/api/v2/inbox/summary') {
      return ok({ unread: 0, actionable: 0 })
    }
    if (url.pathname === '/api/v2/inbox') {
      return ok({ items: [], total: 0 })
    }
    return ok({})
  })

  return calls
}

test('事件登记列表与编辑附件流程符合交互要求', async ({ page }) => {
  const calls = await mockEventRegistry(page)
  await page.goto('/#/events', { waitUntil: 'domcontentloaded' })

  await expect(page.getByRole('button', { name: '归档', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '已归档', exact: true })).toHaveCount(0)

  const eventHeader = page.getByRole('columnheader', { name: '事件', exact: true })
  const eventCell = page.getByRole('cell').filter({ hasText: '系统故障' }).first()
  await expect(eventHeader).toHaveCSS('text-align', 'left')
  await expect(eventCell).toHaveCSS('text-align', 'left')
  await expect(eventCell).not.toContainText('EVT-20260728-test01')

  const editButton = page.getByRole('button', { name: '编辑事件 系统故障' })
  await editButton.hover()
  await expect(page.getByRole('tooltip').filter({ hasText: '编辑事件' })).toHaveCSS('opacity', '1')
  await editButton.click()

  const dialog = page.getByRole('dialog', { name: '编辑事件' })
  await expect(dialog.getByText('故障报告.pdf')).toBeVisible()
  await expect(dialog.getByText('已有附件', { exact: false })).toBeVisible()
  await expect(dialog.getByLabel('事件标题', { exact: false })).toHaveAttribute('required', '')
  await expect(dialog.getByLabel('事件类型', { exact: false })).toHaveAttribute('required', '')
  // Radix combobox 触发器以 aria-required 表达必填语义
  await expect(dialog.getByLabel('严重程度', { exact: false })).toHaveAttribute('aria-required', 'true')
  await expect(dialog.getByLabel('详细描述', { exact: false })).toHaveAttribute('required', '')

  await dialog.getByRole('button', { name: '删除 故障报告.pdf' }).click()
  await expect(dialog.getByText('保存后删除', { exact: false })).toBeVisible()

  await dialog.locator('input[type="file"]').setInputFiles({
    name: '补充说明.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('new evidence'),
  })
  await expect(dialog.getByText('补充说明.txt')).toBeVisible()
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(dialog).toBeHidden()

  expect(calls.updatedBodies).toHaveLength(1)
  expect(calls.deletedAttachmentIds).toEqual(['attachment-1'])
  expect(calls.uploadedFilenames).toEqual(['补充说明.txt'])

  await page.getByRole('button', { name: '登记事件', exact: true }).click()
  const createDialog = page.getByRole('dialog', { name: '登记事件' })
  // 关联本体空值经哨兵渲染为可见的「（不关联本体）」选项（UX 评审 4.4）
  await expect(createDialog.getByText('（不关联本体）')).toBeVisible()
  await expect(createDialog.getByLabel('事件标题', { exact: false })).toHaveAttribute('maxlength', '500')
  // MYW-42 优化点4：必填校验一次报齐全部缺失项，无需多次提交试错。
  await createDialog.getByRole('button', { name: '登记', exact: true }).click()
  await expect(
    createDialog.getByText('请完善必填项：事件标题、事件类型、详细描述'),
  ).toBeVisible()
  // UX 评审 B3：提交后发现缺项的空字段标红并携带 aria-invalid
  await expect(createDialog.getByLabel('事件标题', { exact: false })).toHaveAttribute('aria-invalid', 'true')
  await expect(createDialog.getByLabel('事件类型', { exact: false })).toHaveAttribute('aria-invalid', 'true')
  await expect(createDialog.getByLabel('详细描述', { exact: false })).toHaveAttribute('aria-invalid', 'true')
  // 任一必填项输入后聚合告警即收起
  await createDialog.getByLabel('事件标题', { exact: false }).fill('新事件')
  await expect(createDialog.getByText('请完善必填项')).toBeHidden()
  await createDialog.getByLabel('事件类型', { exact: false }).fill('业务异常')
  await createDialog
    .getByLabel('详细描述', { exact: false })
    .fill('补齐全部必填项后即可提交')
  await createDialog.getByRole('button', { name: '登记', exact: true }).click()
  await expect(createDialog).toBeHidden()
  expect(calls.createdBodies).toHaveLength(1)
  expect(calls.createdBodies[0].title).toBe('新事件')
})
