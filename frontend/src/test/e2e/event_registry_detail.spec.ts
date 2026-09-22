import { expect, test, type Page, type Route } from '@playwright/test'

const activeEvent = {
  id: 'event-1',
  eventNo: 'EVT-20260808-0001',
  title: '产线停机',
  description: 'A3 产线 E502 故障停机两小时，已联系设备供应商到场检修，等待备件。',
  eventType: '设备异常',
  severity: 'high',
  tags: ['产线', '停机'],
  payload: { line: 'A3', code: 'E502' },
  occurredAt: '2026-08-08T01:12:00Z',
  recordedAt: '2026-08-08T01:12:30Z',
  sourceType: 'api',
  sourceLabel: '第三方·MES产线网关',
  sourceSystem: 'MES',
  sourceRef: 'WO-2026-0007',
  reporterType: 'service',
  reporterName: 'MES产线网关',
  ingestKeyId: 'key-1',
  clientIp: '10.1.2.3',
  confidence: 0.92,
  ontologyId: 'ont-1',
  subjectRef: null,
  supersedesId: null,
  status: 'active',
  createdAt: '2026-08-08T01:12:30Z',
  updatedAt: '2026-08-08T01:12:30Z',
  attachmentCount: 1,
}

const archivedEvent = {
  ...activeEvent,
  id: 'event-2',
  eventNo: 'EVT-20260801-0002',
  title: '旧告警已处理',
  severity: 'info',
  tags: [],
  payload: {},
  status: 'archived',
  attachmentCount: 0,
}

const ingestKey = {
  id: 'key-1',
  name: 'MES产线网关',
  keyPrefix: 'ob_ingest_ab12',
  enabled: true,
  allowedSourceSystem: 'MES',
  createdBy: 'admin',
  createdAt: '2026-08-01T00:00:00Z',
  lastUsedAt: '2026-08-08T01:12:30Z',
  revokedAt: null,
}

async function mockEventRegistry(page: Page, options: { role?: string; slowCreate?: boolean; failListAttempts?: number } = {}) {
  const { role = 'admin', slowCreate = false, failListAttempts = 0 } = options
  await page.addInitScript((userRole: string) => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: { token: 'e2e-token', user: { id: 'u1', username: 'tester', role: userRole } },
      version: 0,
    }))
  }, role)

  const calls = {
    listQueries: [] as (string | null)[],
    revokedKeyIds: [] as string[],
    createdBodies: [] as Record<string, unknown>[],
    statusChanges: [] as string[],
    keyStatusParams: [] as (string | null)[],
  }
  // slowCreate 时用闸门卡住 POST 响应，由测试显式放行，避免固定延时与断言时序赛跑
  let releaseCreate: () => void = () => {}
  const createGate = slowCreate ? new Promise<void>(resolve => { releaseCreate = resolve }) : null
  let listAttempts = 0

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
        total: 2, active: 1, archived: 1, platform: 0, api: 2, today: 1,
        bySeverity: { critical: 0, high: 1, medium: 0, low: 0, info: 0 },
        trend7d: [],
      })
    }
    if (url.pathname === '/api/v2/events' && request.method() === 'GET') {
      calls.listQueries.push(url.searchParams.get('q'))
      // 应用层 QueryClient retry: 1——需连续失败 2 次（首次 + 自动重试）才会进入错误态
      if (listAttempts < failListAttempts) {
        listAttempts += 1
        return route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'boom' }) })
      }
      // 带关键字的请求模拟「无匹配」场景，供空态与分页断言复用
      if (url.searchParams.get('q')) {
        return ok({ items: [], total: 0, page: 1, pageSize: 8 })
      }
      return ok({ items: [activeEvent, archivedEvent], total: 2, page: 1, pageSize: 8 })
    }
    if (url.pathname === '/api/v2/events' && request.method() === 'POST') {
      calls.createdBodies.push(request.postDataJSON())
      if (createGate) await createGate
      return ok({ ...activeEvent, id: 'event-new', eventNo: 'EVT-20260808-0009', attachmentCount: 0 }, 201)
    }
    if (/^\/api\/v2\/events\/[^/]+\/status$/.test(url.pathname) && request.method() === 'POST') {
      const body = request.postDataJSON() as { status: string }
      calls.statusChanges.push(body.status)
      return ok({ ...activeEvent, status: body.status })
    }
    if (url.pathname === '/api/v2/events/event-1' && request.method() === 'GET') {
      return ok({
        ...activeEvent,
        attachments: [{
          id: 'att-1', eventId: 'event-1', filename: '停机照片.jpg', fileSize: 204800,
          mimeType: 'image/jpeg', sha256: 'sha', uploadedBy: 'MES产线网关', createdAt: '2026-08-08T01:13:00Z',
        }],
        auditTrail: [
          {
            id: 'audit-1', seq: 1, action: 'ingested', actorType: 'service', actorId: 'key-1',
            actorName: 'MES产线网关', changes: null, note: null, ip: '10.1.2.3', createdAt: '2026-08-08T01:12:30Z',
          },
          {
            id: 'audit-2', seq: 2, action: 'updated', actorType: 'user', actorId: 'u1',
            actorName: 'admin', changes: { severity: { from: 'medium', to: 'high' } },
            note: '升级严重度', ip: null, createdAt: '2026-08-08T02:00:00Z',
          },
        ],
      })
    }
    if (url.pathname === '/api/v2/events/ingest-keys' && request.method() === 'GET') {
      calls.keyStatusParams.push(url.searchParams.get('status'))
      return ok({ items: [ingestKey], total: 1, page: 1, pageSize: 5 })
    }
    if (url.pathname === '/api/v2/events/ingest-keys/key-1' && request.method() === 'DELETE') {
      calls.revokedKeyIds.push('key-1')
      return ok({ ...ingestKey, enabled: false, revokedAt: '2026-08-08T03:00:00Z' })
    }
    if (url.pathname === '/api/v1/ontologies') {
      return ok({ items: [{ id: 'ont-1', name: '供应链本体' }], total: 1, page: 1, page_size: 100 })
    }
    if (url.pathname === '/api/v2/inbox/summary') return ok({ unread: 0, actionable: 0 })
    if (url.pathname === '/api/v2/inbox') return ok({ items: [], total: 0 })
    return ok({})
  })

  return { calls, releaseCreate }
}

// Node 与浏览器共享本机时区：按本地时区计算期望串，使时区断言不依赖运行环境。
function localDateTime(iso: string): string {
  const date = new Date(iso)
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

test('点击行打开详情抽屉：编号、描述、payload、附件与审计轨迹完整可见', async ({ page }) => {
  await mockEventRegistry(page)
  await page.goto('/#/events', { waitUntil: 'domcontentloaded' })

  await page.getByRole('row', { name: /产线停机/ }).click()
  const drawer = page.locator('aside').filter({ hasText: '审计轨迹' })
  await expect(drawer.getByRole('heading', { name: '产线停机' })).toBeVisible()
  await expect(drawer.getByText('EVT-20260808-0001')).toBeVisible()
  await expect(drawer.getByRole('button', { name: '复制' }).first()).toBeVisible()
  await expect(drawer.getByText('供应链本体')).toBeVisible()
  await expect(drawer.getByText(/E502 故障停机两小时/)).toBeVisible()
  await expect(drawer.getByText('"line": "A3"')).toBeVisible()
  await expect(drawer.getByText('停机照片.jpg')).toBeVisible()
  await expect(drawer.getByText('WO-2026-0007')).toBeVisible()
  // UX 评审 A1：登记时间为 naive UTC 序列化，必须按 UTC 解析后展示本地钟点
  await expect(drawer.getByText(localDateTime('2026-08-08T01:12:30Z')).first()).toBeVisible()
  // 审计轨迹：最新在上，字段变更展示中文标签与前后值
  await expect(drawer.getByText('编辑', { exact: true }).first()).toBeVisible()
  await expect(drawer.getByText('第三方上传')).toBeVisible()
  await expect(drawer.getByText(/严重程度：中级/)).toBeVisible()
  await expect(drawer.getByText('升级严重度')).toBeVisible()

  // 手写抽屉支持 Esc 关闭（UX 评审 B5）
  await page.keyboard.press('Escape')
  await expect(drawer).toHaveCount(0)

  // 抽屉内点编辑 → 关闭抽屉并打开编辑弹窗
  await page.getByRole('row', { name: /产线停机/ }).click()
  await expect(drawer.getByRole('heading', { name: '产线停机' })).toBeVisible()
  await drawer.getByRole('button', { name: '编辑', exact: true }).click()
  await expect(page.getByRole('dialog', { name: '编辑事件' })).toBeVisible()
  await expect(drawer).toHaveCount(0)
})

test('提交进行中禁止关闭弹窗；登记成功 toast 展示事件编号', async ({ page }) => {
  const { releaseCreate } = await mockEventRegistry(page, { slowCreate: true })
  await page.goto('/#/events', { waitUntil: 'domcontentloaded' })

  await page.getByRole('button', { name: '登记事件', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '登记事件' })
  await dialog.getByLabel('事件标题', { exact: false }).fill('急报：仓库断电')
  await dialog.getByLabel('事件类型', { exact: false }).fill('安全事件')
  await dialog.getByLabel('详细描述', { exact: false }).fill('断电持续十分钟')
  await dialog.getByRole('button', { name: '登记', exact: true }).click()

  // 提交进行中（POST 被闸门卡住）：等 pending 态渲染完成后，Esc 与遮罩点击都不能关闭弹窗
  await expect(dialog.getByRole('button', { name: '登记', exact: true })).toBeDisabled()
  await page.keyboard.press('Escape')
  await page.mouse.click(8, 8)
  await expect(dialog).toBeVisible()

  releaseCreate()
  const toast = page.locator('[data-sonner-toast]').filter({ hasText: '事件登记成功' })
  await expect(toast).toBeVisible()
  await expect(toast).toContainText('EVT-20260808-0009')
  await expect(dialog).toBeHidden()
})

test('全部 Tab 下归档事件展示状态徽标；非管理员不渲染删除按钮', async ({ page }) => {
  await mockEventRegistry(page)
  await page.goto('/#/events', { waitUntil: 'domcontentloaded' })

  await page.getByRole('button', { name: '全部', exact: true }).click()
  const archivedRow = page.getByRole('row', { name: /旧告警已处理/ })
  await expect(archivedRow.getByText('已归档', { exact: true })).toBeVisible()
  await expect(page.getByRole('row', { name: /产线停机/ }).getByText('已归档', { exact: true })).toHaveCount(0)
  // 管理员可见删除按钮
  await expect(page.getByRole('button', { name: '删除事件 产线停机' })).toBeVisible()
})

test('非管理员（editor）看不到删除按钮与接入管理入口', async ({ page }) => {
  await mockEventRegistry(page, { role: 'editor' })
  await page.goto('/#/events', { waitUntil: 'domcontentloaded' })

  await expect(page.getByRole('row', { name: /产线停机/ })).toBeVisible()
  await expect(page.getByRole('button', { name: /删除事件/ })).toHaveCount(0)
  // UX 评审 B6：密钥接口仅管理员可用，入口不应对其他角色渲染
  await expect(page.getByRole('button', { name: /接入管理/ })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '编辑事件 产线停机' })).toBeVisible()
})

test('吊销密钥必须二次确认，取消不发请求', async ({ page }) => {
  const { calls } = await mockEventRegistry(page)
  await page.goto('/#/events', { waitUntil: 'domcontentloaded' })

  await page.getByRole('button', { name: /接入管理/ }).click()
  const drawer = page.locator('aside').filter({ hasText: '已有密钥' })
  await expect(drawer.getByText('MES产线网关')).toBeVisible()

  // 取消路径
  await drawer.getByRole('button', { name: '吊销', exact: true }).click()
  const confirm = page.getByRole('dialog', { name: '吊销密钥' })
  await expect(confirm).toBeVisible()
  await expect(confirm).toContainText('立即无法上报事件')
  await confirm.getByRole('button', { name: '取消' }).click()
  await expect(confirm).toBeHidden()
  expect(calls.revokedKeyIds).toHaveLength(0)

  // 确认路径
  await drawer.getByRole('button', { name: '吊销', exact: true }).click()
  await page.getByRole('dialog', { name: '吊销密钥' }).getByRole('button', { name: '确认吊销' }).click()
  await expect.poll(() => calls.revokedKeyIds.length).toBe(1)
})

test('搜索输入防抖：快速连续输入只触发一次带关键字请求', async ({ page }) => {
  const { calls } = await mockEventRegistry(page)
  await page.goto('/#/events', { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('row', { name: /产线停机/ })).toBeVisible()

  await page.getByPlaceholder('搜索事件标题、编号、上报人...').pressSequentially('停机', { delay: 60 })
  await expect.poll(() => calls.listQueries.filter(q => q === '停机').length, { timeout: 3000 }).toBe(1)
  expect(calls.listQueries.filter(q => q && q !== '停机')).toEqual([])
})

test('归档需二次确认且文案指明恢复路径；恢复保持即时', async ({ page }) => {
  const { calls } = await mockEventRegistry(page)
  await page.goto('/#/events', { waitUntil: 'domcontentloaded' })

  // 归档：先确认，取消不发请求
  await page.getByRole('button', { name: '归档事件 产线停机' }).click()
  const confirm = page.getByRole('dialog', { name: '归档事件' })
  await expect(confirm).toBeVisible()
  await expect(confirm).toContainText('「归档」页签中查看或恢复')
  await confirm.getByRole('button', { name: '取消' }).click()
  await expect(confirm).toBeHidden()
  expect(calls.statusChanges).toHaveLength(0)

  // 确认后才发状态变更
  await page.getByRole('button', { name: '归档事件 产线停机' }).click()
  await page.getByRole('dialog', { name: '归档事件' }).getByRole('button', { name: '确认归档' }).click()
  await expect.poll(() => calls.statusChanges).toEqual(['archived'])

  // 恢复：低风险，保持即时执行
  await page.getByRole('button', { name: '恢复事件 旧告警已处理' }).click()
  await expect.poll(() => calls.statusChanges).toEqual(['archived', 'active'])
})

test('密钥状态筛选选中「全部状态」回发 all，不发空串', async ({ page }) => {
  const { calls } = await mockEventRegistry(page)
  await page.goto('/#/events', { waitUntil: 'domcontentloaded' })

  await page.getByRole('button', { name: /接入管理/ }).click()
  const drawer = page.locator('aside').filter({ hasText: '已有密钥' })
  await expect(drawer.getByText('MES产线网关')).toBeVisible()

  const statusSelect = drawer.getByRole('combobox', { name: '密钥状态' })
  await statusSelect.click()
  await page.getByRole('option', { name: '有效' }).click()
  await expect.poll(() => calls.keyStatusParams.at(-1)).toBe('active')

  // UX 评审 A2：选回「全部状态」曾把空串发给后端导致 422 被吞成空列表
  await statusSelect.click()
  await page.getByRole('option', { name: '全部状态' }).click()
  await expect(statusSelect).toContainText('全部状态')
  // 全局 staleTime 下同 key 命中缓存不发请求；借搜索改动 query key 验证回发值为 all
  await drawer.getByPlaceholder('搜索名称、前缀或来源').fill('MES')
  await expect.poll(() => calls.keyStatusParams.at(-1)).toBe('all')
  expect(calls.keyStatusParams).not.toContain('')
  // 选中「全部状态」后列表仍能看到密钥，而不是错误空态
  await expect(drawer.getByText('MES产线网关')).toBeVisible()

  // 接入管理抽屉同样支持 Esc 关闭（UX 评审 B5）
  await page.keyboard.press('Escape')
  await expect(drawer).toHaveCount(0)
})

test('列表加载失败展示错误态与重试，不与空列表混淆', async ({ page }) => {
  await mockEventRegistry(page, { failListAttempts: 2 })
  await page.goto('/#/events', { waitUntil: 'domcontentloaded' })

  const alert = page.getByRole('alert').filter({ hasText: '事件列表加载失败' })
  await expect(alert).toBeVisible()
  // 失败态不渲染空态的「暂无匹配事件」引导
  await expect(page.getByText('暂无匹配事件')).toHaveCount(0)

  await alert.getByRole('button', { name: '重新加载' }).click()
  await expect(page.getByRole('row', { name: /产线停机/ })).toBeVisible()
})

test('搜索无结果展示空态引导，分页不出现「1–0」伪影', async ({ page }) => {
  await mockEventRegistry(page)
  await page.goto('/#/events', { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('row', { name: /产线停机/ })).toBeVisible()

  await page.getByPlaceholder('搜索事件标题、编号、上报人...').fill('不存在的关键字')
  await expect(page.getByText('暂无匹配事件')).toBeVisible()
  // UX 评审 A3：空结果时底栏应显示「共 0 条」，而不是「显示 1–0 / 0」
  await expect(page.getByTestId('events-pagination-info')).toHaveText('共 0 条')
  await expect(page.getByRole('button', { name: '立即登记' })).toBeVisible()
})
