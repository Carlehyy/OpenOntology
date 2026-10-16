import { expect, test, type Page, type Route } from '@playwright/test'

// 超级助手快捷输入（mocked）：小卡片展示与插入语义（光标锚点插入 / 未交互
// 拼接末尾）、设置弹窗全链路（列表+详情双栏：新增/编辑/删除/键盘上移下移的
// reorder 契约、点选与切换、Esc 分层回退、窄屏列表⇄详情单栏切换）、空态与
// 加载失败重试。按 AGENTS.md 副作用交互规范，插入效果断言最终落在
// textarea 实际值上（含插入位置），不以提示出现代替；真实拖拽排序在
// 真实环境人工验收（HTML5 DnD 在无头下的合成事件不可靠）。

const json = (route: Route, data: unknown, status = 200) => route.fulfill({
  status,
  contentType: 'application/json',
  body: JSON.stringify({ data }),
})

interface MockQuickInput {
  id: string
  title: string
  content: string
  sort_order: number
  created_at: string
  updated_at: string
}

const INTRO = '我是产品经理，正在做 OpenOntology 平台。'
const FOLLOW_UP = '请展开讲讲第 2 点，给出具体例子。'

function seedItems(): MockQuickInput[] {
  return [
    { id: 'qi-1', title: '自我介绍', content: INTRO, sort_order: 0, created_at: '2026-10-12T00:00:00Z', updated_at: '2026-10-12T00:00:00Z' },
    { id: 'qi-2', title: '追问模板', content: FOLLOW_UP, sort_order: 1, created_at: '2026-10-12T00:00:01Z', updated_at: '2026-10-12T00:00:01Z' },
  ]
}

interface QuickInputMocks {
  items: MockQuickInput[]
  createBodies: Array<{ title: string; content: string }>
  updateBodies: Array<{ title?: string; content?: string }>
  reorderBodies: string[][]
  deletedIds: string[]
  listFailTimes: number
}

async function seedAuth(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: { id: 'u-1', username: 'quick-tester', role: 'admin' },
      },
      version: 0,
    }))
  })
}

// 单一统一 handler（route.continue() 是放行到网络而非落给下一个 handler，
// 拆两个 page.route 会让未命中的基础 API 全部打到已断后端的代理上）
async function mockApis(page: Page, options: { initial?: MockQuickInput[]; listFailTimes?: number } = {}) {
  const mocks: QuickInputMocks = {
    items: options.initial ? [...options.initial] : seedItems(),
    createBodies: [],
    updateBodies: [],
    reorderBodies: [],
    deletedIds: [],
    listFailTimes: options.listFailTimes ?? 0,
  }
  await page.route('**/api/**', route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (!path.startsWith('/api/')) return route.continue()

    // ---- 快捷内容族 ----
    if (path === '/api/v2/super-assistant/quick-inputs') {
      if (request.method() === 'GET') {
        if (mocks.listFailTimes > 0) {
          mocks.listFailTimes -= 1
          return route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: '快捷内容加载失败（mock 500）' }) })
        }
        return json(route, [...mocks.items].sort((a, b) => a.sort_order - b.sort_order))
      }
      if (request.method() === 'POST') {
        const body = request.postDataJSON() as { title: string; content: string }
        mocks.createBodies.push(body)
        const created: MockQuickInput = {
          id: `qi-created-${mocks.createBodies.length}`,
          title: body.title,
          content: body.content,
          sort_order: mocks.items.length,
          created_at: '2026-10-12T01:00:00Z',
          updated_at: '2026-10-12T01:00:00Z',
        }
        mocks.items.push(created)
        return json(route, created, 201)
      }
    }
    if (path === '/api/v2/super-assistant/quick-inputs/reorder' && request.method() === 'PUT') {
      const body = request.postDataJSON() as { ids: string[] }
      mocks.reorderBodies.push(body.ids)
      const byId = new Map(mocks.items.map(item => [item.id, item]))
      const reordered = body.ids.map(id => byId.get(id)).filter((item): item is MockQuickInput => item !== undefined)
      reordered.forEach((item, index) => { item.sort_order = index })
      mocks.items = [...reordered, ...mocks.items.filter(item => !body.ids.includes(item.id))]
      return json(route, [...mocks.items].sort((a, b) => a.sort_order - b.sort_order))
    }
    const match = path.match(/^\/api\/v2\/super-assistant\/quick-inputs\/([^/]+)$/)
    if (match) {
      const id = match[1]
      const entry = mocks.items.find(item => item.id === id)
      if (request.method() === 'PATCH') {
        const body = request.postDataJSON() as { title?: string; content?: string }
        mocks.updateBodies.push(body)
        if (entry) Object.assign(entry, body, { updated_at: '2026-10-12T02:00:00Z' })
        return json(route, entry ?? {})
      }
      if (request.method() === 'DELETE') {
        mocks.deletedIds.push(id)
        mocks.items = mocks.items.filter(item => item.id !== id)
        return route.fulfill({ status: 204, body: '' })
      }
    }

    // ---- 工作台基础 API ----
    if (path === '/api/v1/models') {
      return json(route, [{
        id: 'model-1', name: 'DeepSeek', config_type: 'llm', provider: 'deepseek',
        api_base: 'https://api.deepseek.com', has_api_key: true, enabled: true, is_default: true,
        last_test_status: 'success', last_tested_at: null, last_test_message: 'ok',
        models: ['deepseek-v4-pro'], options: {}, created_by: 'admin',
        created_at: null, updated_at: null,
      }])
    }
    if (path === '/api/v2/super-assistant/conversations') return json(route, [])
    if (path === '/api/v2/super-assistant/skills') return json(route, [])
    if (path === '/api/v2/super-assistant/mcp-servers') return json(route, [])
    if (path === '/api/v2/super-assistant/tools') return json(route, [])
    if (path === '/api/v2/super-assistant/multica/config') {
      return json(route, { configured: false, enabled: false, base_url: '', workspace_id: '', token_set: false, commands: [], last_test_status: null, last_test_message: null, last_tested_at: null })
    }
    return route.continue()
  })
  return mocks
}

async function openWorkbench(page: Page) {
  page.on('pageerror', error => console.error('[quick-input-spec pageerror]', error.message))
  await page.goto('/#/super-assistant')
  await expect(page.getByTestId('super-assistant-composer')).toBeVisible()
}

const quickButton = (page: Page) => page.getByTestId('super-assistant-quick-input-button')
const menu = (page: Page) => page.getByTestId('super-assistant-quick-input-menu')
const composerTextarea = (page: Page) => page.getByTestId('super-assistant-composer').locator('textarea')
const dialog = (page: Page) => page.getByTestId('quick-inputs-dialog')

test.describe('超级助手快捷输入', () => {
  test('卡片展示与光标锚点插入：点击标题插入草稿光标处并回焦输入框', async ({ page }) => {
    await seedAuth(page)
    const mocks = await mockApis(page)
    await openWorkbench(page)

    // 预置草稿并把光标移到「请整|理」中间（voice spec 同款手法）
    await composerTextarea(page).click()
    await composerTextarea(page).fill('请')
    await composerTextarea(page).pressSequentially('整理')
    await composerTextarea(page).press('ArrowLeft')

    await quickButton(page).click()
    await expect(menu(page)).toBeVisible()
    // 卡片按展示优先序列出标题与预览，右上角有设置入口
    await expect(menu(page).getByText('自我介绍')).toBeVisible()
    await expect(menu(page).getByText('我是产品经理，正在做 OpenOntology 平台。')).toBeVisible()
    await expect(menu(page).getByText('追问模板')).toBeVisible()
    await expect(menu(page).getByTestId('super-assistant-quick-input-settings')).toBeVisible()

    await menu(page).getByTestId('super-assistant-quick-input-item').filter({ hasText: '自我介绍' }).click()
    // 插入位置在用户光标（「整理」中间）而非开头/末尾——AGENTS.md：断言最终结果
    await expect(composerTextarea(page)).toHaveValue(`请整${INTRO}理`)
    await expect(menu(page)).toBeHidden()
    await expect(composerTextarea(page)).toBeFocused()
    // 只插入不发送
    expect(mocks.createBodies).toHaveLength(0)
  })

  test('未交互输入框：插入退化为拼接末尾', async ({ page }) => {
    await seedAuth(page)
    await mockApis(page)
    await openWorkbench(page)

    // 全程不触碰 textarea（autoFocus 的 0 位光标不是用户放置的）
    await quickButton(page).click()
    await menu(page).getByTestId('super-assistant-quick-input-item').filter({ hasText: '追问模板' }).click()
    await expect(composerTextarea(page)).toHaveValue(FOLLOW_UP)
    await expect(composerTextarea(page)).toBeFocused()
  })

  test('连续插入：光标停在上次插入末尾，第二段接在其后', async ({ page }) => {
    await seedAuth(page)
    await mockApis(page)
    await openWorkbench(page)

    await quickButton(page).click()
    await menu(page).getByTestId('super-assistant-quick-input-item').filter({ hasText: '自我介绍' }).click()
    await expect(composerTextarea(page)).toHaveValue(INTRO)

    await quickButton(page).click()
    await menu(page).getByTestId('super-assistant-quick-input-item').filter({ hasText: '追问模板' }).click()
    await expect(composerTextarea(page)).toHaveValue(`${INTRO}${FOLLOW_UP}`)
  })

  test('空态：无快捷内容时卡片给出去设置引导', async ({ page }) => {
    await seedAuth(page)
    await mockApis(page, { initial: [] })
    await openWorkbench(page)

    await quickButton(page).click()
    await expect(menu(page).getByTestId('super-assistant-quick-input-empty')).toBeVisible()
  })

  test('加载失败可重试：错误呈现 → 重试后列表恢复', async ({ page }) => {
    await seedAuth(page)
    await mockApis(page, { listFailTimes: 1 })
    await openWorkbench(page)

    await quickButton(page).click()
    await expect(menu(page).getByText('加载失败')).toBeVisible()
    await menu(page).getByRole('button', { name: '重试' }).click()
    await expect(menu(page).getByText('自我介绍')).toBeVisible()
  })

  test('设置弹窗全链路：新增→编辑→上移重排→删除', async ({ page }) => {
    await seedAuth(page)
    const mocks = await mockApis(page)
    await openWorkbench(page)

    // 卡片右上入口跳设置：卡片收起、弹窗打开；未选中时详情区为占位
    await quickButton(page).click()
    await menu(page).getByTestId('super-assistant-quick-input-settings').click()
    await expect(menu(page)).toBeHidden()
    await expect(dialog(page)).toBeVisible()
    const rows = dialog(page).getByTestId('quick-input-row')
    const detail = dialog(page).getByTestId('quick-input-detail')
    await expect(rows).toHaveCount(2)
    await expect(rows.nth(0)).toContainText('自我介绍')
    await expect(rows.nth(1)).toContainText('追问模板')
    await expect(dialog(page).getByTestId('quick-input-detail-empty')).toBeVisible()

    // 新增：入口在列表列头，表单渲染在详情区
    await dialog(page).getByTestId('quick-input-create-toggle').click()
    await dialog(page).getByTestId('quick-input-create-title').fill('周报模板')
    await dialog(page).getByTestId('quick-input-create-content').fill('本周进展：')
    await dialog(page).getByTestId('quick-input-create-submit').click()
    await expect(rows).toHaveCount(3)
    await expect(rows.nth(2)).toContainText('周报模板')
    await expect(dialog(page).getByText('共 3 条')).toBeVisible()
    expect(mocks.createBodies).toEqual([{ title: '周报模板', content: '本周进展：' }])
    // 连续录入：成功后表单保持展开、字段清空回焦标题
    await expect(dialog(page).getByTestId('quick-input-create-title')).toHaveValue('')

    // 空标题行内校验（不发请求）
    await dialog(page).getByTestId('quick-input-create-title').fill('  ')
    await dialog(page).getByTestId('quick-input-create-submit').click()
    await expect(dialog(page).getByText('请填写内容标题')).toBeVisible()
    expect(mocks.createBodies).toHaveLength(1)

    // 取消新增：详情回未选中占位
    await dialog(page).getByRole('button', { name: '取消' }).click()
    await expect(dialog(page).getByTestId('quick-input-detail-empty')).toBeVisible()

    // 编辑首条：先点选行 → 详情区展示 → 详情头部编辑
    await rows.nth(0).getByText('自我介绍').click()
    await expect(detail).toContainText(INTRO)
    await dialog(page).getByTestId('quick-input-edit').click()
    await dialog(page).getByTestId('quick-input-edit-title').fill('自我介绍（新版）')
    await dialog(page).getByTestId('quick-input-edit-submit').click()
    // 保存成功回查看态：列表行与详情标题都已是新标题
    await expect(rows.nth(0)).toContainText('自我介绍（新版）')
    await expect(dialog(page).getByTestId('quick-input-edit-title')).toBeHidden()
    await expect(detail).toContainText('自我介绍（新版）')
    expect(mocks.updateBodies).toEqual([{ title: '自我介绍（新版）', content: INTRO }])

    // 键盘等价排序：第 2 行上移 → PUT reorder 携带全量新顺序，列表即时重排
    await rows.nth(1).getByTestId('quick-input-move-up').click()
    await expect(rows.nth(0)).toContainText('追问模板')
    await expect(rows.nth(1)).toContainText('自我介绍（新版）')
    // 全量顺序契约：ids 覆盖该用户全部条目（含本次新建的周报模板）
    expect(mocks.reorderBodies).toEqual([['qi-2', 'qi-1', 'qi-created-1']])

    // 删除：点选行 → 详情头部删除 → 确认框 → 行消失、选中清空回占位
    await rows.nth(0).getByText('追问模板').click()
    await dialog(page).getByTestId('quick-input-delete').click()
    await page.getByRole('button', { name: '删除', exact: true }).click()
    await expect(rows).toHaveCount(2)
    await expect(dialog(page).getByTestId('quick-input-detail-empty')).toBeVisible()
    expect(mocks.deletedIds).toEqual(['qi-2'])

    // 关闭弹窗后卡片与设置同源：新顺序/新条目即时反映
    await page.keyboard.press('Escape')
    await expect(dialog(page)).toBeHidden()
    await quickButton(page).click()
    await expect(menu(page).getByText('自我介绍（新版）')).toBeVisible()
    await expect(menu(page).getByText('周报模板')).toBeVisible()
  })

  test('列表点选 → 详情展示全文，切换选中跟随', async ({ page }) => {
    await seedAuth(page)
    await mockApis(page)
    await openWorkbench(page)

    await quickButton(page).click()
    await menu(page).getByTestId('super-assistant-quick-input-settings').click()
    await expect(dialog(page)).toBeVisible()
    const rows = dialog(page).getByTestId('quick-input-row')
    const detail = dialog(page).getByTestId('quick-input-detail')

    // 未选中：占位可见
    await expect(dialog(page).getByTestId('quick-input-detail-empty')).toBeVisible()

    // 点选首行 → 详情区完整展示标题与内容（列表行只有截断预览）+ 更新时间 meta
    await rows.nth(0).getByText('自我介绍').click()
    await expect(detail).toContainText('自我介绍')
    await expect(detail).toContainText(INTRO)
    await expect(detail).toContainText('更新于')
    // meta 已内联进详情头（≥lg 可见，对齐 NotificationsDialog 详情头）
    const meta = dialog(page).getByTestId('quick-input-detail-meta')
    await expect(meta).toBeVisible()
    await expect(meta).toContainText('更新于')

    // 再选另一行：详情头部与正文跟随切换
    await rows.nth(1).getByText('追问模板').click()
    await expect(detail).toContainText('追问模板')
    await expect(detail).toContainText(FOLLOW_UP)
    await expect(detail).not.toContainText(INTRO)
  })

  test('Esc 分层：编辑态先退表单不关弹窗，再 Esc 才关闭', async ({ page }) => {
    await seedAuth(page)
    const mocks = await mockApis(page)
    await openWorkbench(page)

    await quickButton(page).click()
    await menu(page).getByTestId('super-assistant-quick-input-settings').click()
    await expect(dialog(page)).toBeVisible()
    const rows = dialog(page).getByTestId('quick-input-row')
    const detail = dialog(page).getByTestId('quick-input-detail')

    await rows.nth(0).getByText('自我介绍').click()
    await dialog(page).getByTestId('quick-input-edit').click()
    await expect(dialog(page).getByTestId('quick-input-edit-title')).toBeVisible()
    // 未保存修改随 Esc 退出一并丢弃
    await dialog(page).getByTestId('quick-input-edit-title').fill('不应保存的草稿')
    await page.keyboard.press('Escape')
    // 第一层：退编辑表单回查看态，弹窗仍在，修改未落库
    await expect(dialog(page).getByTestId('quick-input-edit-title')).toBeHidden()
    await expect(dialog(page)).toBeVisible()
    await expect(detail).toContainText('自我介绍')
    expect(mocks.updateBodies).toHaveLength(0)
    // 第二层：桌面视口无选中回退层，Esc 交 Radix 关弹窗
    await page.keyboard.press('Escape')
    await expect(dialog(page)).toBeHidden()
  })

  test('窄屏单栏：选中后列表隐藏、返回条可见，点返回回列表', async ({ page }) => {
    await page.setViewportSize({ width: 500, height: 800 })
    await seedAuth(page)
    await mockApis(page)
    await openWorkbench(page)

    await quickButton(page).click()
    await menu(page).getByTestId('super-assistant-quick-input-settings').click()
    await expect(dialog(page)).toBeVisible()
    const rows = dialog(page).getByTestId('quick-input-row')
    const detail = dialog(page).getByTestId('quick-input-detail')

    // 列表态（未选中）：列表可见、详情隐藏
    await expect(rows.nth(0)).toBeVisible()
    await expect(detail).toBeHidden()

    // 选中 → 单栏切详情：列表隐藏、返回条可见、正文可读
    await rows.nth(0).getByText('自我介绍').click()
    await expect(detail).toBeVisible()
    await expect(rows.nth(0)).toBeHidden()
    await expect(dialog(page).getByTestId('quick-input-back-list')).toBeVisible()
    await expect(detail).toContainText(INTRO)

    // 点返回条回列表
    await dialog(page).getByTestId('quick-input-back-list').click()
    await expect(rows.nth(0)).toBeVisible()
    await expect(detail).toBeHidden()
  })
})
