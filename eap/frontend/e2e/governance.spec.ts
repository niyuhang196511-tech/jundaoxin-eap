import { expect, test } from '@playwright/test'

/**
 * M46-C 治理政策模板 E2E：九种 kind 下拉齐全，eval-gate 模板可走通创建→列表可见。
 * 前置：后端 127.0.0.1:8300（dev 库）+ 前端 dev。
 * 隔离纪律：自建策略唯一名，用例结束前停用（dev 库共享）。
 */

async function login(page: import('@playwright/test').Page) {
  await page.goto('/login')
  await page.fill('input[type="password"]', 'dev-key-1')
  await page.getByRole('button', { name: '进入控制台' }).click()
  await page.waitForURL('**/agents', { timeout: 15_000 })
}

const NAME = `e2e-gate-${Date.now().toString(36)}`

test('kind 下拉包含全部九种（含 M30/M33/M42-B 新增三种）', async ({ page }) => {
  await login(page)
  await page.goto('/gov')
  await page.getByRole('tab', { name: '策略' }).click()
  await page.getByRole('button', { name: '创建策略' }).click()
  for (const kind of ['tool-sandbox', 'a2a-delegate-allowlist', 'eval-gate',
    'tool-allowlist', 'tool-risk-approval', 'agent-allowlist']) {
    await expect(page.locator('select option', { hasText: kind }).first()).toBeAttached()
  }
})

test('eval-gate 模板创建 → 列表可见 → 用例结束停用（隔离纪律）', async ({ page }) => {
  await login(page)
  await page.goto('/gov')
  await page.getByRole('tab', { name: '策略' }).click()
  await page.getByRole('button', { name: '创建策略' }).click()
  const dialog = page.locator('[role="dialog"]')
  await dialog.locator('input').first().fill(NAME)
  await dialog.locator('select').selectOption('eval-gate')
  // M50-B1：eval-gate 改结构化子表单——模板预填 models=[mock-llm] / require_eval=true / min_pass_rate=0.8
  await expect(dialog.getByRole('button', { name: '切换 JSON 编辑' })).toBeVisible()
  await expect(dialog.getByRole('button', { name: 'mock-llm', exact: true })).toBeVisible()
  await expect(dialog.locator('input[type="checkbox"]')).toBeChecked()
  await expect(dialog.locator('input[type="number"]').last()).toHaveValue('0.8')
  await dialog.getByRole('button', { name: '创建', exact: true }).click()
  await expect(page.getByText(NAME).first()).toBeVisible({ timeout: 10_000 })
  // 隔离纪律：eval-gate 钉住 mock-llm 会拦死 dev 库整条 chat 链，断言后立刻停用
  const row = page.locator('table tbody tr', { hasText: NAME })
  await row.getByRole('button', { name: '停用' }).click()
  await expect(row.getByRole('button', { name: '启用' })).toBeVisible({ timeout: 10_000 })
})
