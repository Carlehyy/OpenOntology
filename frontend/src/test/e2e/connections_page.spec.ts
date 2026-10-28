import { expect, test, type Page, type Route } from '@playwright/test'

/**
 * 连接管理页回归（商业化审查 D-006 / D-005 / D-004）：
 * - 删除被同步数据集引用的连接：409 依赖明细展示在确认弹窗内，弹窗不关闭、卡片仍在
 * - 受控注销 409：数据集/集成资产/历史/映射/任务的完整阻断明细展示
 * - 凭据轮换：只提交填写字段（不回填旧值），成功后自动重测
 * - 同名连接：前端拦截并提示（后端另有 409 兜底）
 * - 同步成功后提供「查看数据集」入口，跳转数据流水线数据集页签
 */

const CONNECTIONS = [
  { id: 'conn-1', name: 'ERP 订单库', kind: 'mysql', status: 'active' },
]

async function mockConnections(page: Page, options?: {
  deleteStatus?: number
  deleteBody?: unknown
  rotateBody?: Array<Record<string, string>>
  /** PATCH 后自动 /test 的响应在网络上丢失（服务端实际已执行并置为 active）。 */
  testAbort?: boolean
}) {
  const rotateBodies = options?.rotateBody ?? []
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: { token: 'e2e-token', user: { id: 'u1', username: 'tester', role: 'admin' } },
      version: 0,
    }))
  })
  const live = new Map(CONNECTIONS.map(item => [item.id, { ...item }]))
  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url())
    if (!url.pathname.startsWith('/api/')) return route.continue()
    const ok = (data: unknown) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ data, message: 'ok' }),
    })

    if (url.pathname === '/api/v2/connections' && route.request().method() === 'GET') {
      return ok([...live.values()])
    }
    if (url.pathname === '/api/v2/connections' && route.request().method() === 'POST') {
      return route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({ detail: '已存在同名连接「ERP 订单库」，请更换连接名称' }),
      })
    }
    // 凭据轮换：部分键合并，响应永不包含 config
    if (/\/api\/v2\/connections\/[^/]+\/config$/.test(url.pathname)
        && route.request().method() === 'PATCH') {
      rotateBodies.push(route.request().postDataJSON().config)
      if (options?.testAbort) {
        // 服务端真实语义：轮换成功即重置为未激活，待重测翻转为 active
        const rotated = live.get('conn-1')
        if (rotated) live.set('conn-1', { ...rotated, status: 'inactive' })
      }
      return ok({ id: url.pathname.split('/').at(-2), name: 'ERP 订单库', kind: 'mysql', status: 'inactive' })
    }
    if (/\/api\/v2\/connections\/[^/]+\/test$/.test(url.pathname)
        && route.request().method() === 'POST') {
      if (options?.testAbort) {
        // 响应丢失：服务端实际已执行测试并置为 active，随后 GET 返回真实状态
        const tested = live.get('conn-1')
        if (tested) live.set('conn-1', { ...tested, status: 'active' })
        return route.abort()
      }
      return ok({ success: true, status: 'active' })
    }
    if (url.pathname === '/api/v2/connections/conn-1' && route.request().method() === 'DELETE') {
      const status = options?.deleteStatus ?? 409
      if (status < 300) {
        live.delete('conn-1')
        // 真实 FastAPI 204 无响应体；带 "null" body 会让 axios 解析出 null
        return route.fulfill({ status })
      }
      return route.fulfill({
        status,
        contentType: 'application/json',
        body: JSON.stringify(options?.deleteBody ?? {
          detail: {
            message: '连接被 2 个同步数据集引用，删除前请先在「数据资产」中删除这些数据集',
            datasets: [
              { id: 'ds-1', name: '同步数据集-订单', kind: 'structured' },
              { id: 'ds-2', name: '同步数据集-客户', kind: 'structured' },
            ],
            total: 2,
          },
        }),
      })
    }
    if (url.pathname === '/api/v2/connections/conn-1/sync'
        && route.request().method() === 'POST') {
      return ok({ status: 'ok', rows: 10, dataset_id: 'ds-1', version_no: 1 })
    }
    return ok({})
  })
  return rotateBodies
}

test('删除被同步数据集引用的连接：弹窗内展示 409 依赖明细且卡片保留', async ({ page }) => {
  await mockConnections(page)
  await page.goto('/#/data/pipelines/connections', { waitUntil: 'domcontentloaded' })

  await expect(page.getByText('ERP 订单库', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '删除' }).click()

  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  await dialog.getByRole('button', { name: '确认删除' }).click()

  await expect(dialog.getByText(/连接被 2 个同步数据集引用/)).toBeVisible()
  await expect(dialog.getByText(/同步数据集-订单、同步数据集-客户/)).toBeVisible()
  // 删除被拒：确认弹窗保持打开（用户可读错误后取消），连接卡片不动
  await expect(page.getByText('ERP 订单库', { exact: true })).toBeVisible()
})

test('删除依赖解除后返回 204：弹窗关闭且卡片消失', async ({ page }) => {
  await mockConnections(page, { deleteStatus: 204 })
  await page.goto('/#/data/pipelines/connections', { waitUntil: 'domcontentloaded' })

  await page.getByRole('button', { name: '删除' }).click()
  await page.getByRole('dialog').getByRole('button', { name: '确认删除' }).click()

  await expect(page.getByText('ERP 订单库', { exact: true })).toHaveCount(0)
})

test('同名连接被前端拦截并给出明确提示', async ({ page }) => {
  await mockConnections(page)
  await page.goto('/#/data/pipelines/connections', { waitUntil: 'domcontentloaded' })

  await page.getByRole('button', { name: '新建连接' }).click()
  await page.getByPlaceholder('例：ERP 订单数据库').fill('ERP 订单库')
  await page.getByRole('button', { name: '保存', exact: true }).click()

  await expect(page.getByText('已存在同名连接「ERP 订单库」，请更换连接名称')).toBeVisible()
})

test('同步成功后提供查看数据集入口并跳转数据集页签', async ({ page }) => {
  await mockConnections(page)
  await page.goto('/#/data/pipelines/connections', { waitUntil: 'domcontentloaded' })

  await page.getByRole('button', { name: '同步' }).click()
  await expect(page.getByText('同步成功，共 10 行')).toBeVisible()

  await page.getByRole('button', { name: '查看数据集' }).click()
  await expect(page).toHaveURL(/#\/data\/pipelines\/datasets/)
})

test('凭据轮换只提交填写字段并自动重测，表单不回填旧值', async ({ page }) => {
  const rotateBodies: Array<Record<string, string>> = []
  await mockConnections(page, { rotateBody: rotateBodies })
  await page.goto('/#/data/pipelines/connections', { waitUntil: 'domcontentloaded' })

  await page.getByRole('button', { name: '轮换凭据' }).click()
  const panel = page.getByTestId('connection-rotate-panel')
  await expect(panel).toBeVisible()
  await expect(panel).toContainText('既有配置不会回显到表单')
  // 只填新密码：其余字段留空 = 保留服务端现值
  await page.getByLabel('轮换 ERP 订单库 密码').fill('rotated-secret-456')
  await panel.getByRole('button', { name: '保存并测试' }).click()
  await expect(panel.getByText(/已更新配置并通过连通测试/)).toBeVisible()
  // 请求只包含填写的键，且不携带任何旧凭据
  expect(rotateBodies).toEqual([{ password: 'rotated-secret-456' }])
})

test('受控注销 409 展示数据集/集成资产/历史/映射/任务的完整阻断明细', async ({ page }) => {
  await mockConnections(page, { deleteBody: { detail: {
    message: '连接仍有依赖，不能注销：1 个同步数据集（请先在「数据资产」中删除）、1 个集成数据集资产（请先在资产页注销零历史资产）、4 条数据历史（版本 2 / 同步运行 1 / 写入回执 1）、1 个本体映射绑定、1 个数据任务。受控注销不会删除任何历史或审计记录，请先解除以上依赖。',
    datasets: [{ id: 'ds-1', name: '同步数据集-订单', kind: 'structured' }],
    datasets_total: 1,
    integrated_assets: [{ id: 'asset-1', name: '集成资产-订单', schema_name: 'public', table_name: 'orders' }],
    integrated_assets_total: 1,
    history: { dataset_versions: 2, sync_runs: 1, mutations: 1 },
    mapping_references: [{ mapping_id: 'm-1', ontology_id: 'ont-1', kind: 'object', name: 'User', dataset_name: '同步数据集-订单' }],
    mapping_references_total: 1,
    tasks: [{ id: 'task-1', name: '消费任务', pipeline_id: 'p-1' }],
    tasks_total: 1,
    total: 1,
  } } })
  await page.goto('/#/data/pipelines/connections', { waitUntil: 'domcontentloaded' })

  await page.getByRole('button', { name: '删除' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toContainText('受控注销要求：零数据集引用、零集成资产、零历史、零本体映射绑定、零数据任务')
  await dialog.getByRole('button', { name: '确认删除' }).click()
  await expect(dialog.getByText(/连接仍有依赖，不能注销/)).toBeVisible()
  await expect(dialog.getByText(/版本 2 \/ 同步运行 1 \/ 写入回执 1/)).toBeVisible()
  await expect(dialog.getByText(/同步数据集-订单、集成资产-订单、消费任务/)).toBeVisible()
  // 拒绝后卡片保留
  await expect(page.getByText('ERP 订单库', { exact: true })).toBeVisible()
})

test('轮换成功但连通测试响应丢失：不误报更新失败，状态按服务端实际值刷新', async ({ page }) => {
  const rotateBodies: Array<Record<string, string>> = []
  await mockConnections(page, { rotateBody: rotateBodies, testAbort: true })
  await page.goto('/#/data/pipelines/connections', { waitUntil: 'domcontentloaded' })

  await page.getByRole('button', { name: '轮换凭据' }).click()
  const panel = page.getByTestId('connection-rotate-panel')
  await page.getByLabel('轮换 ERP 订单库 密码').fill('rotated-secret-789')
  await panel.getByRole('button', { name: '保存并测试' }).click()

  // PATCH 已成功、自动 /test 响应丢失：如实提示“配置已更新但未取得测试结果”
  await expect(panel.getByText(/配置已更新，但未能取得连通测试结果/)).toBeVisible()
  await expect(panel.getByText(/请刷新连接状态后重新测试连通/)).toBeVisible()
  // 不得把响应丢失误报为配置更新本身失败
  await expect(panel.getByText(/配置更新失败/)).toHaveCount(0)
  expect(rotateBodies).toEqual([{ password: 'rotated-secret-789' }])
  // 丢失响应的 /test 服务端实际已生效：随后的 GET 按真实状态渲染（活跃），
  // 而不是停留在轮换后的“未激活”本地状态
  await expect(page.getByText('活跃', { exact: true })).toBeVisible()
})
