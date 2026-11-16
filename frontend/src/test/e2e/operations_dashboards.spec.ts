/**
 * 监控看板工作台（mocked 浏览器验收）。
 *
 * 覆盖（冻结契约 §7 与验收矩阵 C09/B01/B02/B04/B05/B06/B08 的离线分支）：
 * - 导航/深链/刷新/登录 returnTo；
 * - loading/error/empty/filter-empty、Grafana 不可用/unknown/missing、只读角色；
 * - 新建/编辑/删除/拖拽排序与失败回滚、revision 409 保留输入并可恢复；
 * - 编排：同步预开空白页、续签成功才导航、续签失败关闭空白页、popup blocked；
 * - 预览：续签成功才挂 iframe、全屏以 document.fullscreenElement 为最终依据、
 *   Esc 只退全屏不关弹窗、关闭即卸载、CSS 回退与 API 拒绝不假报成功；
 * - 页面挂载 30 分钟续签计时器与卸载清理。
 *
 * 全部请求本地 mock；真实 Grafana 内容、同 UID 原生保存与真实代理由
 * `operations_dashboards_stack.spec.ts` 在隔离栈验收。
 */
import { expect, test, type Locator, type Page, type Route } from '@playwright/test'
import { expectSameBoundingBox } from './support/geometry'

const json = (route: Route, data: unknown) => route.fulfill({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify({ data }),
})

const jsonError = (route: Route, status: number, body: unknown) => route.fulfill({
  status,
  contentType: 'application/json',
  body: JSON.stringify(body),
})

interface DashboardCard {
  id: string
  name: string
  description: string
  icon: string
  grafana_uid: string
  grafana_status: 'ok' | 'missing' | 'unknown'
  edit_path: string
  preview_path: string
  sort_order: number
  revision: number
  created_at: string | null
  updated_at: string | null
}

const BASE_TIME = '2026-11-14T02:00:00+00:00'

function makeCard(overrides: { id: string; name: string } & Partial<DashboardCard>): DashboardCard {
  const uid = overrides.grafana_uid ?? `oo-${overrides.id.replace(/[^a-z0-9]/gi, '')}`
  return {
    id: overrides.id,
    name: overrides.name,
    description: overrides.description ?? '',
    icon: overrides.icon ?? 'activity',
    grafana_uid: uid,
    grafana_status: overrides.grafana_status ?? 'ok',
    edit_path: `/grafana/d/${uid}?orgId=1`,
    preview_path: `/grafana/d/${uid}?orgId=1&kiosk`,
    sort_order: overrides.sort_order ?? 0,
    revision: overrides.revision ?? 1,
    created_at: overrides.created_at ?? BASE_TIME,
    updated_at: overrides.updated_at ?? BASE_TIME,
  }
}

interface DashboardMockState {
  cards: DashboardCard[]
  grafanaAvailable: boolean
  listFails: boolean
  missingUids: Set<string>
  /** 下一次 grafana-session 是否失败（进入页面/编排/预览共用）。 */
  sessionFails: boolean
  createFails: boolean
  deleteFails: boolean
  reorderFails: boolean
  /** 下一次 PATCH 是否以 409 dashboard_revision_conflict 拒绝。 */
  conflictNextPatch: boolean
}

interface DashboardMockRecords {
  sessionCalls: number
  createBodies: Array<Record<string, unknown>>
  patchBodies: Array<Record<string, unknown>>
  reorderBodies: Array<Array<string>>
  deleteIds: string[]
}

function newState(cards: DashboardCard[]): DashboardMockState {
  return {
    cards,
    grafanaAvailable: true,
    listFails: false,
    missingUids: new Set<string>(),
    sessionFails: false,
    createFails: false,
    deleteFails: false,
    reorderFails: false,
    conflictNextPatch: false,
  }
}

function newRecords(): DashboardMockRecords {
  return {
    sessionCalls: 0,
    createBodies: [],
    patchBodies: [],
    reorderBodies: [],
    deleteIds: [],
  }
}

function cardWithStatus(state: DashboardMockState, card: DashboardCard): DashboardCard {
  if (!state.grafanaAvailable) return { ...card, grafana_status: 'unknown' }
  return {
    ...card,
    grafana_status: state.missingUids.has(card.grafana_uid) ? 'missing' : 'ok',
  }
}

/**
 * 看板域 mock：未知端点显式 500（禁止静默 []），请求全部记录供载荷断言。
 * grafana-session 成功返回 204 空 body（与后端一致）。
 */
async function mockDashboards(
  page: Page,
  state: DashboardMockState,
  records: DashboardMockRecords,
): Promise<void> {
  // 壳层请求（收件箱角标等）本地兜底；看板域路由随后注册、优先命中。
  await page.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname
    if (!path.startsWith('/api/')) return route.continue()
    if (path === '/api/v2/inbox/summary') {
      return json(route, { openAlertCount: 0, actionableCount: 0, unreadCount: 0, resolvedCount: 0 })
    }
    return json(route, [])
  })

  await page.route('**/api/v2/operations/dashboards**', route => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    const method = request.method()
    const body = (() => { try { return request.postDataJSON() } catch { return undefined } })() as Record<string, unknown> | undefined

    if (path === '/api/v2/operations/dashboards' && method === 'GET') {
      if (state.listFails) {
        return jsonError(route, 503, {
          detail: { code: 'dashboard_list_unavailable', message: '列表暂不可用' },
        })
      }
      return json(route, {
        grafana_available: state.grafanaAvailable,
        total: state.cards.length,
        items: state.cards.map(card => cardWithStatus(state, card)),
      })
    }
    if (path === '/api/v2/operations/dashboards' && method === 'POST') {
      records.createBodies.push(body ?? {})
      if (state.createFails) {
        return jsonError(route, 502, {
          detail: { code: 'grafana_unavailable', message: 'Grafana 看板服务暂不可用，请稍后重试' },
        })
      }
      const index = state.cards.length
      const id = `dash-created-${index + 1}`
      const card = makeCard({
        id,
        name: String(body?.name ?? ''),
        description: String(body?.description ?? ''),
        icon: String(body?.icon ?? 'activity'),
        sort_order: index,
        created_at: BASE_TIME,
        updated_at: BASE_TIME,
      })
      state.cards.push(card)
      return json(route, { ...card, grafana_status: 'ok' })
    }
    if (path === '/api/v2/operations/dashboards/reorder' && method === 'POST') {
      const ids = Array.isArray(body?.dashboard_ids) ? body!.dashboard_ids as string[] : []
      records.reorderBodies.push(ids)
      const currentIds = state.cards.map(card => card.id)
      const sameSet = ids.length === currentIds.length
        && ids.every(id => currentIds.includes(id))
        && new Set(ids).size === ids.length
      if (state.reorderFails || !sameSet) {
        return jsonError(route, 409, {
          detail: {
            code: 'dashboard_reorder_mismatch',
            message: '排序列表必须包含全部看板卡片且不能重复或包含未知卡片',
          },
        })
      }
      const byId = new Map(state.cards.map(card => [card.id, card]))
      const nextOrder = ids.map(id => byId.get(id)!)
      const next = nextOrder.map((card, index) => ({
        ...card,
        sort_order: index,
        revision: card.sort_order === index ? card.revision : card.revision + 1,
        updated_at: BASE_TIME,
      }))
      state.cards = next
      return json(route, { items: next.map(card => cardWithStatus(state, card)) })
    }
    if (path === '/api/v2/operations/dashboards/grafana-session' && method === 'POST') {
      records.sessionCalls += 1
      if (state.sessionFails) {
        return jsonError(route, 502, {
          detail: { code: 'grafana_unavailable', message: 'Grafana 看板服务暂不可用，请稍后重试' },
        })
      }
      return route.fulfill({ status: 204, body: '' })
    }

    const patchMatch = path.match(/^\/api\/v2\/operations\/dashboards\/([^/]+)$/)
    if (patchMatch && method === 'PATCH') {
      records.patchBodies.push(body ?? {})
      const card = state.cards.find(item => item.id === patchMatch[1])
      if (!card) {
        return jsonError(route, 404, { detail: { code: 'dashboard_not_found', message: '看板卡片不存在' } })
      }
      if (state.conflictNextPatch) {
        state.conflictNextPatch = false
        // 模拟其他终端已保存：库内 revision 前进，本次提交被拒并附最新 revision。
        card.revision += 1
        card.updated_at = BASE_TIME
        return jsonError(route, 409, {
          detail: {
            code: 'dashboard_revision_conflict',
            message: '看板卡片已被修改，请刷新后重试',
            revision: card.revision,
          },
        })
      }
      if (typeof body?.expected_revision === 'number' && body.expected_revision !== card.revision) {
        return jsonError(route, 409, {
          detail: {
            code: 'dashboard_revision_conflict',
            message: '看板卡片已被修改，请刷新后重试',
            revision: card.revision,
          },
        })
      }
      if (typeof body?.name === 'string') card.name = body.name
      if (typeof body?.description === 'string') card.description = body.description
      if (typeof body?.icon === 'string') card.icon = body.icon
      card.revision += 1
      card.updated_at = BASE_TIME
      return json(route, cardWithStatus(state, card))
    }
    if (patchMatch && method === 'DELETE') {
      records.deleteIds.push(patchMatch[1])
      if (state.deleteFails) {
        return jsonError(route, 502, {
          detail: { code: 'grafana_unavailable', message: 'Grafana 看板服务暂不可用，请稍后重试' },
        })
      }
      state.cards = state.cards.filter(item => item.id !== patchMatch[1])
      return json(route, { status: 'deleted', id: patchMatch[1] })
    }

    return jsonError(route, 500, {
      detail: { code: 'unmocked_operations_dashboards_endpoint', message: `unmocked ${method} ${path}` },
    })
  })
}

/** Grafana 同源页面 mock：仅用于 iframe/新标签页加载成功与 URL 断言。 */
async function mockGrafanaPages(page: Page): Promise<void> {
  await page.context().route('**/grafana/**', route => route.fulfill({
    status: 200,
    contentType: 'text/html; charset=utf-8',
    body: '<!doctype html><html><head><title>Grafana Mock</title></head><body><h1 data-testid="grafana-mock">Grafana kiosk</h1></body></html>',
  }))
}

async function mockSession(page: Page, user: Record<string, unknown>): Promise<void> {
  await page.addInitScript(sessionUser => {
    localStorage.setItem('token', 'operations-dashboard-e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: { token: 'operations-dashboard-e2e-token', user: sessionUser },
      version: 0,
    }))
  }, user)
}

const admin = { id: 'admin', username: 'admin', email: 'admin@example.com', role: 'admin', is_active: true }

function roleUser(role: 'editor' | 'viewer' | 'custom', menuPermissions: string[]) {
  return {
    id: `dashboards-${role}`,
    username: `dashboards-${role}`,
    email: `${role}@example.com`,
    role,
    is_active: true,
    menu_permissions: menuPermissions,
  }
}

const operationsMenus = ['overview', 'operations', 'operations.dashboard']

/** 元素在视口最顶层命中（AGENTS.md §5：可见性断言不检测遮挡）。 */
async function expectTopmost(locator: Locator) {
  const handle = await locator.elementHandle()
  const topmost = await handle?.evaluate(element => {
    const rect = element.getBoundingClientRect()
    const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
    return Boolean(hit && (hit === element || element.contains(hit)))
  })
  expect(topmost).toBe(true)
}

async function cardOrder(page: Page): Promise<string[]> {
  return page.locator('[data-testid="dashboard-card"]').evaluateAll(nodes =>
    nodes.map(node => (node as HTMLElement).dataset.dashboardId ?? ''))
}

async function gotoWorkbench(page: Page) {
  await page.goto('/#/operations/dashboard')
  await expect(page.getByTestId('operations-dashboard-workbench')).toBeVisible()
}

test('看板工作台：菜单/深链/刷新可达，筛选计数与空态正确，顶部无标题文案块', async ({ page }) => {
  await mockSession(page, admin)
  const state = newState([
    makeCard({ id: 'dash-core', name: '核心链路运行态势', description: '订单与支付链路', sort_order: 0 }),
    makeCard({ id: 'dash-resource', name: 'Grafana 资源看板', description: 'CPU 与内存', icon: 'cpu', sort_order: 1 }),
  ])
  const records = newRecords()
  await mockDashboards(page, state, records)
  await mockGrafanaPages(page)
  await page.setViewportSize({ width: 1440, height: 900 })

  // 菜单：从平台概览经左侧导航进入监控看板（HashRouter 入口与菜单 key 不变）
  await page.goto('/#/overview')
  const nav = page.getByRole('navigation')
  await nav.getByRole('button', { name: '智能运营', exact: true }).click()
  await nav.getByRole('link', { name: '监控看板', exact: true }).click()
  await expect(page).toHaveURL(/\/#\/operations\/dashboard$/)
  await expect(page.getByTestId('operations-dashboard-workbench')).toBeVisible()

  // 顶部文案块（h1 + 说明段落）已删除：与本体管理页对齐，筛选栏即首屏首个元素
  await expect(page.getByRole('heading', { name: '监控看板' })).toHaveCount(0)
  const firstSectionLabel = await page.getByTestId('operations-dashboard-workbench').evaluate(
    node => node.firstElementChild?.getAttribute('aria-label') ?? null,
  )
  expect(firstSectionLabel).toBe('看板筛选')

  // 页面不回显任何敏感凭证
  const bodyText = await page.locator('body').innerText()
  expect(bodyText).not.toMatch(/GRAFANA_ADMIN_PASSWORD|oo_grafana_session|SECRET_KEY/)
  await expect(page.getByText('共 2 个看板')).toBeVisible()
  await expect.poll(() => cardOrder(page)).toEqual(['dash-core', 'dash-resource'])
  await expect.poll(() => records.sessionCalls).toBeGreaterThanOrEqual(1)

  // 名称/描述筛选 + 计数 + 清除
  await page.getByLabel('按名称或描述筛选看板').fill('支付链路')
  await expect(page.getByText('共 1 / 2 个看板')).toBeVisible()
  await expect.poll(() => cardOrder(page)).toEqual(['dash-core'])
  await page.getByLabel('清除筛选').click()
  await expect.poll(() => cardOrder(page)).toEqual(['dash-core', 'dash-resource'])

  // 无结果空态
  await page.getByLabel('按名称或描述筛选看板').fill('不存在的看板')
  await expect(page.getByText('没有符合条件的看板')).toBeVisible()
  await expect(page.getByText('请调整筛选条件')).toBeVisible()
  await page.getByLabel('清除筛选').click()

  // 刷新深链仍可用
  await page.reload()
  await expect(page.getByTestId('operations-dashboard-workbench')).toBeVisible()
  await expect(page.getByText('核心链路运行态势', { exact: true })).toBeVisible()
})

test('看板工作台：空列表引导创建第一个看板', async ({ page }) => {
  await mockSession(page, admin)
  const state = newState([])
  const records = newRecords()
  await mockDashboards(page, state, records)
  await mockGrafanaPages(page)
  await gotoWorkbench(page)

  await expect(page.getByText('还没有监控看板')).toBeVisible()
  await expect(page.getByText('创建第一个看板卡片，再通过「编排」配置运行态势面板')).toBeVisible()
  await expect(page.getByTestId('dashboard-create')).toBeVisible()
  await expect(page.getByRole('button', { name: '立即创建' })).toBeVisible()
  await expect(page.getByText('先在平台建卡片，再通过「编排」配置数据源与面板')).toBeVisible()
})

test('看板工作台：空态与有卡片时新建入口卡高度一致（占位与卡片统一最小高度）', async ({ page }) => {
  await mockSession(page, admin)
  const state = newState([])
  const records = newRecords()
  await mockDashboards(page, state, records)
  await mockGrafanaPages(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await gotoWorkbench(page)

  // 空态：占位与新建入口卡同行；grid stretch 把同行条目拉到行内最高者，
  // 占位与卡片共用最小高度时行高一致，不出现空态比卡片高出一截的布局。
  const createCard = page.locator('article').filter({ has: page.getByRole('button', { name: '立即创建' }) })
  await expect(page.getByTestId('dashboard-list-empty')).toBeVisible()
  const emptyBox = await createCard.boundingBox()

  // 创建第一个看板后进入有卡片态：新建入口卡高度不发生跳变
  await page.getByTestId('dashboard-create').click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('看板名称').fill('首个看板')
  await dialog.getByTestId('dashboard-form-submit').click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByText('首个看板', { exact: true })).toBeVisible()
  const withCardBox = await createCard.boundingBox()

  expectSameBoundingBox(emptyBox, withCardBox, '新建入口卡')
})

test('看板工作台：列表加载失败显示 role=alert 并可重新加载', async ({ page }) => {
  await mockSession(page, admin)
  const state = newState([])
  const records = newRecords()
  await mockDashboards(page, state, records)
  await mockGrafanaPages(page)
  // 全局 QueryClient retry: 1——首次请求与自动重试都必须失败（共 2 次）
  // 才会进入错误态（与 event_registry_detail.spec 同口径）。
  let failList = 2
  await page.route('**/api/v2/operations/dashboards', route => {
    if (route.request().method() === 'GET' && failList > 0) {
      failList -= 1
      return jsonError(route, 500, { detail: { code: 'internal_error', message: '列表暂时不可用' } })
    }
    return route.fallback()
  })
  await page.goto('/#/operations/dashboard')
  await expect(page.getByRole('alert').filter({ hasText: '看板列表加载失败' })).toBeVisible()
  await page.getByRole('button', { name: '重新加载' }).click()
  await expect(page.getByText('还没有监控看板')).toBeVisible()
})

test('看板工作台：新建校验/提交载荷/失败保留输入，编辑只改平台元数据', async ({ page }) => {
  await mockSession(page, admin)
  const state = newState([])
  const records = newRecords()
  await mockDashboards(page, state, records)
  await mockGrafanaPages(page)
  await gotoWorkbench(page)

  await page.getByTestId('dashboard-create').click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByText('创建后会在 Grafana 中生成一个空白看板', { exact: false })).toBeVisible()

  // 空名称：弹窗内 role=alert，不提交
  await dialog.getByTestId('dashboard-form-submit').click()
  await expect(dialog.getByRole('alert').filter({ hasText: '请输入看板名称' })).toBeVisible()
  expect(records.createBodies).toHaveLength(0)

  // Grafana 故障：失败文案留在弹窗内，输入不丢
  state.createFails = true
  await dialog.getByLabel('看板名称').fill('核心链路运行态势')
  await dialog.getByLabel('描述（可选）').fill('订单与支付链路')
  await dialog.getByRole('button', { name: '数据库' }).click()
  await dialog.getByTestId('dashboard-form-submit').click()
  await expect(dialog.getByRole('alert').filter({ hasText: 'Grafana 看板服务暂不可用' })).toBeVisible()
  await expect(dialog.getByLabel('看板名称')).toHaveValue('核心链路运行态势')
  expect(records.createBodies).toEqual([{
    name: '核心链路运行态势',
    description: '订单与支付链路',
    icon: 'database',
  }])

  // 恢复后重试成功：卡片进入列表
  state.createFails = false
  await dialog.getByTestId('dashboard-form-submit').click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByText('核心链路运行态势', { exact: true })).toBeVisible()
  expect(records.createBodies).toHaveLength(2)

  // 编辑：文案明确只改平台卡片；PATCH 携带列表 revision
  const uidBefore = state.cards[0].grafana_uid
  await page.getByRole('button', { name: '信息 核心链路运行态势' }).click()
  const editDialog = page.getByRole('dialog')
  await expect(editDialog.getByText('修改监控看板基本信息', { exact: false })).toBeVisible()
  await editDialog.getByLabel('看板名称').fill('核心链路运行态势（新）')
  await editDialog.getByRole('button', { name: '趋势图' }).click()
  await editDialog.getByTestId('dashboard-form-submit').click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByText('核心链路运行态势（新）', { exact: true })).toBeVisible()
  expect(records.patchBodies).toHaveLength(1)
  expect(records.patchBodies[0]).toMatchObject({
    name: '核心链路运行态势（新）',
    icon: 'line-chart',
    expected_revision: 1,
  })
  // 改名/改图标只改平台元数据：uid 不变，PATCH 不携带任何 Grafana 字段
  expect(state.cards[0].grafana_uid).toBe(uidBefore)
  expect(records.patchBodies[0]).not.toHaveProperty('grafana_uid')
  expect(records.patchBodies[0]).not.toHaveProperty('edit_path')
  expect(records.patchBodies[0]).not.toHaveProperty('preview_path')
})

test('看板工作台：删除失败保留卡片可重试，成功后卡片消失', async ({ page }) => {
  await mockSession(page, admin)
  const state = newState([makeCard({ id: 'dash-delete', name: '待删除看板' })])
  const records = newRecords()
  await mockDashboards(page, state, records)
  await mockGrafanaPages(page)
  await gotoWorkbench(page)

  state.deleteFails = true
  await page.getByRole('button', { name: '删除看板 待删除看板' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByText('该看板在 Grafana 中的全部面板与配置将被永久删除', { exact: false })).toBeVisible()
  await dialog.getByRole('button', { name: '删除看板', exact: true }).click()
  await expect(page.getByText('删除失败')).toBeVisible()
  await expect(page.getByText('待删除看板', { exact: true })).toBeVisible()

  // 恢复后重试：卡片与确认弹窗一并消失
  state.deleteFails = false
  await dialog.getByRole('button', { name: '删除看板', exact: true }).click()
  await expect(page.getByText('待删除看板', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('dialog')).toHaveCount(0)
  expect(records.deleteIds).toEqual(['dash-delete', 'dash-delete'])
})

test('看板工作台：revision 409 保留输入、展示最新版本，刷新后可再次保存', async ({ page }) => {
  await mockSession(page, admin)
  const state = newState([makeCard({ id: 'dash-conflict', name: '冲突看板' })])
  const records = newRecords()
  await mockDashboards(page, state, records)
  await mockGrafanaPages(page)
  await gotoWorkbench(page)

  state.conflictNextPatch = true
  await page.getByRole('button', { name: '信息 冲突看板' }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('看板名称').fill('冲突看板（我的修改）')
  await dialog.getByTestId('dashboard-form-submit').click()

  const alert = dialog.getByRole('alert').filter({ hasText: '看板卡片已被修改，请刷新后重试' })
  await expect(alert).toBeVisible()
  await expect(alert).toContainText('服务端最新版本：2')
  await expect(dialog.getByLabel('看板名称')).toHaveValue('冲突看板（我的修改）')

  // 刷新最新数据后（列表 revision=2），再次保存使用新 revision 成功
  await dialog.getByTestId('dashboard-form-refresh').click()
  await expect(dialog.getByTestId('dashboard-form-error')).toHaveCount(0)
  await dialog.getByTestId('dashboard-form-submit').click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByText('冲突看板（我的修改）', { exact: true })).toBeVisible()
  expect(records.patchBodies.map(body => body.expected_revision)).toEqual([1, 2])
})

test('看板工作台：拖拽提交全量顺序并持久化；筛选态禁用；失败回滚', async ({ page }) => {
  await mockSession(page, admin)
  const state = newState([
    makeCard({ id: 'dash-a', name: '看板甲', sort_order: 0 }),
    makeCard({ id: 'dash-b', name: '看板乙', sort_order: 1 }),
    makeCard({ id: 'dash-c', name: '看板丙', sort_order: 2 }),
  ])
  const records = newRecords()
  await mockDashboards(page, state, records)
  await mockGrafanaPages(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await gotoWorkbench(page)
  await expect.poll(() => cardOrder(page)).toEqual(['dash-a', 'dash-b', 'dash-c'])

  // 把「丙」拖到「甲」左半区（before 落位）→ 全量顺序 [丙,甲,乙]
  await page.locator('[data-dashboard-id="dash-c"]')
    .dragTo(page.locator('[data-dashboard-id="dash-a"]'), { targetPosition: { x: 10, y: 40 } })
  await expect.poll(() => records.reorderBodies.length).toBe(1)
  expect(records.reorderBodies[0]).toEqual(['dash-c', 'dash-a', 'dash-b'])
  await expect.poll(() => cardOrder(page)).toEqual(['dash-c', 'dash-a', 'dash-b'])

  // 刷新后顺序来自服务端（真实持久化语义）
  await page.reload()
  await expect.poll(() => cardOrder(page)).toEqual(['dash-c', 'dash-a', 'dash-b'])

  // 筛选激活：卡片不可拖拽，不会提交排序
  await page.getByLabel('按名称或描述筛选看板').fill('看板')
  await expect(page.locator('[data-dashboard-id="dash-c"]')).toHaveAttribute('draggable', 'false')
  const beforeDragCalls = records.reorderBodies.length
  await page.locator('[data-dashboard-id="dash-c"]')
    .dragTo(page.locator('[data-dashboard-id="dash-a"]'), { targetPosition: { x: 10, y: 40 } })
  await page.waitForTimeout(100)
  expect(records.reorderBodies).toHaveLength(beforeDragCalls)
  await page.getByLabel('清除筛选').click()

  // 排序写入与随后列表刷新同时失败：仍立即恢复拖拽前缓存顺序并说明数据暂为旧值。
  state.reorderFails = true
  state.listFails = true
  await page.locator('[data-dashboard-id="dash-b"]')
    .dragTo(page.locator('[data-dashboard-id="dash-c"]'), { targetPosition: { x: 10, y: 40 } })
  await expect(page.getByText('排序保存失败')).toBeVisible()
  await expect(page.getByTestId('dashboard-list-stale-error')).toBeVisible()
  await expect.poll(() => cardOrder(page)).toEqual(['dash-c', 'dash-a', 'dash-b'])

  // 服务端重新可读后，显式刷新仍与真实顺序一致。
  state.listFails = false
  await page.getByRole('button', { name: '重新加载' }).click()
  await expect.poll(() => cardOrder(page)).toEqual(['dash-c', 'dash-a', 'dash-b'])
  await expect(page.getByTestId('dashboard-list-stale-error')).toHaveCount(0)
})

test('看板工作台：Grafana 不可用显示告警条，unknown 不误判失效，本地信息仍可编辑', async ({ page }) => {
  await mockSession(page, admin)
  const state = newState([makeCard({ id: 'dash-unknown', name: '探测失败看板' })])
  const records = newRecords()
  await mockDashboards(page, state, records)
  await mockGrafanaPages(page)
  state.grafanaAvailable = false
  await gotoWorkbench(page)

  await expect(page.getByTestId('dashboard-grafana-unavailable')).toContainText('Grafana 服务暂不可用')
  await expect(page.getByTestId('dashboard-missing-badge')).toHaveCount(0)
  await expect(page.locator('[data-dashboard-id="dash-unknown"]')).toHaveAttribute('data-grafana-status', 'unknown')
  await expect(page.getByRole('button', { name: '编排 探测失败看板' })).toBeDisabled()
  await expect(page.getByRole('button', { name: '预览 探测失败看板' })).toBeDisabled()

  // 本地信息编辑不依赖 Grafana 探测，仍可用
  await page.getByRole('button', { name: '信息 探测失败看板' }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('看板名称').fill('探测失败看板（本地改名）')
  await dialog.getByTestId('dashboard-form-submit').click()
  await expect(page.getByText('探测失败看板（本地改名）', { exact: true })).toBeVisible()
})

test('看板工作台：missing 显示已失效并禁用编排/预览，信息与删除仍可用', async ({ page }) => {
  await mockSession(page, admin)
  const state = newState([
    makeCard({ id: 'dash-missing', name: '已删看板' }),
    makeCard({ id: 'dash-ok', name: '正常看板', sort_order: 1 }),
  ])
  state.missingUids.add('oo-dashmissing')
  const records = newRecords()
  await mockDashboards(page, state, records)
  await mockGrafanaPages(page)
  await gotoWorkbench(page)

  const missingCard = page.locator('[data-dashboard-id="dash-missing"]')
  await expect(missingCard.getByTestId('dashboard-missing-badge')).toHaveText('已失效')
  await expect(page.getByRole('button', { name: '编排 已删看板' })).toBeDisabled()
  await expect(page.getByRole('button', { name: '预览 已删看板' })).toBeDisabled()
  // unknown 之外的正常卡片不受影响
  await expect(page.locator('[data-dashboard-id="dash-ok"]').getByTestId('dashboard-missing-badge')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '编排 正常看板' })).toBeEnabled()
  await expect(page.getByRole('button', { name: '信息 已删看板' })).toBeEnabled()
})

test('看板工作台：编排同步预开空白页，续签成功后导航到后端 edit_path', async ({ page }) => {
  await mockSession(page, admin)
  const state = newState([makeCard({ id: 'dash-orchestrate', name: '编排看板' })])
  const records = newRecords()
  await mockDashboards(page, state, records)
  await mockGrafanaPages(page)
  await gotoWorkbench(page)
  const callsBefore = records.sessionCalls

  const popupPromise = page.context().waitForEvent('page')
  await page.getByRole('button', { name: '编排 编排看板' }).click()
  const popup = await popupPromise
  await expect.poll(() => popup.url()).toContain('/grafana/d/oo-dashorchestrate?orgId=1')
  await expect(popup.getByTestId('grafana-mock')).toBeVisible()
  // 普通路径，不是 HashRouter 深链
  expect(popup.url()).not.toContain('#')
  // 点击编排先续签（挂载时已签发过，点击后签发次数增加）
  await expect.poll(() => records.sessionCalls).toBeGreaterThan(callsBefore)
})

test('看板工作台：编排时弹窗被拦截明确反馈，且不发起会话', async ({ page }) => {
  await mockSession(page, admin)
  const state = newState([makeCard({ id: 'dash-popup', name: '弹窗看板' })])
  const records = newRecords()
  await mockDashboards(page, state, records)
  await mockGrafanaPages(page)
  await page.addInitScript(() => {
    window.open = () => null
  })
  await gotoWorkbench(page)
  await expect.poll(() => records.sessionCalls).toBeGreaterThanOrEqual(1)
  const callsBefore = records.sessionCalls

  await page.getByRole('button', { name: '编排 弹窗看板' }).click()
  await expect(page.getByText('浏览器拦截了新标签页')).toBeVisible()
  expect(records.sessionCalls).toBe(callsBefore)
  await expect(page).toHaveURL(/\/#\/operations\/dashboard$/)
})

test('看板工作台：编排续签失败关闭空白页并明确报错', async ({ page }) => {
  await mockSession(page, admin)
  const state = newState([makeCard({ id: 'dash-session-fail', name: '会话失败看板' })])
  const records = newRecords()
  await mockDashboards(page, state, records)
  await mockGrafanaPages(page)
  await gotoWorkbench(page)
  await expect.poll(() => records.sessionCalls).toBeGreaterThanOrEqual(1)

  state.sessionFails = true
  const popupPromise = page.context().waitForEvent('page')
  await page.getByRole('button', { name: '编排 会话失败看板' }).click()
  const popup = await popupPromise
  await expect(page.getByText('打开编排失败')).toBeVisible()
  await expect.poll(() => popup.isClosed()).toBe(true)
  await expect(page).toHaveURL(/\/#\/operations\/dashboard$/)
})

test('看板工作台：预览先续签再挂同源 kiosk iframe，全屏依据 document.fullscreenElement，Esc 只退全屏', async ({ page }) => {
  await mockSession(page, admin)
  const state = newState([makeCard({ id: 'dash-preview', name: '预览看板' })])
  const records = newRecords()
  await mockDashboards(page, state, records)
  await mockGrafanaPages(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await gotoWorkbench(page)

  const callsBefore = records.sessionCalls
  await page.getByRole('button', { name: '预览 预览看板' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  const iframe = page.getByTestId('dashboard-preview-iframe')
  await expect(iframe).toBeVisible()
  expect(await iframe.getAttribute('src')).toBe('/grafana/d/oo-dashpreview?orgId=1&kiosk')
  expect(await iframe.getAttribute('sandbox')).toBeNull()
  // 挂载前先续签（次数只增不减；挂载时的签发不在此断言精确值）
  await expect.poll(() => records.sessionCalls).toBeGreaterThan(callsBefore)
  await expect(page.frameLocator('[data-testid="dashboard-preview-iframe"]').getByTestId('grafana-mock')).toBeVisible()

  // 弹窗焦点与控件层级：焦点在弹窗内，全屏按钮为最顶层命中
  const focusInDialog = await dialog.evaluate(node => node.contains(document.activeElement))
  expect(focusInDialog).toBe(true)
  await expectTopmost(dialog.getByTestId('dashboard-preview-fullscreen'))

  // 全屏成功以 document.fullscreenElement 为最终依据
  await dialog.getByRole('button', { name: '全屏' }).click()
  await expect.poll(() => page.evaluate(() => {
    const element = document.fullscreenElement as HTMLElement | null
    return element?.dataset.testid ?? null
  })).toBe('dashboard-preview-container')
  await expect(dialog.getByRole('button', { name: '退出全屏' })).toBeVisible()

  // Esc 退出全屏：fullscreenElement 归 null，弹窗保持打开且按钮状态同步。
  // 无头浏览器没有浏览器 UI 的「Esc 退出原生全屏」行为（实测 headless 中
  // Escape 后 fullscreenElement 不变）；真实 Esc 行为由 t6 在真实环境验证
  // （B05）。这里以捕获监听模拟浏览器的原生退出，应用侧只依赖
  // fullscreenchange 同步状态，验证弹窗不被 Esc 关闭且按钮状态回退。
  await page.evaluate(() => {
    document.addEventListener('keydown', () => {
      if (document.fullscreenElement) void document.exitFullscreen()
    }, { capture: true })
  })
  await page.keyboard.press('Escape')
  await expect.poll(() => page.evaluate(() => document.fullscreenElement === null)).toBe(true)
  await expect(dialog).toBeVisible()
  await expect(dialog.getByRole('button', { name: '全屏' })).toBeVisible()

  // 按钮退出同样落到最终态 null
  await dialog.getByRole('button', { name: '全屏' }).click()
  await expect.poll(() => page.evaluate(() => document.fullscreenElement !== null)).toBe(true)
  await dialog.getByRole('button', { name: '退出全屏' }).click()
  await expect.poll(() => page.evaluate(() => document.fullscreenElement === null)).toBe(true)

  // 关闭即卸载 iframe（连带 Grafana WS/计时器），不存在隐藏常驻预览
  await dialog.getByTestId('dashboard-preview-close').click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByTestId('dashboard-preview-iframe')).toHaveCount(0)
  expect(await page.evaluate(() => document.fullscreenElement)).toBeNull()

  // 连续 3 次开/关不残留 iframe
  for (let index = 0; index < 3; index += 1) {
    await page.getByRole('button', { name: '预览 预览看板' }).click()
    await expect(page.getByTestId('dashboard-preview-iframe')).toBeVisible()
    await page.getByTestId('dashboard-preview-close').click()
    await expect(page.getByTestId('dashboard-preview-iframe')).toHaveCount(0)
  }
})

test('看板工作台：预览弹窗全屏/关闭按钮同构浅底深字配色（明暗两态）', async ({ page }) => {
  await mockSession(page, admin)
  const state = newState([makeCard({ id: 'dash-button-tone', name: '配色看板' })])
  const records = newRecords()
  await mockDashboards(page, state, records)
  await mockGrafanaPages(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await gotoWorkbench(page)

  const openPreview = async () => {
    await page.getByRole('button', { name: '预览 配色看板' }).click()
    await expect(page.getByTestId('dashboard-preview-iframe')).toBeVisible()
    return page.getByRole('dialog')
  }

  // 两按钮统一「浅底 + 深字 + 35% 同色系边框」，仅语义色不同（token 明暗成对维护）
  const tone = {
    light: {
      fullscreenBg: 'rgb(232, 245, 233)',
      fullscreenText: 'rgb(45, 138, 78)',
      fullscreenBorder: /rgba?\(45,\s*138,\s*78,\s*0\.35\)|color\(srgb[^)]*\/\s*0\.35\)/,
      closeBg: 'rgb(253, 232, 232)',
      closeText: 'rgb(194, 59, 59)',
      closeBorder: /rgba?\(194,\s*59,\s*59,\s*0\.35\)|color\(srgb[^)]*\/\s*0\.35\)/,
    },
    dark: {
      fullscreenBg: 'rgba(63, 185, 80, 0.14)',
      fullscreenText: 'rgb(63, 185, 80)',
      fullscreenBorder: /rgba?\(63,\s*185,\s*80,\s*0\.35\)|color\(srgb[^)]*\/\s*0\.35\)/,
      closeBg: 'rgba(229, 83, 75, 0.14)',
      closeText: 'rgb(229, 83, 75)',
      closeBorder: /rgba?\(229,\s*83,\s*75,\s*0\.35\)|color\(srgb[^)]*\/\s*0\.35\)/,
    },
  }

  const assertTones = async (dialog: Locator, values: typeof tone.light) => {
    const fullscreen = dialog.getByTestId('dashboard-preview-fullscreen')
    const close = dialog.getByTestId('dashboard-preview-close')
    // 全屏=浅绿底+深绿字，关闭=浅红底+深红字
    await expect(fullscreen).toHaveCSS('background-color', values.fullscreenBg)
    await expect(fullscreen).toHaveCSS('color', values.fullscreenText)
    await expect(close).toHaveCSS('background-color', values.closeBg)
    await expect(close).toHaveCSS('color', values.closeText)
    // 边框为 35% 同色系语义色（color-mix 计算值按浏览器序列化形态匹配）
    await expect(fullscreen).toHaveCSS('border-color', values.fullscreenBorder)
    await expect(close).toHaveCSS('border-color', values.closeBorder)
    // 除颜色外完全同构：同边框宽度与圆角
    await expect(fullscreen).toHaveCSS('border-width', '1px')
    await expect(close).toHaveCSS('border-width', '1px')
    const fullscreenRadius = await fullscreen.evaluate(node => getComputedStyle(node).borderRadius)
    const closeRadius = await close.evaluate(node => getComputedStyle(node).borderRadius)
    expect(closeRadius).toBe(fullscreenRadius)
  }

  const dialog = await openPreview()
  await assertTones(dialog, tone.light)
  await dialog.getByTestId('dashboard-preview-close').click()
  await expect(page.getByRole('dialog')).toHaveCount(0)

  // 深色档：.dark 覆盖同一组 token，两按钮色值成对翻转
  await page.evaluate(() => {
    localStorage.setItem('theme', JSON.stringify({ state: { theme: 'dark' }, version: 0 }))
  })
  await page.reload()
  await gotoWorkbench(page)
  const darkDialog = await openPreview()
  await assertTones(darkDialog, tone.dark)
  await darkDialog.getByTestId('dashboard-preview-close').click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
})

test('看板工作台：预览续签失败不挂 iframe，重试成功后恢复', async ({ page }) => {
  await mockSession(page, admin)
  const state = newState([makeCard({ id: 'dash-preview-error', name: '预览失败看板' })])
  const records = newRecords()
  await mockDashboards(page, state, records)
  await mockGrafanaPages(page)
  await gotoWorkbench(page)
  await expect.poll(() => records.sessionCalls).toBeGreaterThanOrEqual(1)

  state.sessionFails = true
  await page.getByRole('button', { name: '预览 预览失败看板' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByTestId('dashboard-preview-error')).toBeVisible()
  await expect(dialog.getByRole('alert')).toContainText('无法打开预览')
  await expect(page.getByTestId('dashboard-preview-iframe')).toHaveCount(0)

  state.sessionFails = false
  await dialog.getByTestId('dashboard-preview-retry').click()
  await expect(page.getByTestId('dashboard-preview-iframe')).toBeVisible()
})

test('看板工作台：浏览器不支持 Fullscreen API 时 CSS 铺满回退，退出后恢复', async ({ page }) => {
  await mockSession(page, admin)
  const state = newState([makeCard({ id: 'dash-css-fs', name: '回退全屏看板' })])
  const records = newRecords()
  await mockDashboards(page, state, records)
  await mockGrafanaPages(page)
  await page.addInitScript(() => {
    Object.defineProperty(Element.prototype, 'requestFullscreen', {
      configurable: true,
      value: undefined,
    })
  })
  await page.setViewportSize({ width: 1280, height: 800 })
  await gotoWorkbench(page)
  await page.getByRole('button', { name: '预览 回退全屏看板' }).click()
  const dialog = page.getByRole('dialog')
  await expect(page.getByTestId('dashboard-preview-iframe')).toBeVisible()

  await dialog.getByRole('button', { name: '全屏' }).click()
  // 回退模式不产生原生全屏，但容器铺满视口（CSS 最终结果可验证；滚动条等
  // 边缘不小于 innerWidth 的 96% 即视为铺满）
  expect(await page.evaluate(() => document.fullscreenElement)).toBeNull()
  await expect.poll(async () => {
    const box = await page.getByTestId('dashboard-preview-container').boundingBox()
    const viewport = await page.evaluate(() => window.innerWidth)
    return box ? box.width / viewport : 0
  }).toBeGreaterThan(0.96)
  await expect(dialog.getByRole('button', { name: '退出全屏' })).toBeVisible()

  await dialog.getByRole('button', { name: '退出全屏' }).click()
  await expect.poll(async () => {
    const box = await dialog.boundingBox()
    const viewport = await page.evaluate(() => window.innerWidth)
    return box ? box.width / viewport : 0
  }).toBeLessThan(0.96)
})

test('看板工作台：Fullscreen API 拒绝时不假称已全屏', async ({ page }) => {
  await mockSession(page, admin)
  const state = newState([makeCard({ id: 'dash-fs-denied', name: '拒绝全屏看板' })])
  const records = newRecords()
  await mockDashboards(page, state, records)
  await mockGrafanaPages(page)
  await page.addInitScript(() => {
    Element.prototype.requestFullscreen = () => Promise.reject(new Error('denied by test'))
  })
  await gotoWorkbench(page)
  await page.getByRole('button', { name: '预览 拒绝全屏看板' }).click()
  const dialog = page.getByRole('dialog')
  await expect(page.getByTestId('dashboard-preview-iframe')).toBeVisible()

  await dialog.getByRole('button', { name: '全屏' }).click()
  await expect(page.getByText('无法进入全屏')).toBeVisible()
  expect(await page.evaluate(() => document.fullscreenElement)).toBeNull()
  await expect(dialog.getByRole('button', { name: '全屏' })).toBeVisible()
  await expect(dialog.getByRole('button', { name: '退出全屏' })).toHaveCount(0)
})

test('看板工作台：editor 可写，viewer/custom 只读但可预览', async ({ page }) => {
  await mockSession(page, roleUser('editor', operationsMenus))
  const state = newState([makeCard({ id: 'dash-editor', name: '编辑者看板' })])
  const records = newRecords()
  await mockDashboards(page, state, records)
  await mockGrafanaPages(page)
  await gotoWorkbench(page)
  await expect(page.getByTestId('dashboard-create')).toBeVisible()
  await expect(page.getByRole('button', { name: '信息 编辑者看板' })).toBeVisible()
  await expect(page.getByRole('button', { name: '编排 编辑者看板' })).toBeVisible()
  await expect(page.getByRole('button', { name: '删除看板 编辑者看板' })).toBeVisible()

  await page.getByRole('button', { name: '删除看板 编辑者看板' }).click()
  await page.getByRole('dialog').getByRole('button', { name: '取消' }).click()
})

test('看板工作台：viewer 隐藏平台写入口，仍可预览', async ({ page }) => {
  await mockSession(page, roleUser('viewer', operationsMenus))
  const state = newState([makeCard({ id: 'dash-viewer', name: '查看者看板' })])
  const records = newRecords()
  await mockDashboards(page, state, records)
  await mockGrafanaPages(page)
  await gotoWorkbench(page)

  await expect(page.getByTestId('dashboard-create')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '立即创建' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '信息 查看者看板' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '编排 查看者看板' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '删除看板 查看者看板' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '预览 查看者看板' })).toBeEnabled()

  await page.getByRole('button', { name: '预览 查看者看板' }).click()
  await expect(page.getByTestId('dashboard-preview-iframe')).toBeVisible()
})

test('看板工作台：custom 未授权 dashboard 菜单时深链落到无权限页', async ({ page }) => {
  await mockSession(page, roleUser('custom', ['overview']))
  const state = newState([])
  const records = newRecords()
  await mockDashboards(page, state, records)
  await mockGrafanaPages(page)
  await page.goto('/#/operations/dashboard')
  await expect(page.getByRole('heading', { name: '当前页面无法访问' })).toBeVisible()
  await expect(page.getByTestId('operations-dashboard-workbench')).toHaveCount(0)
})

test('看板工作台：未登录深链跳登录并携带 returnTo，登录后回到监控看板', async ({ page }) => {
  const state = newState([makeCard({ id: 'dash-return', name: '登录后看板' })])
  const records = newRecords()
  await mockDashboards(page, state, records)
  await mockGrafanaPages(page)
  await page.route('**/api/v1/auth/login', route => json(route, {
    access_token: 'login-token',
    token_type: 'bearer',
  }))
  await page.route('**/api/v1/auth/profile', route => json(route, admin))

  await page.goto('/#/operations/dashboard')
  await expect(page).toHaveURL(/#\/login\?returnTo=%2Foperations%2Fdashboard/)
  await page.getByLabel('用户名').fill('admin')
  await page.locator('#login-password').fill('secret')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page).toHaveURL(/\/#\/operations\/dashboard$/)
  await expect(page.getByTestId('operations-dashboard-workbench')).toBeVisible()
  await expect(page.getByText('登录后看板', { exact: true })).toBeVisible()
})

test('看板工作台：进入页面签发看板会话，每 30 分钟续签，离开页面后计时器清理', async ({ page }) => {
  await mockSession(page, admin)
  const state = newState([makeCard({ id: 'dash-timer', name: '续签看板' })])
  const records = newRecords()
  await mockDashboards(page, state, records)
  await mockGrafanaPages(page)
  await page.clock.install({ time: new Date('2026-11-14T08:00:00+08:00') })
  await gotoWorkbench(page)
  await expect.poll(() => records.sessionCalls).toBeGreaterThanOrEqual(1)
  const afterMount = records.sessionCalls

  await page.clock.fastForward('30:00')
  await expect.poll(() => records.sessionCalls).toBe(afterMount + 1)

  // 离开工作台：清理计时器，再推进 30 分钟不再续签
  await page.goto('/#/overview')
  await expect(page.getByTestId('operations-dashboard-workbench')).toHaveCount(0)
  const afterLeave = records.sessionCalls
  await page.clock.fastForward('30:00')
  await page.waitForTimeout(100)
  expect(records.sessionCalls).toBe(afterLeave)
})
