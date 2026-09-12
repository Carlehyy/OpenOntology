import { expect, test, type Page, type Route } from '@playwright/test'

const ontologyId = 'ontology-structure-initial-view'

async function mockOntologyStructure(page: Page, options: { includeUxFixtures?: boolean; holdLayoutResponse?: boolean } = {}) {
  const includeUxFixtures = options.includeUxFixtures === true
  const holdLayoutResponse = options.holdLayoutResponse === true
  await page.addInitScript(() => {
    localStorage.setItem('token', 'structure-view-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'structure-view-token',
        user: { id: 'structure-view-user', username: 'structure-view-user', role: 'admin' },
      },
      version: 0,
    }))
  })

  const ok = (route: Route, data: unknown) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ data, message: 'ok' }),
  })
  const layoutCalls: Array<{ positions: Record<string, { x: number; y: number }> }> = []
  let layoutFailure: string | null = null
  // holdLayoutResponse：把 PUT /layout 的响应挂起，直到测试显式放行，
  // 让「正在保存布局」瞬态在慢 CI 上也能被确定性地断言到（顺序安全：
  // 放行先于请求到达时直接落开门闩，处理器随后 await 立即通过）。
  let layoutGateOpen = false
  let layoutGateResolve: (() => void) | null = null

  await page.route(/^https?:\/\/[^/]+\/api\/v[12]\//, async (route) => {
    const path = new URL(route.request().url()).pathname

    if (path === `/api/v1/ontologies/${ontologyId}`) {
      return ok(route, {
        id: ontologyId,
        name: '初始视口测试本体',
        domain: '供应链',
        description: '验证本体结构首次展示时直接居中',
        version: 'v1',
        current_release_id: 'release-1',
        current_release_version: 'v1',
        status: 'published',
        entity_count: 1,
        relation_count: 0,
        action_count: 0,
        sentinel_count: 0,
        created_by: 'structure-view-user',
        created_at: '2026-08-07T00:00:00Z',
        updated_at: '2026-08-07T00:00:00Z',
      })
    }
    if (path === `/api/v2/ontologies/${ontologyId}/current-release/workspace`) {
      return ok(route, {
        version: 'v1',
        versionId: 'release-1',
        workspaceMode: 'release',
        editable: false,
        isCurrentRelease: true,
        objectTypes: [{
          id: 'object-order',
          name: 'Order',
          displayName: '订单',
          primaryKey: 'order_no',
          properties: [{
            id: 'order_no',
            name: 'order_no',
            displayName: '订单号',
            type: 'string',
            required: true,
          }],
        }, ...(includeUxFixtures ? [{
          id: 'object-customer',
          name: 'Customer',
          displayName: '客户',
          primaryKey: 'customer_no',
          properties: [{
            id: 'customer_no',
            name: 'customer_no',
            displayName: '客户编号',
            type: 'string',
            required: true,
          }],
        }] : [])],
        linkTypes: includeUxFixtures ? [{
          id: 'link-order-customer',
          name: 'belongs_to_customer',
          displayName: '订单归属客户',
          sourceObjectTypeId: 'object-order',
          targetObjectTypeId: 'object-customer',
          cardinality: 'many-to-one',
        }] : [],
        actions: includeUxFixtures ? [{
          id: 'action-create-order',
          name: 'create_order',
          displayName: '创建订单',
          objectTypeId: 'object-order',
          requiresApproval: false,
        }] : [],
        functions: includeUxFixtures ? [{
          id: 'function-validate-order',
          name: 'validate_order',
          displayName: '校验订单',
          functionType: 'validation',
          language: 'python',
          enabled: true,
          targetObjectTypeId: 'object-order',
        }] : [],
        sentinels: includeUxFixtures ? [{
          id: 'sentinel-public-order',
          name: 'public_order_watch',
          displayName: '公共订单监控',
          description: '随发布版本固化的公共规则',
          bindings: [{ alias: 'order', objectTypeId: 'object-order', filter: null }],
          links: [],
          condition: 'order.order_no != null',
          conditionRows: [],
          conditionLogic: 'and',
          primaryAlias: 'order',
          actionIds: ['action-create-order'],
          actionParameters: {},
          onChange: true,
          onSchedule: false,
          muted: false,
          enabled: true,
          origin: 'release_builtin',
        }] : [],
        canvasLayout: {},
      })
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/agent/dynamic-sentinels`) {
      return ok(route, includeUxFixtures ? [{
        id: 'sentinel-dynamic-order',
        ontologyId,
        name: 'dynamic_order_watch',
        displayName: '动态订单监控',
        description: '由本体助手按对话创建的动态规则',
        bindings: [{ alias: 'order', objectTypeId: 'object-order', filter: null }],
        links: [],
        condition: 'order.order_no != null',
        conditionRows: [],
        conditionLogic: 'and',
        primaryAlias: 'order',
        actionIds: ['action-create-order'],
        actionParameters: {},
        onChange: true,
        onSchedule: false,
        scanIntervalSeconds: 300,
        triggerMode: 'on_enter',
        muted: false,
        origin: 'assistant_dynamic',
        boundReleaseId: 'release-1',
        definitionRevision: 1,
        enabled: true,
        status: 'active',
        validationReport: { passed: true, errors: [] },
        trialCurrent: true,
        canEnable: true,
      }] : [])
    }
    if (path === '/api/v2/inbox/summary') {
      return ok(route, { openAlertCount: 0, actionableCount: 0, unreadCount: 0, resolvedCount: 0 })
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/overview`) {
      return ok(route, {
        release: { id: 'release-1', version: 'v1', publishedAt: '2026-08-07T00:00:00Z' },
        model: {
          objectTypes: 1,
          linkTypes: 0,
          actions: 0,
          actionsRequiringApproval: 0,
          functions: 0,
          sentinels: { total: 0, enabled: 0, muted: 0 },
        },
        data: {
          instances: 0,
          instancesBySource: {},
          linkInstances: 0,
          mappings: { total: 0, bound: 0, nameMatch: 0, autoCreate: 0, autoApply: 0 },
          topTypes: [],
        },
        runtime: {
          pendingApprovals: 0,
          decisions: { total: 0, approved: 0, rejected: 0, recentApprovalRate: null },
          firings7d: { total: 0, fired: 0, error: 0 },
          actionRuns7d: { total: 0, success: 0, failed: 0 },
          daily7d: [],
        },
        facts: { total: 0, byKind: {} },
      })
    }
    if (path === `/api/v2/formal/ontologies/${ontologyId}/facts/recent`) {
      return ok(route, [])
    }
    if (route.request().method() === 'PUT' && path === `/api/v2/ontologies/${ontologyId}/layout`) {
      const body = JSON.parse(route.request().postData() || '{}')
      layoutCalls.push({ positions: body.positions ?? {} })
      if (layoutFailure) {
        return route.fulfill({
          status: 500,
          contentType: 'application/json',
          body: JSON.stringify({ detail: layoutFailure }),
        })
      }
      if (holdLayoutResponse) {
        await new Promise<void>(resolve => {
          if (layoutGateOpen) { resolve(); return }
          layoutGateResolve = resolve
        })
      } else {
        // 让「正在保存布局」状态停留足够长，便于断言捕获
        await new Promise(resolve => setTimeout(resolve, 400))
      }
      return ok(route, { versionId: 'release-1', positions: {} })
    }
    return ok(route, [])
  })

  return {
    layoutCalls,
    failLayout: (message: string) => { layoutFailure = message },
    recoverLayout: () => { layoutFailure = null },
    releaseLayout: () => {
      layoutGateOpen = true
      layoutGateResolve?.()
    },
  }
}

test('点击本体结构后画布内容首次可见时已经居中且不再横向滑动', async ({ page }) => {
  await mockOntologyStructure(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(`/#/ontologies/${ontologyId}`, { waitUntil: 'domcontentloaded' })
  await expect(page.getByRole('button', { name: '本体结构', exact: true })).toBeVisible()

  await page.evaluate(() => {
    const state = window as typeof window & {
      __structureViewportFrames?: Array<{ centerX: number; centerY: number; transform: string }>
    }
    state.__structureViewportFrames = []
    let firstVisibleAt: number | null = null
    const capture = () => {
      const node = document.querySelector<HTMLElement>('[data-testid="structure-node-object"]')
      const viewport = document.querySelector<HTMLElement>('.react-flow__viewport')
      if (node && viewport && getComputedStyle(node).visibility !== 'hidden') {
        firstVisibleAt ??= performance.now()
        const rect = node.getBoundingClientRect()
        state.__structureViewportFrames?.push({
          centerX: rect.x + rect.width / 2,
          centerY: rect.y + rect.height / 2,
          transform: viewport.style.transform,
        })
      }
      if (firstVisibleAt === null || performance.now() - firstVisibleAt < 700) requestAnimationFrame(capture)
    }
    requestAnimationFrame(capture)
  })

  await page.getByRole('button', { name: '本体结构', exact: true }).click()
  const graph = page.getByTestId('ontology-structure-graph')
  const node = page.getByTestId('structure-node-object')
  await expect(graph).toBeVisible()
  await expect(node).toBeVisible()
  // 画布常驻操作提示
  await expect(page.getByTestId('structure-canvas-hint')).toContainText('左键拖节点 · 拖空白平移 · 滚轮缩放')
  await page.waitForTimeout(750)

  const frames = await page.evaluate(() => {
    const state = window as typeof window & {
      __structureViewportFrames?: Array<{ centerX: number; centerY: number; transform: string }>
    }
    return state.__structureViewportFrames || []
  })
  expect(frames.length).toBeGreaterThan(5)
  const firstFrame = frames[0]
  const lastFrame = frames.at(-1)!
  expect(Math.abs(lastFrame.centerX - firstFrame.centerX)).toBeLessThanOrEqual(1)
  expect(Math.abs(lastFrame.centerY - firstFrame.centerY)).toBeLessThanOrEqual(1)
  expect(new Set(frames.map(frame => frame.transform)).size).toBe(1)

  const canvasBox = await page.locator('.react-flow').boundingBox()
  const nodeBox = await node.boundingBox()
  expect(canvasBox).not.toBeNull()
  expect(nodeBox).not.toBeNull()
  expect(Math.abs(nodeBox!.x + nodeBox!.width / 2 - (canvasBox!.x + canvasBox!.width / 2))).toBeLessThanOrEqual(1)
  // 对象节点常态带 -translate-y-0.5（2px）悬浮观感，垂直居中断言放宽 1px 位移量；
  // ReactFlow 的视口适配量的是外层 wrapper，不受该内层样式影响。
  expect(Math.abs(nodeBox!.y + nodeBox!.height / 2 - (canvasBox!.y + canvasBox!.height / 2))).toBeLessThanOrEqual(3)
})

test('切换 L1/L2 视角时视口直接到位而不从角落滑入', async ({ page }) => {
  await mockOntologyStructure(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(`/#/ontologies/${ontologyId}`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('button', { name: '本体结构', exact: true }).click()
  await expect(page.getByTestId('structure-node-object')).toBeVisible()
  // Wait for the initial fit to fully settle so every later transform change
  // is caused by the level switch itself.
  await page.waitForTimeout(750)

  await page.evaluate(() => {
    const state = window as typeof window & { __levelSwitchTransforms?: string[] }
    state.__levelSwitchTransforms = []
    const startedAt = performance.now()
    const capture = () => {
      const viewport = document.querySelector<HTMLElement>('.react-flow__viewport')
      if (viewport) state.__levelSwitchTransforms?.push(viewport.style.transform)
      if (performance.now() - startedAt < 900) requestAnimationFrame(capture)
    }
    requestAnimationFrame(capture)
  })

  await page.getByRole('button', { name: 'L2 结构展开', exact: true }).click()
  await expect(page.getByTestId('structure-node-property')).toBeVisible()
  await page.waitForTimeout(950)

  const transforms = await page.evaluate(() => {
    const state = window as typeof window & { __levelSwitchTransforms?: string[] }
    return state.__levelSwitchTransforms || []
  })
  expect(transforms.length).toBeGreaterThan(5)
  // An animated fit interpolates the viewport across ~16 frames; a direct fit
  // changes the transform at most once (before vs after the switch).
  expect(new Set(transforms).size).toBeLessThanOrEqual(2)

  const canvasBox = await page.locator('.react-flow').boundingBox()
  const graphBox = await page.evaluate(() => {
    const rects = Array.from(document.querySelectorAll<HTMLElement>('.react-flow__node'))
      .map(element => element.getBoundingClientRect())
    if (!rects.length) return null
    const left = Math.min(...rects.map(rect => rect.x))
    const top = Math.min(...rects.map(rect => rect.y))
    const right = Math.max(...rects.map(rect => rect.x + rect.width))
    const bottom = Math.max(...rects.map(rect => rect.y + rect.height))
    return { centerX: (left + right) / 2, centerY: (top + bottom) / 2 }
  })
  expect(canvasBox).not.toBeNull()
  expect(graphBox).not.toBeNull()
  expect(Math.abs(graphBox!.centerX - (canvasBox!.x + canvasBox!.width / 2))).toBeLessThanOrEqual(2)
  expect(Math.abs(graphBox!.centerY - (canvasBox!.y + canvasBox!.height / 2))).toBeLessThanOrEqual(2)
})

test('结构工具栏使用友好层级名称、完整下拉文案并在输入时展示分类候选', async ({ page }) => {
  await mockOntologyStructure(page, { includeUxFixtures: true })
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/ontologies/' + ontologyId, { waitUntil: 'domcontentloaded' })
  await page.getByRole('button', { name: '本体结构', exact: true }).click()
  await expect(page.getByTestId('ontology-structure-graph')).toBeVisible()

  const perspective = page.getByLabel('图谱视角')
  const l1 = perspective.getByRole('button', { name: 'L1 结构概览', exact: true })
  const l2 = perspective.getByRole('button', { name: 'L2 结构展开', exact: true })
  await expect(l1).toHaveAttribute('aria-pressed', 'true')
  await expect(l2).toHaveAttribute('aria-pressed', 'false')

  const functionTrigger = page.getByLabel('查看计算函数使用关系')
  const sentinelTrigger = page.getByLabel('查看哨兵规则覆盖范围')
  await expect(functionTrigger).toContainText('计算函数 · 查看使用关系')
  await expect(sentinelTrigger).toContainText('哨兵规则 · 查看覆盖范围')
  await expect(functionTrigger).toHaveCSS('width', '224px')
  await expect(sentinelTrigger).toHaveCSS('width', '224px')
  for (const trigger of [functionTrigger, sentinelTrigger]) {
    const labelFits = await trigger.locator('span.min-w-0.flex-1').evaluate(element => element.scrollWidth <= element.clientWidth)
    expect(labelFits).toBeTruthy()
  }

  const search = page.getByLabel('搜索本体结构')
  await search.fill('订')
  const objectCandidate = page.getByTestId('structure-search-result-object-object-order')
  await expect(objectCandidate).toBeVisible()
  const candidateBox = await objectCandidate.boundingBox()
  expect(candidateBox).toBeTruthy()
  const hitCandidate = await page.evaluate(({ x, y }) =>
    document.elementFromPoint(x, y)?.closest('[role="option"]')?.textContent,
  { x: candidateBox!.x + candidateBox!.width / 2, y: candidateBox!.y + candidateBox!.height / 2 })
  expect(hitCandidate).toContain('订单')

  await search.fill('归属')
  await expect(page.getByTestId('structure-search-result-relation-link:link-order-customer')).toBeVisible()

  await l2.click()
  await expect(l2).toHaveAttribute('aria-pressed', 'true')
  await search.fill('订单号')
  await expect(page.getByTestId('structure-search-result-property-property:object-order:order_no')).toBeVisible()
  await search.fill('创建')
  await expect(page.getByTestId('structure-search-result-action-action:action-create-order')).toBeVisible()
  await search.fill('')

  await sentinelTrigger.click()
  const sentinelDialog = page.getByRole('dialog', { name: '选择哨兵规则' })
  await expect(sentinelDialog).toBeVisible()
  await expect(page.getByTestId('sentinel-dependency-source-counts')).toHaveText('· 公共哨兵 1 · 动态哨兵 1')
  const options = sentinelDialog.getByRole('option')
  await expect(options).toHaveCount(2)
  await expect(options.nth(0)).toContainText('公共订单监控')
  await expect(options.nth(0)).toContainText('公共哨兵')
  await expect(options.nth(1)).toContainText('动态订单监控')
  await expect(options.nth(1)).toContainText('动态哨兵')
  const publicBadge = page.getByTestId('sentinel-dependency-source-sentinel-public-order')
  const dynamicBadge = page.getByTestId('sentinel-dependency-source-sentinel-dynamic-order')
  await expect(publicBadge).toHaveText(/●\s*公共哨兵/)
  await expect(dynamicBadge).toHaveText(/✦\s*动态哨兵/)
  expect(await publicBadge.evaluate(element => element.previousElementSibling?.textContent)).toBe('公共订单监控')
  expect(await dynamicBadge.evaluate(element => element.previousElementSibling?.textContent)).toBe('动态订单监控')
})

test('拖拽节点后自动保存提示按 3→2→1 倒计时并复位', async ({ page }) => {
  const { layoutCalls, releaseLayout } = await mockOntologyStructure(page, { holdLayoutResponse: true })
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/ontologies/' + ontologyId, { waitUntil: 'domcontentloaded' })
  await page.getByRole('button', { name: '本体结构', exact: true }).click()
  const node = page.getByTestId('structure-node-object')
  await expect(node).toBeVisible()
  // 等待初始适配稳定，避免拖拽起点落在视图过渡中
  await page.waitForTimeout(750)

  const box = await node.boundingBox()
  expect(box).toBeTruthy()
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2)
  await page.mouse.down()
  await page.mouse.move(box!.x + box!.width / 2 + 60, box!.y + box!.height / 2 + 40, { steps: 8 })
  await page.mouse.up()

  const status = page.getByTestId('structure-save-status')
  await expect(status).toContainText('3 秒后自动保存')
  // 倒计时阶段使用琥珀色强调（--color-warning 语义令牌）
  expect(await status.evaluate(element => getComputedStyle(element).color)).toBe('rgb(201, 134, 26)')
  await expect(status).toContainText('2 秒后自动保存', { timeout: 2500 })
  await expect(status).toContainText('1 秒后自动保存', { timeout: 2500 })
  // 布局接口响应被 mock 挂起：「正在保存布局」会稳定停留，消除慢 CI 上
  // 错过瞬态的抖动（回归点：CI 曾从「1 秒后」直接跳到「布局已保存」）
  await expect(status).toContainText('正在保存布局', { timeout: 3000 })
  releaseLayout()
  await expect(status).toContainText('布局已保存', { timeout: 3000 })
  // 成功阶段使用绿色强调（--color-success 语义令牌）
  expect(await status.evaluate(element => getComputedStyle(element).color)).toBe('rgb(45, 138, 78)')
  // 成功提示短暂停留后回到空闲文案，不留存「布局已保存」
  await expect(status).toContainText('拖动后自动保存布局', { timeout: 6000 })

  expect(layoutCalls.length).toBe(1)
  expect(layoutCalls[0].positions['l1:object-order']).toBeTruthy()
  const saved = layoutCalls[0].positions['l1:object-order']
  expect(typeof saved.x).toBe('number')
  expect(typeof saved.y).toBe('number')
})
test('布局保存失败展示红色重试，接口恢复后点击重试成功并复位', async ({ page }) => {
  const { layoutCalls, failLayout, recoverLayout } = await mockOntologyStructure(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/#/ontologies/' + ontologyId, { waitUntil: 'domcontentloaded' })
  await page.getByRole('button', { name: '本体结构', exact: true }).click()
  const node = page.getByTestId('structure-node-object')
  await expect(node).toBeVisible()
  await page.waitForTimeout(750)

  failLayout('模拟布局保存失败')
  const box = await node.boundingBox()
  expect(box).toBeTruthy()
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2)
  await page.mouse.down()
  await page.mouse.move(box!.x + box!.width / 2 + 60, box!.y + box!.height / 2 + 40, { steps: 8 })
  await page.mouse.up()

  // 倒计时结束后保存失败，出现红色重试入口
  const retry = page.getByRole('button', { name: '保存失败 · 点击重试' })
  await expect(retry).toBeVisible({ timeout: 6000 })

  // 恢复接口并点击重试：成功保存并最终复位为空闲文案
  recoverLayout()
  await retry.click()
  const status = page.getByTestId('structure-save-status')
  await expect(status).toContainText('布局已保存', { timeout: 4000 })
  await expect(status).toContainText('拖动后自动保存布局', { timeout: 6000 })

  // 至少包含第一次失败与重试成功两次提交
  expect(layoutCalls.length).toBeGreaterThanOrEqual(2)
})
