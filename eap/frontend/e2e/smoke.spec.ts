import { expect, test } from '@playwright/test'

/**
 * 关键路径 E2E：登录 → 智能体对话（流式回复）→ 知识库浏览。
 * 前置：后端 127.0.0.1:8300（mock 模型离线可跑）+ 前端 dev（webServer 自动起）。
 */
test.describe.serial('关键路径', () => {
  test('登录（dev API Key）进入控制台', async ({ page }) => {
    await page.goto('/login')
    await page.fill('input[type="password"]', 'dev-key-1')
    await page.getByRole('button', { name: '进入控制台' }).click()
    await page.waitForURL('**/agents', { timeout: 15_000 })
    // 侧栏已渲染（登录态成立）
    await expect(page.getByText('EAP 控制台')).toBeVisible()
  })

  test('智能体对话：发送消息收到流式回复', async ({ page }) => {
    // 登录态注入（避免依赖上一用例的 storage）
    await page.goto('/login')
    await page.fill('input[type="password"]', 'dev-key-1')
    await page.getByRole('button', { name: '进入控制台' }).click()
    await page.waitForURL('**/agents', { timeout: 15_000 })

    await page.fill('textarea', 'E2E 冒烟测试')
    await page.keyboard.press('Enter')
    // mock 模型确定性回声
    await expect(page.getByText(/E2E 冒烟测试/).nth(1)).toBeVisible({ timeout: 30_000 })
  })

  test('知识库：打开库详情与召回测试', async ({ page }) => {
    await page.goto('/login')
    await page.fill('input[type="password"]', 'dev-key-1')
    await page.getByRole('button', { name: '进入控制台' }).click()
    await page.waitForURL('**/agents', { timeout: 15_000 })

    await page.goto('/kb')
    // 种子知识库卡片
    await expect(page.getByText('website-faq')).toBeVisible({ timeout: 15_000 })
    await page.getByText('website-faq').first().click()
    // 详情：召回测试面板
    await expect(page.getByText('召回测试').first()).toBeVisible()
    await page.fill('textarea', '如何创建知识库？')
    await page.getByRole('button', { name: '检索 top 5' }).click()
    await expect(page.getByText(/score/).first()).toBeVisible({ timeout: 15_000 })
  })
})
