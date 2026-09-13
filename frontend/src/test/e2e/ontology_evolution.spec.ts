import { expect, test, type APIRequestContext, type Locator, type Page } from '@playwright/test'

import {
  STACK_ADMIN_PASSWORD,
  STACK_ADMIN_USERNAME,
} from './support/stack-credentials'

const API = process.env.PLAYWRIGHT_API_URL || 'http://localhost:8000'

async function login(page: Page): Promise<string> {
  await page.goto('/#/login')
  await page.getByLabel('用户名', { exact: true }).fill(STACK_ADMIN_USERNAME)
  await page.getByLabel('密码', { exact: true }).fill(STACK_ADMIN_PASSWORD)
  await page.locator('button[type="submit"]').click()
  await page.waitForURL('**/#/super-assistant')
  const token = await page.evaluate(() => localStorage.getItem('token'))
  expect(token).toBeTruthy()
  return token!
}

async function api<T>(request: APIRequestContext, token: string, method: 'get' | 'post' | 'put', path: string, data?: unknown): Promise<T> {
  const response = await request[method](`${API}${path}`, {
    headers: { Authorization: `Bearer ${token}` }, data,
  })
  expect(response.ok(), `${method.toUpperCase()} ${path}: ${await response.text()}`).toBeTruthy()
  const body = await response.json()
  return body.data ?? body
}

async function waitForPublishedDatasetProjection(
  request: APIRequestContext,
  token: string,
  datasetId: string,
  versionNo: number,
  ontologyId: string,
  mappingId: string,
) {
  let automation: any
  await expect.poll(async () => {
    const versions = await api<any[]>(request, token, 'get', `/api/v2/datasets/${datasetId}/versions`)
    automation = versions.find(version => version.version_no === versionNo)?.automation
    return automation?.status
  }, {
    message: `dataset version ${versionNo} automation must finish before runtime actions`,
    timeout: 60_000,
    intervals: [250, 500, 1_000, 2_000],
  }).toBe('completed')
  expect(automation?.last_error).toBeNull()
  expect(automation?.result?.manual_mapping?.status).toBe('applied')
  expect(automation?.result?.manual_mapping?.ontologies?.some(
    (item: any) => item.ontology_id === ontologyId,
  )).toBe(true)

  await waitForMappingApplied(request, token, ontologyId, mappingId)
}

async function waitForMappingApplied(
  request: APIRequestContext,
  token: string,
  ontologyId: string,
  mappingId: string,
) {
  await expect.poll(async () => {
    const mappings = await api<any[]>(request, token, 'get', `/api/v2/ontologies/${ontologyId}/mappings`)
    return mappings.find(mapping => mapping.id === mappingId)?.status
  }, {
    message: `mapping ${mappingId} must remain applied after dataset automation`,
    timeout: 30_000,
    intervals: [250, 500, 1_000],
  }).toBe('applied')
}

async function verifyWorkspaceStagePosition(page: Page) {
  const stage = page.getByTestId('graph-workspace-stage')
  await expect(stage).toBeVisible()
  const box = await stage.boundingBox()
  const viewport = page.viewportSize()
  expect(box).toBeTruthy()
  expect(viewport).toBeTruthy()
  expect(Math.abs(box!.x + box!.width / 2 - viewport!.width / 2)).toBeLessThan(2)
  const bottomGap = viewport!.height - box!.y - box!.height
  expect(bottomGap).toBeGreaterThanOrEqual(20)
  expect(bottomGap).toBeLessThanOrEqual(32)
}

async function verifyVerticalListScroll(list: Locator) {
  await expect(list).toHaveCSS('overflow-y', 'auto')
  const metrics = await list.evaluate(element => {
    const filler = document.createElement('div')
    filler.style.height = '2000px'
    filler.style.flex = 'none'
    filler.setAttribute('aria-hidden', 'true')
    element.appendChild(filler)
    const before = {
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
      scrollTop: element.scrollTop,
    }
    element.scrollTop = element.scrollHeight
    const afterScrollTop = element.scrollTop
    filler.remove()
    element.scrollTop = 0
    return { ...before, afterScrollTop }
  })
  expect(metrics.clientHeight).toBeGreaterThan(0)
  expect(metrics.scrollHeight).toBeGreaterThan(metrics.clientHeight)
  expect(metrics.afterScrollTop).toBeGreaterThan(0)
}

async function verifyReadonlyGraphInspection(page: Page, objectTypeId: string) {
  const objectButton = page.getByRole('button', { name: /查看对象实体，共 1 个/ })
  const linkButton = page.getByRole('button', { name: /查看实体关系，共 1 个/ })
  const actionButton = page.getByRole('button', { name: /查看执行动作，共 1 个/ })
  const functionButton = page.getByRole('button', { name: /查看计算函数，共 1 个/ })
  await expect(objectButton).toBeEnabled()
  await expect(linkButton).toBeEnabled()
  await expect(actionButton).toBeEnabled()
  await expect(functionButton).toBeEnabled()

  await objectButton.click()
  await expect(page.getByRole('heading', { name: '对象实体列表' })).toBeVisible()
  await page.getByTitle('查看对象详情').click()
  await expect(page.getByTestId('readonly-definition-detail')).toContainText('订单号')
  await page.getByRole('button', { name: '关闭详情' }).click()

  await linkButton.click()
  await expect(page.getByRole('heading', { name: '实体关系列表' })).toBeVisible()
  await page.getByTitle('查看关系详情').click()
  await expect(page.getByTestId('readonly-definition-detail')).toContainText('一对一 (1:1)')
  await page.getByRole('button', { name: '关闭详情' }).click()

  await actionButton.click()
  await expect(page.getByRole('heading', { name: '动作列表' })).toBeVisible()
  await page.getByTitle('查看动作详情').click()
  await expect(page.getByTestId('readonly-definition-detail')).toContainText('人工审批')
  await page.getByRole('button', { name: '关闭详情' }).click()

  await functionButton.click()
  await expect(page.getByRole('heading', { name: '函数列表' })).toBeVisible()
  await page.getByTitle('查看函数详情').click()
  await expect(page.getByTestId('readonly-definition-detail')).toContainText('object.name')
  await page.getByRole('button', { name: '关闭详情' }).click()

  const node = page.locator(`.react-flow__node[data-id="${objectTypeId}"]`)
  await expect(node).toBeVisible()
  await expect(node.getByTestId('object-type-icon')).toHaveText('📦')
  await expect(node.getByTestId('object-type-icon')).not.toContainText('真机订单')
  const before = await node.boundingBox()
  expect(before).toBeTruthy()
  const layoutSaved = page.waitForResponse(response =>
    response.request().method() === 'PUT'
      && response.url().includes('/api/v2/ontologies/')
      && response.url().endsWith('/layout'))
  await page.mouse.move(before!.x + before!.width / 2, before!.y + before!.height / 2)
  await page.mouse.down()
  await page.mouse.move(before!.x + before!.width / 2 + 110, before!.y + before!.height / 2 + 70, { steps: 8 })
  await page.mouse.up()
  expect((await layoutSaved).ok()).toBeTruthy()
  const after = await node.boundingBox()
  expect(after).toBeTruthy()
  expect(Math.abs(after!.x - before!.x)).toBeGreaterThan(50)

  await node.dblclick()
  await expect(page.locator('h2').filter({ hasText: '真机订单' })).toBeVisible()
  await expect(page.getByTestId('readonly-definition-detail')).toContainText('订单号')
  await page.getByRole('button', { name: '关闭详情' }).click()
  await expect(page.getByRole('button', { name: /保存全部更改/ })).toHaveCount(0)
}

test('complete branch → real-data trial → reviewed release works in the browser', async ({ page, request }) => {
  test.setTimeout(240_000)
  const token = await login(page)
  const suffix = Date.now().toString(36)
  const objectTypeId = `ot-browser-order-${suffix}`
  const ontology = await api<any>(request, token, 'post', '/api/v1/ontologies', {
    name: `版本演进真机-${suffix}`, domain: '供应链', description: 'Playwright lifecycle check',
  })
  expect(ontology.version).toBe('v0')
  expect(ontology.current_release_version).toBe('v0')

  // The project-level compatibility status is still draft at birth, but its
  // immutable v0 release must already be selectable and queryable by Agent.
  // 登录已落地 /agent：同 URL 的 goto 不会重载页面，必须显式 reload 才能
  // 让本体列表查询拿到刚创建的本体。
  await page.goto('/#/agent')
  await page.reload()
  const ontologySelect = page.getByLabel('选择本体')
  const v0Option = ontologySelect.locator(`option[value="${ontology.id}"]`)
  await expect(v0Option).toContainText(`${ontology.name} · v0`)
  const capabilitiesLoaded = page.waitForResponse(response =>
    response.url().includes(`/api/v2/formal/ontologies/${ontology.id}/agent/capabilities`)
      && response.status() === 200)
  await ontologySelect.selectOption(ontology.id)
  await capabilitiesLoaded
  await expect(ontologySelect).toHaveValue(ontology.id)

  const tree = await api<any>(request, token, 'get', `/api/v2/ontologies/${ontology.id}/version-tree`)
  const root = tree.versions.find((item: any) => item.version_number === 'v0')
  const draft = await api<any>(request, token, 'post', `/api/v2/ontologies/${ontology.id}/versions/${root.id}/drafts`, {
    versionLabel: '浏览器验证分支', description: '真实湖数据隔离试跑',
  })
  const saved = await api<any>(request, token, 'put', `/api/v2/ontologies/${ontology.id}/versions/${draft.id}/workspace`, {
    baseRevision: `${draft.revision}:${draft.snapshot_hash}`,
    version: draft.version_number,
    objectTypes: [{
      id: objectTypeId, name: 'BrowserOrder', displayName: '真机订单', icon: '真机订单',
      primaryKey: 'p-id', positionX: 100, positionY: 100,
      properties: [
        { id: 'p-id', name: 'id', displayName: '订单号', type: 'string', required: true },
        { id: 'p-name', name: 'name', displayName: '名称', type: 'string', required: true },
      ],
    }],
    linkTypes: [{
      id: `lt-browser-order-${suffix}`, name: 'order_lineage', displayName: '订单追踪',
      sourceObjectTypeId: objectTypeId, targetObjectTypeId: objectTypeId,
      cardinality: 'one-to-one', description: '订单自身追踪关系', properties: [],
    }],
    actions: [{
      id: `act-browser-order-${suffix}`, name: 'review_order', displayName: '审核订单',
      description: '核对订单定义', objectTypeId, requiresApproval: true,
      parameters: [{ id: 'ap-note', name: 'note', displayName: '审核备注', type: 'string', required: false }],
      rules: [{
        id: `rule-browser-order-${suffix}`,
        name: '记录审核通知',
        type: 'notification',
        enabled: true,
        order: 0,
        config: {
          channel: 'internal',
          recipientSource: 'constant',
          recipient: 'ontology-reviewers',
          messageTemplate: '订单定义已进入人工审核',
        },
      }],
    }],
    functions: [{
      id: `fn-browser-order-${suffix}`, name: 'order_name', displayName: '订单名称',
      description: '读取订单名称', functionType: 'object', language: 'expression',
      targetObjectTypeId: objectTypeId, parameters: [], returnType: 'string',
      body: 'object.name', cacheStrategy: 'none', enabled: true,
    }],
    instances: [], linkInstances: [],
  })
  const dataset = await api<any>(request, token, 'post', '/api/v2/datasets/create-table', {
    name: `真机订单数据-${suffix}`,
    columns: [
      { name: 'id', display_name: '订单号', type: 'string', nullable: false },
      { name: 'name', display_name: '名称', type: 'string', nullable: false },
    ],
    primary_key: 'id',
  })
  const upload = await request.post(`${API}/api/v2/datasets/${dataset.id}/upload`, {
    headers: { Authorization: `Bearer ${token}` },
    multipart: {
      file: {
        name: 'orders.csv', mimeType: 'text/csv',
        buffer: Buffer.from('id,name\nE2E-1,真机一号\nE2E-2,真机二号\n'),
      },
    },
  })
  const uploadText = await upload.text()
  expect(upload.ok(), uploadText).toBeTruthy()
  const mappingId = `map-browser-order-${suffix}`
  await api<any>(request, token, 'put', `/api/v2/ontologies/${ontology.id}/versions/${draft.id}/workspace/mappings`, {
    baseRevision: saved.revision,
    mappings: [{
      id: mappingId, curatedDatasetId: dataset.id,
      entityClass: 'BrowserOrder', targetObjectTypeId: objectTypeId,
      fieldMapping: {
        id: 'id',
        name: 'name',
        __primary_key__: 'id',
        __auto_apply_on_version__: true,
      },
      status: 'draft', confidence: 1,
    }],
    linkMappings: [{
      id: `link-map-browser-order-${suffix}`,
      linkTypeId: `lt-browser-order-${suffix}`,
      relationType: 'order_lineage',
      srcDatasetId: dataset.id,
      tgtDatasetId: dataset.id,
      edgeDatasetId: null,
      srcKey: 'id',
      tgtKey: 'id',
      fieldMapping: { __auto_apply_on_version__: true },
      status: 'draft',
    }],
    sentinels: [],
  })
  const incompleteDraft = await api<any>(request, token, 'post', `/api/v2/ontologies/${ontology.id}/versions/${root.id}/drafts`, {
    versionLabel: '缺少映射分支', description: '用于验证草稿转试跑硬门禁',
  })
  await api<any>(request, token, 'put', `/api/v2/ontologies/${ontology.id}/versions/${incompleteDraft.id}/workspace`, {
    baseRevision: `${incompleteDraft.revision}:${incompleteDraft.snapshot_hash}`,
    version: incompleteDraft.version_number,
    objectTypes: [{
      id: `ot-unmapped-${suffix}`, name: 'UnmappedOrder', displayName: '未映射订单',
      primaryKey: 'p-unmapped-id', properties: [
        { id: 'p-unmapped-id', name: 'id', displayName: '订单号', type: 'string', required: true },
        { id: 'p-unmapped-note', name: 'note', displayName: '备注', type: 'string', required: false },
      ],
    }],
    linkTypes: [], actions: [], functions: [], instances: [], linkInstances: [],
  })

  // 日常入口不再要求用户选择版本：列表卡片点击本体名称先进入本体总览。
  await page.goto('/#/ontologies')
  const ontologyCard = page.locator('article').filter({ hasText: ontology.name })
  await ontologyCard.getByRole('button', { name: ontology.name, exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/ontologies/${ontology.id}$`))
  await expect(page.getByTestId('current-release-version')).toHaveText('v0')
  await expect(page.getByRole('button', { name: '本体总览' })).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByText('这里始终展示当前发布投影。')).toHaveCount(0)
  await page.getByRole('button', { name: '查看当前发布图谱' }).click()
  await expect(page).toHaveURL(new RegExp(`/ontologies/${ontology.id}/graph$`))

  await page.goto(`/#/ontologies/${ontology.id}`)
  await expect(page.getByTestId('current-release-version')).toHaveText('v0')
  await page.getByRole('button', { name: '查看历史版本' }).click()
  await expect(page.getByText('每次新建版本都会复制一份完整快照')).toBeVisible()
  await expect(page.getByTestId('version-tree')).toBeVisible()
  await expect(page.getByTestId('version-node-v0')).toContainText('当前发布')
  const draftRow = page.getByTestId('version-node-v0.1')
  await expect(draftRow).toContainText('草稿态')
  await expect(draftRow).not.toContainText('真实湖数据隔离试跑')
  await expect(draftRow.getByRole('button', { name: '在线配置' })).toBeVisible()
  await expect(draftRow.getByRole('button', { name: '打开编辑器' })).toHaveCount(0)
  await expect(draftRow.getByRole('button', { name: '数据映射' })).toHaveCount(0)
  await expect(draftRow.getByRole('button', { name: 'v0.1 更多操作' })).toBeVisible()

  // 版本操作菜单互斥展开：切换到另一个版本时，前一个菜单必须立即收起。
  const releaseRow = page.getByTestId('version-node-v0')
  await releaseRow.getByRole('button', { name: 'v0 更多操作' }).click()
  await expect(releaseRow.getByRole('button', { name: '创建新版本' })).toBeVisible()
  await draftRow.getByRole('button', { name: 'v0.1 更多操作' }).click()
  await expect(releaseRow.getByRole('button', { name: '创建新版本' })).toHaveCount(0)
  await expect(draftRow.getByRole('button', { name: '创建新版本' })).toBeVisible()
  await expect(draftRow.getByRole('button', { name: '配置映射' })).toHaveCount(0)
  await draftRow.getByRole('button', { name: 'v0.1 更多操作' }).click()

  // 草稿 → 试跑是后端硬门禁：至少需要一个完成数据映射的对象实体，
  // 并在弹窗内给出可执行的修复入口，不能只靠按钮状态假装受控。
  const incompleteRow = page.getByTestId('version-node-v0.2')
  await incompleteRow.getByRole('button', { name: '转为试跑态' }).click()
  await expect(page.getByRole('alert')).toContainText('草稿尚未满足转为试跑态的硬性条件')
  await expect(page.getByRole('alert')).toContainText('至少需要一个已绑定数据集并完成全部存储属性映射的对象实体')
  await expect(incompleteRow).toContainText('草稿态')

  // 未发布叶子分支可以安全删除；确认弹窗必须说明连同隔离数据删除且编号不复用。
  await incompleteRow.getByRole('button', { name: 'v0.2 更多操作' }).click()
  await incompleteRow.getByRole('button', { name: '删除此分支' }).click()
  const deleteDialog = page.getByRole('dialog', { name: '删除叶子分支 v0.2' })
  await expect(deleteDialog).toContainText('版本编号不会复用')
  await deleteDialog.getByRole('button', { name: '删除此分支' }).click()
  await expect(page.getByTestId('version-node-v0.2')).toHaveCount(0)

  // 草稿的「在线配置」统一进入业务澄清工作台；数据映射在「数据映射」视图中完成。
  await draftRow.getByRole('button', { name: '在线配置' }).click()
  await expect(page).toHaveURL(new RegExp(`/explore\\?ontologyId=${ontology.id}&versionId=${draft.id}`))
  await page.getByTestId('explore-view-mapping').click()
  await expect(page).toHaveURL(new RegExp(`/explore\\?ontologyId=${ontology.id}&versionId=${draft.id}&view=mapping`))
  // 嵌入工作台不自动弹新手教程（保留手动入口），打开后仍居中于工作台
  await expect(page.locator('.dmc-tutorial-card')).toHaveCount(0)
  await page.getByTitle('新手教程').click()
  const mappingWorkspaceBox = await page.getByTestId('mapping-workspace').boundingBox()
  const tutorialCardBox = await page.locator('.dmc-tutorial-card').boundingBox()
  expect(mappingWorkspaceBox).toBeTruthy()
  expect(tutorialCardBox).toBeTruthy()
  expect(Math.abs(
    tutorialCardBox!.x + tutorialCardBox!.width / 2 - (mappingWorkspaceBox!.x + mappingWorkspaceBox!.width / 2),
  )).toBeLessThan(2)
  expect(Math.abs(
    tutorialCardBox!.y + tutorialCardBox!.height / 2 - (mappingWorkspaceBox!.y + mappingWorkspaceBox!.height / 2),
  )).toBeLessThan(2)
  await page.locator('.dmc-tutorial header button').click()
  await expect(page.getByTestId('mapping-workspace')).toHaveAttribute('data-workspace-mode', 'draft')
  await expect(page.locator('.react-flow__node')).toHaveCount(3, { timeout: 20_000 })
  await expect(page.locator('.react-flow__edge')).toHaveCount(4)
  await expect(page.getByTestId('mapping-focus-guide')).toContainText('连线追踪')
  await expect(page.locator('.dmc-node__fields').first()).toHaveCSS('overflow', 'visible')
  await expect(page.locator('.react-flow__edge-path').first()).toHaveAttribute('marker-end', /url\(['"]?#/)
  const mappingDatasetNode = page.locator(`.react-flow__node[data-id="dataset:${dataset.id}"]`)
  const mappingObjectNode = page.locator(`.react-flow__node[data-id="object:${objectTypeId}"]`)
  const mappingRelationNode = page.locator(`.react-flow__node[data-id="relation:lt-browser-order-${suffix}"]`)
  const mappingDatasetNodeBox = await mappingDatasetNode.boundingBox()
  const mappingObjectNodeBox = await mappingObjectNode.boundingBox()
  const mappingRelationNodeBox = await mappingRelationNode.boundingBox()
  expect(mappingDatasetNodeBox).toBeTruthy()
  expect(mappingObjectNodeBox).toBeTruthy()
  expect(mappingRelationNodeBox).toBeTruthy()
  expect(mappingObjectNodeBox!.x).toBeGreaterThan(mappingDatasetNodeBox!.x)
  expect(mappingRelationNodeBox!.x).toBeGreaterThan(mappingObjectNodeBox!.x)
  await mappingObjectNode.click()
  await expect(page.getByTestId('mapping-focus-guide')).toContainText('2 条相关连线已突出显示')
  await expect(page.locator('.react-flow__edge-path[style*="opacity: 0.1"]')).toHaveCount(2)
  await expect(mappingDatasetNode).not.toHaveClass(/dmc-node-shell--dimmed/)
  await expect(mappingRelationNode).toHaveClass(/dmc-node-shell--dimmed/)
  await page.getByRole('button', { name: '清除连线聚焦' }).click()
  await expect(page.getByTestId('mapping-focus-guide')).toContainText('连线追踪')
  await expect(page.locator('.dmc-canvas-stats')).toContainText('字段映射 4')

  // 节点位置是独立展示元数据：拖拽即写入版本布局，不点亮“保存配置”。
  const mappingLayoutSaved = page.waitForResponse(response =>
    response.request().method() === 'PUT'
      && response.url().includes('/api/v2/ontologies/')
      && response.url().endsWith('/layout'))
  const mappingObjectDragBox = await mappingObjectNode.boundingBox()
  expect(mappingObjectDragBox).toBeTruthy()
  await page.mouse.move(
    mappingObjectDragBox!.x + mappingObjectDragBox!.width / 2, mappingObjectDragBox!.y + 14)
  await page.mouse.down()
  await page.mouse.move(
    mappingObjectDragBox!.x + mappingObjectDragBox!.width / 2 + 100,
    mappingObjectDragBox!.y + 14 + 60,
    { steps: 8 })
  await page.mouse.up()
  expect((await mappingLayoutSaved).ok()).toBeTruthy()
  await expect(page.locator('.dmc-canvas-stats')).toContainText('已与数据库同步')

  // 两侧清单都必须在当前工作台高度内独立滚动，不能由内容撑高后被页面裁掉。
  await verifyVerticalListScroll(page.getByTestId('mapping-assets-list'))
  await verifyVerticalListScroll(page.getByTestId('mapping-ontology-list'))
  await page.getByRole('button', { name: /实体关系 1/ }).click()
  await verifyVerticalListScroll(page.getByTestId('mapping-ontology-list'))

  // 画布和左侧清单共用同一预览开关：同一只眼睛再次点击必须收起。
  const canvasPreviewButton = mappingDatasetNode.getByTitle('预览数据')
  await canvasPreviewButton.click()
  await expect(page.getByTestId('mapping-dataset-preview')).toBeVisible()
  await canvasPreviewButton.click()
  await expect(page.getByTestId('mapping-dataset-preview')).toHaveCount(0)
  const assetPreviewButton = page.getByTestId('mapping-assets-list').locator('.dmc-eye').first()
  await assetPreviewButton.click()
  await expect(assetPreviewButton).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByTestId('mapping-dataset-preview')).toBeVisible()
  await assetPreviewButton.click()
  await expect(assetPreviewButton).toHaveAttribute('aria-pressed', 'false')
  await expect(page.getByTestId('mapping-dataset-preview')).toHaveCount(0)

  await page.getByTestId('explore-view-model').click()
  await expect(page).toHaveURL(new RegExp(`/explore\\?ontologyId=${ontology.id}&versionId=${draft.id}&view=model`))
  await expect(page.getByTestId('graph-workspace-stage')).toContainText('草稿', { timeout: 20_000 })
  await page.goto(`/#/ontologies/${ontology.id}`)
  await page.getByRole('button', { name: '查看历史版本' }).click()
  await expect(page.getByTestId('version-tree')).toBeVisible()

  const trialResponsePromise = page.waitForResponse(response =>
    response.request().method() === 'POST'
      && response.url().endsWith(`/api/v2/ontologies/${ontology.id}/versions/${draft.id}/trial-runs`))
  await page.getByTestId('version-node-v0.1').getByRole('button', { name: '转为试跑态' }).click()
  const trialResponse = await trialResponsePromise
  expect(trialResponse.ok(), await trialResponse.text()).toBeTruthy()
  const trialResponseBody = await trialResponse.json()
  expect((trialResponseBody.data ?? trialResponseBody).status).toBe('passed')
  const trialDialog = page.getByRole('dialog', { name: '隔离试跑结果' })
  await expect(trialDialog).toBeVisible({ timeout: 20_000 })
  await expect(trialDialog.getByText('外部动作执行数：0')).toBeVisible()
  await expect(trialDialog.getByRole('button', { name: '打开试跑图谱' })).toBeVisible()
  await trialDialog.getByRole('button', { name: '关闭', exact: true }).click()
  await expect(draftRow.getByText('试跑态', { exact: true })).toBeVisible()
  await expect(draftRow.getByRole('button', { name: '转为发布态' })).toBeVisible()
  await expect(draftRow.getByRole('button', { name: '编辑模型' })).toHaveCount(0)

  // 试跑态虽然冻结结构，但模型定义必须可查看，画布仍可移动。
  await page.goto(`/#/ontologies/${ontology.id}/graph?versionId=${draft.id}`)
  await expect(page.getByText(/试跑态 v0\.1 · 可只读查看本次隔离实例/)).toBeVisible({ timeout: 20_000 })
  // 功能入口在所有状态保持稳定：安全查看/布局能力可用，结构和正式运行操作明确禁用。
  await expect(page.getByRole('button', { name: '对象实体', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: '导入', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: '搜索定义' })).toBeEnabled()
  await expect(page.getByTitle('自动布局')).toBeEnabled()
  await page.getByRole('button', { name: '搜索定义' }).click()
  await expect(page.getByPlaceholder('搜索对象 / 关系 / 动作 / 函数 / 属性名…')).toBeVisible()
  await page.keyboard.press('Escape')
  await page.getByTitle('打开菜单').click()
  await expect(page.getByTestId('graph-runtime-tool-instances')).toBeEnabled()
  await expect(page.getByTestId('graph-runtime-tool-runaction')).toBeEnabled()
  await expect(page.getByTestId('graph-runtime-tool-graphdb')).toBeEnabled()
  await expect(page.getByTestId('graph-runtime-tool-help')).toBeEnabled()

  // 功能区不因状态而消失：试跑实例来自隔离表，内容可见但写操作禁用。
  await page.getByTestId('graph-runtime-tool-instances').click()
  await expect(page.getByRole('heading', { name: '对象实例浏览器' })).toBeVisible()
  await expect(page.getByText('正在查看试跑隔离空间的数据')).toBeVisible()
  await page.locator('select').filter({ has: page.locator(`option[value="${objectTypeId}"]`) }).selectOption(objectTypeId)
  await expect(page.getByText('真机一号')).toBeVisible()
  await expect(page.getByText('真机二号')).toBeVisible()
  await expect(page.getByRole('button', { name: '新建实例' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: /属性溯源/ })).toHaveCount(0)
  await page.getByRole('button', { name: '关闭对象实例浏览器' }).click()

  await page.getByTitle('打开菜单').click()
  await expect(page.getByTestId('graph-runtime-tool-sentinel')).toBeEnabled()
  await page.getByTestId('graph-runtime-tool-sentinel').click()
  await expect(page.getByRole('heading', { name: '哨兵引擎' })).toBeVisible()
  await expect(page.getByText('正在查看冻结的哨兵定义和隔离试跑评估')).toBeVisible()
  await page.getByRole('button', { name: '关闭哨兵引擎' }).click()

  await page.getByTitle('打开菜单').click()
  await page.getByTestId('graph-runtime-tool-runaction').click()
  await expect(page.getByRole('heading', { name: '动作执行器' })).toBeVisible()
  await expect(page.getByText('当前版本可查看动作定义、参数与规则')).toBeVisible()
  await page.locator('select').first().selectOption(`act-browser-order-${suffix}`)
  await expect(page.getByRole('button', { name: '模拟执行（本地，不修改数据）' })).toBeDisabled()
  await page.getByRole('button', { name: '关闭动作执行器' }).click()

  await page.getByTitle('打开菜单').click()
  await page.getByTestId('graph-runtime-tool-runhistory').click()
  await expect(page.getByTestId('trial-run-summary')).toContainText('隔离试跑')
  await expect(page.getByTestId('trial-run-summary')).toContainText('2')
  await page.getByRole('button', { name: '关闭运行历史' }).click()
  await verifyReadonlyGraphInspection(page, objectTypeId)
  const movedTrial = await api<any>(request, token, 'get', `/api/v2/ontologies/${ontology.id}/versions/${draft.id}/workspace`)
  const trialObject = movedTrial.objectTypes.find((item: any) => item.id === objectTypeId)
  const trialPosition = { x: trialObject.positionX, y: trialObject.positionY }
  expect(trialPosition).not.toEqual({ x: 100, y: 100 })
  await page.goto(`/#/ontologies/${ontology.id}`)
  await page.getByRole('button', { name: '查看历史版本' }).click()
  await expect(page.getByTestId('version-tree')).toBeVisible()

  // 发布前先返回结构化门禁结果：历史环境里通过过试跑的冻结版本也可能
  // 缺少当前规则要求的属性映射。界面应归组解释并给出一条可执行修复路径。
  const impactPattern = `**/api/v2/ontologies/${ontology.id}/versions/${draft.id}/impact`
  const legacyFields = [
    ['ot-governance-case', '治理案例', 'GovernanceCase', [
      'appointment_person_id', 'appointment_person_name', 'eamap_person_id',
      'eamap_person_name', 'has_change_review',
    ]],
    ['ot-control-snapshot', '业务代表治理监测快照', 'RepresentativeControlSnapshot', [
      'appointment_person_id', 'appointment_person_name', 'change_review_ref',
      'eamap_person_id', 'eamap_person_name',
    ]],
    ['ot-application-module', '应用系统模块', 'ApplicationModule', ['sub_product']],
  ] as const
  await page.route(impactPattern, async route => {
    const response = await route.fetch()
    const body = await response.json()
    body.data.releaseReadiness = {
      ready: false,
      blockingCount: 11,
      trialRunId: draft.latest_trial?.id,
      repairStrategy: 'create_draft',
      repairSourceVersionId: draft.id,
      errors: legacyFields.flatMap(([targetId, targetName, mappingName, fields]) =>
        fields.map(field => ({
          code: 'mapping_property_missing',
          kind: 'mapping',
          id: `mapping-${targetId}`,
          name: mappingName,
          targetId,
          targetName,
          field,
          message: `Mapping「${mappingName}」未覆盖 ObjectType「${targetName}」的存储属性「${field}」`,
        }))),
    }
    await route.fulfill({ response, json: body })
  })
  await page.getByTestId('version-node-v0.1').getByRole('button', { name: '转为发布态' }).click()
  const blockedDialog = page.getByRole('dialog', { name: '发布前检查 · v0.1' })
  await expect(blockedDialog.getByTestId('release-readiness-blocked')).toContainText('3 个本体元素存在 11 项映射问题')
  await expect(blockedDialog.getByTestId('release-readiness-blocked')).toContainText('11 个存储属性尚未映射')
  await expect(blockedDialog.getByRole('button', { name: '暂不可发布' })).toBeDisabled()
  await expect(blockedDialog.getByRole('button', { name: '确认发布' })).toHaveCount(0)
  const issueGroups = blockedDialog.getByTestId('release-readiness-group')
  await expect(issueGroups).toHaveCount(3)
  await expect(issueGroups.first()).toContainText('治理案例')
  await issueGroups.first().locator('summary').click()
  await expect(issueGroups.first()).toContainText('appointment_person_id')
  await blockedDialog.getByRole('button', { name: '创建修复分支并完善映射' }).click()
  await expect(page).toHaveURL(new RegExp(`/ontologies/${ontology.id}/graph\\?versionId=.*&view=mapping`))
  await page.unroute(impactPattern)

  const repairTree = await api<any>(request, token, 'get', `/api/v2/ontologies/${ontology.id}/version-tree`)
  const repairDraft = repairTree.versions.find((item: any) => item.version_number === 'v0.1.1')
  expect(repairDraft).toMatchObject({ lifecycle_status: 'editing', node_kind: 'draft' })
  const repairWorkspace = await api<any>(request, token, 'get', `/api/v2/ontologies/${ontology.id}/versions/${repairDraft.id}/workspace`)
  expect(repairWorkspace).toMatchObject({ workspaceMode: 'draft', editable: true })
  expect(repairWorkspace.objectTypes).toHaveLength(1)
  expect(repairWorkspace.linkTypes).toHaveLength(1)
  expect(repairWorkspace.actions).toHaveLength(1)
  expect(repairWorkspace.functions).toHaveLength(1)
  expect(repairWorkspace.mappings).toHaveLength(1)
  expect(repairWorkspace.linkMappings).toHaveLength(1)

  // 原试跑版本实际满足条件时仍走正常发布路径，验证友好预检没有放松后端门禁。
  await page.goto(`/#/ontologies/${ontology.id}`)
  await page.getByRole('button', { name: '查看历史版本' }).click()
  await page.getByTestId('version-node-v0.1').getByRole('button', { name: '转为发布态' }).click()
  const readyDialog = page.getByRole('dialog', { name: '发布前检查 · v0.1' })
  await expect(readyDialog.getByTestId('release-readiness-ready')).toContainText('发布条件已满足')
  await expect(readyDialog.getByRole('button', { name: '确认发布' })).toBeEnabled()
  await page.getByRole('button', { name: '确认发布' }).click()
  await expect(page.getByTestId('version-node-v1')).toContainText('当前发布', { timeout: 20_000 })
  await expect(draftRow).toContainText('已发布为 v1')
  const evolvedVersionOrder = await page.getByTestId('version-tree')
    .locator('article[data-testid^="version-node-"]')
    .evaluateAll(rows => rows.map(row => row.getAttribute('data-testid')?.replace('version-node-', '')))
  // 发布版始终位于第一列主干，分支缩进挂在所属版本下：
  // v0（主干）→ v0.1（v0 的分支）→ v0.1.1（v0.1 的修复分支）→ v1（主干）。
  expect(evolvedVersionOrder.slice(0, 4)).toEqual(['v0', 'v0.1', 'v0.1.1', 'v1'])

  const releasedTree = await api<any>(request, token, 'get', `/api/v2/ontologies/${ontology.id}/version-tree`)
  await waitForMappingApplied(request, token, ontology.id, mappingId)
  const publishedUpload = await request.post(`${API}/api/v2/datasets/${dataset.id}/upload`, {
    headers: { Authorization: `Bearer ${token}` },
    multipart: {
      file: {
        name: 'orders-after-publish.csv', mimeType: 'text/csv',
        buffer: Buffer.from('id,name\nE2E-1,真机一号\nE2E-2,真机二号\n'),
      },
    },
  })
  const publishedUploadText = await publishedUpload.text()
  expect(publishedUpload.ok(), publishedUploadText).toBeTruthy()
  const publishedUploadBody = JSON.parse(publishedUploadText)
  const publishedVersionNo = Number(
    (publishedUploadBody.data ?? publishedUploadBody).version_no,
  )
  expect(Number.isInteger(publishedVersionNo)).toBeTruthy()
  await waitForPublishedDatasetProjection(
    request, token, dataset.id, publishedVersionNo, ontology.id, mappingId,
  )
  await api<any>(request, token, 'put', `/api/v2/formal/ontologies/${ontology.id}/agent/profile`, {
    allowedActionIds: [`act-browser-order-${suffix}`],
  })
  const dynamicSentinel = await api<any>(request, token, 'post', `/api/v2/formal/ontologies/${ontology.id}/agent/dynamic-sentinels`, {
    releaseId: releasedTree.current_release_id,
    definition: {
      name: `assistant_browser_order_${suffix}`,
      displayName: '真机订单动态哨兵',
      description: '验证本体结构能够合并展示后天创建的哨兵',
      bindings: [{ alias: 'o', objectTypeId, filter: null }],
      links: [],
      condition: "o.name == '真机一号'",
      conditionRows: [],
      conditionLogic: 'and',
      primaryAlias: 'o',
      actionIds: [`act-browser-order-${suffix}`],
      actionParameters: {},
      onChange: true,
      onSchedule: false,
      scanIntervalSeconds: 300,
      triggerMode: 'on_enter',
      muted: false,
    },
  })
  expect(dynamicSentinel).toMatchObject({ origin: 'assistant_dynamic', enabled: false, trialCurrent: false })

  // 只有版本演进里可以打开历史快照，且历史发布版严格只读。
  await page.getByTestId('version-node-v0').getByRole('button', { name: '打开编辑器' }).click()
  await expect(page).toHaveURL(new RegExp(`/ontologies/${ontology.id}/graph\\?versionId=`))
  await expect(page.getByText(/历史发布 v0 · 可查看定义并保存画布布局/)).toBeVisible({ timeout: 20_000 })

  // 本体结构页只读展示最新发布快照，但顶部仍提供统一的图谱编辑器入口；
  // 用户反馈的总览、治理和正常的结构页之间不能改变顶部导航尺寸或表面层级。
  await page.goto(`/#/ontologies/${ontology.id}`)
  const detailHeader = page.getByTestId('ontology-detail-header')
  const detailContent = page.getByTestId('ontology-detail-content')
  await expect(detailHeader).toBeVisible()
  const headerBeforeStructure = await detailHeader.boundingBox()
  const historyBeforeStructure = await page.getByRole('button', { name: '查看历史版本' }).boundingBox()
  expect(headerBeforeStructure).toBeTruthy()
  expect(historyBeforeStructure).toBeTruthy()
  await expect(detailHeader).toHaveCSS('box-shadow', 'none')
  await expect(page.locator('.overview-panel').first()).toHaveCSS('box-shadow', 'none')

  await page.getByRole('button', { name: '治理推演', exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/ontologies/${ontology.id}\\?tab=governance`))
  expect(await detailHeader.boundingBox()).toEqual(headerBeforeStructure)
  expect(await page.getByRole('button', { name: '查看历史版本' }).boundingBox()).toEqual(historyBeforeStructure)
  await expect(detailHeader).toHaveCSS('box-shadow', 'none')
  await expect(detailContent).toHaveCSS('box-shadow', 'none')

  await page.getByRole('button', { name: '本体总览', exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/ontologies/${ontology.id}$`))
  expect(await detailHeader.boundingBox()).toEqual(headerBeforeStructure)
  expect(await page.getByRole('button', { name: '查看历史版本' }).boundingBox()).toEqual(historyBeforeStructure)

  await page.getByRole('button', { name: '本体结构', exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/ontologies/${ontology.id}\\?tab=design`))
  const headerAfterStructure = await detailHeader.boundingBox()
  const historyAfterStructure = await page.getByRole('button', { name: '查看历史版本' }).boundingBox()
  expect(headerAfterStructure).toBeTruthy()
  expect(historyAfterStructure).toBeTruthy()
  expect(headerAfterStructure).toEqual(headerBeforeStructure)
  expect(historyAfterStructure).toEqual(historyBeforeStructure)
  await expect(detailHeader).toHaveCSS('box-shadow', 'none')
  await expect(detailContent).toHaveCSS('box-shadow', 'none')
  await expect(page.getByTestId('current-release-version')).toHaveText('v1')
  await expect(page.getByTestId('published-structure-version')).toHaveText('v1')
  await expect(page.getByTestId('ontology-structure-graph')).toBeVisible()
  const miniMap = page.getByTestId('rf__minimap')
  const miniMapSvg = miniMap.locator('svg.react-flow__minimap-svg')
  await expect(miniMap).toHaveCSS('width', '150px')
  await expect(miniMap).toHaveCSS('height', '96px')
  await expect(miniMapSvg).toHaveAttribute('width', '150')
  await expect(miniMapSvg).toHaveAttribute('height', '96')
  expect(await page.evaluate(() => document.documentElement.scrollHeight <= window.innerHeight + 1)).toBeTruthy()
  await expect(page.getByTestId('structure-node-object')).toHaveCount(1)
  await expect(page.getByTestId('structure-node-property')).toHaveCount(0)
  await expect(page.getByTestId('structure-node-action')).toHaveCount(0)
  await expect(page.getByTestId('structure-edge-relation')).toHaveCount(1)
  await expect(page.getByLabel('搜索本体结构')).toHaveAttribute('placeholder', '搜索对象实体或实体关系')
  await expect(page.getByTestId('published-structure-readonly')).toHaveText('发布快照 · 结构只读')
  await expect(page.getByRole('button', { name: '打开图谱编辑器修改模型' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '查看当前发布图谱' })).toBeVisible()
  // 「业务文档」弹窗：读取当前发布版本语义层的需求文档。该本体由 API 直接
  // 建模产生、未经需求文档生成，语义层为空 —— 查不到文档是允许的空态。
  await expect(page.getByRole('button', { name: '业务文档' })).toBeVisible()
  await expect(page.getByRole('button', { name: '智能整理图谱' })).toHaveCount(0)
  const semanticLoaded = page.waitForResponse(response => response.request().method() === 'GET' && response.url().includes('/semantic'))
  await page.getByRole('button', { name: '业务文档' }).click()
  expect((await semanticLoaded).ok()).toBeTruthy()
  await expect(page.getByTestId('structure-doc-empty')).toContainText('当前版本没有关联的需求文档')
  await page.getByLabel('关闭业务文档').click()
  await expect(page.getByTestId('structure-doc-empty')).toHaveCount(0)

  // L2 追加属性和动作；函数/哨兵保持为分析选择器，不成为常驻节点。
  await page.getByRole('button', { name: 'L2 结构展开', exact: true }).click()
  await expect(page.getByTestId('structure-node-property')).toHaveCount(2)
  await expect(page.getByTestId('structure-node-action')).toHaveCount(1)
  await expect(page.getByLabel('搜索本体结构')).toHaveAttribute('placeholder', '搜索对象、关系、属性或动作')

  // 发布快照保持不可变，但结构分析选择器需额外合并当前发布版的动态哨兵。
  await page.getByLabel('查看哨兵规则覆盖范围').click()
  await expect(page.getByTestId('sentinel-dependency-source-counts')).toHaveText('· 公共哨兵 0 · 动态哨兵 1')
  const dynamicOption = page.getByTestId(`sentinel-dependency-option-${dynamicSentinel.id}`)
  await expect(dynamicOption).toContainText('真机订单动态哨兵')
  await expect(dynamicOption).toContainText('动态哨兵')
  await expect(dynamicOption).toContainText('待试跑 · 变更触发')
  await expect(page.getByTestId(`sentinel-dependency-source-${dynamicSentinel.id}`)).toHaveText('✦动态哨兵')
  await dynamicOption.click()
  await expect(page.locator(`.react-flow__node[data-id="${objectTypeId}"] > div`)).toHaveClass(/border-fuchsia-500/)
  await expect(page.locator(`.react-flow__node[data-id="property:${objectTypeId}:p-name"] > div`)).toHaveClass(/border-violet-500/)
  await expect(page.locator(`.react-flow__node[data-id="action:act-browser-order-${suffix}"] > div`)).toHaveClass(/border-violet-500/)

  await page.getByLabel('查看计算函数使用关系').click()
  await page.getByTestId(`function-dependency-option-fn-browser-order-${suffix}`).click()
  await expect(page.locator(`.react-flow__node[data-id="${objectTypeId}"] > div`)).toHaveClass(/border-violet-500/)

  // L2 节点同样只保存独立画布布局，并采用 3 秒尾随保存。
  await page.getByLabel('查看计算函数使用关系').click()
  await page.getByTestId('function-dependency-clear').click()
  const propertyNode = page.locator(`.react-flow__node[data-id="property:${objectTypeId}:p-name"]`)
  await expect(propertyNode).toBeVisible()
  await propertyNode.hover()
  const propertyBefore = await propertyNode.boundingBox()
  expect(propertyBefore).toBeTruthy()
  const l2LayoutSaved = page.waitForResponse(response => response.request().method() === 'PUT' && response.url().endsWith('/layout'))
  await page.mouse.move(propertyBefore!.x + propertyBefore!.width / 2, propertyBefore!.y + propertyBefore!.height / 2)
  await page.mouse.down()
  await page.mouse.move(propertyBefore!.x + propertyBefore!.width / 2 + 90, propertyBefore!.y + propertyBefore!.height / 2 + 45, { steps: 8 })
  await page.mouse.up()
  expect((await l2LayoutSaved).ok()).toBeTruthy()

  // 拖动对象实体时，其属性和动作应保持相对位置并作为整组一起移动。
  const objectNode = page.locator(`.react-flow__node[data-id="${objectTypeId}"]`)
  const actionNode = page.locator(`.react-flow__node[data-id="action:act-browser-order-${suffix}"]`)
  const objectBefore = await objectNode.boundingBox()
  const childBefore = await propertyNode.boundingBox()
  const actionBefore = await actionNode.boundingBox()
  expect(objectBefore).toBeTruthy()
  expect(childBefore).toBeTruthy()
  expect(actionBefore).toBeTruthy()
  const groupedLayoutSaved = page.waitForResponse(response => response.request().method() === 'PUT' && response.url().endsWith('/layout'))
  await page.mouse.move(objectBefore!.x + objectBefore!.width / 2, objectBefore!.y + objectBefore!.height / 2)
  await page.mouse.down()
  await page.mouse.move(objectBefore!.x + objectBefore!.width / 2 + 70, objectBefore!.y + objectBefore!.height / 2 + 40, { steps: 8 })
  await page.mouse.up()
  expect((await groupedLayoutSaved).ok()).toBeTruthy()
  const objectAfter = await objectNode.boundingBox()
  const childAfter = await propertyNode.boundingBox()
  const actionAfter = await actionNode.boundingBox()
  expect(objectAfter).toBeTruthy()
  expect(childAfter).toBeTruthy()
  expect(actionAfter).toBeTruthy()
  const objectDelta = { x: objectAfter!.x - objectBefore!.x, y: objectAfter!.y - objectBefore!.y }
  expect(childAfter!.x - childBefore!.x).toBeCloseTo(objectDelta.x, 1)
  expect(childAfter!.y - childBefore!.y).toBeCloseTo(objectDelta.y, 1)
  expect(actionAfter!.x - actionBefore!.x).toBeCloseTo(objectDelta.x, 1)
  expect(actionAfter!.y - actionBefore!.y).toBeCloseTo(objectDelta.y, 1)

  const structureWorkspace = await api<any>(request, token, 'get', `/api/v2/ontologies/${ontology.id}/current-release/workspace`)
  expect(structureWorkspace).toMatchObject({ isCurrentRelease: true, workspaceMode: 'release', editable: false })
  expect(structureWorkspace.canvasLayout[`l2:property:${objectTypeId}:p-name`]).toBeTruthy()

  await expect(page.getByText('真机订单', { exact: true }).first()).toBeVisible()
  // 直接访问图谱路由验证纵深防线：当前发布版仍只能查看，模型写入被前后端共同冻结。
  await page.goto(`/#/ontologies/${ontology.id}/graph`)
  await expect(page).toHaveURL(new RegExp(`/ontologies/${ontology.id}/graph$`))
  await expect(page.getByText(/当前发布 v1 · 可查看定义并保存画布布局/)).toBeVisible({ timeout: 20_000 })
  await verifyWorkspaceStagePosition(page)
  const inheritedRelease = await api<any>(request, token, 'get', `/api/v2/formal/ontologies/${ontology.id}/full`)
  const inheritedReleaseObject = inheritedRelease.objectTypes.find((item: any) => item.id === objectTypeId)
  expect({ x: inheritedReleaseObject.positionX, y: inheritedReleaseObject.positionY }).toEqual(trialPosition)
  await verifyReadonlyGraphInspection(page, objectTypeId)
  const movedRelease = await api<any>(request, token, 'get', `/api/v2/formal/ontologies/${ontology.id}/full`)
  const releaseObject = movedRelease.objectTypes.find((item: any) => item.id === objectTypeId)
  expect({ x: releaseObject.positionX, y: releaseObject.positionY }).not.toEqual(trialPosition)

  // 运行入口在所有状态可见，但只有当前发布态连接正式运行数据并允许操作。
  await page.getByTitle('打开菜单').click()
  await expect(page.getByRole('button', { name: 'API 文档' })).toHaveCount(0)
  await page.getByRole('button', { name: '哨兵引擎' }).click()
  await expect(page.getByRole('heading', { name: '哨兵引擎' })).toBeVisible()
  await page.getByRole('button', { name: '关闭哨兵引擎' }).click()

  await page.getByRole('button', { name: '基于此版本开始修改' }).click()
  await expect(page).toHaveURL(new RegExp(`/ontologies/${ontology.id}/graph\\?versionId=`))
  await expect(page.getByText(/草稿 v1\.1 · 可编辑并查看全部模型定义/)).toBeVisible({ timeout: 20_000 })
  await expect(page.getByRole('button', { name: '对象实体', exact: true })).toBeEnabled()

  // 草稿态拖动也只走独立布局接口：自动保存、不点亮模型保存、不推进 revision。
  const draftTreeAfterCreate = await api<any>(request, token, 'get', `/api/v2/ontologies/${ontology.id}/version-tree`)
  const nextDraft = draftTreeAfterCreate.versions.find((item: any) => item.version_number === 'v1.1')
  const draftWorkspaceBeforeLayout = await api<any>(request, token, 'get', `/api/v2/ontologies/${ontology.id}/versions/${nextDraft.id}/workspace`)
  const draftNode = page.locator(`.react-flow__node[data-id="${objectTypeId}"]`)
  const draftNodeBefore = await draftNode.boundingBox()
  expect(draftNodeBefore).toBeTruthy()
  const draftLayoutSaved = page.waitForResponse(response =>
    response.request().method() === 'PUT' && response.url().endsWith('/layout'))
  await page.mouse.move(draftNodeBefore!.x + draftNodeBefore!.width / 2, draftNodeBefore!.y + draftNodeBefore!.height / 2)
  await page.mouse.down()
  await page.mouse.move(draftNodeBefore!.x + draftNodeBefore!.width / 2 + 84, draftNodeBefore!.y + draftNodeBefore!.height / 2 + 48, { steps: 8 })
  await page.mouse.up()
  expect((await draftLayoutSaved).ok()).toBeTruthy()
  await expect(page.getByTestId('layout-save-status')).toContainText('布局已自动保存')
  await expect(page.getByText('保存全部更改')).toHaveCount(0)
  const draftWorkspaceAfterLayout = await api<any>(request, token, 'get', `/api/v2/ontologies/${ontology.id}/versions/${nextDraft.id}/workspace`)
  expect(draftWorkspaceAfterLayout.revision).toBe(draftWorkspaceBeforeLayout.revision)
  expect({
    x: draftWorkspaceAfterLayout.objectTypes[0].positionX,
    y: draftWorkspaceAfterLayout.objectTypes[0].positionY,
  }).not.toEqual({
    x: draftWorkspaceBeforeLayout.objectTypes[0].positionX,
    y: draftWorkspaceBeforeLayout.objectTypes[0].positionY,
  })

  const finalTree = await api<any>(request, token, 'get', `/api/v2/ontologies/${ontology.id}/version-tree`)
  expect(finalTree.current_release_number).toBe('v1')
  expect(finalTree.versions.some((item: any) => item.version_number === 'v1.1')).toBeTruthy()
  const releasedOntology = await api<any>(request, token, 'get', `/api/v1/ontologies/${ontology.id}`)
  expect(releasedOntology.version).toBe('v1')
  expect(releasedOntology.current_release_version).toBe('v1')
  expect(releasedOntology.current_release_id).toBe(finalTree.current_release_id)
  const instances = await api<any[]>(request, token, 'get', `/api/v2/formal/ontologies/${ontology.id}/instances`)
  expect(instances).toHaveLength(2)

  // 总览使用当前发布投影与真实 HITL 待办：包裹可选择、可直接决策，结果进入事实流。
  const pendingAction = await api<any>(request, token, 'post', `/api/v2/formal/ontologies/${ontology.id}/run-action`, {
    actionId: `act-browser-order-${suffix}`,
    targetInstanceId: instances[0].id,
    parameters: { note: 'overview-e2e' },
    dryRun: false,
  })
  expect(pendingAction.status).toBe('pending')
  await page.goto(`/#/ontologies/${ontology.id}`)
  await expect(page.getByRole('heading', { name: '本体概况' })).toBeVisible()
  const profile = page.locator('.ontology-profile')
  await expect(profile.locator('.profile-meta')).toContainText('当前发布')
  await expect(profile.locator('.profile-meta')).toContainText('更新时间')
  await expect(profile.locator('.profile-meta')).not.toContainText('创建时间')
  await expect(profile.getByTestId('version-evolution-card')).toBeVisible()
  await expect(profile.getByRole('button', { name: '播放' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '运行汇总' })).toBeVisible()
  // 已下线面板不得回潮：事实构成无操作性（KPI 副标题已含构成概要），
  // 事实流预览与治理推演重复，总览不再常驻这两个面板。
  await expect(page.getByRole('heading', { name: '事实类型构成' })).toHaveCount(0)
  await expect(page.getByRole('heading', { name: '最近发生了什么' })).toHaveCount(0)
  await expect(page.getByRole('heading', { name: '模型资产构成' })).toHaveCount(0)
  await expect(page.getByRole('heading', { name: '待审批流水线' })).toHaveCount(0)
  await expect(page.getByRole('heading', { name: '实例分布与来源' })).toHaveCount(0)
  await expect(page.getByRole('heading', { name: '映射状态' })).toHaveCount(0)
  await expect(page.getByRole('heading', { name: '健康检查' })).toHaveCount(0)
  const profileBox = await profile.boundingBox()
  const kpiBox = await page.locator('.kpi-rail').boundingBox()
  const runtimeBox = await page.locator('.runtime-summary').boundingBox()
  expect(profileBox).not.toBeNull()
  expect(kpiBox).not.toBeNull()
  expect(runtimeBox).not.toBeNull()
  expect(Math.abs(profileBox!.height - (kpiBox!.height + runtimeBox!.height + 14))).toBeLessThan(3)
  expect(Math.abs(kpiBox!.x - runtimeBox!.x)).toBeLessThan(2)
  expect(Math.abs(kpiBox!.width - runtimeBox!.width)).toBeLessThan(2)
  expect(runtimeBox!.x).toBeGreaterThan(profileBox!.x + profileBox!.width)
  expect(kpiBox!.height).toBeLessThan(300)
  // 总览允许纵向滚动展示完整内容，但外壳必须始终可滚动，
  // 绝不允许回到 overflow:hidden 裁切内容。
  await expect(page.locator('.ontology-overview-shell')).toHaveCSS('overflow-y', 'auto')
  const runtimeSummary = page.locator('.runtime-summary')
  // 时间维度下拉替代原"时间范围"拖拽条：默认近7天（走 overview 自带 daily7d，
  // 不额外发请求）；其余维度走 runtime-summary 显式时间窗接口。
  const dimensionSelect = runtimeSummary.getByLabel('运行汇总时间维度')
  await expect(dimensionSelect).toHaveValue('last7')
  await expect(runtimeSummary.locator('.runtime-range-input')).toHaveCount(0)
  const summaryResponsePromise = page.waitForResponse(response =>
    response.url().includes(`/api/v2/formal/ontologies/${ontology.id}/runtime-summary`)
    && response.request().method() === 'GET')
  await dimensionSelect.selectOption('last30')
  const summaryResponse = await summaryResponsePromise
  expect(summaryResponse.ok(), `runtime-summary: ${await summaryResponse.text()}`).toBeTruthy()
  const summaryBody = await summaryResponse.json()
  expect(summaryBody.data.days.length).toBe(30)

  // 待审批信息不再摆上总览（横条与卡片均不展示），审批统一在「治理推演」处理。
  const overviewMain = page.locator('.overview-dashboard')
  await expect(overviewMain.getByText(/等待审批|需人工审批/)).toHaveCount(0)
  await page.locator('[data-tab-value="governance"]').click()
  await expect(page).toHaveURL(new RegExp(`/ontologies/${ontology.id}\\?tab=governance`))
  await page.getByRole('button', { name: '本体总览', exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/ontologies/${ontology.id}$`))
  await api<any>(request, token, 'post',
    `/api/v2/formal/ontologies/${ontology.id}/action-logs/${pendingAction.id}/decide`,
    { decision: 'rejected' })
  const decisionFacts = await api<any[]>(request, token, 'get',
    `/api/v2/formal/ontologies/${ontology.id}/facts/recent?limit=10&kind=decision`)
  expect(decisionFacts.some((fact: any) => fact.value === 'REJECTED')).toBeTruthy()

  // 旧的无版本映射 URL 只能展示当前发布快照，不得写运行投影表。
  await page.goto(`/#/ontologies/${ontology.id}/mapping-config`)
  await expect(page.getByTestId('mapping-workspace')).toHaveAttribute('data-workspace-mode', 'release')
  await expect(page.getByText('发布快照 · 只读查看')).toBeVisible()
  await expect(page.getByRole('button', { name: '保存配置' })).toHaveCount(0)

  await request.delete(`${API}/api/v1/ontologies/${ontology.id}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
})
