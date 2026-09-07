import { expect, test, type Page, type Route } from '@playwright/test'
import type { SceneSummary } from '@/types/scene'

// 三维场景（阶段一）：导航暂时隐藏（hiddenFromNavigation，深链仍可达）、卡片列表 CRUD/克隆、详情三标签深链、角色授权。
// 全部 API 本地 mock，属 mocked 套件。

const now = '2026-08-24T08:00:00+00:00'

const json = (route: Route, data: unknown, status = 200) => route.fulfill({
  status,
  contentType: 'application/json',
  body: JSON.stringify({ data }),
})

const sceneA = {
  id: 'scn-1',
  name: '供应链园区',
  description: '园区白模演示场景',
  icon: 'boxes',
  status: 'published',
  current_version_no: 2,
  published_version_no: 1,
  created_by: null,
  created_at: now,
  updated_at: now,
}

const sceneB = {
  id: 'scn-2',
  name: '能源场站',
  description: '',
  icon: 'boxes',
  status: 'draft',
  current_version_no: 0,
  published_version_no: null,
  created_by: null,
  created_at: now,
  updated_at: now,
}

const definitionV1 = {
  meta: { id: 'demo-park', name: '演示园区', version: '0.1.0' },
  stage: { camera: { pos: [92, 78, 92], target: [0, 0, 0], fov: 30 } },
  objects: [
    { id: 'office', label: '办公楼', type: 'office', layout: { x: -20, z: 0, w: 12, d: 10, h: 16 } },
    { id: 'warehouse', label: '仓库', type: 'warehouse', layout: { x: 26, z: -10, w: 14, d: 20, h: 18 } },
  ],
  relations: [{ from: 'office', to: 'warehouse', kind: 'flow' }],
}

const versions = {
  items: [
    { id: 'sv-2', scene_id: 'scn-1', version_no: 2, source: 'manual', note: 'v2 草稿', created_by: null, created_at: now },
    { id: 'sv-1', scene_id: 'scn-1', version_no: 1, source: 'manual', note: '初始版本', created_by: null, created_at: now },
  ],
  total: 2,
}

const logs = {
  items: [
    { id: 'log-1', scene_id: 'scn-1', level: 'alarm', object_id: 'warehouse', event_key: 'binding.rule',
      message: '库位利用率 > 95%', payload: { value: 97.2 }, occurred_at: now, recorded_at: now },
    { id: 'log-2', scene_id: 'scn-1', level: 'normal', object_id: 'warehouse', event_key: 'binding.rule',
      message: '指标恢复 normal', payload: {}, occurred_at: now, recorded_at: now },
  ],
  total: 2,
}

async function seedAuth(page: Page, role = 'admin', menuPermissions?: string[]) {
  await page.addInitScript(([userRole, menus]) => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: {
          id: 'u-scene',
          username: 'scene-tester',
          role: userRole,
          ...(menus ? { menu_permissions: menus } : {}),
        },
      },
      version: 0,
    }))
  }, [role, menuPermissions])
}

async function mockPlatformShell(page: Page) {
  await page.route('**/api/v2/inbox/**', route => json(route, { openAlertCount: 0, actionableCount: 0, unreadCount: 0, resolvedCount: 0 }))
  await page.route('**/api/v2/inbox', route => json(route, { items: [], nextCursor: null, hasMore: false }))
}

async function mockScenesApi(page: Page, options: { listItems?: SceneSummary[] } = {}) {
  const listItems = options.listItems ?? [sceneA, sceneB]
  const createdScenes: SceneSummary[] = []
  await page.route(/\/api\/v2\/scenes(\?.*)?$/, route => {
    const request = route.request()
    if (request.method() === 'GET') {
      const items = [...listItems, ...createdScenes]
      return json(route, { items, total: items.length })
    }
    if (request.method() === 'POST') {
      const body = request.postDataJSON()
      const created: SceneSummary = { ...sceneB, id: 'scn-' + (createdScenes.length + 1), name: body.name, description: body.description || '', status: 'draft', current_version_no: 0, published_version_no: null }
      createdScenes.push(created)
      return json(route, { ...created }, 201)
    }
    return json(route, {})
  })
  await page.route(/\/api\/v2\/scenes\/scn-clone(\?.*)?$/, route =>
    json(route, { ...sceneA, id: 'scn-clone', name: '供应链园区-副本', status: 'draft', published_version_no: null } as SceneSummary))
  await page.route(/\/api\/v2\/scenes\/scn-1\/clone(\?.*)?$/, route =>
    json(route, { ...sceneA, id: 'scn-clone', name: '供应链园区-副本', status: 'draft', published_version_no: null } as SceneSummary, 201))
  await page.route(/\/api\/v2\/scenes\/scn-1\/publish(\?.*)?$/, route =>
    json(route, { ...sceneA, status: 'published', published_version_no: 2 }))
  await page.route(/\/api\/v2\/scenes\/scn-1\/versions(\?.*)?$/, route => json(route, versions))
  await page.route(/\/api\/v2\/scenes\/scn-1\/versions\/\d+(\?.*)?$/, route =>
    json(route, { ...versions.items[1], definition: definitionV1 }))
  await page.route(/\/api\/v2\/scenes\/scn-1\/runtime-logs(\?.*)?$/, route => json(route, logs))
  await page.route(/\/api\/v2\/scenes\/scn-1(\?.*)?$/, route => json(route, { ...sceneA, version_count: 2 }))
}

test('admin 左侧导航暂时隐藏「三维场景」，深链仍可达且「本体助手」保持可见', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)
  await mockScenesApi(page)
  await page.goto('/#/scenes')
  const nav = page.locator('nav')
  // hiddenFromNavigation 只隐藏导航渲染：深链 /#/scenes 仍按 menu 权限放行（下方新建按钮可见）
  await expect(nav.getByText('三维场景', { exact: true })).toHaveCount(0)
  await expect(nav.getByText('本体助手', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '新建场景' })).toBeVisible()
})

test('列表页渲染卡片并支持新建场景', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)
  await mockScenesApi(page)
  await page.goto('/#/scenes')
  await expect(page.getByText('供应链园区')).toBeVisible()
  await expect(page.getByText('能源场站')).toBeVisible()
  // 状态徽章：发布态 / 草稿态
  const publishedCard = page.locator('article', { hasText: '供应链园区' })
  await expect(publishedCard.getByText('已发布', { exact: true })).toBeVisible()
  const draftCard = page.locator('article', { hasText: '能源场站' })
  await expect(draftCard.getByText('草稿', { exact: true })).toBeVisible()
  // 新建
  await page.getByRole('button', { name: '新建场景' }).click()
  await page.getByLabel('场景名称', { exact: true }).fill('港口调度场景')
  await page.getByRole('button', { name: '创建', exact: true }).click()
  await expect(page.getByText('港口调度场景')).toBeVisible()
})

test('克隆发布态场景生成副本草稿', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)
  await mockScenesApi(page)
  await page.goto('/#/scenes')
  const card = page.locator('article', { hasText: '供应链园区' })
  await card.getByRole('button', { name: '克隆' }).click()
  await page.getByRole('button', { name: /确认克隆|克隆/ }).last().click()
  await expect(page.getByText('供应链园区-副本')).toBeVisible()
})

test('详情页左右双卡：五标签平铺、深链、指示器与画布常驻', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)
  await mockScenesApi(page)
  // 顶部信息卡 + 左可视化 + 右操作栏；图标操作组（返回在最左）
  await page.goto('/#/scenes/scn-1')
  await expect(page.locator('[aria-label="场景操作栏"]')).toBeVisible()
  await expect(page.locator('[aria-label="三维场景可视化"]')).toBeVisible()
  await expect(page.getByRole('button', { name: '返回列表' })).toBeVisible()
  // 首屏指示器 bug 回归：默认面板下滑块必须有宽度
  const indicator = page.locator('[data-testid="panel-indicator"]')
  await expect(indicator).toBeVisible()
  await expect.poll(async () => (await indicator.boundingBox())?.width ?? 0).toBeGreaterThan(10)
  // 默认面板=对象清单（含概念列）
  await expect(page.getByRole('cell', { name: 'warehouse' }).first()).toBeVisible()
  await expect(page.locator('th', { hasText: '概念' })).toBeVisible()
  // 深链直达运行日志标签
  await page.goto('/#/scenes/scn-1?tab=logs')
  await expect(page.getByText('库位利用率 > 95%')).toBeVisible()
  await expect(page).toHaveURL(/tab=logs/)
  // 场景模型标签=数据绑定平铺区块（fixture 无绑定→空态文案）
  await page.goto('/#/scenes/scn-1?tab=models')
  await expect(page.getByRole('heading', { name: '数据绑定' })).toBeVisible()
  await expect(page.getByText('暂无数据绑定')).toBeVisible()
  await expect(page).toHaveURL(/tab=models/)
  // 兼容旧三标签深链：tab=display 归一为默认对象面板且画布仍常驻
  await page.goto('/#/scenes/scn-1?tab=display')
  await expect(page.getByRole('cell', { name: 'office' }).first()).toBeVisible()
  // 左卡版本下拉默认选中已发布 v1（Radix combobox 显示所选选项文本）
  await expect(page.locator('[aria-label="三维场景可视化"]').getByRole('combobox')).toContainText('v1')
})

test('custom 角色未授权 scenes 时直达被拦截', async ({ page }) => {
  await seedAuth(page, 'custom', ['overview'])
  await mockPlatformShell(page)
  await mockScenesApi(page)
  await page.goto('/#/scenes')
  await expect(page.getByText('供应链园区')).toHaveCount(0)
})

// —— 场景建模页（阶段二）——

const LF = String.fromCharCode(10)

const chatStreamOk = [
  'event: meta',
  'data: {"conversation_id":"sc-1","scene_id":null}',
  '',
  'event: scene_updated',
  'data: {"scene_id":"scn-1","name":"供应链园区","version_no":3,"status":"draft","note":"初版布局"}',
  '',
  'event: done',
  'data: {}',
  '',
].join(LF)

const chatStreamError = [
  'event: meta',
  'data: {"conversation_id":"sc-2","scene_id":null}',
  '',
  'event: error',
  'data: {"code":"model_unavailable","message":"尚未配置可用的对话模型"}',
  '',
  'event: done',
  'data: {}',
  '',
].join(LF)

async function mockModeling(page: Page, sseBody: string) {
  await mockPlatformShell(page)
  await mockScenesApi(page)
  const convId = sseBody.includes('model_unavailable') ? 'sc-2' : 'sc-1'
  await page.route(/\/api\/v2\/scenes\/conversations(\?.*)?$/, route => {
    if (route.request().method() === 'POST') {
      return json(route, { id: convId, scene_id: null, title: '', model_config_id: null, created_at: now, updated_at: now }, 201)
    }
    return json(route, { items: [], total: 0 })
  })
  const chatPattern = '**/api/v2/scenes/conversations/' + convId + '/chat'
  await page.route(chatPattern, route =>
    route.fulfill({ status: 200, contentType: 'text/event-stream', body: sseBody }))
}

test('建模页从零新建：对话应用定义生成版本并绑定场景', async ({ page }) => {
  await seedAuth(page)
  await mockModeling(page, chatStreamOk)
  await page.goto('/#/scenes/modeling')
  await page.getByPlaceholder('描述要从零构建的场景…').fill('建一个园区场景')
  await page.getByRole('button', { name: /发送/ }).click()
  await expect(page.getByText('已应用 v3 · 初版布局')).toBeVisible()
  // 版本管理收进画布卡顶栏「版本管理」按钮，点开浮层查看历史版本（MYW-64）
  await page.getByRole('button', { name: '版本管理' }).click()
  const panel = page.getByRole('dialog', { name: '版本管理' })
  await expect(panel).toBeVisible()
  await expect(panel.getByText('v2 · 手动')).toBeVisible()
  // 关闭浮层再验证右下角命中：edge-to-edge 后输入区贴视口右下角，
  // 悬浮助手球必须整体让位（aboveComposer 锚点 + elementFromPoint 命中检测）。
  // 先输入内容让「发送」处于可点态——禁用态按钮带 disabled:pointer-events-none，不会成为命中目标。
  await page.getByRole('button', { name: '关闭版本历史' }).click()
  await page.getByPlaceholder('描述对当前场景的调整…').fill('再加一栋办公楼')
  const sendHit = await page.evaluate(() => {
    const sendButton = document.querySelector<HTMLButtonElement>('button[aria-label="发送"]')
    if (!sendButton) return 'send-missing'
    const rect = sendButton.getBoundingClientRect()
    const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
    if (!hit) return 'no-hit'
    if (sendButton.contains(hit)) return 'ok'
    const owner = hit.closest('[data-testid]')
    const label = owner
      ? owner.getAttribute('data-testid')
      : hit.tagName + '.' + String(hit.className).slice(0, 80)
    return 'covered-by:' + label
  })
  expect(sendHit).toBe('ok')
})

test('建模页 error 事件展示告警条', async ({ page }) => {
  await seedAuth(page)
  await mockModeling(page, chatStreamError)
  await page.goto('/#/scenes/modeling')
  await page.getByPlaceholder('描述要从零构建的场景…').fill('你好')
  await page.getByRole('button', { name: /发送/ }).click()
  await expect(page.getByText('尚未配置可用的对话模型')).toBeVisible()
})

test('建模页历史会话切换与消息回放', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)
  await mockScenesApi(page)
  await page.route(/\/api\/v2\/scenes\/conversations(\?.*)?$/, route => {
    if (route.request().method() === 'POST') {
      return json(route, { id: 'sc-9', scene_id: 'scn-1', title: '旧会话', model_config_id: null, created_at: now, updated_at: now }, 201)
    }
    return json(route, { items: [{ id: 'sc-9', scene_id: 'scn-1', title: '旧会话', model_config_id: null, created_at: now, updated_at: now }], total: 1 })
  })
  await page.route(/\/api\/v2\/scenes\/conversations\/sc-9\/messages(\?.*)?$/, route => json(route, {
    items: [
      { id: 'm1', conversation_id: 'sc-9', role: 'user', content: '建一个仓库场景', status: 'complete', version_no: null, created_at: now },
      { id: 'm2', conversation_id: 'sc-9', role: 'assistant', content: '初版布局', status: 'complete', version_no: 2, created_at: now },
    ],
    total: 2,
  }))
  await page.goto('/#/scenes/modeling')
  await page.getByRole('button', { name: '历史会话' }).click()
  await page.getByText('旧会话').click()
  // 消息回放：用户消息 + 版本应用系统条
  await expect(page.getByText('建一个仓库场景')).toBeVisible()
  await expect(page.getByText('已应用 v2 · 初版布局')).toBeVisible()
  // 会话绑定场景后，顶栏「版本管理」浮层可打开查看版本行
  await page.getByRole('button', { name: '版本管理' }).click()
  await expect(page.getByRole('dialog', { name: '版本管理' }).getByText('v2 · 手动')).toBeVisible()
})

// —— 建模页搜索式草稿选择器与版本回滚（阶段三）——

const selectorDraftC: SceneSummary = {
  id: 'scn-c',
  name: '城市交通枢纽',
  description: '地铁站点人流场景',
  icon: 'boxes',
  status: 'draft',
  current_version_no: 4,
  published_version_no: null,
  created_by: null,
  created_at: now,
  updated_at: now,
}

const selectorDraftD: SceneSummary = {
  id: 'scn-d',
  name: '仓储物流中心',
  description: '库位与分拣线演示',
  icon: 'boxes',
  status: 'draft',
  current_version_no: 2,
  published_version_no: null,
  created_by: null,
  created_at: now,
  updated_at: now,
}

const selectorDraftE: SceneSummary = {
  id: 'scn-e',
  name: '医院门诊楼',
  description: '',
  icon: 'boxes',
  status: 'draft',
  current_version_no: 1,
  published_version_no: null,
  created_by: null,
  created_at: now,
  updated_at: now,
}

test('建模页搜索式选择器过滤与切换', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)
  await mockScenesApi(page, {
    listItems: [selectorDraftC, selectorDraftD, selectorDraftE],
  })
  await page.goto('/#/scenes/modeling')

  const trigger = page.getByRole('button', { name: '选择草稿场景' })
  await expect(trigger).toHaveText(/从零新建/)

  // 打开浮层：「从零新建」常驻首项 + 3 条草稿
  await trigger.click()
  const listbox = page.getByRole('listbox', { name: '草稿场景' })
  await expect(listbox.getByRole('option')).toHaveCount(4)

  // 关键词过滤命中的场景 + 常驻首项
  await page.getByPlaceholder('搜索草稿场景…').fill('枢纽')
  await expect(listbox.getByRole('option')).toHaveCount(2)

  // 无匹配时展示空态提示，仅剩「从零新建」可回退
  await page.getByPlaceholder('搜索草稿场景…').fill('不存在的关键词xyz')
  await expect(listbox.getByText('无匹配场景')).toBeVisible()
  await expect(listbox.getByRole('option')).toHaveCount(1)

  // 选中一条草稿：触发按钮显示所选名称，会话目标切换带动输入框占位语
  await page.getByPlaceholder('搜索草稿场景…').fill('仓储')
  await listbox.getByRole('option', { name: /仓储物流中心/ }).click()
  await expect(trigger).toHaveText(/仓储物流中心/)
  await expect(page.getByPlaceholder('描述对当前场景的调整…')).toBeEnabled()
  await expect(page.getByRole('button', { name: '清除已选场景' })).toBeVisible()

  // 重开浮层切回「从零新建」：目标清空、清除按钮消失
  await trigger.click()
  await listbox.getByRole('option', { name: '从零新建' }).click()
  await expect(trigger).toHaveText(/从零新建/)
  await expect(page.getByPlaceholder('描述要从零构建的场景…')).toBeEnabled()
  await expect(page.getByRole('button', { name: '清除已选场景' })).toHaveCount(0)
})

test('版本浮层回滚生成新版本', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)
  await mockScenesApi(page)

  // 回滚后的 PUT 会把冻结出的 v3 追加进版本列表（onSuccess 后 invalidate refetch）
  const rollbackVersionMetas = [
    { id: 'sv-2', scene_id: 'scn-1', version_no: 2, source: 'manual', note: 'v2 草稿', created_by: null, created_at: now },
    { id: 'sv-1', scene_id: 'scn-1', version_no: 1, source: 'manual', note: '初始版本', created_by: null, created_at: now },
  ]
  // 覆盖 mockScenesApi 的静态版本路由（后注册的 handler 优先命中）
  await page.route(/\/api\/v2\/scenes\/scn-1\/versions(\?.*)?$/, route =>
    json(route, { items: rollbackVersionMetas, total: rollbackVersionMetas.length }))
  await page.route(/\/api\/v2\/scenes\/scn-1\/versions\/\d+(\?.*)?$/, route => {
    const versionNo = Number(new URL(route.request().url()).pathname.split('/').pop())
    const meta = rollbackVersionMetas.find(item => item.version_no === versionNo) ?? rollbackVersionMetas[0]
    return json(route, { ...meta, definition: definitionV1 })
  })
  // 注意：source=manual 会作为 query 附在 URL 上，路由须兼容 query（本文件约定 (\?.*)?$ 形式）
  await page.route(/\/api\/v2\/scenes\/scn-1\/definition(\?.*)?$/, route => {
    if (route.request().method() !== 'PUT') return json(route, {})
    rollbackVersionMetas.unshift({
      id: 'sv-3', scene_id: 'scn-1', version_no: 3, source: 'manual', note: '回滚自 v2', created_by: null, created_at: now,
    })
    return json(route, {
      scene: { ...sceneA, current_version_no: 3 },
      version: { ...rollbackVersionMetas[0], definition: definitionV1 },
    })
  })

  // 复用历史会话绑定 scn-1：免 SSE 流程直接让版本条可用
  await page.route(/\/api\/v2\/scenes\/conversations(\?.*)?$/, route => json(route, {
    items: [{ id: 'sc-7', scene_id: 'scn-1', title: '旧会话', model_config_id: null, created_at: now, updated_at: now }],
    total: 1,
  }))
  await page.route(/\/api\/v2\/scenes\/conversations\/sc-7\/messages(\?.*)?$/, route => json(route, {
    items: [
      { id: 'm1', conversation_id: 'sc-7', role: 'user', content: '建一个仓库场景', status: 'complete', version_no: null, created_at: now },
      { id: 'm2', conversation_id: 'sc-7', role: 'assistant', content: '初版布局', status: 'complete', version_no: 2, created_at: now },
    ],
    total: 2,
  }))

  await page.goto('/#/scenes/modeling')
  await page.getByRole('button', { name: '历史会话' }).click()
  await page.getByText('旧会话').click()
  await expect(page.getByText('已应用 v2 · 初版布局')).toBeVisible()

  // 版本管理在画布卡顶栏按钮内：先打开浮层（MYW-64）
  await page.getByRole('button', { name: '版本管理' }).click()
  // 打开确认弹窗，确认语义文案
  await page.getByRole('button', { name: '回滚为当前' }).click()
  await expect(page.getByRole('heading', { name: '回滚到 v2？' })).toBeVisible()
  await expect(page.getByText(/复制冻结为新版本 v3/)).toBeVisible()

  const definitionPut = page.waitForRequest(request =>
    request.method() === 'PUT' && request.url().includes('/api/v2/scenes/scn-1/definition'))
  await page.getByRole('button', { name: '确认回滚' }).click()
  const putRequest = await definitionPut
  // 冻结的是 v2 定义且备注/来源符合约定
  expect(putRequest.postDataJSON().note).toBe('回滚自 v2')
  expect(putRequest.url()).toContain('source=manual')
  expect(putRequest.postDataJSON().definition.objects).toHaveLength(2)

  // 成功 toast + 新版本芯片 v3 出现并选中
  await expect(page.locator('[data-sonner-toast]').filter({ hasText: '已回滚并生成新版本 v3' })).toBeVisible()
  const chipV3 = page.getByRole('button', { name: 'v3 · 手动' })
  await expect(chipV3).toBeVisible()
  await expect(chipV3).toHaveAttribute('aria-pressed', 'true')
})

test('版本浮层切换版本并在顶栏提示当前预览，未绑定时展示空态', async ({ page }) => {
  await seedAuth(page)
  await mockPlatformShell(page)
  await mockScenesApi(page)
  // 复用历史会话绑定 scn-1：免 SSE 流程直接进入有版本的画布态
  await page.route(/\/api\/v2\/scenes\/conversations(\?.*)?$/, route => json(route, {
    items: [{ id: 'sc-8', scene_id: 'scn-1', title: '旧会话', model_config_id: null, created_at: now, updated_at: now }],
    total: 1,
  }))
  await page.route(/\/api\/v2\/scenes\/conversations\/sc-8\/messages(\?.*)?$/, route => json(route, {
    items: [
      { id: 'm1', conversation_id: 'sc-8', role: 'user', content: '建一个仓库场景', status: 'complete', version_no: null, created_at: now },
      { id: 'm2', conversation_id: 'sc-8', role: 'assistant', content: '初版布局', status: 'complete', version_no: 2, created_at: now },
    ],
    total: 2,
  }))

  await page.goto('/#/scenes/modeling')
  // 未绑定/未回放：浮层展示空态引导
  await page.getByRole('button', { name: '版本管理' }).click()
  await expect(page.getByRole('dialog', { name: '版本管理' })).toBeVisible()
  await expect(page.locator('[data-testid="scene-canvas-subtitle"]')).toHaveText(/从零新建/)
  await expect(page.getByText('尚未绑定场景：助手生成首个版本后，即可在此回看。')).toBeVisible()
  await page.getByRole('button', { name: '关闭版本历史' }).click()

  // 回放会话绑定 scn-1：默认选中最新版 v2，顶栏副标题跟随
  await page.getByRole('button', { name: '历史会话' }).click()
  await page.getByText('旧会话').click()
  await expect(page.getByRole('dialog', { name: '版本管理' })).not.toBeAttached()
  await expect(page.locator('[data-testid="scene-canvas-subtitle"]')).toHaveText(/供应链园区 · 共 2 个版本 · 预览 v2/)
  await page.getByRole('button', { name: '版本管理' }).click()
  const panel = page.getByRole('dialog', { name: '版本管理' })
  // 切换到 v1：行选中态切换且顶栏预览提示更新（行名含备注/时间，按前缀子串定位）
  await panel.getByRole('button', { name: /^v1 · 手动/ }).click()
  await expect(panel.getByRole('button', { name: /^v1 · 手动/ })).toHaveAttribute('aria-pressed', 'true')
  await expect(panel.getByRole('button', { name: /^v2 · 手动/ })).toHaveAttribute('aria-pressed', 'false')
  await expect(page.locator('[data-testid="scene-canvas-subtitle"]')).toHaveText(/预览 v1/)
})
