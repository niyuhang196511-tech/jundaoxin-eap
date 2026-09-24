# eap-sdk（Python 扩展 SDK 独立分发包）

EAP 平台扩展 SDK 的独立打包形态（M50-C「打包就绪」）。**薄 re-export 层**：
包内只有 `eap_sdk/__init__.py` 一个模块，全部契约符号在运行时从平台包 `eap`
再导出——`eap/src/eap/ext/sdk.py`、`eap/src/eap/agents/sdk.py`、
`eap/src/eap/runtime/tools.py` 仍是唯一事实源，本包不含任何平台代码拷贝。

契约面与用法见 [docs/14-extension-sdk.md](../../docs/14-extension-sdk.md)
（七类 SDK：RAG / Workflow Node / Model Provider / Connector / UI / Tool / Agent）。

## 安装

平台包 `eap` 未发布 PyPI，须**先装平台、再装 eap-sdk**：

```bash
# ① 平台包（仓库源码，任选其一）
uv pip install ./eap                 # 本地检出
uv pip install "eap-sdk[platform-git]"  # 或：经 extra 从 git 源一并装平台（需仓库读取权限）

# ② 本包
uv pip install eap_sdk-<version>-py3-none-any.whl   # CI artifact / 本地 uv build 产物
```

平台缺失时 `import eap_sdk` 会抛出带上述安装指引的 `ModuleNotFoundError`
（import 期友好报错，而非深层 ImportError 栈）。

> **依赖声明取舍**：`dependencies` 留空 + extras 提供 git 源通道 + import 期
> 报错兜底。不写 `dependencies = ["eap"]` 是因为 PyPI 无此包时会导致
> `pip install eap-sdk` 直接解析失败（连报错说明都看不到）；平台包是否上
> PyPI 属发布前外部决策，届时可把硬依赖加回来。

## 使用

```python
from eap_sdk import Chunker, register_chunker, register_workflow_node

class SlashChunker(Chunker):
    def chunk(self, text, params=None):
        return [p for p in text.split("/") if p]

register_chunker("slash-chunker", SlashChunker(), description="按斜杠切分")
```

`eap_sdk.X` 与 `eap.ext.X` / `eap.agents.sdk.X` 是同一对象（re-export），
扩展代码两种 import 路径可互换；`.eapext` 打包与安装生命周期不变
（`python -m eap.scaffold pack` → `POST /extensions/install`）。

## 构建

```bash
cd sdk/python && uv build      # 产出 dist/*.whl + dist/*.tar.gz
```

## 版本策略（单一事实源）

- **唯一手改处**：`eap/src/eap/__init__.py` 的 `__version__`（平台版本）。
- **构建期**：`setup.py`（setuptools 动态版本入口）从平台源码推导；
  从 sdist 重建 wheel 时回退读 sdist 自带 `PKG-INFO`，特殊场景可用环境变量
  `EAP_SDK_VERSION` 显式注入。本目录任何文件不写死版本号。
- **运行期**：`eap_sdk.__version__` 动态读 `eap.__version__`。
- **CI 漂移守卫**：`.github/workflows/sdk.yml` 校验 wheel METADATA 版本 ==
  平台 `__version__`，不一致即红。

## 发布前置条件（外部决策，当前只出 CI artifacts 不发布）

1. PyPI 包名 `eap-sdk` 保留（先查名可用）+ 账号/组织，凭据以
   `PYPI_TOKEN` secret 或 PyPI Trusted Publishing（OIDC，推荐）配置；
2. 许可证确定（仓库当前无 LICENSE 文件，pyproject 以
   `LicenseRef-Proprietary` 占位）；
3. 平台包 `eap` 是否发布 PyPI——决定本包依赖声明能否从「extras + import 期
   报错」升级为常规硬依赖；
4. 扩展契约面（`eap.ext` / `eap.agents.sdk`）的 API 稳定承诺（semver）。

发布 job 在 `.github/workflows/sdk.yml` 中以 `environment: pypi` +
`workflow_dispatch` 双重门控占位，实际发布步骤保持注释态。
