import { expect, test } from '@playwright/test'

/**
 * M46-A 评测中心三页签 E2E：影子流量全链路（配置→API 调用触发→配对运行→报表）
 * 与人工抽检全链路（任务抽样→评分→报表）。
 * 前置：后端 127.0.0.1:8300（dev 库 + mock 模型）+ 前端 dev。
 * 隔离纪律：自建配置/策略用唯一名，用例结束前删除/停用（dev 库共享）。
 */

const API = 'http://127.0.0.1:8300'
const AUTH = { Authorization: 'Bearer dev-key-1', 'Content-Type': 'application/json' }

async function login(page: import('@playwright/test').Page) {
  await page.goto('/login')
  await page.fill('input[type="password"]', 'dev-key-1')
  await page.getByRole('button', { name: '进入控制台' }).click()
  await page.waitForURL('**/agents', { timeout: 15_000 })
}

test.describe.serial('评测中心·影子流量', () => {
  const CFG = `e2e-shadow-${Date.now().toString(36)}`

  test('创建影子配置（生产→影子）', async ({ page }) => {
    await login(page)
    await page.goto('/evals')
    await page.getByRole('tab', { name: '影子流量' }).click()
    await page.getByRole('button', { name: '新建影子配置' }).click()
    await page.getByPlaceholder('order-shadow-v2').fill(CFG)
    await page.getByPlaceholder('order-agent', { exact: true }).fill('faq-agent')
    await page.getByPlaceholder('order-agent-v2').fill('canvas-demo')
    await page.getByRole('button', { name: '创建' }).click()
    // 表格出现新配置
    await expect(page.getByText(CFG).first()).toBeVisible({ timeout: 10_000 })
  })

  test('API 调用生产 agent → 配对运行落库 → 控制台报表可见', async ({ page, request }) => {
    // 经 API 触发一次生产调用（影子分流在调用完成点发生）
    const inv = await request.post(`${API}/api/v1/agents/faq-agent/invocations`, {
      headers: AUTH, data: { input: `影子流量 E2E ${CFG}` },
    })
    expect(inv.ok()).toBeTruthy()
    await login(page)
    await page.goto('/evals')
    await page.getByRole('tab', { name: '影子流量' }).click()
    await page.getByText(CFG).first().click()
    // 影子运行表出现成功记录（mock 确定性，抽1 命中即 1 条）
    const okBadge = page.getByRole('button', { name: '查看报表' })
    await expect(okBadge).toBeVisible({ timeout: 10_000 })
    await okBadge.click()
    // 报表：影子运行 ≥1、成功、一致率 100%（两侧同为 mock 回声）
    await expect(page.getByText('影子运行').first()).toBeVisible({ timeout: 10_000 })
    await expect(page.getByText('100.0%').first()).toBeVisible({ timeout: 10_000 })
  })

  test('运行详情对话框展示主/影两侧输出', async ({ page }) => {
    await login(page)
    await page.goto('/evals')
    await page.getByRole('tab', { name: '影子流量' }).click()
    await page.getByText(CFG).first().click()
    // 页面上有两个表（配置表 + 运行表）：第二个才是配对运行
    await page.locator('table').nth(1).locator('tbody tr').first().click()
    await expect(page.getByText('主侧输出')).toBeVisible({ timeout: 10_000 })
    await expect(page.getByText('影侧输出')).toBeVisible()
    await page.keyboard.press('Escape')
  })

  test('清理：删除影子配置', async ({ page }) => {
    await login(page)
    await page.goto('/evals')
    await page.getByRole('tab', { name: '影子流量' }).click()
    await page.getByText(CFG).first().click()
    // 删除按钮按行定位（dev 库可能存在多个历史配置）；confirm 确认框自动接受
    page.once('dialog', d => d.accept())
    await page.locator('table').first().locator('tbody tr', { hasText: CFG })
      .getByRole('button', { name: '删除' }).click()
    await expect(page.getByText(CFG)).toHaveCount(0, { timeout: 10_000 })
  })
})

test.describe.serial('评测中心·人工抽检', () => {
  let taskId = ''

  test('API 提交 agent.invoke 任务到终态 → 控制台抽样入队', async ({ page, request }) => {
    const sub = await request.post(`${API}/api/v1/tasks`, {
      headers: AUTH, data: { type: 'agent.invoke', payload: { agent: 'faq-agent', input: '抽检 E2E 输入' } },
    })
    taskId = (await sub.json()).task_id
    // 轮询到 COMPLETED
    for (let i = 0; i < 40; i++) {
      const t = await request.get(`${API}/api/v1/tasks/${taskId}`, { headers: AUTH })
      if ((await t.json()).state === 'COMPLETED') break
      await new Promise(res => setTimeout(res, 300))
    }
    await login(page)
    await page.goto('/evals')
    await page.getByRole('tab', { name: '人工抽检' }).click()
    await page.getByPlaceholder('agent.invoke/agent.hitl 任务 id').fill(taskId)
    await page.getByRole('button', { name: '抽样' }).click()
    // 队列出现待评审样本
    await expect(page.getByText('待评审').first()).toBeVisible({ timeout: 10_000 })
  })

  test('评审对话框：三维评分 + 备注 → 已评审回显', async ({ page }) => {
    await login(page)
    await page.goto('/evals')
    await page.getByRole('tab', { name: '人工抽检' }).click()
    // 队列按 id 倒序：首行 = 上一步刚抽样的样本（getByText('待评审') 会误中过滤下拉的 option）
    await page.locator('table tbody tr').first().click()
    await expect(page.getByText('评分（1-5，至少一项）')).toBeVisible({ timeout: 10_000 })
    // 三个维度输入（按标签定位）
    for (const dim of ['正确性', '相关性', '格式']) {
      await page.locator(`div:has(> p:text-is("${dim}")) input[type="number"]`).fill('4')
    }
    await page.getByPlaceholder('评审备注（可选）').fill('E2E 自动评审备注')
    await page.getByRole('button', { name: '提交评审' }).click()
    // 首行（刚评审的样本）状态徽章变已评审（getByText 会误中过滤下拉的隐藏 option）
    await expect(page.locator('table tbody tr').first()).toContainText('已评审', { timeout: 10_000 })
  })

  test('抽检报表聚合可见', async ({ page }) => {
    await login(page)
    await page.goto('/evals')
    await page.getByRole('tab', { name: '人工抽检' }).click()
    await expect(page.getByText('抽检报表')).toBeVisible({ timeout: 10_000 })
    await expect(page.getByText('好评率').first()).toBeVisible()
  })
})
