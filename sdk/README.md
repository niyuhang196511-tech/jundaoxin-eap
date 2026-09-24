# sdk/ — EAP SDK 独立分发包（M50-C 打包就绪态）

平台扩展 SDK 的独立打包形态。**零平台代码拷贝**：Python 侧是运行时薄
re-export 层，JS 侧构建期从平台源码取源——单一事实源均在 `eap/src/eap/`。

| 目录 | 产物 | 事实源 | 构建 |
| --- | --- | --- | --- |
| [`python/`](python/README.md) | `eap-sdk` wheel + sdist | `eap.ext.sdk` / `eap.agents.sdk`（运行时 re-export） | `cd python && uv build` |
| [`widget/`](widget/README.md) | `@eap/widget` npm tarball | `eap/src/eap/static/eap-widget.js`（构建期取源） | `cd widget && pnpm pack` |

- 版本单一事实源：`eap/src/eap/__init__.py` 的 `__version__`（两侧构建期推导，CI 漂移守卫）。
- CI：[`../.github/workflows/sdk.yml`](../.github/workflows/sdk.yml) 只产 artifacts 不发布；
  publish job 为 `workflow_dispatch` + `environment`（pypi/npm）双重门控的注释态占位。
- 契约与生命周期文档：[`../docs/14-extension-sdk.md`](../docs/14-extension-sdk.md)（§四 打包与分发）。
- 发布前置条件（包名保留/账号/许可证/API 稳定承诺）属外部决策，见两份 README 的清单。
