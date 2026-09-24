# EAP 控制台前端（eap/frontend）

Next.js（App Router）+ React + Tailwind 控制台。常用命令（本目录执行）：

```bash
pnpm dev          # 开发（端口 3000）
pnpm typecheck    # tsc --noEmit
pnpm test         # vitest 单测
pnpm build        # 生产构建
```

## i18n 规约（M50-D）

基建：`src/lib/i18n.tsx` 轻量词典（zh/en，无运行时依赖）+ `I18nProvider`，
已在 `src/app/layout.tsx` 全局挂载（locale 固定 `DEFAULT_LOCALE = 'zh'`，
无语言切换 UI——切换需求属产品决策，未做）。缺失 key 时 `t()` 原样返回，
增量翻译不阻断渲染。

规则：

1. **新增页面/组件的用户可见文案必须走 `t()`**：`const { t } = useI18n()`，
   key 按 `域.名` 命名（如 `nav.agents`、`login.title`），zh/en 词典条目同步补齐。
2. **存量硬编码中文按批次抽取**：当前 11 个视图约 4700 行硬编码中文暂不迁移，
   待产品确定语言切换需求后排批次替换为 `t('key')`。
3. 不引入 i18n 运行时库（next-intl 等）——如未来需要 SSR 语言协商/路由级
   locale，先在 docs/ 提案再动，勿直接加依赖。
