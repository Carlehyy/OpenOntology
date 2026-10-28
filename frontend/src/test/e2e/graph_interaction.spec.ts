import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

import {
  STACK_ADMIN_PASSWORD,
  STACK_ADMIN_USERNAME,
} from './support/stack-credentials'

const API = (
  process.env.PLAYWRIGHT_API_URL
  || process.env.E2E_API_BASE
  || 'http://localhost:8000'
).replace(/\/+$/, '')

async function login(page: Page): Promise<string> {
  await page.goto('/#/login')
  await page.getByLabel('用户名', { exact: true }).fill(STACK_ADMIN_USERNAME)
  await page.getByLabel('密码', { exact: true }).fill(STACK_ADMIN_PASSWORD)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.waitForURL('**/#/super-assistant')
  const token = await page.evaluate(() => localStorage.getItem('token'))
  expect(token).toBeTruthy()
  return token!
}

async function createOntology(request: APIRequestContext, token: string): Promise<string> {
  const response = await request.post(`${API}/api/v1/ontologies`, {
    headers: { Authorization: `Bearer ${token}` },
    data: {
      name: `图谱测试-${Date.now().toString(36)}`,
      domain: '供应链',
      description: '验证当前发布图谱的只读空态',
    },
  })
  expect(response.ok(), await response.text()).toBeTruthy()
  const body = await response.json()
  return (body.data ?? body).id
}

async function openCurrentReleaseGraph(page: Page, ontologyId: string) {
  await page.goto(`/#/ontologies/${ontologyId}`)
  await page.getByRole('button', { name: '查看本体图谱', exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/ontologies/${ontologyId}/graph$`))
  await expect(page.getByTestId('graph-workspace-stage')).toContainText('当前发布 v0')
}

async function removeOntology(request: APIRequestContext, token: string, ontologyId: string) {
  const response = await request.delete(`${API}/api/v1/ontologies/${ontologyId}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  expect(response.ok(), await response.text()).toBeTruthy()
}

test.describe('Graph Tab Interaction', () => {
  test('opens the immutable empty v0 release graph from ontology detail', async ({ page, request }) => {
    const token = await login(page)
    const ontologyId = await createOntology(request, token)
    try {
      await openCurrentReleaseGraph(page, ontologyId)
      await expect(page.locator('.react-flow')).toBeVisible()
    } finally {
      await removeOntology(request, token, ontologyId)
    }
  })

  test('empty v0 reports zero object and relation definitions', async ({ page, request }) => {
    const token = await login(page)
    const ontologyId = await createOntology(request, token)
    try {
      await openCurrentReleaseGraph(page, ontologyId)
      await expect(page.getByRole('button', { name: '查看对象实体，共 0 个' })).toBeVisible()
      await expect(page.getByRole('button', { name: '查看实体关系，共 0 个' })).toBeVisible()
    } finally {
      await removeOntology(request, token, ontologyId)
    }
  })

  test('current release graph is read-only and offers an explicit draft transition', async ({ page, request }) => {
    const token = await login(page)
    const ontologyId = await createOntology(request, token)
    try {
      await openCurrentReleaseGraph(page, ontologyId)
      await expect(page.getByText('当前发布', { exact: true })).toBeVisible()
      await expect(page.getByRole('button', { name: '基于此版本开始修改', exact: true })).toBeVisible()
    } finally {
      await removeOntology(request, token, ontologyId)
    }
  })
})
