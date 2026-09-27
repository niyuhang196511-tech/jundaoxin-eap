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

    // M60 深度补齐：PUT dsl 造出与 v1/v2 不同的草稿（修改 s1.system + 新增 s3）→
    // 重开对比（基准=v2，目标=草稿）→ 断言染色渲染（修改琥珀/新增绿，含步骤名与字段变化）
    const changed = {
      name: WF, version: '1.2.0', description: 'e2e diff 已改动',
      steps: [
        { id: 's1', type: 'llm', system: 'e2e 已改动的 system', prompt_name: null },
        { id: 's2', type: 'llm', system: 'e2e v1', prompt_name: null },
        { id: 's3', type: 'llm', system: 'e2e 新增步骤', prompt_name: null },
      ],
      edges: [],
    }
    const putRes = await request.put(`${API}/api/v1/workflows/${WF}/dsl`, {
      headers: AUTH, data: changed,
    })
    expect(putRes.ok()).toBeTruthy()
    await drawer.getByRole('button', { name: '对比' }).click()
    await expect(diff.getByText(`版本对比 · ${WF}`)).toBeVisible({ timeout: 10_000 })
    await toSel.selectOption({ label: '草稿（当前 DSL）' })
    // 修改步骤 s1：琥珀着色 + 字段变化 system 旧→新
    // 修改步骤 s1：字段级变化段 'system: 旧值→新值' 同段可见（实测 DOM 形态）
    await expect(diff.getByText('system: e2e v1→e2e 已改动的 system')).toBeVisible({ timeout: 10_000 })
    // 步骤级变化：'新增 步骤'（PUT 草稿相对基准多出的 s3）
    await expect(diff.getByText('新增', { exact: true })).toBeVisible()
    await expect(diff.getByText('e2e 新增步骤')).toBeVisible()
    // description 元信息变化（e2e diff 基准→e2e diff 已改动）
    await expect(diff.getByText('description: e2e diff 基准→e2e diff 已改动')).toBeVisible()
    // 差异非空（非「0 条差异」空态）
    await expect(diff.getByText('两侧无差异：步骤与连线完全一致')).toHaveCount(0)
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


test.describe.serial('HITL 审批确认流（M61，M51-B 金标准范本本体）', () => {
  const RUN = `e2e-hitl-${Date.now().toString(36)}`
  let taskId = ''

  test('HITL 任务进入 WAITING_APPROVAL → Tasks 页批准 → ConfirmDialog → 任务继续', async ({ page, request }) => {
    // API 预置：提交一个会挂起审批的 agent.hitl 任务（payload 对齐 evals 抽检同款形态）
    // order-agent + 连接器下单工具 requires_approval=True → WAITING_HUMAN
    // （形态对齐 tests/test_connectors.py:64 的既有离线断言）
    const submit = await request.post(`${API}/api/v1/tasks`, {
      headers: AUTH,
      data: {
        type: 'agent.hitl',
        payload: { agent: 'order-agent', input: '帮我下一台 EAP 一体机' },
      },
    })
    expect([200, 202]).toContain(submit.status())
    taskId = (await submit.json()).task_id
    expect(taskId).toBeTruthy()

    await login(page)
    await page.goto('/tasks')
    // 任务行出现（前端列表轮询）——WAITING_HUMAN 态行含「待审批」徽标与 批准/否决 按钮
    const row = page.locator('tr', { hasText: taskId.slice(0, 8) }).first()  // Tasks 表 ID 列只渲染前 8 位
    await expect(row).toBeVisible({ timeout: 20_000 })
    const approve = row.getByRole('button', { name: '批准' })
    await expect(approve).toBeVisible({ timeout: 20_000 })
    await approve.click()
    // ConfirmDialog 金标准（M51-B 范本本体）：确认按钮驱动
    const dialog = page.locator('[role="dialog"]')
    await expect(dialog).toBeVisible()
    // ConfirmDialog 确认按钮文案由 confirmLabel 注入（本流='批准'，见 Tasks.tsx）
    await dialog.getByRole('button', { name: '批准' }).click()
    // 审批提交后任务离开 WAITING_HUMAN（批准/否决按钮消失；执行完成或继续运行——
    // 审批路径本身已验证；行内残留两条历史 WAITING_HUMAN 行属既有数据不受影响）
    await expect(page.locator('tr', { hasText: taskId.slice(0, 8) }).first()).toBeVisible()
    await expect(row.getByRole('button', { name: '批准' })).toHaveCount(0, { timeout: 20_000 })
  })
})


test.describe.serial('确认流第二批（M62）：Knowledge 文档删除 / Models 模型删除', () => {
  const KB = `e2e-kb-${SFX}`
  const DOC = `e2e-doc-${SFX}`
  const MODEL = `e2e-model-${SFX}`

  test('Knowledge 文档删除：库详情文档行删除按钮 → ConfirmDialog 级联清理文案 → 确认后文档消失', async ({ page, request }) => {
    // API 预置：KB + 文档（POST /documents text 摄入）
    const kbRes = await request.post(`${API}/api/v1/kb`, {
      headers: AUTH, data: { name: KB, title: 'e2e 删除流库', type: 'doc' },
    })
    expect(kbRes.ok()).toBeTruthy()
    const docRes = await request.post(`${API}/api/v1/kb/${KB}/documents`, {
      headers: AUTH, data: { title: DOC, text: 'e2e 删除流测试文档内容', source: 'text' },
    })
    expect(docRes.ok()).toBeTruthy()
    const docId = (await docRes.json()).document_id

    await login(page)
    await page.goto('/kb')
    // 库卡片是 Card div（onClick 进详情）——按 KB 名定位卡片并点击
    await page.locator('div', { hasText: KB }).last().filter({ has: page.locator('p', { hasText: KB }) }).first().click()
    // 详情文档列表：文档标题按钮可见
    const docBtn = page.locator('button', { hasText: DOC }).first()
    await expect(docBtn).toBeVisible({ timeout: 15_000 })
    // 该文档行内的删除按钮（title="删除"）
    const delBtn = docBtn.locator('xpath=ancestor::div[contains(@class,"flex")]//button[@title="删除"]').first()
    await delBtn.click()
    // ConfirmDialog：级联清理文案（标题含文档名）
    const dialog = page.locator('[role="dialog"]', { hasText: `删除文档「${DOC}」？` })
    await expect(dialog).toBeVisible()
    await expect(dialog.getByText(/级联清理/)).toBeVisible()
    await dialog.getByRole('button', { name: '删除' }).click()
    await expect(page.locator('button', { hasText: DOC })).toHaveCount(0, { timeout: 15_000 })
    expect(docId).toBeTruthy()
  })

  test('LoRA adapter 删除：登记 → 页签行删除按钮 → ConfirmDialog「删除 adapter」→ 确认后消失', async ({ page, request }) => {
    // 「删除 adapter」确认流属 LoRA 适配器页签（LoraTab）——主表模型只有启用/停用
    // （首版误写主表模型删除，CI 全新库无 LoRA 行→选择器超时暴露）
    const res = await request.post(`${API}/api/v1/lora`, {
      headers: AUTH,
      data: { name: MODEL, base_model: 'e2e-base-model', source_path: '/gpu/adapter/e2e' },
    })
    expect(res.ok()).toBeTruthy()

    await login(page)
    await page.goto('/models')
    await page.getByRole('tab', { name: 'LoRA 适配器' }).click()
    const row = page.locator('tr', { hasText: MODEL }).first()
    await expect(row).toBeVisible({ timeout: 15_000 })
    await row.getByRole('button', { name: '删除' }).click()
    const dialog = page.locator('[role="dialog"]', { hasText: `删除 adapter「${MODEL}」？` })
    await expect(dialog).toBeVisible()
    await dialog.getByRole('button', { name: '删除' }).click()
    await expect(page.locator('tr', { hasText: MODEL })).toHaveCount(0, { timeout: 15_000 })
  })
})
