import { readFileSync } from 'node:fs'
import { test, expect, type Page } from '@playwright/test'

import {
  STACK_ADMIN_PASSWORD,
  STACK_ADMIN_USERNAME,
} from './support/stack-credentials'

const API = (
  process.env.PLAYWRIGHT_API_URL
  || process.env.E2E_API_BASE
  || 'http://localhost:8000'
).replace(/\/+$/, '')

async function login(page: Page) {
  await page.goto('/#/login')
  await page.getByLabel('用户名', { exact: true }).fill(STACK_ADMIN_USERNAME)
  await page.getByLabel('密码', { exact: true }).fill(STACK_ADMIN_PASSWORD)
  await page.getByRole('button', { name: '登录' }).click()
  // 登录落地页现为超级助手（AI 工作台前台），不再是本体助手
  await page.waitForURL(/\/#\/super-assistant$/)
  const token = await page.evaluate(() => localStorage.getItem('token'))
  const domainResponse = await page.request.post(`${API}/api/v1/domains`, {
    headers: { Authorization: `Bearer ${token}` },
    data: { name: '供应链', description: '导出导入浏览器测试' },
  })
  if (![201, 409].includes(domainResponse.status())) {
    throw new Error(`测试领域初始化失败: ${domainResponse.status()}`)
  }
}

async function createOntology(page: Page): Promise<{ name: string; ontologyId: string }> {
  await page.goto('/#/ontologies')
  const name = `导出导入测试-${Date.now()}`
  await page.getByRole('button', { name: '立即创建', exact: true }).first().click()
  await page.getByLabel('本体名称', { exact: true }).fill(name)
  await page.getByRole('button', { name: '创建本体' }).click()
  await expect(page.getByText(name, { exact: true })).toBeVisible()
  await page.getByRole('button', { name, exact: true }).click()
  await page.waitForURL(/\/#\/ontologies\/[a-f0-9-]+$/)
  const ontologyId = page.url().split('/').at(-1) ?? ''
  const token = await page.evaluate(() => localStorage.getItem('token'))
  const objectTypeResponse = await page.request.post(
    `${API}/api/v2/formal/ontologies/${ontologyId}/object-types`,
    {
      headers: { Authorization: `Bearer ${token}` },
      data: {
        name: 'Order',
        displayName: '订单',
        primaryKey: 'order_no',
        properties: [{
          id: 'order-no',
          name: 'order_no',
          displayName: '订单号',
          type: 'string',
          required: true,
        }],
      },
    },
  )
  if (objectTypeResponse.status() !== 201) {
    throw new Error(`测试对象类型初始化失败: ${objectTypeResponse.status()}`)
  }
  return { name, ontologyId }
}

/** 结构画布（l1: 前缀）与全屏编辑器（无前缀）各写一个位置，模拟用户排版。
    仅对已发布本体可用（v0 快照含结构，布局校验通过）。 */
async function saveCanvasPositions(page: Page, ontologyId: string): Promise<{
  objectTypeId: string
  positions: Record<string, { x: number; y: number }>
}> {
  const token = await page.evaluate(() => localStorage.getItem('token'))
  const headers = { Authorization: `Bearer ${token}` }
  const full = await page.request.get(
    `${API}/api/v2/formal/ontologies/${ontologyId}/full`, { headers })
  const objectTypeId = (await full.json()).data.objectTypes[0].id
  const positions = {
    [objectTypeId]: { x: 88, y: 132 },
    [`l1:${objectTypeId}`]: { x: 264, y: 96 },
  }
  const layoutResponse = await page.request.put(
    `${API}/api/v2/ontologies/${ontologyId}/layout`,
    { headers, data: { positions } },
  )
  if (layoutResponse.status() !== 200) {
    throw new Error(`测试画布布局保存失败: ${layoutResponse.status()}`)
  }
  return { objectTypeId, positions }
}

test.describe('Ontology structure export and import', () => {
  test.beforeEach(async ({ page }) => {
    await login(page)
  })

  test('detail export downloads JSON directly without a format modal', async ({ page }) => {
    const { name } = await createOntology(page)
    const downloadPromise = page.waitForEvent('download')

    await page.getByRole('button', { name: '导出本体结构' }).click()
    const download = await downloadPromise

    expect(download.suggestedFilename()).toBe(`${name}_v0.json`)
    await expect(page.getByText('选择格式下载')).toHaveCount(0)
  })

  test('a downloaded package can be selected from the management page', async ({ page }, testInfo) => {
    await createOntology(page)
    // 第一步导入得到已发布本体（v0 快照含结构），模拟用户现实中的迁移起点
    const firstDownload = page.waitForEvent('download')
    await page.getByRole('button', { name: '导出本体结构' }).click()
    const bootstrapPath = testInfo.outputPath('bootstrap.json')
    await (await firstDownload).saveAs(bootstrapPath)

    await page.goto('/#/ontologies')
    await expect(page.getByRole('button', { name: '本地导入' })).toBeVisible()
    let importResponsePromise = page.waitForResponse(response => (
      response.url().includes('/api/v1/ontologies/import') && response.request().method() === 'POST'
    ))
    await page.getByLabel('选择本体结构 JSON 文件').setInputFiles(bootstrapPath)
    const firstImport = await importResponsePromise
    const firstImported = (await firstImport.json()).data
    expect(firstImport.status()).toBe(201)
    expect(firstImported.ontology.status).toBe('published')
    expect(firstImported.ontology.version).toBe('v0')
    await page.waitForURL(new RegExp(`/#/ontologies/${firstImported.ontology.id}$`))

    // 第二步在已发布本体上排版（结构画布 + 全屏编辑器各一处），导出应携带位置
    const publishedId = firstImported.ontology.id
    const { objectTypeId, positions } = await saveCanvasPositions(page, publishedId)
    const secondDownload = page.waitForEvent('download')
    await page.getByRole('button', { name: '导出本体结构' }).click()
    const packagePath = testInfo.outputPath('ontology-structure.json')
    await (await secondDownload).saveAs(packagePath)
    const exported = JSON.parse(readFileSync(packagePath, 'utf8'))
    expect(exported.canvasLayout).toEqual(positions)

    // 第三步再次本地导入：新本体沿用画布位置，key 重映射为新对象 ID
    await page.goto('/#/ontologies')
    importResponsePromise = page.waitForResponse(response => (
      response.url().includes('/api/v1/ontologies/import') && response.request().method() === 'POST'
    ))
    await page.getByLabel('选择本体结构 JSON 文件').setInputFiles(packagePath)
    const secondImport = await importResponsePromise
    const secondImported = (await secondImport.json()).data
    expect(secondImport.status()).toBe(201)
    await page.waitForURL(new RegExp(`/#/ontologies/${secondImported.ontology.id}$`))

    const token = await page.evaluate(() => localStorage.getItem('token'))
    const workspaceResponse = await page.request.get(
      `${API}/api/v2/ontologies/${secondImported.ontology.id}/current-release/workspace`,
      { headers: { Authorization: `Bearer ${token}` } },
    )
    const workspace = (await workspaceResponse.json()).data
    const newObjectTypeId = workspace.objectTypes[0].id
    expect(newObjectTypeId).not.toBe(objectTypeId)
    expect(workspace.canvasLayout).toEqual({
      [newObjectTypeId]: { x: 88, y: 132 },
      [`l1:${newObjectTypeId}`]: { x: 264, y: 96 },
    })
  })
})
