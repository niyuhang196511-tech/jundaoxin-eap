# rag-pipeline — 自定义 RAG 重排器示例（可运行）

演示 RAG SDK（docs/14-extension-sdk.md §二.2）的 **reranker** 契约：

    rerank(query, candidates, params) -> list[int]   # 候选下标，相关性降序

实现为纯函数（鸭子类型兼容 `eap.ext.Reranker` 基类，继承可选），离线确定性，
不依赖任何模型/网络——适合作为自定义检索管线的起步模板。

## 文件

- `manifest.py`：`keyword_rerank` 实现 + `EAP_PLUGIN` 注册声明（平台插件约定入口）

## 注册

**方式一：直接注册体验（不落库，在 `eap/` 目录执行）**

```bash
uv run python -c "import importlib.util as iu; spec=iu.spec_from_file_location('rag_pipeline_demo','examples/rag-pipeline/manifest.py'); m=iu.module_from_spec(spec); spec.loader.exec_module(m); m.EAP_PLUGIN['register'](); from eap.knowledge.components import get_reranker; print(get_reranker('keyword-rerank')('库存 查询', [(0,'天气很好'),(1,'库存查询接口'),(2,'周报模板')]))"
```

应输出 `[1, 0, 2]`（含「库存」「查询」词的候选排前，其余保持原序）。

**方式二：装进平台**

```bash
cp -r examples/rag-pipeline plugins/rag-pipeline   # 或 EAP_PLUGINS_DIR 指向的目录
```

重启平台，或热加载：`POST /api/v1/extensions/plugins/reload`（admin）。
`GET /api/v1/extensions/plugins` 可见 `rag-pipeline`（type=rag，exposes 含组件名）。

## 验证（平台内）

创建知识库时经 `pipeline` 字段选择本重排器：

```json
{"name": "demo-kb", "pipeline": {"reranker": {"name": "keyword-rerank"}}}
```

此后该 KB 的检索流程即走 `keyword_rerank`（初排结果 → 重排 → 取 top_k）。

## 下一步

- 换成你的打分逻辑（同义改写 / 语义模型 / 业务规则）
- 需要 chunker 时照同样方式 `register_chunker(name, fn)`（见
  `python -m eap.scaffold generate rag <name>` 生成的模板，两者可同插件共存）
