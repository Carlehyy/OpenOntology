import { expect, test, type Page, type Route } from '@playwright/test'

const pendingTicket = {
  id: 'tk-1',
  ticketNo: 'TK-20260828-A1B2C3',
  title: '登录页偶尔白屏',
  content: '生产环境登录后偶发白屏，刷新一次恢复，控制台报 ChunkLoadError。',
  status: 'pending',
  category: 'system_fault',
  pageUrl: 'http://10.68.17.78:5173/#/login?returnTo=%2Foverview',
  submitterId: 'u-editor',
  submitterName: 'editor',
  createdAt: '2026-08-28T02:00:00Z',
  updatedAt: '2026-08-28T02:00:00Z',
  attachmentCount: 1,
}

const verifyingTicket = {
  ...pendingTicket,
  id: 'tk-2',
  ticketNo: 'TK-20260828-D4E5F6',
  title: '导出 CSV 很慢',
  content: '十万行数据导出需要三分钟，希望能加快。',
  status: 'verifying',
  category: 'experience',
  pageUrl: null,
  createdAt: '2026-08-27T08:30:00Z',
  updatedAt: '2026-08-28T01:00:00Z',
  attachmentCount: 0,
}

const detailBody = {
  ...pendingTicket,
  attachments: [{
    id: 'att-1', ticketId: 'tk-1', filename: '白屏截图.png', fileSize: 204800,
    mimeType: 'image/png', sha256: 'sha', uploadedBy: 'u-editor',
    createdAt: '2026-08-28T02:01:00Z',
  }],
  progressLogs: [{
    id: 'log-1', seq: 1, fromStatus: 'pending', toStatus: 'verifying',
    comment: '已复现，等待用户提供浏览器版本', actorId: 'u-admin', actorName: 'admin',
    createdAt: '2026-08-28T03:00:00Z',
  }],
}

const IMAGE_PNG_BASE64 =
  'iVBORw0KGgoAAAANSUhEUgAAAAoAAAAKCAYAAACNMs+9AAAAFUlEQVR42mP8z8BQz0AEYBxVOKwliOOTAOn4zxO+AAAAAElFTkSuQmCC'

async function mockTickets(page: Page, options: { role?: string } = {}) {
  const { role = 'admin' } = options
  await page.addInitScript((userRole: string) => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: { token: 'e2e-token', user: { id: 'u-current', username: userRole === 'admin' ? 'admin' : 'editor', role: userRole } },
      version: 0,
    }))
  }, role)

  const calls = {
    progressBodies: [] as Record<string, unknown>[],
    createdBodies: [] as Record<string, unknown>[],
    uploadedFiles: [] as string[],
    listStatusFilters: [] as (string | null)[],
    previewedAttachmentIds: [] as string[],
  }
  let progressCount = 0

  await page.route(/^https?:\/\/[^/]+\/api\/v[12]\//, async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    const ok = (data: unknown, status = 200) => route.fulfill({
      status,
      contentType: 'application/json',
      body: JSON.stringify({ data, message: 'ok' }),
    })

    if (url.pathname === '/api/v2/tickets/stats/summary') {
      return ok({
        total: 2,
        byStatus: { pending: 1, verifying: 1, accepted: 0, completed: 0, cancelled: 0 },
      })
    }
    if (url.pathname === '/api/v2/tickets' && request.method() === 'GET') {
      calls.listStatusFilters.push(url.searchParams.get('status'))
      // 弹窗取「处理中」多状态最近工单；页面单状态/全部走完整列表
      if (url.searchParams.get('status') === 'pending,verifying,accepted') {
        return ok({ items: [pendingTicket, verifyingTicket], total: 2, page: 1, pageSize: 10 })
      }
      return ok({ items: [pendingTicket, verifyingTicket], total: 2, page: 1, pageSize: 8 })
    }
    if (url.pathname === '/api/v2/tickets' && request.method() === 'POST') {
      calls.createdBodies.push(request.postDataJSON())
      return ok({
        ...pendingTicket, id: 'tk-new', ticketNo: 'TK-20260828-0009',
        title: request.postDataJSON().title, content: request.postDataJSON().content,
        category: request.postDataJSON().category ?? 'other',
        pageUrl: request.postDataJSON().pageUrl ?? null,
        submitterId: 'u-current', submitterName: role === 'admin' ? 'admin' : 'editor',
        attachmentCount: 0,
      }, 201)
    }
    if (url.pathname === '/api/v2/tickets/tk-1' && request.method() === 'GET') {
      return ok(detailBody)
    }
    if (url.pathname === '/api/v2/tickets/tk-1/attachments/att-1/download' && request.method() === 'GET') {
      calls.previewedAttachmentIds.push('att-1')
      return route.fulfill({
        status: 200,
        contentType: 'image/png',
        body: Buffer.from(IMAGE_PNG_BASE64, 'base64'),
      })
    }
    if (url.pathname === '/api/v2/tickets/tk-1/progress' && request.method() === 'POST') {
      calls.progressBodies.push(request.postDataJSON())
      progressCount += 1
      return ok({
        ...detailBody,
        status: 'accepted',
        progressLogs: [
          ...detailBody.progressLogs,
          {
            id: `log-new-${progressCount}`, seq: 2, fromStatus: 'pending',
            toStatus: 'accepted', comment: request.postDataJSON().comment,
            actorId: 'u-admin', actorName: 'admin', createdAt: '2026-08-28T04:00:00Z',
          },
        ],
      })
    }
    if (url.pathname === '/api/v2/tickets/tk-new/attachments' && request.method() === 'POST') {
      calls.uploadedFiles.push(request.postData()?.split('filename="')[1]?.split('"')[0] ?? '')
      return ok({
        id: 'att-new', ticketId: 'tk-new', filename: '复现步骤.txt', fileSize: 128,
        mimeType: 'text/plain', sha256: 'sha', uploadedBy: 'u-current',
        createdAt: '2026-08-28T05:00:00Z',
      }, 201)
    }
    if (url.pathname === '/api/v2/inbox/summary') return ok({ unread: 0, actionable: 0 })
    if (url.pathname === '/api/v2/inbox') return ok({ items: [], total: 0 })
    return ok({})
  })

  return { calls }
}

test('顶栏工单弹窗：位于收件箱左侧，展示处理中工单并可跳转/提单', async ({ page }) => {
  await mockTickets(page)
  await page.goto('/#/overview', { waitUntil: 'domcontentloaded' })

  const ticketButton = page.locator('button[aria-controls="global-ticket-popover"]')
  const inboxButton = page.getByRole('button', { name: /收件箱/ })
  await expect(ticketButton).toBeVisible()
  const ticketBox = await ticketButton.boundingBox()
  const inboxBox = await inboxButton.boundingBox()
  expect(ticketBox && inboxBox && ticketBox.x < inboxBox.x).toBe(true)

  // 点击图标出现弹窗（与收件箱同构），展示最近处理中的工单
  await ticketButton.click()
  const popover = page.locator('#global-ticket-popover')
  await expect(popover).toBeVisible()
  await expect(popover.getByText('2 条处理中')).toBeVisible()
  await expect(popover.getByRole('button', { name: '打开工单：登录页偶尔白屏' })).toBeVisible()
  await expect(popover.getByRole('button', { name: '打开工单：导出 CSV 很慢' }).getByText('查验中')).toBeVisible()

  // 底部「查看全部工单」进入工单页
  await popover.getByRole('button', { name: '查看全部工单' }).click()
  await expect(page).toHaveURL(/#\/tickets$/)
  await expect(page.getByRole('tab', { name: '系统设置 · 工单反馈' })).toBeVisible()
  await expect(page.getByRole('row', { name: /登录页偶尔白屏/ })).toBeVisible()
  await expect(page.getByText('展示全部用户的工单')).toBeVisible()
})

test('弹窗内点击工单深链定位详情；Esc 关闭弹窗', async ({ page }) => {
  await mockTickets(page)
  await page.goto('/#/overview', { waitUntil: 'domcontentloaded' })

  await page.locator('button[aria-controls="global-ticket-popover"]').click()
  const popover = page.locator('#global-ticket-popover')
  await popover.getByRole('button', { name: '打开工单：登录页偶尔白屏' }).click()
  await expect(page).toHaveURL(/#\/tickets\?focus=tk-1$/)

  // 深链自动打开对应工单的详情抽屉
  const drawer = page.locator('aside').filter({ hasText: '处理记录' })
  await expect(drawer.getByRole('heading', { name: '登录页偶尔白屏' })).toBeVisible()

  // 关闭抽屉后，弹窗可再次打开并用 Esc 关闭
  await page.keyboard.press('Escape')
  await expect(drawer).toHaveCount(0)
  await page.locator('button[aria-controls="global-ticket-popover"]').click()
  await expect(page.locator('#global-ticket-popover')).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.locator('#global-ticket-popover')).toHaveCount(0)
})

test('任意页面从弹窗提交工单：分类必选并自动携带当前页面地址', async ({ page }) => {
  const { calls } = await mockTickets(page)
  await page.goto('/#/overview', { waitUntil: 'domcontentloaded' })

  await page.locator('button[aria-controls="global-ticket-popover"]').click()
  await page.locator('#global-ticket-popover').getByRole('button', { name: '提交工单' }).click()
  const dialog = page.getByRole('dialog', { name: '提交工单' })
  await expect(dialog).toBeVisible()
  // 弹窗内提示将记录当前页面
  await expect(dialog.getByText(/将随工单记录当前页面/)).toBeVisible()

  // 未选分类时与其他必填项一次报齐
  await dialog.getByLabel('工单标题', { exact: false }).fill('悬浮助手遮挡按钮')
  await dialog.getByLabel('反馈内容', { exact: false }).fill('右下角悬浮球挡住了提交按钮。')
  await dialog.getByRole('button', { name: '提交工单', exact: true }).click()
  await expect(dialog.getByText('请完善必填项：工单分类')).toBeVisible()

  await dialog.getByRole('radio', { name: '体验优化' }).click()
  await dialog.getByRole('button', { name: '提交工单', exact: true }).click()

  await expect(page.locator('[data-sonner-toast]').filter({ hasText: '工单提交成功' })).toBeVisible()
  expect(calls.createdBodies).toHaveLength(1)
  expect(calls.createdBodies[0].category).toBe('experience')
  // 提交页面 = 提交时所在页面完整地址（含 hash 路由）
  expect(String(calls.createdBodies[0].pageUrl)).toMatch(/#\/overview$/)
})

test('反馈内容粘贴图片自动转附件并可预览；不支持的粘贴格式友好提示', async ({ page }) => {
  const { calls } = await mockTickets(page)
  await page.goto('/#/tickets', { waitUntil: 'domcontentloaded' })

  await page.getByRole('button', { name: '提交工单', exact: true }).first().click()
  const dialog = page.getByRole('dialog', { name: '提交工单' })
  await dialog.getByLabel('工单标题', { exact: false }).fill('拓扑图缩放异常')
  await dialog.getByRole('radio', { name: '系统故障' }).click()

  // 在反馈内容中粘贴一张图片 → 自动转为附件，并提示
  await dialog.locator('#ticket-content').evaluate(target => {
    const dataTransfer = new DataTransfer()
    dataTransfer.items.add(new File([new Uint8Array([137, 80, 78, 71])], 'screenshot.png', { type: 'image/png' }))
    target.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dataTransfer, bubbles: true, cancelable: true }))
  })
  await expect(dialog.getByText(/已将粘贴的 1 张图片转为附件/)).toBeVisible()
  const pastedRow = dialog.getByRole('button', { name: /移除 粘贴图片-/ })
  await expect(pastedRow).toBeVisible()

  // 图片附件可点击查看大图
  await dialog.getByRole('button', { name: /预览 粘贴图片-/ }).click()
  const preview = page.getByRole('dialog', { name: /^图片预览/ })
  await expect(preview.locator('img')).toBeVisible()
  await preview.getByRole('button', { name: '关闭图片预览' }).click()
  await expect(preview).toHaveCount(0)

  // 粘贴不支持的文件格式 → 友好提示引导使用文件选择
  await dialog.locator('#ticket-content').evaluate(target => {
    const dataTransfer = new DataTransfer()
    dataTransfer.items.add(new File([new Uint8Array([1, 2, 3])], 'notes.md', { type: 'text/markdown' }))
    target.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dataTransfer, bubbles: true, cancelable: true }))
  })
  await expect(dialog.getByText(/暂不支持直接粘贴「notes.md」/)).toBeVisible()

  // 纯文本粘贴不受影响（默认行为未被拦截）
  await dialog.getByLabel('反馈内容', { exact: false }).fill('画布放大后卡片文字模糊。')
  await expect(dialog.locator('#ticket-content')).toHaveValue('画布放大后卡片文字模糊。')

  await dialog.getByRole('button', { name: '提交工单', exact: true }).click()
  await expect(page.locator('[data-sonner-toast]').filter({ hasText: '工单提交成功' })).toBeVisible()
  // 粘贴的图片随工单一并上传
  await expect.poll(() => calls.uploadedFiles).toEqual([expect.stringMatching(/^粘贴图片-.*\.png$/)])
})

test('工单页提交：必填校验一次报齐，成功后展示工单编号并上传附件', async ({ page }) => {
  const { calls } = await mockTickets(page)
  await page.goto('/#/tickets', { waitUntil: 'domcontentloaded' })

  await page.getByRole('button', { name: '提交工单', exact: true }).first().click()
  const dialog = page.getByRole('dialog', { name: '提交工单' })
  // 直接提交：三个必填项一次报齐
  await dialog.getByRole('button', { name: '提交工单', exact: true }).click()
  await expect(dialog.getByText('请完善必填项：工单标题、工单分类、反馈内容')).toBeVisible()

  await dialog.getByLabel('工单标题', { exact: false }).fill('拓扑图缩放异常')
  await dialog.getByLabel('反馈内容', { exact: false }).fill('画布放大到 180% 后卡片文字模糊。')
  await dialog.getByRole('radio', { name: '体验优化' }).click()
  await dialog.locator('input[type="file"]').setInputFiles({
    name: '复现步骤.txt', mimeType: 'text/plain', buffer: Buffer.from('step1 open canvas'),
  })
  await dialog.getByRole('button', { name: '提交工单', exact: true }).click()

  const toast = page.locator('[data-sonner-toast]').filter({ hasText: '工单提交成功' })
  await expect(toast).toBeVisible()
  // 全局提示位置契约：统一顶部居中（DESIGN.md §4.3）
  await expect(page.locator('[data-sonner-toaster]')).toHaveAttribute('data-x-position', 'center')
  await expect(page.locator('[data-sonner-toaster]')).toHaveAttribute('data-y-position', 'top')
  await expect(toast).toContainText('TK-20260828-0009')
  await expect(toast).toContainText('待处理')
  await expect(dialog).toBeHidden()
  expect(calls.createdBodies).toHaveLength(1)
  expect(calls.createdBodies[0].category).toBe('experience')
  await expect.poll(() => calls.uploadedFiles).toEqual(['复现步骤.txt'])
})

test('状态 Tab 触发筛选；搜索框防抖后携带查询参数', async ({ page }) => {
  const { calls } = await mockTickets(page)
  await page.goto('/#/tickets', { waitUntil: 'domcontentloaded' })

  await page.getByRole('button', { name: '待处理', exact: true }).click()
  await expect.poll(() => calls.listStatusFilters.at(-1)).toBe('pending')

  await page.getByPlaceholder('搜索工单标题、内容、编号、提交人...').fill('白屏')
  await expect
    .poll(() => calls.listStatusFilters.length)
    .toBeGreaterThan(1)
})

test('管理员处理工单：评论必填，提交后状态更新并落入处理记录', async ({ page }) => {
  const { calls } = await mockTickets(page)
  await page.goto('/#/tickets', { waitUntil: 'domcontentloaded' })

  await page.getByRole('row', { name: /登录页偶尔白屏/ }).click()
  const drawer = page.locator('aside').filter({ hasText: '处理记录' })
  await expect(drawer.getByRole('heading', { name: '登录页偶尔白屏' })).toBeVisible()
  // 详情展示分类与提交页面（供审查定位）
  await expect(drawer.getByText('系统故障')).toBeVisible()
  await expect(drawer.getByText('#/login?returnTo=%2Foverview')).toBeVisible()
  // 既有处理记录与附件可见
  await expect(drawer.getByText('已复现，等待用户提供浏览器版本')).toBeVisible()
  await expect(drawer.getByText('白屏截图.png')).toBeVisible()

  // 图片附件可在线预览
  await drawer.getByRole('button', { name: '预览 白屏截图.png' }).click()
  const preview = page.getByRole('dialog', { name: /^图片预览/ })
  await expect(preview.locator('img')).toBeVisible()
  await preview.getByRole('button', { name: '关闭图片预览' }).click()

  // 处理面板：空评论时提交按钮禁用（评论必填）
  const submit = drawer.getByRole('button', { name: '提交处理' })
  await expect(submit).toBeDisabled()
  await drawer.getByLabel('进度状态', { exact: false }).click()
  await page.getByRole('option', { name: '已接纳' }).click()
  await drawer.getByLabel('处理评论', { exact: false }).fill('确认为有效反馈，排入下个迭代')
  await expect(submit).toBeEnabled()
  await submit.click()

  await expect(page.locator('[data-sonner-toast]').filter({ hasText: '工单已处理' })).toBeVisible()
  expect(calls.progressBodies).toEqual([
    { status: 'accepted', comment: '确认为有效反馈，排入下个迭代' },
  ])
})

test('非管理员：仅见自己的工单提示、无处理面板，仍可提交工单', async ({ page }) => {
  const { calls } = await mockTickets(page, { role: 'editor' })
  await page.goto('/#/tickets', { waitUntil: 'domcontentloaded' })

  await expect(page.getByText('展示我提交的工单（editor）')).toBeVisible()
  await page.getByRole('row', { name: /登录页偶尔白屏/ }).click()
  const drawer = page.locator('aside').filter({ hasText: '处理记录' })
  await expect(drawer.getByRole('heading', { name: '登录页偶尔白屏' })).toBeVisible()
  // 非管理员不渲染处理面板，但能查看处理记录与附件
  await expect(drawer.getByText('处理工单')).toHaveCount(0)
  await expect(drawer.getByText('已复现，等待用户提供浏览器版本')).toBeVisible()
  // 提交工单入口可用
  await page.keyboard.press('Escape')
  await page.getByRole('button', { name: '提交工单', exact: true }).first().click()
  const dialog = page.getByRole('dialog', { name: '提交工单' })
  await dialog.getByLabel('工单标题', { exact: false }).fill('编辑者的新反馈')
  await dialog.getByLabel('反馈内容', { exact: false }).fill('筛选栏希望支持日期范围')
  await dialog.getByRole('radio', { name: '其他' }).click()
  await dialog.getByRole('button', { name: '提交工单', exact: true }).click()
  await expect(page.locator('[data-sonner-toast]').filter({ hasText: '工单提交成功' })).toBeVisible()
  expect(calls.createdBodies).toHaveLength(1)
  expect(calls.createdBodies[0].category).toBe('other')
})
