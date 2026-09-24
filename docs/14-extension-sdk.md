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
