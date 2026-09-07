import { test, expect } from '@playwright/test'

import {
  STACK_ADMIN_PASSWORD,
  STACK_ADMIN_USERNAME,
} from './support/stack-credentials'

async function login(page: any) {
  await page.goto('/#/login')
  await page.getByLabel('用户名', { exact: true }).fill(STACK_ADMIN_USERNAME)
  await page.getByLabel('密码', { exact: true }).fill(STACK_ADMIN_PASSWORD)
  await page.click('button[type="submit"]')
  await page.waitForURL('**/#/super-assistant')
}

test.describe('Settings Page', () => {
  test.beforeEach(async ({ page }) => {
    await login(page)
    await page.goto('/#/settings')
  })

  test('defaults to the domain settings module', async ({ page }) => {
    await expect(page).toHaveURL(/\/#\/settings\/domains$/)
    await expect(page.getByRole('heading', { name: '领域设置' })).toBeVisible()
  })

  test('keeps the remaining settings navigation', async ({ page }) => {
    const navigation = page.getByRole('navigation')
    await expect(navigation.getByText('领域设置', { exact: true })).toBeVisible()
    await expect(navigation.getByText('运行监控', { exact: true })).toBeVisible()
  })

  test('does not expose retired settings entries', async ({ page }) => {
    const retired = ['用户管理', '规则设置', '提示词模板', '开放接口', 'MinIO 存储', '工作流配置', '智能体配置']
    for (const label of retired) {
      await expect(page.getByText(label, { exact: true })).toHaveCount(0)
    }
  })

  test('legacy settings deep links resolve to domain settings', async ({ page }) => {
    const retired = ['users', 'extraction', 'rules', 'prompts', 'open-interfaces', 'minio', 'workflows', 'agents']
    for (const tab of retired) {
      await page.goto(`/#/settings/${tab}`)
      await expect(page).toHaveURL(/\/#\/settings\/domains$/)
    }
  })
})
