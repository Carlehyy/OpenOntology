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
      name: `详情测试-${Date.now().toString(36)}`,
      domain: '供应链',
      description: '验证当前五段式本体详情信息架构',
    },
  })
  expect(response.ok(), await response.text()).toBeTruthy()
  const body = await response.json()
  return (body.data ?? body).id
}

async function removeOntology(request: APIRequestContext, token: string, ontologyId: string) {
  const response = await request.delete(`${API}/api/v1/ontologies/${ontologyId}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  expect(response.ok(), await response.text()).toBeTruthy()
}

test.describe('Ontology Detail Page', () => {
  test('shows the overview and current release by default', async ({ page, request }) => {
    const token = await login(page)
    const ontologyId = await createOntology(request, token)
    try {
      await page.goto(`/#/ontologies/${ontologyId}`)
      await expect(page.getByRole('button', { name: '本体总览', exact: true })).toHaveAttribute('aria-pressed', 'true')
      await expect(page.getByTestId('ontology-detail-content')).toBeVisible()
      await expect(page.getByTestId('current-release-version')).toHaveText('v0')
    } finally {
      await removeOntology(request, token, ontologyId)
    }
  })

  test('switches to the published model structure', async ({ page, request }) => {
    const token = await login(page)
    const ontologyId = await createOntology(request, token)
    try {
      await page.goto(`/#/ontologies/${ontologyId}`)
      await page.getByRole('button', { name: '本体结构', exact: true }).click()
      await expect(page.getByRole('button', { name: '本体结构', exact: true })).toHaveAttribute('aria-pressed', 'true')
      await expect(page.getByText('当前发布版还没有对象实体', { exact: true })).toBeVisible()
    } finally {
      await removeOntology(request, token, ontologyId)
    }
  })

  test('exposes graph, history and JSON export actions', async ({ page, request }) => {
    const token = await login(page)
    const ontologyId = await createOntology(request, token)
    try {
      await page.goto(`/#/ontologies/${ontologyId}`)
      await expect(page.getByRole('button', { name: '查看当前发布图谱', exact: true })).toBeVisible()
      await expect(page.getByRole('button', { name: '打开数据映射工作台', exact: true })).toBeVisible()
      await expect(page.getByRole('button', { name: '查看历史版本', exact: true })).toBeVisible()
      await expect(page.getByRole('button', { name: '导出本体结构', exact: true })).toBeVisible()
    } finally {
      await removeOntology(request, token, ontologyId)
    }
  })

  test('sidebar ontology management link navigates back to the list', async ({ page, request }) => {
    const token = await login(page)
    const ontologyId = await createOntology(request, token)
    try {
      await page.goto(`/#/ontologies/${ontologyId}`)
      await page.getByRole('link', { name: '本体管理', exact: true }).click()
      await expect(page).toHaveURL(/\/#\/ontologies$/)
      await expect(page.getByRole('region', { name: '本体筛选' })).toBeVisible()
    } finally {
      await removeOntology(request, token, ontologyId)
    }
  })
})
