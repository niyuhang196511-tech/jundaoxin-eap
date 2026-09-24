# @eap/widget（EAP 嵌入外链 Widget 的 npm 分发形态）

EAP 嵌入外链聊天 Widget——`<eap-chat>` Web Component + `EAP.widget.mount()`
可编程挂载。本包是 M50-C「打包就绪」产出的 **npm/tarball 分发形态**，与平台
服务端分发**并存**（两种方式内容等价，npm 产物仅多版本横幅头）：

| 获取方式 | 来源 | 适用 |
| --- | --- | --- |
| 服务端分发 | 部署实例的 `GET /sdk/eap-widget.js`（`main.py` 挂载 static 目录） | 第三方站点直接 `<script>` 引用部署方，随平台升级自动更新 |
| npm 包 / tarball | 本包 `dist/eap-widget.js`（构建期取源生成） | 需要版本锁定、走自有 CDN/制品库、离线分发的场景 |

**单一事实源**：widget 源码 = `eap/src/eap/static/eap-widget.js`（构建期读取，
不复制入库）；版本 = `eap/src/eap/__init__.py` 的 `__version__`（构建脚本自动
同步进 `package.json` 并注入产物横幅，CI `--check` 守卫漂移）。

## 构建与打包

```bash
cd sdk/widget
node scripts/build.mjs   # 产出 dist/eap-widget.js（带 /*! @eap/widget v<x.y.z> */ 横幅）
pnpm pack                # 产出 eap-widget-<version>.tgz（prepack 钩子自动先构建）
```

零运行时依赖、零构建工具链（纯 Node ≥ 18 脚本）；`dist/` 与 `*.tgz` 不入库
（根 `.gitignore` 覆盖）。tarball 内容：`package/dist/eap-widget.js`、
`package/README.md`、`package/package.json`。

## 嵌入用法

### 方式一：服务端分发（script 标签直引部署实例）

```html
<script src="https://your-eap-host/sdk/eap-widget.js"></script>
<eap-chat agent="faq-agent"
          endpoint="https://your-eap-host"
          token="eap_emb_..."
          height="480px"></eap-chat>
```

### 方式二：npm 产物（版本锁定，经自有静态资源链路分发）

```bash
npm install ./eap-widget-1.0.0.tgz   # 或发布后的 @eap/widget
```

```html
<script src="/node_modules/@eap/widget/dist/eap-widget.js"></script>
<!-- 或打包进前端工程后以静态资源引用 -->
```

### 可编程挂载

```html
<div id="box"></div>
<script>
  EAP.widget.mount("#box", {
    agent: "faq-agent",
    endpoint: "https://your-eap-host",
    session: "<由页面后端换取的 session token>",
    height: "420px",
    title: "库存助手"
  });
</script>
```

`<eap-chat>` 属性全集：`agent` / `endpoint`（默认同源）/ `token` 或 `session` /
`height` / `title` / `user-id`。

**安全约定**（同 docs/07 §嵌入）：`token`（`eap_emb_*`）只应出现在内网/开发
演示；生产环境由页面后端调 `POST /api/v1/embed/session` 换取 session 后注入。

## 发布前置条件（外部决策，当前只出 CI artifacts 不发布）

1. npm `@eap` scope（组织）保留或改名（发布名以实际 scope 决策为准）；
2. `NPM_TOKEN` secret + GitHub `environment: npm` 保护配置；
3. 许可证确定（仓库无 LICENSE 文件，package.json 以
   `SEE LICENSE IN README.md` 占位——发布前须替换为明确 SPDX 标识）。

发布 job 在 `.github/workflows/sdk.yml` 中双重门控占位（`workflow_dispatch`
输入 + environment），实际 `npm publish` 步骤保持注释态。
