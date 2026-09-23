import { expect, test, type Page, type Route } from '@playwright/test'

interface MockOptions {
  failFirstApply?: boolean
  datasetStatus?: 'approved' | 'rejected'
  withInstances?: boolean
  withUnmappedObject?: boolean
  /** 追加第二条映射（日志类型，同源数据集）：全景出现 1 数据资产卡 + 2 本体元素卡 */
  withSecondMappedObject?: boolean
  /** 生成 24 个对象实体与映射：清单长列表，验证卡片内细滚动与整页单页 */
  manyRows?: boolean
  noMappings?: boolean
  darkTheme?: boolean
}

async function mockMappingPreview(page: Page, options: MockOptions = {}) {
  const columns = Array.from({ length: 8 }, (_, index) => `field_${index + 1}`)
  const datasetStatus = options.datasetStatus ?? 'approved'
  let applyAttempts = 0
  const requests = { objectsTypeId: '' }
  await page.addInitScript(opts => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: { token: 'e2e-token', user: { id: 'u1', username: 'tester', role: 'admin' } },
      version: 0,
    }))
    if (opts.darkTheme) {
      localStorage.setItem('theme', JSON.stringify({ state: { theme: 'dark' }, version: 0 }))
    }
  }, { darkTheme: Boolean(options.darkTheme) })

  await page.route(/^https?:\/\/[^/]+\/api\/v[12]\//, async (route: Route) => {
    const url = new URL(route.request().url())
    const ok = (data: unknown) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ data, message: 'ok' }),
    })

    if (url.pathname === '/api/v1/ontologies/ontology-preview') return ok({
      id: 'ontology-preview', name: '预览测试本体', domain: '测试', version: 'v1',
      current_release_id: 'release-1', current_release_version: 'v1', status: 'published',
      entity_count: 1, relation_count: 0, action_count: 0, sentinel_count: 0,
      created_by: 'tester', created_at: '2026-07-22T00:00:00Z', updated_at: '2026-07-22T00:00:00Z',
    })
    // 头部"导出本体结构"按钮：以 blob 方式下载，mocked 套件内返回最小 JSON 结构
    if (url.pathname === '/api/v1/ontologies/ontology-preview/export') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ data: { ontology: { id: 'ontology-preview', name: '预览测试本体' } } }),
      })
    }
    if (url.pathname === '/api/v2/ontologies/ontology-preview/current-release/workspace') return ok({
      versionId: 'release-1', versionNumber: 'v1', isCurrentRelease: true,
      workspaceMode: 'release', editable: false, revision: 'r1',
      objectTypes: [
        {
          id: 'object-order', name: 'Order', displayName: '订单', primaryKey: 'id',
          properties: [{ id: 'id', name: 'id', displayName: '订单编号', type: 'string' }],
        },
        ...(options.withUnmappedObject || options.withSecondMappedObject ? [{
          id: 'object-log', name: 'Log', displayName: '日志', primaryKey: null,
          properties: [{ id: 'msg', name: 'msg', displayName: '消息', type: 'string' }],
        }] : []),
        ...(options.manyRows ? Array.from({ length: 24 }, (_, index) => ({
          id: `object-batch-${index}`, name: `Batch${index}`, displayName: `批量对象${String(index + 1).padStart(2, '0')}`,
          primaryKey: 'id',
          properties: [{ id: 'id', name: 'id', displayName: '编号', type: 'string' }],
        })) : []),
      ],
      linkTypes: [],
      mappings: options.noMappings ? [] : [
        {
          id: 'mapping-1', curatedDatasetId: 'dataset-wide', targetObjectTypeId: 'object-order',
          entityClass: 'Order',
          fieldMapping: { field_1: 'id', __applied_dataset_version_id__: 'dataset-version-23' },
          status: 'published',
        },
        ...(options.withSecondMappedObject ? [{
          id: 'mapping-log', curatedDatasetId: 'dataset-wide', targetObjectTypeId: 'object-log',
          entityClass: 'Log',
          fieldMapping: { field_2: 'msg' },
          status: 'published',
        }] : []),
        ...(options.manyRows ? Array.from({ length: 24 }, (_, index) => ({
          id: `mapping-batch-${index}`, curatedDatasetId: 'dataset-wide', targetObjectTypeId: `object-batch-${index}`,
          entityClass: `Batch${index}`,
          fieldMapping: { field_1: 'id' },
          status: 'published',
        })) : []),
      ],
      linkMappings: [],
    })
    if (url.pathname === '/api/v2/curated') return ok([{
      id: 'dataset-wide', name: '订单宽表', status: datasetStatus, row_count: 45,
      quality_score: .96, primary_key: 'field_1', producer_pipeline_id: null,
      output_key: null, has_review_evidence: true,
    }])
    if (url.pathname === '/api/v2/datasets/overview') return ok({ items: [], total: 0, page: 1, page_size: 20 })
    if (url.pathname === '/api/v2/datasets/dataset-wide/schema') return ok({
      dataset_id: 'dataset-wide',
      columns: columns.map((name, index) => ({
        name, display_name: `字段 ${index + 1}`, type: 'string', nullable: index > 0,
        is_primary_key: index === 0, sample_values: [`值 ${index + 1}`],
      })),
    })
    if (url.pathname === '/api/v2/datasets/dataset-wide/versions') return ok([
      { id: 'dataset-version-23', version_no: 23, rowcount: 45, processed_at: '2026-07-26T08:27:21+00:00' },
    ])
    if (url.pathname === '/api/v2/datasets/dataset-wide/preview') return ok({
      columns,
      total_rows: 2,
      rows: Array.from({ length: 2 }, (_, rowIndex) => Object.fromEntries(
        columns.map((column, columnIndex) => [column, `R${rowIndex + 1}-C${columnIndex + 1}`]),
      )),
    })
    if (
      url.pathname === '/api/v2/ontologies/ontology-preview/mappings/mapping-1/apply-from-dataset'
      && route.request().method() === 'POST'
    ) {
      applyAttempts += 1
      await new Promise(resolve => setTimeout(resolve, 180))
      if (options.failFirstApply && applyAttempts === 1) {
        return route.fulfill({
          status: 409,
          contentType: 'application/json',
          body: JSON.stringify({
            detail: {
              code: 'dataset_version_not_approved',
              message: '当前成品版本尚未批准，不能进入正式本体',
            },
          }),
        })
      }
      return ok({
        total_entities: 2,
        total_relations: 1,
        sentinel_dispatch: { evaluated: 2, fired: 1 },
      })
    }
    if (url.pathname === '/api/v2/curated/dataset-wide/preview') {
      const offset = Number(url.searchParams.get('offset') || 0)
      const limit = Number(url.searchParams.get('limit') || 20)
      const end = Math.min(offset + limit, 45)
      return ok({
        dataset_id: 'dataset-wide', name: '订单宽表', columns, total_rows: 45,
        offset, limit, has_more: end < 45,
        rows: Array.from({ length: end - offset }, (_, rowIndex) => Object.fromEntries(
          columns.map((column, columnIndex) => [column, `R${offset + rowIndex + 1}-C${columnIndex + 1}`]),
        )),
        count: end - offset,
      })
    }
    if (
      url.pathname === '/api/v2/formal/ontologies/ontology-preview/instances'
      && options.withInstances
    ) return ok([{ id: 'inst-1', objectTypeId: 'object-order' }])
    if (url.pathname === '/api/v2/formal/ontologies/ontology-preview/instance-browser/catalog') {
      return ok({
        release: { id: 'release-1', version: 'v1' },
        objectTypes: [{
          id: 'object-order', name: 'Order', displayName: '订单', primaryKey: 'id',
          properties: [{ id: 'id', name: 'id', displayName: '订单编号', type: 'string', required: true }],
          instanceCount: 1,
          associatedDatasets: [{
            id: 'dataset-wide', name: '订单宽表', kind: 'curated', roles: ['实体数据'], available: true,
          }],
        }],
        linkTypes: [],
        legacyProjection: {
          objectInstances: 0, linkInstances: 0, total: 0,
          canAdopt: false, recommendedAction: 'none', blockingReasons: [],
        },
      })
    }
    if (url.pathname === '/api/v2/formal/ontologies/ontology-preview/instance-browser/objects') {
      requests.objectsTypeId = url.searchParams.get('object_type_id') || ''
      return ok({
        release: { id: 'release-1', version: 'v1' },
        items: [{
          id: 'inst-1', objectTypeId: 'object-order',
          properties: { id: 'ORD-1001' }, computed: {},
          createdAt: '2026-07-26T00:00:00Z', updatedAt: '2026-07-26T00:00:00Z',
        }],
        total: 1, page: 1, pageSize: 20,
      })
    }
    if (url.pathname.startsWith('/api/v2/formal/ontologies/ontology-preview/')) return ok([])
    return ok([])
  })
  return { applyAttempts: () => applyAttempts, requests }
}

test('数据源眼睛按钮打开分页预览，宽表提供横向滚动', async ({ page }) => {
  await mockMappingPreview(page)
  await page.goto('/#/ontologies/ontology-preview?tab=data-mapping', { waitUntil: 'domcontentloaded' })

  await expect(page.getByText('映射结果清单')).toBeVisible()
  // 血缘详情不常驻页面：点击清单行后以弹窗展示
  await expect(page.getByText('数据血缘详情')).toHaveCount(0)
  await page.locator('.dmo-map-row').first().click()
  const lineage = page.getByRole('dialog', { name: '订单', exact: true })
  await expect(lineage).toBeVisible()
  await expect(page.getByText('把本体结构，接到真实数据上')).toHaveCount(0)
  // 画布已随视图互斥移出清单视图；KPI 动画/字体落地仍会轻微移动清单头部，
  // 先等其 y 连续两次采样不变，再在同一个 JS 任务内原子量测两个元素——
  // 分两次 await 读取会在中间插入重排，造成对齐断言偶发失败。
  const registerHead = page.locator('.dmo-register-head')
  let anchor = await registerHead.boundingBox()
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await page.waitForTimeout(120)
    const next = await registerHead.boundingBox()
    const settled = anchor && next && Math.abs(next.y - anchor.y) < 1
    anchor = next
    if (settled) break
  }
  const { filtersBox, searchBox } = await page.evaluate(() => {
    const read = (selector: string) => {
      const element = document.querySelector(selector)
      if (!element) return null
      const { x, y, width, height } = element.getBoundingClientRect()
      return { x, y, width, height }
    }
    return { filtersBox: read('.dmo-filters'), searchBox: read('.dmo-search') }
  })
  expect(filtersBox).not.toBeNull()
  expect(searchBox).not.toBeNull()
  expect(filtersBox!.x + filtersBox!.width).toBeLessThanOrEqual(searchBox!.x)
  expect(Math.abs(filtersBox!.y + filtersBox!.height / 2 - (searchBox!.y + searchBox!.height / 2))).toBeLessThan(2)
  await expect(page.locator('.dmo-target-cell b').first()).toHaveCSS('font-size', '13px')
  await expect(page.locator('.dmo-target-cell small').first()).toHaveCSS('font-size', '11px')
  // 自然文档流：卡片/清单不做内嵌滚动，内容多高页面就多高；页面级滚动只发生在根容器
  await expect(page.locator('.dmo-card')).not.toHaveCSS('overflow-y', 'auto')
  expect(await page.locator('.dmo-row-list').evaluate(element => element.scrollHeight <= element.clientHeight + 1)).toBeTruthy()
  expect(await page.evaluate(() => document.documentElement.scrollHeight <= window.innerHeight + 1)).toBeTruthy()
  // MYW-77：汇总带头部改 flex 换行、卡片撑满一屏，任何宽度都不出现左右滚动条
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBeTruthy()
  expect(await page.evaluate(() => document.body.scrollWidth <= window.innerWidth + 1)).toBeTruthy()
  // 清单视图独占工作区（画布已移出）：内容少时卡片仍不低于 min-height 下限
  const cardBox = await page.locator('.dmo-card').boundingBox()
  expect(cardBox).not.toBeNull()
  expect(cardBox!.height).toBeGreaterThanOrEqual(420)

  await lineage.getByRole('button', { name: '预览数据源 订单宽表' }).click()
  const dialog = page.getByRole('dialog', { name: '订单宽表' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText('1–20 / 45 行')).toBeVisible()
  await expect(dialog.getByText('R1-C1')).toBeVisible()

  const scrollRegion = dialog.locator('.dmo-preview-table-scroll')
  await expect(dialog.locator('.dmo-preview-table')).toHaveClass(/is-scrollable/)
  expect(await scrollRegion.evaluate(element => element.scrollWidth > element.clientWidth)).toBeTruthy()
  expect((await dialog.boundingBox())!.height).toBeLessThanOrEqual(680)

  await dialog.getByRole('button', { name: '下一页' }).click()
  await expect(dialog.getByText('21–40 / 45 行')).toBeVisible()
  await expect(dialog.getByText('R21-C1')).toBeVisible()
  await dialog.getByRole('button', { name: '关闭数据预览' }).click()
  await expect(dialog).toHaveCount(0)
})

test('查看字段级映射按钮进入映射工作台，左上角按钮返回上一页', async ({ page }) => {
  await mockMappingPreview(page)
  await page.goto('/#/ontologies/ontology-preview?tab=data-mapping', { waitUntil: 'domcontentloaded' })

  const mappingButton = page.locator('.dmo-primary-button')
  await expect(mappingButton).toHaveText('查看字段级映射')
  await mappingButton.click()

  await expect(page).toHaveURL(/\/ontologies\/ontology-preview\/graph\?view=mapping$/)
  await expect(page.getByTestId('mapping-workspace')).toBeVisible()
  const tutorialNextButton = page.getByRole('button', { name: '下一步' })
  await expect(tutorialNextButton).toHaveCSS('background-image', /linear-gradient/)
  await tutorialNextButton.hover()
  await expect(tutorialNextButton).toHaveCSS('background-image', /linear-gradient/)
  await expect(tutorialNextButton).toHaveCSS('color', 'rgb(255, 255, 255)')
  await page.locator('.dmc-tutorial header button').click()

  await page.locator('.dmc-eye').first().click()
  const previewPanel = page.locator('.dmc-preview-panel')
  await expect(previewPanel).toBeVisible()
  await expect(previewPanel).toHaveCSS('bottom', '12px')
  await expect(previewPanel).toHaveCSS('border-radius', '10px')
  await expect.poll(async () => {
    const canvasBox = await page.locator('.dmc-canvas-wrap').boundingBox()
    const previewBox = await previewPanel.boundingBox()
    if (!canvasBox || !previewBox) return null
    return Math.round(canvasBox.y + canvasBox.height - previewBox.y - previewBox.height)
  }).toBe(12)

  await page.getByRole('button', { name: '返回上一页' }).click()
  await expect(page).toHaveURL(/\/ontologies\/ontology-preview\?tab=data-mapping$/)
  await expect(
    page.getByTestId('ontology-detail-header').getByRole('button', { name: '数据映射', exact: true }),
  ).toHaveAttribute('aria-pressed', 'true')

  await page.locator('.dmo-primary-button').click()
  await expect(page.getByTestId('mapping-workspace')).toBeVisible()
  await page.getByRole('button', { name: '模型结构', exact: true }).click()
  await expect(page).toHaveURL(/\/ontologies\/ontology-preview\/graph$/)
})

test('当前发布态可安全重放已批准数据：先确认，再明确反馈加载、失败与完整灌入结果', async ({ page }) => {
  const calls = await mockMappingPreview(page, { failFirstApply: true })
  await page.goto('/#/ontologies/ontology-preview?tab=data-mapping', { waitUntil: 'domcontentloaded' })

  // 立即灌入入口在血缘详情弹窗内：先点击清单行打开弹窗
  await page.locator('.dmo-map-row').first().click()
  const lineage = page.getByRole('dialog', { name: '订单', exact: true })
  await expect(lineage).toBeVisible()
  const reconcile = lineage.getByRole('button', { name: '立即灌入' })
  await expect(reconcile).toBeVisible()
  await expect(page.getByText('只读取当前映射绑定的最新已批准版本')).toBeVisible()

  // 取消支线：弹窗出现但不发请求
  await reconcile.click()
  const confirmDialog = page.getByRole('dialog', { name: '确认重新灌入数据' })
  await expect(confirmDialog).toBeVisible()
  await expect(confirmDialog.getByText('重写「订单」的对象与关系实例')).toBeVisible()
  await confirmDialog.getByRole('button', { name: '取消' }).click()
  await expect(confirmDialog).toHaveCount(0)
  expect(calls.applyAttempts()).toBe(0)

  // 确认支线：第一次 409 失败反馈
  await reconcile.click()
  await confirmDialog.getByRole('button', { name: '确认灌入' }).click()
  await expect(page.getByRole('button', { name: '正在灌入…' })).toBeDisabled()
  await expect(page.getByRole('alert')).toContainText('当前成品版本尚未批准，不能进入正式本体')

  // 重试成功反馈
  await page.getByRole('button', { name: '立即灌入' }).click()
  await confirmDialog.getByRole('button', { name: '确认灌入' }).click()
  await expect(page.getByRole('status')).toContainText(
    '灌入完成：对象实例 2 条、关系实例 1 条已更新；哨兵评估 2 次、触发 1 次。',
  )
  expect(calls.applyAttempts()).toBe(2)
})

test('审核异常数据集前置可见：徽标、禁用预览与灌入、横幅提示、已灌入版本', async ({ page }) => {
  await mockMappingPreview(page, { datasetStatus: 'rejected', withInstances: true })
  await page.goto('/#/ontologies/ontology-preview?tab=data-mapping', { waitUntil: 'domcontentloaded' })

  // 行全通但来源数据集被拒绝 → 横幅审核异常态
  await expect(page.getByText('数据链路已连通，但 1 个来源数据集审核状态异常')).toBeVisible()
  // 行内带「已拒绝」徽标；血缘详情弹窗内资产卡同样展示
  await expect(page.locator('.dmo-dataset-cell .dmo-review-badge')).toHaveText('已拒绝')
  await page.locator('.dmo-map-row').first().click()
  const lineage = page.getByRole('dialog', { name: '订单', exact: true })
  await expect(lineage).toBeVisible()
  await expect(lineage.locator('.dmo-source-item .dmo-review-badge')).toHaveText('已拒绝')
  // 已灌入版本新鲜度
  await expect(lineage.locator('.dmo-source-item small')).toContainText('已灌入 v23')
  // 预览前置禁用并给出原因
  const preview = lineage.getByRole('button', { name: '预览数据源 订单宽表' })
  await expect(preview).toBeDisabled()
  await expect(preview).toHaveAttribute('title', '数据集已拒绝，仅保留审计，不可预览')
  // 灌入前置禁用并给出原因，且不出现确认弹窗
  const reconcile = lineage.getByRole('button', { name: '立即灌入' })
  await expect(reconcile).toBeDisabled()
  await expect(page.getByText('来源数据集已拒绝，不能灌入；请先完成数据审核。')).toBeVisible()
})

test('未配置元素：实例列占位、下一步卡指向图谱页创建草稿', async ({ page }) => {
  await mockMappingPreview(page, { withUnmappedObject: true })
  await page.goto('/#/ontologies/ontology-preview?tab=data-mapping', { waitUntil: 'domcontentloaded' })

  // 未配置行实例列显示占位符而非误导性的 0 条
  await expect(page.locator('.dmo-instance-cell b.is-empty')).toHaveText('—')
  // KPI 口径：只统计本本体的来源数据资产
  await expect(page.locator('.dmo-kpis > div').nth(3)).toContainText('来源数据资产')
  await expect(page.locator('.dmo-kpis > div').nth(3)).toContainText('成品 1 · 人工 0')

  // 选中未配置行 → 下一步卡仅给草稿路径
  await page.locator('.dmo-map-row', { hasText: '日志' }).click()
  await expect(page.getByText('建立映射需在草稿版本中进行')).toBeVisible()
  await page.getByRole('button', { name: '前往图谱页创建草稿' }).click()
  await expect(page).toHaveURL(/\/ontologies\/ontology-preview\/graph$/)
})

test('侧栏齿轮携带元素上下文跳转字段级映射视图', async ({ page }) => {
  await mockMappingPreview(page)
  await page.goto('/#/ontologies/ontology-preview?tab=data-mapping', { waitUntil: 'domcontentloaded' })

  await page.locator('.dmo-map-row').first().click()
  const lineage = page.getByRole('dialog', { name: '订单', exact: true })
  await expect(lineage).toBeVisible()
  await lineage.getByRole('button', { name: '查看该元素字段映射' }).click()
  await expect(page).toHaveURL(/\/ontologies\/ontology-preview\/graph\?view=mapping&focus=object(%3A|:)object-order$/)
  await expect(page.getByTestId('mapping-workspace')).toBeVisible()
})

test('各视口宽度下数据映射页均不出现水平滚动条', async ({ page }) => {
  await mockMappingPreview(page)
  // 复现真实使用场景：顶栏多标签 + 常见窗口宽度扫描（MYW-77 三轮）
  await page.addInitScript(() => {
    const tabs = Array.from({ length: 8 }, (_, index) => ({
      key: `tab-${index}`, title: `标签页标题较长的情况${index}`, path: '/agent', lastUsedAt: index,
    }))
    localStorage.setItem('nav-tabs', JSON.stringify({
      state: { tabs, activeKey: 'tab-0', owner: 'tester' },
      version: 0,
    }))
  })
  for (const width of [960, 1024, 1100, 1180, 1240, 1280, 1366, 1440, 1536, 1600, 1920]) {
    await page.setViewportSize({ width, height: 800 })
    await page.goto('/#/ontologies/ontology-preview?tab=data-mapping', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(500)
    const overflow = await page.evaluate(() => ({
      doc: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      body: document.body.scrollWidth - document.body.clientWidth,
    }))
    // 注：详情页根容器带 overflow-x-hidden 兜底，其内部偶发的隐藏元素溢出
    // （如 opacity-0 的悬停提示）不会变成页面滚动条，故只断言页面级为零。
    expect(overflow, `width=${width}`).toEqual({ doc: 0, body: 0 })
  }

  // 全景画布视图同样严格单页：切到画布后按宽度扫描（sessionStorage 保持视图）
  await page.getByRole('group', { name: '切换视图' }).getByRole('button', { name: '全景画布' }).click()
  await expect(page.getByTestId('mapping-chain-panorama')).toBeVisible()
  for (const width of [960, 1180, 1366, 1600, 1920]) {
    await page.setViewportSize({ width, height: 800 })
    await page.goto('/#/ontologies/ontology-preview?tab=data-mapping', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(500)
    const overflow = await page.evaluate(() => ({
      doc: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      body: document.body.scrollWidth - document.body.clientWidth,
    }))
    expect(overflow, `panorama width=${width}`).toEqual({ doc: 0, body: 0 })
  }
})

test('对象与关系很多时两视图各自完整呈现：清单随内容撑高、画布独占视口高度', async ({ page }) => {
  await mockMappingPreview(page, { manyRows: true })
  await page.setViewportSize({ width: 1440, height: 800 })
  await page.goto('/#/ontologies/ontology-preview?tab=data-mapping', { waitUntil: 'domcontentloaded' })
  await expect(page.getByText('映射结果清单')).toBeVisible()
  await expect(page.getByText('批量对象01')).toBeVisible()

  // 清单视图（默认）：卡片随内容自然撑高，行多时页面级滚动可达末行
  const viewportHeight = page.viewportSize()?.height ?? 800
  const card = page.locator('.dmo-card')
  const cardBox = await card.boundingBox()
  expect(cardBox!.height).toBeGreaterThan(viewportHeight - 200)

  // 清单不承担内部滚动（自然高度，仅容忍亚像素级伪差）：末行经页面滚动可达，
  // 表头/筛选行仍随清单呈现
  const rowList = page.locator('.dmo-row-list')
  expect(await rowList.evaluate(element => element.scrollHeight - element.clientHeight < 24)).toBeTruthy()
  await rowList.getByText('批量对象24').scrollIntoViewIfNeeded()
  await expect(rowList.getByText('批量对象24')).toBeInViewport()
  await expect(page.locator('.dmo-table-head')).toBeVisible()

  // 切到全景画布：清单不在场，画布独占且高度给足（旧纵向三段仅 300-430px），
  // 25 个本体元素卡 + 1 个数据资产卡全部渲染
  await page.getByRole('group', { name: '切换视图' }).getByRole('button', { name: '全景画布' }).click()
  const chart = page.getByTestId('mapping-chain-panorama')
  await expect(chart).toBeVisible()
  await expect(page.getByText('映射结果清单')).toHaveCount(0)
  const flowBox = await page.locator('.dmo-flow').boundingBox()
  expect(flowBox).not.toBeNull()
  expect(flowBox!.height).toBeGreaterThanOrEqual(460)
  await expect(chart.locator('.dmo-chain-node')).toHaveCount(26)
})

test('视图互斥切换：画布与清单二选一，按本体记住上次选择', async ({ page }) => {
  await mockMappingPreview(page)
  await page.goto('/#/ontologies/ontology-preview?tab=data-mapping', { waitUntil: 'domcontentloaded' })

  // 默认清单视图：画布不渲染
  const toggle = page.getByRole('group', { name: '切换视图' })
  await expect(toggle.getByRole('button', { name: '映射清单' })).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByText('映射结果清单')).toBeVisible()
  await expect(page.getByTestId('mapping-chain-panorama')).toHaveCount(0)

  // 切到全景画布：清单不在场，选择按本体写回 sessionStorage
  await toggle.getByRole('button', { name: '全景画布' }).click()
  await expect(page.getByTestId('mapping-chain-panorama')).toBeVisible()
  await expect(page.getByText('映射结果清单')).toHaveCount(0)
  expect(await page.evaluate(() => window.sessionStorage.getItem('dmo:mapping-view:ontology-preview'))).toBe('panorama')

  // 同会话重新进入记住画布；切回清单后画布卸载
  await page.goto('/#/ontologies/ontology-preview?tab=data-mapping', { waitUntil: 'domcontentloaded' })
  await expect(page.getByTestId('mapping-chain-panorama')).toBeVisible()
  await toggle.getByRole('button', { name: '映射清单' }).click()
  await expect(page.getByText('映射结果清单')).toBeVisible()
  await expect(page.getByTestId('mapping-chain-panorama')).toHaveCount(0)
})

test('全景画布视图：节点卡链路渲染，点击节点打开血缘弹窗', async ({ page }) => {
  await mockMappingPreview(page)
  await page.goto('/#/ontologies/ontology-preview?tab=data-mapping', { waitUntil: 'domcontentloaded' })

  // 视图互斥后画布在全景视图下渲染：先切换再断言；画布直接铺满面板
  await page.getByRole('group', { name: '切换视图' }).getByRole('button', { name: '全景画布' }).click()
  await expect(page.getByRole('region', { name: '数据供给全景' })).toBeVisible()
  await expect(page.locator('.dmo-flow-title')).toHaveCount(0)
  const chart = page.getByTestId('mapping-chain-panorama')
  await expect(chart).toBeVisible()
  // 1 个数据集 + 1 个对象 → 两张节点卡 + 一条连线；节点可拖拽、连线为流动粒子样式
  await expect(chart.getByText('订单宽表')).toBeVisible()
  await expect(chart.getByText('订单', { exact: true })).toBeVisible()
  await expect.poll(async () => chart.locator('svg path').count()).toBeGreaterThan(0)
  await expect(chart.locator('.react-flow__node.draggable')).toHaveCount(2)
  await expect.poll(async () => chart.locator('.dmo-chain-particle').count()).toBeGreaterThan(0)
  // 列抬头是面板标题（字号已上调），两列计数如实呈现
  await expect(chart.getByText('来源数据资产')).toBeVisible()
  await expect(chart.getByText('本体元素')).toBeVisible()
  // 点击元素节点卡 → 连线进入聚焦态（加粗 2.2 + 粒子放大），并打开血缘详情弹窗
  await chart.getByText('订单', { exact: true }).click()
  await expect.poll(async () => chart.locator('path.react-flow__edge-path').first()
    .evaluate(element => (element.getAttribute('stroke-width') || getComputedStyle(element).strokeWidth).replace('px', ''))).toBe('2.2')
  const lineage = page.getByRole('dialog', { name: '订单', exact: true })
  await expect(lineage).toBeVisible()
  await lineage.getByRole('button', { name: '关闭血缘详情' }).click()
  await expect(lineage).toHaveCount(0)
  await expect(chart).toBeVisible()
})

test('无映射本体：清单如实呈现，画布视图显示诚实空态而非空画布', async ({ page }) => {
  await mockMappingPreview(page, { noMappings: true })
  await page.goto('/#/ontologies/ontology-preview?tab=data-mapping', { waitUntil: 'domcontentloaded' })

  // 默认清单视图：未配置元素的去向在清单中如实呈现，画布不渲染
  await expect(page.getByText('映射结果清单')).toBeVisible()
  await expect(page.getByTestId('mapping-chain-panorama')).toHaveCount(0)
  // 切到全景画布：无可见链路时显示空态而非空画布
  await page.getByRole('group', { name: '切换视图' }).getByRole('button', { name: '全景画布' }).click()
  await expect(page.getByText('暂无数据流')).toBeVisible()
  await expect(page.getByTestId('mapping-chain-panorama')).toHaveCount(0)
})

test('实例数链接跳转实例数据 Tab 并选中对应类型', async ({ page }) => {
  const { requests } = await mockMappingPreview(page, { withInstances: true })
  await page.goto('/#/ontologies/ontology-preview?tab=data-mapping', { waitUntil: 'domcontentloaded' })

  const link = page.locator('.dmo-instance-link').first()
  await expect(link).toBeVisible()
  await link.click()
  await expect(page).toHaveURL(/tab=data&type=object(%3A|:)object-order/)
  // 实例数据页消费 type 参数：按选中的对象类型请求实例列表
  await expect.poll(() => requests.objectsTypeId).toBe('object-order')
})

test('深色模式下数据映射页保持固定浅色作用域（DESIGN.md §5.4）', async ({ page }) => {
  await mockMappingPreview(page, { darkTheme: true })
  await page.goto('/#/ontologies/ontology-preview?tab=data-mapping', { waitUntil: 'domcontentloaded' })

  await expect(page.locator('html')).toHaveClass(/dark/)
  await page.getByRole('group', { name: '切换视图' }).getByRole('button', { name: '全景画布' }).click()
  await expect(page.getByTestId('mapping-chain-panorama')).toBeVisible()
  await expect.poll(async () => page.locator('.dmo-flow-canvas svg path').count()).toBeGreaterThan(0)
  // 本体详情域为固定浅色：html.dark 下映射卡片不翻深（--dmo-surface 浅色钉死）
  await expect(page.locator('.dmo-card')).toHaveCSS('background-color', 'rgba(255, 255, 255, 0.9)')
})

test('详情页头部操作区：映射入口跳转、悬停即时提示、导出反馈不再推挤页面', async ({ page }) => {
  await mockMappingPreview(page)
  await page.goto('/#/ontologies/ontology-preview?tab=data-mapping', { waitUntil: 'domcontentloaded' })

  // 版本徽章与三个图标按钮等高（h-10 = 40px），不再是偏小的 h-9
  const versionBadge = page.getByTestId('current-release-version')
  const graphButton = page.getByRole('button', { name: '查看当前发布图谱', exact: true })
  const exportButton = page.getByRole('button', { name: '导出本体结构', exact: true })
  await expect(versionBadge).toHaveCSS('height', '40px')
  await expect(graphButton).toHaveCSS('height', '40px')
  await expect(exportButton).toHaveCSS('height', '40px')

  // 新增的数据映射工作台入口位于图谱编辑器右侧：点击后带 view=mapping 跳转
  const mappingEntry = page.getByTestId('open-mapping-workspace')
  await expect(mappingEntry).toHaveCSS('height', '40px')

  // 下载按钮与其他图标按钮共用样式：悬停不再上下位移
  await expect(exportButton).toHaveCSS('transform', 'none')
  await exportButton.hover()
  await expect(exportButton).toHaveCSS('transform', 'none')

  // 悬停即时提示浮层（替代原生 title）：hover 后 opacity 从 0 → 1
  const exportTip = exportButton.locator('..').getByRole('tooltip')
  await expect(exportTip).toHaveText('导出本体结构')
  await expect(exportTip).toHaveCSS('opacity', '1')
  // 浮层出现在按钮正下方且不改变任何布局（不把内容区往下顶）
  const buttonBox = await exportButton.boundingBox()
  const tipBox = await exportTip.boundingBox()
  expect(buttonBox).toBeTruthy()
  expect(tipBox).toBeTruthy()
  expect(tipBox!.y).toBeGreaterThanOrEqual(buttonBox!.y + buttonBox!.height)

  // 内容区基准要在懒加载链路全景图落地、布局完全沉降后采样（与本文件其他用例同一手法），
  // 否则会把图表自身的落位误判成“导出反馈推挤页面”。
  const contentLocator = page.getByTestId('ontology-detail-content')
  let contentBefore = await contentLocator.boundingBox()
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await page.waitForTimeout(120)
    const next = await contentLocator.boundingBox()
    const settled = contentBefore && next
      && Math.abs(next.y - contentBefore.y) < 1
      && Math.abs((next.height ?? 0) - (contentBefore.height ?? 0)) < 1
    contentBefore = next
    if (settled) break
  }

  // 导出成功：真实触发下载 + 顶部居中 toast 提示；页面文档流保持原位
  const downloadPromise = page.waitForEvent('download')
  await exportButton.click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe('预览测试本体_v1.json')
  const successToast = page.getByText('本体结构 JSON 已下载')
  await expect(successToast).toBeVisible()
  expect(await successToast.count()).toBe(1)
  expect(await contentLocator.boundingBox()).toEqual(contentBefore)

  // 最后验证数据映射入口的跳转目标（离开详情页的断言放最后）
  await mappingEntry.click()
  await expect(page).toHaveURL(/\/ontologies\/ontology-preview\/graph\?view=mapping$/)
  await expect(page.getByTestId('mapping-workspace')).toBeVisible()
});

test('element 深链在页面已打开时同样生效（同文档路由更新直接打开对应弹窗）', async ({ page }) => {
  await mockMappingPreview(page, { withUnmappedObject: true })
  await page.goto('/#/ontologies/ontology-preview?tab=data-mapping', { waitUntil: 'domcontentloaded' })

  await expect(page.getByText('映射结果清单')).toBeVisible()
  await expect(page.getByRole('dialog', { name: '订单', exact: true })).toHaveCount(0)
  // 模拟「已停留在本页时收到他人分享的深链」：仅 hash 变化的同文档导航，
  // 选中状态必须跟随 URL 参数打开对应元素的血缘详情，而不是被组件内旧状态覆盖回删。
  await page.evaluate(() => {
    window.location.hash = '#/ontologies/ontology-preview?tab=data-mapping&element=object:object-log'
  })
  const lineage = page.getByRole('dialog', { name: '日志', exact: true })
  await expect(lineage).toBeVisible()
  // 打开后参数保持，不因同步逻辑被误删
  expect(decodeURIComponent(page.url())).toContain('element=object:object-log')
})

test('KPI 数字 beUI 滚动入场，数据就绪后短时内落到终值', async ({ page }) => {
  await mockMappingPreview(page, { withInstances: true })
  await page.goto('/#/ontologies/ontology-preview?tab=data-mapping', { waitUntil: 'domcontentloaded' })

  // 清单可见即代表数据已就绪；KPI 经 AnimatedNumber 滚动（约 0.9s）后落在
  // 终值（1/3 可用、100% 字段连接），不允许长时间停留在 0 造成链路全断误读。
  await expect(page.getByText('映射结果清单')).toBeVisible()
  await expect.poll(async () => page.locator('.dmo-kpis').innerText(), { timeout: 3000 }).toContain('1 /')
  await expect.poll(async () => page.locator('.dmo-kpis').innerText(), { timeout: 3000 }).toContain('100%')
})

test('悬停全景节点卡时静止非聚焦连线粒子，移开后恢复流动', async ({ page }) => {
  // MYW-51：流动粒子逐帧重绘，缩放悬停检视时卡片边缘闪烁；悬停（未聚焦）期间
  // 非聚焦连线不得渲染粒子。MYW-77 二轮起连线改为基线 + SMIL 流动粒子，
  // 静止语义由连线 data.still 承载（原 data-still 属性机制已下线）。
  await mockMappingPreview(page)
  await page.goto('/#/ontologies/ontology-preview?tab=data-mapping', { waitUntil: 'domcontentloaded' })
  await page.getByRole('group', { name: '切换视图' }).getByRole('button', { name: '全景画布' }).click()

  const canvas = page.getByTestId('mapping-chain-panorama')
  await expect(canvas).toBeVisible()
  const nodeCard = canvas.locator('.react-flow__node-mappingChainNode').first()
  await expect(nodeCard).toBeVisible()

  // 悬停前粒子在流动
  await expect.poll(async () => canvas.locator('.dmo-chain-particle').count()).toBeGreaterThan(0)
  await nodeCard.hover()
  // 悬停检视（未聚焦）：非聚焦连线不渲染粒子
  await expect(canvas.locator('.dmo-chain-particle')).toHaveCount(0)

  // 离开节点卡后恢复流动
  const canvasBox = await canvas.boundingBox()
  expect(canvasBox).not.toBeNull()
  await page.mouse.move(canvasBox!.x + 8, canvasBox!.y - 10)
  await expect.poll(async () => canvas.locator('.dmo-chain-particle').count()).toBeGreaterThan(0)
})

test('供给全景连线不可命中，悬停节点卡无 enter/leave 振荡', async ({ page }) => {
  // MYW-60：边线层重挂载/回流会打断节点卡悬停命中测试（enter/leave 高频振荡），
  // 表现为「鼠标悬停画布卡片一直在闪」。连线改为纯视觉元素后必须保持零振荡。
  await mockMappingPreview(page)
  await page.goto('/#/ontologies/ontology-preview?tab=data-mapping', { waitUntil: 'domcontentloaded' })
  await page.getByRole('group', { name: '切换视图' }).getByRole('button', { name: '全景画布' }).click()

  const canvas = page.getByTestId('mapping-chain-panorama')
  await expect(canvas).toBeVisible()
  await expect(canvas.locator('.react-flow__edge')).toHaveCount(1)

  // 连线不再携带 20px 透明命中条，且路径禁用指针事件
  await expect(canvas.locator('.react-flow__edge-interaction')).toHaveCount(0)
  const edgePath = canvas.locator('.react-flow__edge path.react-flow__edge-path').first()
  await expect(edgePath).toHaveCSS('pointer-events', 'none')

  // 指针静止停在节点卡中心：进入/离开事件不得振荡
  await page.evaluate(() => {
    const host = document.querySelector('[data-testid="mapping-chain-panorama"]')
    if (!host) return
    host.setAttribute('data-probe-enter', '0')
    host.setAttribute('data-probe-leave', '0')
    const bump = (attribute: string) => () => {
      host.setAttribute(attribute, String(Number(host.getAttribute(attribute) || 0) + 1))
    }
    document.querySelectorAll('.dmo-chain-node').forEach(node => {
      node.addEventListener('mouseenter', bump('data-probe-enter'))
      node.addEventListener('mouseleave', bump('data-probe-leave'))
    })
  })
  const nodeCard = canvas.locator('.react-flow__node-mappingChainNode').first()
  const cardBox = await nodeCard.boundingBox()
  expect(cardBox).not.toBeNull()
  await page.mouse.move(cardBox!.x - 40, cardBox!.y - 40)
  await page.waitForTimeout(200)
  await page.mouse.move(cardBox!.x + cardBox!.width / 2, cardBox!.y + cardBox!.height / 2)
  await page.waitForTimeout(1500)

  const enter = Number(await canvas.getAttribute('data-probe-enter'))
  const leave = Number(await canvas.getAttribute('data-probe-leave'))
  expect(enter).toBeLessThanOrEqual(2)
  expect(Math.abs(enter - leave)).toBeLessThanOrEqual(1)

  // 离开后仍恢复流动粒子（不破坏 MYW-51 的检视静止语义）
  const canvasBox = await canvas.boundingBox()
  expect(canvasBox).not.toBeNull()
  await page.mouse.move(canvasBox!.x + 8, canvasBox!.y - 10)
  await expect.poll(async () => canvas.locator('.dmo-chain-particle').count()).toBeGreaterThan(0)
})

test('供给全景列抬头居中于卡片列，且整块内容水平居中于画布', async ({ page }) => {
  // MYW-60：fitView 仅在初始化执行一次，节点测量未完成时常得到靠左的陈旧视口；
  // 节点测量完成后必须重新 fitView 居中，抬头也要水平居中于自己的卡片列。
  await mockMappingPreview(page)
  await page.goto('/#/ontologies/ontology-preview?tab=data-mapping', { waitUntil: 'domcontentloaded' })
  await page.getByRole('group', { name: '切换视图' }).getByRole('button', { name: '全景画布' }).click()

  const canvas = page.getByTestId('mapping-chain-panorama')
  await expect(canvas).toBeVisible()
  await expect(canvas.locator('.react-flow__node')).not.toHaveCount(0)
  // 等测量后的 refit 生效：布局稳定两个采样点一致
  let anchor = await canvas.boundingBox()
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await page.waitForTimeout(150)
    const next = await canvas.boundingBox()
    if (anchor && next && Math.abs(next.y - anchor.y) < 1) break
    anchor = next
  }

  const layout = await page.evaluate(() => {
    const canvasEl = document.querySelector('[data-testid="mapping-chain-panorama"]') as HTMLElement
    const a = canvasEl.getBoundingClientRect()
    const rel = (el: Element) => {
      const b = el.getBoundingClientRect()
      return { left: b.left - a.left, right: b.right - a.left, cx: b.left - a.left + b.width / 2 }
    }
    const cards = Array.from(canvasEl.querySelectorAll('.dmo-chain-node')).map(rel)
    const heads = Array.from(canvasEl.querySelectorAll('.dmo-chain-colhead')).map(rel)
    return { canvasW: a.width, cards, heads }
  })
  expect(layout.cards.length).toBeGreaterThan(0)
  expect(layout.heads.length).toBe(2)

  // 整块内容水平居中：左右留白差 ≤ 24px
  const minX = Math.min(...layout.cards.map(c => c.left))
  const maxX = Math.max(...layout.cards.map(c => c.right))
  expect(Math.abs(minX - (layout.canvasW - maxX))).toBeLessThanOrEqual(24)

  // 每列抬头中心与该列卡片中心一致（≤ 8px）：左列 / 右列按画布中线分簇
  for (const head of layout.heads) {
    const inCol = layout.cards.filter(c => (c.cx < layout.canvasW / 2) === (head.cx < layout.canvasW / 2))
    expect(inCol.length).toBeGreaterThan(0)
    const colCenter = inCol.reduce((sum, c) => sum + (c.left + c.right) / 2, 0) / inCol.length
    expect(Math.abs(head.cx - colCenter)).toBeLessThanOrEqual(8)
  }
})

test('供给全景两列卡片按列垂直居中，滚轮可缩放画布', async ({ page }) => {
  // 1 数据资产卡 + 2 本体元素卡：矮列不再从顶部堆起，卡片堆垂直中心与最高列对齐；
  // 画布滚轮从"只透传页面滚动"改为缩放（preventScrolling 捕获滚轮）。
  await mockMappingPreview(page, { withSecondMappedObject: true })
  await page.goto('/#/ontologies/ontology-preview?tab=data-mapping', { waitUntil: 'domcontentloaded' })
  await page.getByRole('group', { name: '切换视图' }).getByRole('button', { name: '全景画布' }).click()

  const canvas = page.getByTestId('mapping-chain-panorama')
  await expect(canvas).toBeVisible()
  await expect(canvas.locator('.dmo-chain-node')).toHaveCount(3)
  // 等测量后的 refit 生效：布局稳定两个采样点一致
  let anchor = await canvas.boundingBox()
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await page.waitForTimeout(150)
    const next = await canvas.boundingBox()
    if (anchor && next && Math.abs(next.y - anchor.y) < 1) break
    anchor = next
  }

  // ① 按列垂直居中：每列卡片堆的垂直中心与另一列一致（≤ 8px）
  const layout = await page.evaluate(() => {
    const canvasEl = document.querySelector('[data-testid="mapping-chain-panorama"]') as HTMLElement
    const hostBox = canvasEl.getBoundingClientRect()
    const cards = Array.from(canvasEl.querySelectorAll('.dmo-chain-node')).map(el => {
      const box = el.getBoundingClientRect()
      return { cx: box.left - hostBox.left + box.width / 2, cy: box.top - hostBox.top + box.height / 2 }
    })
    return { canvasW: hostBox.width, cards }
  })
  const leftCol = layout.cards.filter(card => card.cx < layout.canvasW / 2)
  const rightCol = layout.cards.filter(card => card.cx >= layout.canvasW / 2)
  expect(leftCol.length).toBe(1)
  expect(rightCol.length).toBe(2)
  const stackCenter = (column: typeof leftCol) => column.reduce((sum, card) => sum + card.cy, 0) / column.length
  expect(Math.abs(stackCenter(leftCol) - stackCenter(rightCol))).toBeLessThanOrEqual(8)

  // ② 滚轮缩放：视口 transform 的缩放分量必须变化（此前 zoomOnScroll=false 滚轮无效果）
  const readScale = () => page.evaluate(() => {
    const viewport = document.querySelector(
      '[data-testid="mapping-chain-panorama"] .react-flow__viewport',
    ) as HTMLElement | null
    const transform = viewport ? getComputedStyle(viewport).transform : 'none'
    return transform && transform !== 'none' ? new DOMMatrix(transform).a : 1
  })
  const scaleBefore = await readScale()
  await canvas.hover()
  await page.mouse.wheel(0, -480)
  await page.waitForTimeout(450)
  const scaleAfter = await readScale()
  expect(scaleAfter).toBeGreaterThan(scaleBefore + 0.05)
})
