import { expect, test } from '@playwright/test'

/**
 * M59 e2e 覆盖补强：M52~M55 四条用户流（连接器 PATCH 编辑 / 连接器 DELETE 确认 /
 * 工作流版本 diff 弹窗 / env-protection 428→ConfirmDialog 重发）。
 * 前置：后端 127.0.0.1:8300 + 前端 dev。对全新种子库自包含（只依赖 lifespan 种子的
 * dev-key-1 与 mock 模型），数据一律 API 直插 + 唯一名（uuid 后缀）保证可重入。
 * 隔离纪律：连接器用例结束即删除；策略/工作流无删除端点 → 结束前停用（dev 库共享）。
 */

const API = 'http://127.0.0.1:8300'
const AUTH = { Authorization: 'Bearer dev-key-1', 'Content-Type': 'application/json' }
const SFX = Date.now().toString(36)

async function login(page: import('@playwright/test').Page) {
  await page.goto('/login')
  await page.fill('input[type="password"]', 'dev-key-1')
  await page.getByRole('button', { name: '进入控制台' }).click()
  await page.waitForURL('**/agents', { timeout: 15_000 })
}

test.describe.serial('连接器 PATCH 编辑与 DELETE 确认（M52-C/M53-B）', () => {
  const NAME = `e2e-conn-${SFX}`
  const TRIG = `e2e-trig-${SFX}`

  test('PATCH 编辑流：对话框回填（name/kind 不可变、secret 留空保留）→ 改描述/Base URL → 列表刷新', async ({ page, request }) => {
    // API 预置连接器（rest kind 后端强制 http(s) base_url，EAP-7002）
    const res = await request.post(`${API}/api/v1/connectors`, {
      headers: AUTH,
      data: {
        name: NAME, kind: 'rest', description: 'e2e 旧描述',
        base_url: 'http://127.0.0.1:8300/health', api_key: 'e2e-old-secret',
        endpoints: [{ name: 'e2e.ep', tool_name: `e2e.${NAME}.query`,
          method: 'GET', path: '/', requires_approval: false }],
      },
    })
    expect(res.ok()).toBeTruthy()

    await login(page)
    await page.goto('/conn')
    await page.getByRole('tab', { name: '连接器' }).click()
    const row = page.locator('table tbody tr', { hasText: NAME })
    await expect(row).toBeVisible({ timeout: 10_000 })

    // 操作列「编辑」→ 双模式对话框回填详情
    await row.getByRole('button', { name: '编辑' }).click()
    const dialog = page.locator('[role="dialog"]')
    await expect(dialog.getByText(`编辑连接器 ${NAME}`)).toBeVisible({ timeout: 10_000 })
    await expect(dialog.getByText('name/kind 不可变')).toBeVisible()
    // 回填：name/kind 不可变（disabled）
    const nameInput = dialog.locator('input').first()
    await expect(nameInput).toHaveValue(NAME)
    await expect(nameInput).toBeDisabled()
    const kindSelect = dialog.locator('select').first()
    await expect(kindSelect).toHaveValue('rest')
    await expect(kindSelect).toBeDisabled()
    // secret 不回显：留空 = 保留原值（placeholder 如实提示）
    const apiKeyInput = dialog.locator('input[type="password"]').first()
    await expect(apiKeyInput).toHaveValue('')
    await expect(apiKeyInput).toHaveAttribute('placeholder', '已保存（留空保留）')

    // 改描述 + Base URL → 保存
    await dialog.locator('div:has(> label:text-is("描述")) input').fill('e2e 新描述-已改')
    await dialog.locator('div:has(> label:has-text("Base URL")) input').fill('http://127.0.0.1:9/patched')
    await dialog.getByRole('button', { name: '保存', exact: true }).click()
    // 列表刷新：新 base_url 可见；连接目标变更 → 状态重置 pending（M52-C）
    await expect(dialog).toHaveCount(0, { timeout: 10_000 })
    await expect(row.getByText('http://127.0.0.1:9/patched')).toBeVisible({ timeout: 10_000 })
    await expect(row.getByText('pending', { exact: true })).toBeVisible()
    // 复核：重新打开编辑对话框 → 描述已持久化、密钥仍保留（secret 三态之「留空保留」）
    await row.getByRole('button', { name: '编辑' }).click()
    await expect(dialog.getByText(`编辑连接器 ${NAME}`)).toBeVisible({ timeout: 10_000 })
    await expect(dialog.locator('div:has(> label:text-is("描述")) input')).toHaveValue('e2e 新描述-已改')
    await expect(dialog.locator('input[type="password"]').first())
      .toHaveAttribute('placeholder', '已保存（留空保留）')
    await dialog.getByRole('button', { name: '取消', exact: true }).click()
  })

  test('DELETE 确认流：ConfirmDialog 后果文案 → 409 引用详情 toast → 解除引用 → 确认删除行消失', async ({ page, request }) => {
    // 预置触发器规则引用连接器（cron 源，删除连接器时后端 409 阻断）
    const res = await request.post(`${API}/api/v1/triggers`, {
      headers: AUTH,
      data: { name: TRIG, source: 'cron', cron: '0 9 * * *',
        target_type: 'connector', target_name: NAME },
    })
    expect(res.ok()).toBeTruthy()
    const trigId = (await res.json()).id as number

    await login(page)
    await page.goto('/conn')
    await page.getByRole('tab', { name: '连接器' }).click()
    const row = page.locator('table tbody tr', { hasText: NAME })
    await expect(row).toBeVisible({ timeout: 10_000 })

    // 操作列「删除」→ ConfirmDialog（后果说明含触发器引用 409 语义）
    await row.getByRole('button', { name: '删除' }).click()
    const confirm = page.locator('[role="dialog"]')
    await expect(confirm.getByText(`删除连接器「${NAME}」？`)).toBeVisible({ timeout: 10_000 })
    await expect(confirm.getByText(/触发器规则引用/)).toBeVisible()
    await confirm.getByRole('button', { name: '删除', exact: true }).click()
    // 409 EAP-2002：toast 透传后端引用详情（列出引用规则名）
    await expect(page.getByText(new RegExp(`EAP-2002.*${TRIG}`)).first())
      .toBeVisible({ timeout: 10_000 })
    // 解除引用（API 删除触发器规则）→ 对话框内再次确认 → 删除成功、行消失
    await request.delete(`${API}/api/v1/triggers/${trigId}`, { headers: AUTH })
    await confirm.getByRole('button', { name: '删除', exact: true }).click()
    await expect(page.getByText('连接器已删除').first()).toBeVisible({ timeout: 10_000 })
    await expect(row).toHaveCount(0, { timeout: 10_000 })
  })
})

test.describe.serial('工作流版本 diff 与 env-protection 428（M54-B/M55-E）', () => {
  const WF = `e2e-wf-${SFX}`
  const POLICY = `e2e-envprot-${SFX}`

  test('版本抽屉「对比」→ diff 弹窗渲染（版本/草稿选区 + 差异摘要）', async ({ page, request }) => {
    // API 预置工作流 + 草稿 v1。后端无 DSL 更新端点（POST 同名 409，画布「保存」仅限新建），
    // 故两侧 DSL 恒同 → diff 断言按「弹窗渲染 + 0 差异空态」口径（M59 允许的降级）。
    const res = await request.post(`${API}/api/v1/workflows`, {
      headers: AUTH,
      data: {
        name: WF, version: '1.0.0', description: 'e2e diff 基准',
        steps: [{ id: 's1', type: 'llm', system: 'e2e v1' }], edges: [],
      },
    })
    expect(res.ok()).toBeTruthy()
    const draft = await request.post(`${API}/api/v1/workflows/${WF}/versions`, {
      headers: AUTH, data: { note: 'e2e v1' },
    })
    expect(draft.ok()).toBeTruthy()

    await login(page)
    await page.goto('/canvas')
    // 目录点开工作流 DSL → 画布载入后「版本」按钮出现（dsl 非空才渲染）
    await page.getByRole('button', { name: WF }).click()
    const versionsBtn = page.locator('button[title="版本管理（发布/回滚）"]')
    await expect(versionsBtn).toBeVisible({ timeout: 15_000 })
    await versionsBtn.click()
    const drawer = page.locator('[role="dialog"]', { hasText: `版本 · ${WF}` })
    await expect(drawer.getByText('v1', { exact: true })).toBeVisible({ timeout: 10_000 })
    await expect(drawer.getByText('e2e v1')).toBeVisible()

    // 存草稿 → v2（同 DSL）；「对比」→ diff 弹窗（默认基准=最新版本，目标=草稿）
    await drawer.getByRole('button', { name: '存草稿' }).click()
    await expect(drawer.getByText('v2', { exact: true })).toBeVisible({ timeout: 10_000 })
    await drawer.getByRole('button', { name: '对比' }).click()
    const diff = page.locator('[role="dialog"]', { hasText: '版本对比' })
    await expect(diff.getByText(`版本对比 · ${WF}`)).toBeVisible({ timeout: 10_000 })
    const fromSel = diff.locator('div:has(> label:text-is("基准版本（旧）")) select')
    const toSel = diff.locator('div:has(> label:text-is("对比目标（新）")) select')
    // option 在收起的 select 内视为 hidden → 只能等 attached（对齐 evals 先例）；
    // UI 存草稿不带 note → v2 的选项/摘要文案是「v2（无说明）」，v1 带 API 预置 note「e2e v1」
    await expect(fromSel.locator('option', { hasText: 'v1（e2e v1）' })).toBeAttached({ timeout: 10_000 })
    await expect(fromSel.locator('option', { hasText: 'v2（' })).toBeAttached()
    await expect(toSel.locator('option', { hasText: '草稿（当前 DSL）' })).toBeAttached()
    // 摘要条 + 差异空态（两版 DSL 与服务端草稿一致 → 0 条差异）
    await expect(diff.getByText(/0 条差异/)).toBeVisible({ timeout: 10_000 })
    await expect(diff.getByText('两侧无差异：步骤与连线完全一致')).toBeVisible()
    // 切换基准到 v1 → 版本对版本（摘要条回显两侧版本元信息）
    await fromSel.selectOption({ label: 'v1（e2e v1）' })
    await expect(diff.getByText(/v1（e2e v1）.*→/)).toBeVisible({ timeout: 10_000 })
    await page.keyboard.press('Escape')
  })

  test('env-protection：策略创建对话框专用子表单 → PROD 发布 428 → 确认重发成功', async ({ page, request }) => {
    // —— 前半：/gov 策略对话框选中 env-protection → 专用子表单（模板预填规则）→ 创建
    await login(page)
    await page.goto('/gov')
    await page.getByRole('tab', { name: '策略' }).click()
    await page.getByRole('button', { name: '创建策略' }).click()
    const dialog = page.locator('[role="dialog"]')
    await dialog.locator('input').first().fill(POLICY)
    await dialog.locator('div:has(> label:text-is("类型")) select').selectOption('env-protection')
    // 专用子表单：env Select + actors ChipPicker + require_confirm Checkbox
    // 模板预填 规则 1 = prod + ['api-key'] + require_confirm=true
    await expect(dialog.getByText('规则 1')).toBeVisible()
    await expect(dialog.locator('div:has(> label:has-text("rules[0].env")) select')).toHaveValue('prod')
    await expect(dialog.getByRole('button', { name: 'api-key', exact: true })).toBeVisible()
    await expect(dialog.locator('input[type="checkbox"]')).toBeChecked()
    await dialog.getByRole('button', { name: '创建', exact: true }).click()
    const prow = page.locator('table tbody tr', { hasText: POLICY })
    await expect(prow).toBeVisible({ timeout: 10_000 })

    // —— 后半：画布版本抽屉发布 v1 到 PROD → 428 EAP-3011 → ConfirmDialog 确认重发
    await page.goto('/canvas')
    await page.getByRole('button', { name: WF }).click()
    const versionsBtn = page.locator('button[title="版本管理（发布/回滚）"]')
    await expect(versionsBtn).toBeVisible({ timeout: 15_000 })
    await versionsBtn.click()
    const drawer = page.locator('[role="dialog"]', { hasText: `版本 · ${WF}` })
    await expect(drawer.getByText('v1', { exact: true })).toBeVisible({ timeout: 10_000 })
    // 上条用例存过 v2 草稿 → 抽屉内有两行「发布到 prod」；按 v1 行唯一 note 圈定目标行
    const v1row = drawer.locator('div.rounded-lg.border', { hasText: 'e2e v1' })
    // 操作环境默认 prod → v1 草稿行「发布到 prod」→ 命中 require_confirm 规则 428
    await v1row.getByRole('button', { name: '发布到 prod' }).click()
    const confirm = page.locator('[role="dialog"]', { hasText: '环境保护确认' })
    await expect(confirm).toBeVisible({ timeout: 10_000 })
    await expect(confirm.getByText(/EAP-3011/)).toBeVisible()
    // 显式确认 → 带 confirm=true 重发 → 成功
    await confirm.getByRole('button', { name: '确认执行' }).click()
    await expect(page.getByText(/已发布到 prod/).first()).toBeVisible({ timeout: 10_000 })
    await expect(v1row.getByText('已发布', { exact: true })).toBeVisible({ timeout: 10_000 })
    await expect(v1row.getByText('生产生效', { exact: true })).toBeVisible()

    // 隔离纪律：策略停用（env-protection 跨租户生效，不留启用态残留）；
    // 工作流停用（避免残留 enabled 智能体，对齐 tests/test_workflow_versions.py 收尾）
    await request.post(`${API}/api/v1/policies/${POLICY}/enabled?enabled=false`, { headers: AUTH })
    await request.delete(`${API}/api/v1/workflows/${WF}`, { headers: AUTH })
  })
})
