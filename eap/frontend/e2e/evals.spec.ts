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
    // 输入改选择（M49-E1）：生产/影子 agent 由自由 Input 改为 Select（选项来自 Agent Registry）
    const dialog = page.locator('[role="dialog"]')
    await dialog.locator('div:has(> label:text-is("生产 agent")) select').selectOption('faq-agent')
    await dialog.locator('div:has(> label:text-is("影子 agent（候选）")) select').selectOption('canvas-demo')
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
    // 页面上有两个表（配置表 + 运行表）：第二个才是配对运行。
    // 运行数据异步加载，未到位时 tbody 先渲染空态行（无 code 元素）→ 等真实数据行再点击，避免竞态
    const runRow = page.locator('table').nth(1).locator('tbody tr:has(code)').first()
    await expect(runRow).toBeVisible({ timeout: 10_000 })
    await runRow.click()
    await expect(page.getByText('主侧输出')).toBeVisible({ timeout: 10_000 })
    await expect(page.getByText('影侧输出')).toBeVisible()
    await page.keyboard.press('Escape')
  })

  test('清理：删除影子配置', async ({ page }) => {
    await login(page)
    await page.goto('/evals')
    await page.getByRole('tab', { name: '影子流量' }).click()
    await page.getByText(CFG).first().click()
    // 删除按钮按行定位（dev 库可能存在多个历史配置）
    await page.locator('table').first().locator('tbody tr', { hasText: CFG })
      .getByRole('button', { name: '删除' }).click()
    // M51-B：window.confirm → ConfirmDialog，点击对话框内「删除」确认
    const confirm = page.locator('[role="dialog"]')
    await expect(confirm.getByText(`删除影子配置「${CFG}」？`)).toBeVisible({ timeout: 10_000 })
    await confirm.getByRole('button', { name: '删除', exact: true }).click()
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
    // 输入改选择（M49-E1）：task_id 由自由 Input 改为 Select（仅列 agent.invoke/agent.hitl
    // 任务，选项异步加载 → 先等目标任务 option 出现再选）
    const taskSelect = page.locator('div:has(> label:text("从任务抽样")) select')
    // option 在收起的 select 内视为 hidden → 只能等 attached（不能等 visible）
    await taskSelect.locator(`option[value="${taskId}"]`).waitFor({ state: 'attached', timeout: 15_000 })
    await taskSelect.selectOption(taskId)
    await page.getByRole('button', { name: '抽样' }).click()
    // 队列出现待评审样本
    await expect(page.getByText('待评审').first()).toBeVisible({ timeout: 10_000 })
  })

  test('评审对话框：三维评分 + 备注 → 已评审回显', async ({ page }) => {
    await login(page)
    await page.goto('/evals')
    await page.getByRole('tab', { name: '人工抽检' }).click()
    // 队列按 id 倒序：首个数据行 = 上一步刚抽样的样本。列表异步加载，未到位时 tbody
    // 先渲染空态行 → 用 :has(code) 锁定真实数据行（样本行含 <code>#id</code>），避免竞态
    const firstRow = page.locator('table tbody tr:has(code)').first()
    await expect(firstRow).toBeVisible({ timeout: 10_000 })
    await firstRow.click()
    await expect(page.getByText('评分（1-5，至少一项）')).toBeVisible({ timeout: 10_000 })
    // 三个维度评分（M49-E1：number Input → Select 1-5，按标签定位）
    for (const dim of ['正确性', '相关性', '格式']) {
      await page.locator(`div:has(> p:text-is("${dim}")) select`).selectOption('4')
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
