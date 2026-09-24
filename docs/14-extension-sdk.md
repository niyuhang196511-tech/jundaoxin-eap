# 14 - Extension SDK 开发者指南（v0.7）

> 面向扩展开发者：统一 Manifest、各类型 SDK 契约、开发→打包→安装→启停的生命周期。
> 配套示例见 `python -m eap.scaffold generate <type> <name>` 生成的模板
> （type 全集见 §五 CLI；`eap/examples/` 另有可运行示例：rag-pipeline（自定义重排器）、
> task-flow（经任务引擎跑长任务））。
>
> API 契约机读版：`docs/openapi.json`（由 `scripts/export_openapi.py` 从 create_app()
> 导出，CI 校验漂移——接口/Schema 变更未同步入库版本即红）。

## 一、统一 Manifest

所有类型的扩展共用一份 Manifest（`runtime/extension_manifest.py`），支持
`manifest.json` / `manifest.yaml` / manifest.py 内 `EXTENSION_MANIFEST` dict 三种载体：

```json
{
  "apiVersion": "eap.io/v1",
  "type": "tool",                      // agent|tool|rag|workflow_node|connector|ui|model_provider|package
  "name": "inventory-tool",
  "version": "1.0.0",
  "title": "库存查询工具",
  "description": "查询 ERP 库存",
  "runtime": {"min_version": "0.7.0"},
  "permissions": ["connector.invoke"],
  "dependencies": {"model": "mock-llm", "tools": []},
  "config_schema": {},
  "entry": {}
}
```

- `runtime.min_version`：要求的平台最低版本，安装/加载时检查（低于当前版本拒绝）。
- `permissions`：声明即权限边界（预留，v0.8 策略引擎消费）。
- 旧 `EAP_PLUGIN` dict（kind/name/description/register）自动归一化兼容，零改动可用。

## 二、各类型 SDK

### 1. Tool SDK
`eap.runtime.tools.Tool`：name/description/parameters（JSON Schema）/handler（args JSON → 结果文本），
治理元数据 `risk_level`（low/medium/high）与 `timeout_s`。注册：`register_workflow_tool(name, factory)`
（工具节点）或 agent 内组装进工具池。

### 2. RAG SDK
`eap.ext.Chunker` / `eap.ext.Reranker` 基类（继承可选，鸭子类型兼容）：

```python
from eap.ext import register_chunker, register_reranker

class SlashChunker(Chunker):
    def chunk(self, text, params=None):
        return [p for p in text.split("/") if p]

register_chunker("slash-chunker", SlashChunker(), description="按斜杠切分")
```

KB 创建后经 `pipeline` 字段选择：`{"chunker": {"name": "slash-chunker"}}`。

### 3. Workflow Node SDK
`register_workflow_node(kind, executor)`：自定义节点类型进入 DSL 合法集合，
executor(step, ctx, db, state, invoke_input) -> str（async）；parallel/loop 体内不可用。
脚手架：`python -m eap.scaffold generate workflow-node <name>`（executor 骨架 + manifest + README）。

### 4. Model Provider SDK
`register_provider(name, provider)`：实现 `complete(...)/stream_complete(...)`
协议（见 `modelhub/providers.py` Provider Protocol）的供应商适配即可入路由链；
模型登记的 provider 字段填该名字，`get_provider(name)` 即解析到它（自定义注册表
→ mock → openai_compat）。脚手架：`python -m eap.scaffold generate model-provider <name>`。

### 5. Connector SDK
`register_connector_kind(kind, factory)`：factory(ConnectorRecord) -> list[Tool]，
扩展 rest/mock-erp 之外的连接器形态（如 SDK 化的 SaaS 客户端）。
脚手架：`python -m eap.scaffold generate connector <name>`（endpoints→工具集骨架 + 鉴权占位）。

### 6. UI SDK（MVP）
`register_ui_component(id, schema_fragment)`：自定义交互组件 = 组合式 UISchema 片段，
interaction schema 以 `{"type": "custom", "component": "id"}` 引用，前端以内联子表单渲染。
脚手架：`python -m eap.scaffold generate ui-component <name>`。

### 7. Agent SDK
`AgentApp` / `AgentManifest` / `register_agent`（见 docs/03 §7 与 `agents/sdk.py`），
v0.5 起支持 `output_schema` / `interaction_schema` 声明。

## 三、生命周期（docs/unfinished 四十五/四十八节）

```
scaffold generate → 开发/测试 → scaffold pack（.eapext）
  → POST /extensions/install（admin）→ registry 登记
  → 启用/停用（/registry/{name}/enable|disable，状态跨重启保持）
  → 升级：同名更高 version 安装（低版本 409）
  → 卸载：DELETE /registry/{name}（目录+记录移除，全程审计）
```

CLI：`python -m eap.scaffold generate <type> <name> [--dir ./plugins]`、
`python -m eap.scaffold pack <dir> <out.eapext>`。
type 全集：`agent` / `tool` / `rag` / `mcp` / `skill` /
`workflow-node` / `model-provider` / `connector` / `ui-component`
（后四者为 M48-B 补齐的 SDK 脚手架：每类生成 manifest.json + 入口 manifest.py + README，
manifest type 对应统一 Manifest 的 workflow_node / model_provider / connector / ui，
`pack` 打包与安装端点开箱可用）。

## 四、SDK 打包与分发（打包就绪态，M50-C）

上述生命周期面向「扩展包（.eapext）」；SDK 本身另有**独立分发包**形态，
源码树在 `sdk/`（[sdk/README.md](../sdk/README.md)），两种产物：

| 产物 | 目录 | 形态 | 构建命令 |
| --- | --- | --- | --- |
| `eap-sdk`（import 名 `eap_sdk`） | `sdk/python/` | wheel + sdist，**薄 re-export 层**——运行时从平台包再导出 §二 契约面，零平台代码拷贝 | `cd sdk/python && uv build` |
| `@eap/widget` | `sdk/widget/` | npm tarball（`pnpm pack`），**构建期取源**——从 `eap/src/eap/static/eap-widget.js` 读取并注入版本横幅产出 `dist/eap-widget.js` | `cd sdk/widget && pnpm pack` |

widget 的 npm 形态与服务端分发（`GET /sdk/eap-widget.js`，docs/07 §嵌入）**并存**：
内容等价（npm 产物仅多版本横幅头），前者服务版本锁定/自有 CDN 场景，后者随平台升级。
两份包 README：[sdk/python/README.md](../sdk/python/README.md)、
[sdk/widget/README.md](../sdk/widget/README.md)。

### 版本策略（单一事实源）

唯一手改处 = `eap/src/eap/__init__.py` 的 `__version__`（平台版本）：

- **Python 构建期**：`sdk/python/setup.py`（setuptools 动态版本入口）
  从平台源码推导；sdist 异地重建回退读 PKG-INFO，特殊场景可注入环境变量
  `EAP_SDK_VERSION`。**运行期** `eap_sdk.__version__` 动态读 `eap.__version__`。
- **widget 构建期**：`scripts/build.mjs` 读平台版本注入产物横幅并自动同步
  `package.json` version。
- **CI 漂移守卫**：[.github/workflows/sdk.yml](../.github/workflows/sdk.yml)
  校验 wheel METADATA / package.json 版本 == 平台 `__version__`，不一致即红。

### 安装与依赖声明

平台包 `eap` 未发布 PyPI，`eap-sdk` 取舍为「**无硬依赖 + import 期友好报错 +
git 源 extra**」：先装平台（`uv pip install ./eap` 或
`uv pip install "eap-sdk[platform-git]"`，后者需仓库读取权限），再装
`eap-sdk`；平台缺失时 `import eap_sdk` 抛带安装指引的 `ModuleNotFoundError`。

### CI 形态（只出 artifacts，不发布）

`sdk.yml` 在 push(main)/PR（paths 收敛到 sdk/ 与平台契约源）构建两侧产物、
冒烟验证（临时 venv 装 wheel+平台源码；tarball 清单+横幅核对）后
upload-artifact。`publish-pypi` / `publish-npm` 为**双重门控占位**
（workflow_dispatch 的 publish 输入 + `environment: pypi|npm` 保护），
实际发布步骤保持注释态。

### 发布前置条件（外部决策，打包就绪 ≠ 已发布）

1. **包名保留**：PyPI `eap-sdk` 查名可用并注册账号/组织；npm `@eap` scope
   （组织）保留或定名；
2. **凭据**：`PYPI_TOKEN`（或 PyPI Trusted Publishing/OIDC，推荐）与
   `NPM_TOKEN` secrets + GitHub Environments（pypi/npm）审批配置；
3. **许可证**：仓库当前无 LICENSE 文件，两包元数据均为占位
   （`LicenseRef-Proprietary` / `SEE LICENSE IN README.md`），发布前须定值；
4. **平台包去向**：`eap` 是否发布 PyPI——决定 `eap-sdk` 依赖声明能否从
   extras+报错兜底升级为常规硬依赖；
5. **API 稳定承诺**：`eap.ext` / `eap.agents.sdk` 契约面冻结后的 semver
   承诺（独立分发包意味着第三方将锁定版本消费）。
