import { expect, test, type Page, type Route } from '@playwright/test'

import type { DecisionItem, DecisionStatus } from '../../api/autoloop'

// 待处理决策事项提醒（全部接口本地 mock，不触真实后端）：
// 聊天区右上角迷你卡片、标签页标题角标、新到达提醒的通道选择（页面可见且聚焦 → 应用内 toast；
// 后台标签页或窗口未聚焦 → 系统通知；无法送达 → 回到页面后补提示）、桌面通知开关、
// 长期未处理的进位提醒，以及无菜单授权时完全不轮询。
// 轮询周期 15s 对测试太慢：各用例经 window.__openOntologyRefreshDecisionAlerts 手动触发一次等价轮询；
// 页面的可见性与焦点由 setPageAttention 显式固定，不依赖无头浏览器的默认焦点行为。

const HOUR = 3_600_000
const now = '2026-11-22T08:00:00+00:00'

interface FakeNotification {
  title: string
  body?: string
  tag?: string
  onclick: (() => void) | null
}

type AlertWindow = Window & {
  __openOntologyRefreshDecisionAlerts: (now?: number) => Promise<void>
  __decisionNotifications: FakeNotification[]
}

const json = (route: Route, data: unknown) => route.fulfill({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify({ data }),
})

const decision = (id: string, createdAt: string, title: string, status: DecisionStatus = 'open'): DecisionItem => ({
  id, source: 'watchdog', status, title, question: '是否按当前方案继续？', factsSummary: '任务等待决定。',
  eventIds: ['event-1'], options: [{ id: 'yes', label: '继续' }, { id: 'no', label: '放弃' }],
  freeTextAllowed: true, priority: 'high', contentVersion: 2, createdAt, updatedAt: createdAt, writebackSteps: [],
})

async function seedAuth(page: Page, role: 'admin' | 'custom' = 'admin', menuPermissions?: string[]) {
  await page.addInitScript((seed) => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: { id: 'u-1', username: 'decision-alert-tester', role: seed.role, ...(seed.menuPermissions === undefined ? {} : { menu_permissions: seed.menuPermissions }) },
      },
      version: 0,
    }))
  }, { role, menuPermissions })
}

/** 替换浏览器 Notification：授权状态经 sessionStorage 保留以跨 reload，创建的实例记录在 window 上供断言 */
async function mockDesktopNotifications(page: Page, mode: 'default' | 'granted' | 'denied') {
  await page.addInitScript((initial: string) => {
    const created: FakeNotification[] = []
    const permissionKey = '__mockNotificationPermission'
    let permission = sessionStorage.getItem(permissionKey) ?? initial
    class MockNotification {
      static get permission() { return permission }
      static async requestPermission() {
        permission = 'granted'
        sessionStorage.setItem(permissionKey, permission)
        return 'granted'
      }
      title: string
      body?: string
      tag?: string
      onclick: (() => void) | null = null
      constructor(title: string, options: { body?: string; tag?: string } = {}) {
        this.title = title
        this.body = options.body
        this.tag = options.tag
        created.push(this)
      }
      close() {}
    }
    ;(window as unknown as { Notification: unknown }).Notification = MockNotification
    ;(window as unknown as AlertWindow).__decisionNotifications = created
  }, mode)
}

async function mockDecisionAlerts(page: Page, initial: DecisionItem[]) {
  const state = {
    decisions: [...initial],
    summaryRequests: 0,
    handled: [] as string[],
  }
  // 其余接口一律 404：本 spec 只关心决策提醒链路，不依赖的入口保持不可达。
  // 按路径前缀匹配而非 `**/api/**`：后者会连 Vite 的 /src/api/*.ts 源码模块一并拦截，页面直接白屏
  await page.route(url => url.pathname.startsWith('/api/'), route => route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' }))
  await page.route('**/api/v1/models', route => json(route, [{
    id: 'model-1', name: 'DeepSeek', config_type: 'llm', provider: 'deepseek',
    api_base: 'https://api.deepseek.com', has_api_key: true, enabled: true, is_default: true,
    last_test_status: 'success', last_tested_at: now, last_test_message: 'ok',
    models: ['deepseek-v4-pro'], options: {}, created_by: 'admin',
    created_at: now, updated_at: now,
  }]))
  await page.route('**/api/v2/super-assistant/**', route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    const count = (value: DecisionStatus) => state.decisions.filter(item => item.status === value).length
    if (path === '/api/v2/super-assistant/autoloop/rules') return json(route, [])
    if (path === '/api/v2/super-assistant/autoloop/decision-items/summary') {
      state.summaryRequests++
      const counts = {
        openCount: count('open'), pendingDispatchCount: count('pending_dispatch'), sendingCount: count('sending'),
        unknownCount: count('writeback_unknown'), failedCount: count('writeback_failed'),
      }
      return json(route, { ...counts, unfinishedTotal: Object.values(counts).reduce((total, value) => total + value, 0) })
    }
    if (path === '/api/v2/super-assistant/autoloop/decision-items') {
      const statuses = (url.searchParams.get('status') ?? '').split(',')
      return json(route, { items: state.decisions.filter(item => statuses.includes(item.status)), hasMore: false, nextCursor: null })
    }
    const itemPath = path.match(/^\/api\/v2\/super-assistant\/autoloop\/decision-items\/([^/]+)(\/mark-handled)?$/)
    if (itemPath) {
      const item = state.decisions.find(entry => entry.id === itemPath[1])
      if (!item) return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' })
      if (itemPath[2]) {
        state.handled.push(item.id)
        item.status = 'handled'
      }
      return json(route, item)
    }
    if (path === '/api/v2/super-assistant/conversations') return json(route, [{
      id: 'c-1', title: '决策提醒会话', model_config_id: 'model-1', status: 'active', created_at: now, updated_at: now,
    }])
    if (path === '/api/v2/super-assistant/conversations/c-1/messages') return json(route, [])
    if (path === '/api/v2/super-assistant/conversations/c-1/files') return json(route, [])
    if (path === '/api/v2/super-assistant/conversations/c-1/pending-confirmation') return json(route, null)
    if (path === '/api/v2/super-assistant/skills') return json(route, [])
    if (path === '/api/v2/super-assistant/mcp-servers') return json(route, [])
    if (path === '/api/v2/super-assistant/tools') return json(route, [])
    if (path === '/api/v2/super-assistant/multica/config') return json(route, { enabled: false, commands: [] })
    if (path === '/api/v2/super-assistant/runs') return json(route, [])
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' })
  })
  return state
}

/** 手动触发一次等价轮询；now 仅「等待天数进位」用例传入 */
const refreshAlerts = (page: Page, at?: number) => page.evaluate((value) => {
  const target = window as unknown as AlertWindow
  return target.__openOntologyRefreshDecisionAlerts(value)
}, at)

/** 固定页面的注意力状态：提醒通道据此在「应用内提示」与「系统通知」之间择一 */
async function setPageAttention(page: Page, state: 'focused' | 'blurred' | 'hidden') {
  await page.evaluate((next) => {
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => (next === 'hidden' ? 'hidden' : 'visible'),
    })
    Object.defineProperty(document, 'hasFocus', { configurable: true, value: () => next === 'focused' })
    if (next === 'focused') window.dispatchEvent(new Event('focus'))
    document.dispatchEvent(new Event('visibilitychange'))
  }, state)
}

const notificationsOf = (page: Page) => page.evaluate(() => {
  const target = window as unknown as AlertWindow
  return target.__decisionNotifications.map(({ title, body, tag }) => ({ title, body, tag }))
})

function requireBox(box: { x: number; y: number; width: number; height: number } | null) {
  if (!box) throw new Error('元素尚未完成布局')
  return box
}

const toasts = (page: Page, text: string) => page.locator('[data-sonner-toast]').filter({ hasText: text })

test.describe('待处理决策事项提醒', () => {
  test('提醒卡片：计数与等待时长悬浮在聊天区右上角并置顶，点击打开决策弹窗，右键入口保持可用', async ({ page }) => {
    await seedAuth(page)
    await mockDecisionAlerts(page, [decision('d-1', new Date(Date.now() - 98 * HOUR).toISOString(), '决定由谁刷新镜像 pin 并重新部署')])
    await page.goto('/#/super-assistant')

    const card = page.getByTestId('super-assistant-pending-decisions')
    await expect(card).toBeVisible()
    await expect(page.getByTestId('super-assistant-pending-decisions-count')).toHaveText('1')
    await expect(page.getByTestId('super-assistant-pending-decisions-waiting')).toHaveText('· 已等待 4 天')
    await expect(card.getByRole('button', { name: /查看待决策事项/ })).toHaveAttribute('title', /决定由谁刷新镜像 pin 并重新部署/)
    // 页面打开前已存在的事项只建立基线：不弹提醒、不脉冲
    await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
    await expect(card).toHaveAttribute('data-pulsing', 'false')
    await expect(card.locator('.animate-ping')).toHaveCount(0)
    await expect(page).toHaveTitle('(1) OpenOntology')

    // 落点即用户指定的 #root > div > section > main > div（聊天滚动区）右上角内侧 12px
    await card.evaluate(element => Promise.all(element.getAnimations().map(animation => animation.finished)))
    const chatBox = requireBox(await page.getByTestId('super-assistant-chat-area').boundingBox())
    const mainBox = requireBox(await page.locator('main').first().boundingBox())
    const cardBox = requireBox(await card.boundingBox())
    expect(cardBox.x + cardBox.width).toBeCloseTo(mainBox.x + mainBox.width - 12, 0)
    expect(cardBox.y).toBeCloseTo(mainBox.y + 12, 0)
    expect(cardBox.x).toBeGreaterThanOrEqual(chatBox.x)
    // 悬浮于消息区之上：卡片中心点的命中最上层元素属于卡片自身
    expect(await card.evaluate(element => {
      const rect = element.getBoundingClientRect()
      const hit = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2)
      return !!hit && element.contains(hit)
    })).toBe(true)

    await card.getByRole('button', { name: /查看待决策事项/ }).click()
    const dialog = page.getByRole('dialog')
    await expect(dialog).toBeVisible()
    await expect(dialog.getByText('决定由谁刷新镜像 pin 并重新部署').first()).toBeVisible()
    await expect(page).toHaveURL(/decisions=1/)

    // 原有右键入口不受影响
    await page.keyboard.press('Escape')
    await expect(dialog).toHaveCount(0)
    await page.getByTestId('super-assistant-chat-area').click({ button: 'right', position: { x: 12, y: 12 } })
    const menu = page.locator('[data-ctx-menu="super-assistant-chat"]')
    await expect(menu).toBeVisible()
    await menu.locator('[data-ctx-item="decisions"]').click()
    await expect(dialog).toBeVisible()
  })

  test('新决策事项到达：前台弹应用内提示并可定位到该事项，卡片计数与注意力动效更新，处理后卡片与标题角标消失', async ({ page }) => {
    // 含一次真实的注意力脉冲到期等待（15s），放宽超时
    test.slow()
    await seedAuth(page)
    await mockDesktopNotifications(page, 'denied')
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    const state = await mockDecisionAlerts(page, [])
    await page.goto('/#/super-assistant')
    await expect(page.getByTestId('super-assistant-composer')).toBeVisible()
    await expect(page.getByTestId('super-assistant-pending-decisions')).toHaveCount(0)
    await expect(page).toHaveTitle('OpenOntology')
    await setPageAttention(page, 'focused')

    state.decisions.push(decision('d-2', new Date().toISOString(), '是否升级镜像并重新部署'))
    await refreshAlerts(page)

    const arrival = toasts(page, '超级助手：新的决策事项待处理')
    await expect(arrival).toHaveCount(1)
    await expect(arrival).toContainText('「是否升级镜像并重新部署」')
    const card = page.getByTestId('super-assistant-pending-decisions')
    await expect(page.getByTestId('super-assistant-pending-decisions-count')).toHaveText('1')
    await expect(card).toHaveAttribute('data-pulsing', 'true')
    await expect(card.locator('.animate-ping')).toHaveCount(1)
    await expect(page).toHaveTitle('(1) OpenOntology')

    const bell = card.getByTestId('super-assistant-decision-notify').locator('svg')
    await expect(bell).toHaveCSS('animation-name', 'decisionBellRing')
    await expect(bell).toHaveCSS('animation-iteration-count', '1')
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await expect(bell).toHaveCSS('animation-name', 'none')
    await page.emulateMedia({ reducedMotion: 'no-preference' })

    await arrival.getByRole('button', { name: '查看' }).click()
    await expect(page).toHaveURL(/decision=d-2/)
    const dialog = page.getByRole('dialog', { name: '决策事项', exact: true })
    await expect(dialog.getByText('是否升级镜像并重新部署').first()).toBeVisible()

    // 注意力脉冲只持续一小段：到点自动静止，用户打算稍后处理时不被持续打扰
    await expect(card).toHaveAttribute('data-pulsing', 'false', { timeout: 20_000 })
    await expect(card.locator('.animate-ping')).toHaveCount(0)
    await expect(bell).toHaveCSS('animation-name', 'none')

    // 弹窗内处理后同步对齐：卡片与标题角标立即消失，而不是滞后到下一轮轮询
    await dialog.getByRole('button', { name: '关闭本地事项', exact: true }).click()
    const confirmation = page.getByRole('dialog', { name: '关闭本地事项？', exact: true })
    await expect(confirmation).toBeVisible()
    expect(state.handled).toEqual([])
    await confirmation.getByRole('button', { name: '确认关闭', exact: true }).click()
    await expect.poll(() => state.handled).toEqual(['d-2'])
    await expect(page.getByTestId('super-assistant-pending-decisions')).toHaveCount(0)
    await expect(page).toHaveTitle('OpenOntology')
  })

  test('后台标签页且已授权：系统通知保护规则草稿，确认后定位到对应事项', async ({ page }) => {
    await seedAuth(page)
    await mockDesktopNotifications(page, 'granted')
    const state = await mockDecisionAlerts(page, [])
    await page.goto('/#/super-assistant?conversation=c-1&ruleRouting=1&ruleRoutingTab=rules')
    const routing = page.getByRole('dialog', { name: '规则路由', exact: true })
    await routing.getByRole('button', { name: '新建规则', exact: true }).click()
    const draft = routing.getByRole('textbox', { name: '规则名称', exact: true })
    await draft.fill('规则草稿')

    await setPageAttention(page, 'hidden')
    state.decisions.push(decision('d-3', new Date().toISOString(), '是否扩容副本 executor'))
    await refreshAlerts(page)

    expect(await notificationsOf(page)).toEqual([{
      title: '超级助手：新的决策事项待处理', body: '「是否扩容副本 executor」', tag: 'openontology-decision-d-3',
    }])
    await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
    await expect(page).toHaveTitle('(1) OpenOntology')

    const clickNotification = () => page.evaluate(() => {
      const target = window as unknown as AlertWindow
      target.__decisionNotifications[0].onclick?.()
    })
    const exit = page.getByRole('dialog', { name: '放弃未保存的规则草稿？' })
    const detail = page.getByRole('dialog', { name: '决策事项', exact: true })
    await clickNotification()
    await expect(detail).toHaveCount(0)
    await exit.getByRole('button', { name: '继续编辑', exact: true }).click()
    await expect(exit).toHaveCount(0)
    await expect(draft).toHaveValue('规则草稿')
    await expect(draft).toBeFocused()
    await expect(page).toHaveURL(/conversation=c-1/)
    await expect(page).toHaveURL(/ruleRoutingTab=rules/)
    await expect(page).not.toHaveURL(/decision=/)

    await clickNotification()
    await exit.getByRole('button', { name: '放弃草稿', exact: true }).click()
    await expect(page).toHaveURL(/conversation=c-1/)
    await expect(page).toHaveURL(/decision=d-3/)
    await expect(routing).toHaveCount(0)
    await expect(detail.getByRole('heading', { name: '是否扩容副本 executor', exact: true })).toBeVisible()
    // 已经由系统通知送达：回到页面后不再补应用内提示
    await setPageAttention(page, 'focused')
    await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
  })

  test('后台标签页未授权：不弹系统通知，回到前台后补一条应用内提示且不重复', async ({ page }) => {
    await seedAuth(page)
    await mockDesktopNotifications(page, 'denied')
    const state = await mockDecisionAlerts(page, [])
    await page.goto('/#/super-assistant')
    await expect(page.getByTestId('super-assistant-composer')).toBeVisible()

    await setPageAttention(page, 'hidden')
    state.decisions.push(decision('d-4', new Date().toISOString(), '是否轮换访问密钥'))
    await refreshAlerts(page)

    expect(await notificationsOf(page)).toEqual([])
    await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
    // 标题角标不依赖通知授权：扫一眼标签栏即可知
    await expect(page).toHaveTitle('(1) OpenOntology')

    await setPageAttention(page, 'focused')
    const arrival = toasts(page, '超级助手：新的决策事项待处理')
    await expect(arrival).toHaveCount(1)
    await expect(arrival).toContainText('「是否轮换访问密钥」')
  })

  test('窗口可见但未聚焦：已授权时走系统通知，被静音时在重新获得焦点后补应用内提示', async ({ page }) => {
    await seedAuth(page)
    await mockDesktopNotifications(page, 'granted')
    const state = await mockDecisionAlerts(page, [])
    await page.goto('/#/super-assistant')
    await expect(page.getByTestId('super-assistant-composer')).toBeVisible()

    // 桌面办公最常见的离开方式：窗口没最小化、焦点在别的应用。应用内提示看不到，只有系统通知能触达
    await setPageAttention(page, 'blurred')
    state.decisions.push(decision('d-9', new Date().toISOString(), '是否重建索引'))
    await refreshAlerts(page)
    expect(await notificationsOf(page)).toHaveLength(1)
    await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)

    // 用户在卡片上静音后：未聚焦时不再打扰，回到窗口才补一条应用内提示
    const bell = page.getByTestId('super-assistant-decision-notify')
    await bell.click()
    await expect(bell).toHaveAttribute('data-state', 'muted')
    state.decisions.push(decision('d-10', new Date().toISOString(), '是否切换备用通道'))
    await refreshAlerts(page)
    expect(await notificationsOf(page)).toHaveLength(1)
    await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)

    await setPageAttention(page, 'focused')
    const arrival = toasts(page, '超级助手：新的决策事项待处理')
    await expect(arrival).toHaveCount(1)
    await expect(arrival).toContainText('「是否切换备用通道」')
  })

  test('桌面通知开关：发起授权后进入已开启态，可静音并跨刷新保留，再次点击恢复', async ({ page }) => {
    await seedAuth(page)
    await mockDesktopNotifications(page, 'default')
    await mockDecisionAlerts(page, [decision('d-5', new Date(Date.now() - 2 * HOUR).toISOString(), '是否归档旧版本')])
    await page.goto('/#/super-assistant')
    const bell = page.getByTestId('super-assistant-decision-notify')
    await expect(bell).toHaveAttribute('data-state', 'default')
    await expect(bell).toHaveAccessibleName('开启桌面通知')

    await bell.click()
    await expect(bell).toHaveAttribute('data-state', 'granted')
    await expect(toasts(page, '桌面通知已开启')).toHaveCount(1)

    await bell.click()
    await expect(bell).toHaveAttribute('data-state', 'muted')
    expect(await page.evaluate(() => localStorage.getItem('ob:sa-decision-alerts:v1:u-1'))).toBe('{"muted":true}')

    // 静音偏好落盘：刷新后仍是静音态，可一键恢复
    await page.reload()
    const bellAfterReload = page.getByTestId('super-assistant-decision-notify')
    await expect(bellAfterReload).toHaveAttribute('data-state', 'muted')
    await bellAfterReload.click()
    await expect(bellAfterReload).toHaveAttribute('data-state', 'granted')
  })

  test('浏览器已阻止通知：开关呈阻止态，点击给出站点设置指引', async ({ page }) => {
    await seedAuth(page)
    await mockDesktopNotifications(page, 'denied')
    await mockDecisionAlerts(page, [decision('d-8', new Date().toISOString(), '是否清理过期副本')])
    await page.goto('/#/super-assistant')
    const bell = page.getByTestId('super-assistant-decision-notify')
    await expect(bell).toHaveAttribute('data-state', 'denied')
    await expect(bell).toHaveAccessibleName('浏览器已阻止桌面通知')

    await bell.click()
    await expect(toasts(page, '浏览器已阻止本站通知')).toHaveCount(1)
    await expect(bell).toHaveAttribute('data-state', 'denied')
  })

  test('长期未处理：等待天数进位时提醒一次并定位到最老事项，同一天内不重复', async ({ page }) => {
    await seedAuth(page)
    await mockDecisionAlerts(page, [decision('d-6', new Date(Date.now() - 23 * HOUR).toISOString(), '是否下线旧通道')])
    await page.goto('/#/super-assistant')
    await expect(page.getByTestId('super-assistant-pending-decisions-waiting')).toHaveText('· 已等待 23 小时')
    // 加载当下不为既有积压弹提醒：卡片与标题角标已经持续可见
    await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)

    await setPageAttention(page, 'focused')
    await refreshAlerts(page, Date.now() + 2 * HOUR)
    const stale = toasts(page, '超级助手：决策事项长期未处理')
    await expect(stale).toHaveCount(1)
    await expect(stale).toContainText('1 项决策已等待 1 天')

    await refreshAlerts(page, Date.now() + 3 * HOUR)
    await expect(stale).toHaveCount(1)

    await stale.getByRole('button', { name: '查看' }).click()
    await expect(page).toHaveURL(/decision=d-6/)
    await expect(page.getByRole('dialog')).toBeVisible()
  })

  test('无 super_assistant 菜单授权：不轮询决策摘要、不提示、标题不变', async ({ page }) => {
    await seedAuth(page, 'custom', ['ontologies'])
    const state = await mockDecisionAlerts(page, [decision('d-7', new Date().toISOString(), '是否扩容存储')])
    await page.goto('/#/super-assistant')
    // 先等应用完成权限判定，再手动触发轮询，避免在入口脚本尚未加载时调用测试函数。
    await expect(page.getByRole('heading', { name: '当前页面无法访问', exact: true })).toBeVisible()
    await refreshAlerts(page)
    await refreshAlerts(page)
    expect(state.summaryRequests).toBe(0)
    await expect(page.getByTestId('super-assistant-pending-decisions')).toHaveCount(0)
    await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
    await expect(page).toHaveTitle('OpenOntology')
  })
})
