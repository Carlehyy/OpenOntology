import { readFile } from 'node:fs/promises'

import { expect, test, type Page, type Route } from '@playwright/test'

/**
 * 全屏图谱编辑器（/ontologies/:id/graph）导出契约：
 * - 后端绑定模式必须与详情页「导出本体结构」调用同一端点（可移植结构包），
 *   不再是旧本地格式 {ontology, nodes, edges}；
 * - 草稿/归档工作区导出携带 version_id，运行态不携带（当前发布投影）；
 * - 导入弹窗只接收画布本地备份，结构包必须被拦截并指引到本体管理页。
 */

const ontologyId = 'ontology-structure-export'
const draftVersionId = 'version-draft-export'
const ontologyName = '供应链风险本体'
const runtimeVersion = 'v1'
const draftVersion = 'v1.1'

const sentinel = {
  id: 'sentinel-high-risk',
  ontologyId,
  name: 'high_risk_order',
  displayName: '高风险订单监控',
  description: '风险评分达到阈值时触发',
  bindings: [{ alias: 'o', objectTypeId: 'object-order', filter: null }],
  links: [],
  condition: 'o["risk_score"] >= 80',
  conditionRows: [],
  conditionLogic: 'and',
  primaryAlias: 'o',
  actionIds: [],
  actionParameters: {},
  onChange: true,
  onSchedule: false,
  scanIntervalSeconds: 300,
  triggerMode: 'on_enter',
  muted: false,
  enabled: true,
  releaseId: 'release-v1',
  enableGeneration: 0,
  origin: 'release_builtin',
  status: 'published',
}

const objectType = (id: string, name: string, displayName: string, x: number, y: number) => ({
  id,
  name,
  displayName,
  primaryKey: 'id',
  properties: [{ id: 'id', name: 'id', displayName: '标识', type: 'string', required: true }],
  positionX: x,
  positionY: y,
})

/** 最小可移植结构包：真实格式 {format, formatVersion, exportedAt, ontology, structure, canvasLayout?} */
const structurePackage = {
  format: 'ontology-structure',
  formatVersion: 1,
  exportedAt: '2026-11-01T00:00:00Z',
  ontology: {
    id: ontologyId,
    name: ontologyName,
    domain: '供应链',
    description: '图谱编辑器导出测试',
    icon: 'network',
    sourceVersion: runtimeVersion,
    sourceStatus: 'published',
  },
  structure: {
    objectTypes: [{
      id: 'object-order',
      name: 'Order',
      displayName: '订单',
      primaryKey: 'id',
      properties: [{ id: 'id', name: 'id', displayName: '标识', type: 'string', required: true }],
    }],
    linkTypes: [],
    actions: [],
    functions: [],
    sentinels: [sentinel],
  },
  canvasLayout: { 'object-order': { x: 120, y: 120 } },
}

type GraphMode = 'runtime' | 'draft'

interface GraphMock {
  /** 记录导出端点收到的请求 URL（含 query，用于断言 version_id 语义） */
  exportUrls: URL[]
}

async function mockGraph(page: Page, mode: GraphMode): Promise<GraphMock> {
  const exportUrls: URL[] = []

  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('auth-store', JSON.stringify({
      state: {
        token: 'e2e-token',
        user: { id: 'u1', username: 'tester', role: 'admin' },
      },
      version: 0,
    }))
  })

  const ok = (route: Route, data: unknown) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ data, message: 'ok' }),
  })

  await page.route(/^https?:\/\/[^/]+\/api\/v2\//, route => {
    const path = new URL(route.request().url()).pathname
    if (mode === 'runtime' && path === `/api/v2/formal/ontologies/${ontologyId}/full`) {
      return ok(route, {
        id: ontologyId,
        name: ontologyName,
        description: '供应链接构导出测试',
        version: runtimeVersion,
        revision: '1:runtime',
        workspaceMode: 'runtime',
        editable: false,
        objectTypes: [objectType('object-order', 'Order', '订单', 120, 120)],
        linkTypes: [],
        actions: [],
        functions: [],
        instances: [],
        linkInstances: [],
        executionLogs: [],
        sentinels: [sentinel],
      })
    }
    if (mode === 'draft'
      && path === `/api/v2/ontologies/${ontologyId}/versions/${draftVersionId}/workspace`) {
      return ok(route, {
        id: ontologyId,
        name: ontologyName,
        description: '供应链草稿导出测试',
        version: draftVersion,
        revision: '3:draft',
        workspaceMode: 'draft',
        editable: true,
        objectTypes: [
          objectType('object-order', 'Order', '订单', 120, 120),
          objectType('object-item', 'OrderItem', '订单明细', 520, 120),
        ],
        linkTypes: [],
        actions: [],
        functions: [],
        instances: [],
        linkInstances: [],
        executionLogs: [],
        sentinels: [sentinel],
        canvasLayout: { 'object-order': { x: 120, y: 120 } },
      })
    }
    return ok(route, [])
  })

  await page.route(/^https?:\/\/[^/]+\/api\/v1\//, route => {
    const url = new URL(route.request().url())
    if (url.pathname === `/api/v1/ontologies/${ontologyId}/export`) {
      exportUrls.push(url)
      // 导出端点返回裸包体（不包 {data}），与真实后端 Response 一致
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(structurePackage),
      })
    }
    return ok(route, [])
  })

  return { exportUrls }
}

async function readDownloadedPackage(path: string | null) {
  expect(path).not.toBeNull()
  return JSON.parse(await readFile(path!, 'utf8')) as {
    format?: string
    formatVersion?: number
    structure?: { sentinels?: unknown[] }
  }
}

test.describe('图谱编辑器结构包导出', () => {
  test('runtime 模式走可移植结构包端点，不带 version_id', async ({ page }) => {
    const { exportUrls } = await mockGraph(page, 'runtime')
    await page.goto(`/#/ontologies/${ontologyId}/graph`, { waitUntil: 'domcontentloaded' })

    // 加载完成信号：版本状态条与画布节点（此时 store.backendId 已就绪）
    await expect(page.getByTestId('graph-workspace-stage')).toContainText(`当前发布 ${runtimeVersion}`)
    await expect(page.locator('.react-flow__node').getByText('订单', { exact: true })).toBeVisible()

    const downloadPromise = page.waitForEvent('download')
    await page.getByTitle('导出').click()
    const download = await downloadPromise

    expect(download.suggestedFilename()).toBe(`${ontologyName}_${runtimeVersion}.json`)
    expect(exportUrls).toHaveLength(1)
    expect(exportUrls[0].pathname).toBe(`/api/v1/ontologies/${ontologyId}/export`)
    expect(exportUrls[0].searchParams.has('version_id')).toBe(false)

    const payload = await readDownloadedPackage(await download.path())
    expect(payload.format).toBe('ontology-structure')
    expect(payload.formatVersion).toBe(1)
    expect(Array.isArray(payload.structure?.sentinels)).toBe(true)
    expect(payload.structure?.sentinels).toHaveLength(1)
    await expect(page.getByText('本体结构 JSON 已下载')).toBeVisible()
  })

  test('draft 模式导出携带 version_id 并下载同一结构包', async ({ page }) => {
    const { exportUrls } = await mockGraph(page, 'draft')
    await page.goto(`/#/ontologies/${ontologyId}/graph?versionId=${draftVersionId}`, {
      waitUntil: 'domcontentloaded',
    })

    await expect(page.getByTestId('graph-workspace-stage')).toContainText(`草稿 ${draftVersion}`)
    await expect(page.locator('.react-flow__node')).toHaveCount(2)

    const downloadPromise = page.waitForEvent('download')
    await page.getByTitle('导出').click()
    const download = await downloadPromise

    expect(download.suggestedFilename()).toBe(`${ontologyName}_${draftVersion}.json`)
    expect(exportUrls).toHaveLength(1)
    expect(exportUrls[0].pathname).toBe(`/api/v1/ontologies/${ontologyId}/export`)
    expect(exportUrls[0].searchParams.get('version_id')).toBe(draftVersionId)

    const payload = await readDownloadedPackage(await download.path())
    expect(payload.format).toBe('ontology-structure')
    expect(Array.isArray(payload.structure?.sentinels)).toBe(true)
    await expect(page.getByText('本体结构 JSON 已下载')).toBeVisible()
  })

  test('导入弹窗拦截结构包并保留画布，旧格式画布备份仍可导入', async ({ page }) => {
    await mockGraph(page, 'draft')
    await page.goto(`/#/ontologies/${ontologyId}/graph?versionId=${draftVersionId}`, {
      waitUntil: 'domcontentloaded',
    })
    await expect(page.locator('.react-flow__node')).toHaveCount(2)

    await page.getByTitle('导入').click()
    const dialog = page.getByRole('dialog', { name: '导入本体' })
    await expect(dialog).toBeVisible()

    // 结构包：必须拦截、给出明确指引、不关闭弹窗、不清空画布
    await dialog.locator('textarea').fill(JSON.stringify(structurePackage))
    await dialog.getByRole('button', { name: '导入', exact: true }).click()

    const alert = dialog.getByRole('alert')
    await expect(alert).toContainText('可移植结构包')
    await expect(alert).toContainText('本地导入')
    await expect(dialog).toBeVisible()
    await expect(page.locator('.react-flow__node')).toHaveCount(2)
    await expect(page.locator('.react-flow__node').getByText('订单', { exact: true })).toBeVisible()

    // 旧格式画布备份：保持原有导入能力，成功后关闭弹窗
    const legacyBackup = {
      ontology: {
        id: 'local-canvas-backup',
        name: '本地画布备份',
        version: 'v1',
        objectTypes: [],
        linkTypes: [],
        actions: [],
        functions: [],
      },
      nodes: [],
      edges: [],
    }
    await dialog.locator('textarea').fill(JSON.stringify(legacyBackup))
    await dialog.getByRole('button', { name: '导入', exact: true }).click()
    await expect(dialog).toHaveCount(0)
  })
})
