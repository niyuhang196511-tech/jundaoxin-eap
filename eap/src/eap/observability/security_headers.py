"""安全响应头中间件（M48-C，成熟度盘点 #10：后端无任何安全响应头）。

为 API/静态响应补齐基线响应头：
- ``X-Content-Type-Options: nosniff``——禁 MIME 嗅探；
- ``Referrer-Policy: strict-origin-when-cross-origin``——收敛 referrer 泄漏面；
- ``X-Frame-Options: DENY`` + ``Content-Security-Policy: default-src 'none';
  frame-ancestors 'none'``——API/JSON 与 /media 静态资源无被框 iframe 的合法场景
  （嵌入通道的界面在控制台前端，后端不直出业务 HTML）。

豁免（诚实说明，逐条有功能理由）：
- ``/sdk/*``（embed 静态资源）：demo.html 的功能本体就是被第三方站点 iframe 嵌入，
  且页面需加载同源 eap-widget.js——套 DENY/default-src 'none' 会自断嵌入通道；
  嵌入页自身的 CSP 属前端/嵌入模板职责；
- ``/docs`` ``/redoc`` ``/openapi.json``：Swagger UI 需要脚本与外链资源（开发态文档工具）。

异常路径说明（如实）：call_next 抛出时本层无响应对象可挂头，500 由外层
ServerErrorMiddleware 生成——该 500 不带本层安全头，属既有架构边界。

注册（main.py）：add_middleware 常规注册，纯响应头改写无顺序语义依赖。
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}

# 豁免前缀：嵌入静态资源（iframe 嵌入是功能本体）与开发态 API 文档（需脚本/外链）
_EXEMPT_PREFIXES = ("/sdk", "/docs", "/redoc", "/openapi.json")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """响应头统一追加（业务已设置的同类头不覆盖；豁免路径见模块 docstring）。"""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if not is_exempt(request.url.path):
            for name, value in _HEADERS.items():
                if name not in response.headers:
                    response.headers[name] = value
        return response


def is_exempt(path: str) -> bool:
    """豁免判定（独立函数便于测试）。"""
    return any(path.startswith(p) for p in _EXEMPT_PREFIXES)
