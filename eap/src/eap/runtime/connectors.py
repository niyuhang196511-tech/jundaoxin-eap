"""企业连接器运行时（docs/04 §4，M3）：把已登记连接器的端点包装为平台 Tool。

- rest：httpx 按 endpoint 的 method/path 调外部系统（GET → query，POST/PUT → JSON body）
- mock-erp：内置离线演示 ERP（库存查询/下单），供开发与测试

工具名取 endpoint.tool_name（如 erp.order.create），requires_approval 透传给 HITL 门控。
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

from ..models import ConnectorRecord
from .tools import Tool

_TIMEOUT = 10.0


def _safe_url(base_url: str, path: str) -> str:
    """仅允许登记时写死的 http(s) base_url 拼接 path（SSRF 基线：拒绝其它 scheme）。"""
    scheme = urlparse(base_url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"EAP-7002 连接器 base_url 须为 http(s)，当前 {base_url!r}")
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def _mock_erp_handler(endpoint_name: str):
    """内置演示 ERP：确定性的库存查询与下单处理。"""

    async def handler(arguments: str) -> str:
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        if endpoint_name == "inventory.query":
            product = str(args.get("product") or args.get("query") or "").strip()
            stock = {"EAP 一体机": 15, "EAP 网关": 42}.get(product, 8)
            return json.dumps({"product": product, "warehouse": "CN-EAST-1",
                               "stock": stock, "unit": "台"}, ensure_ascii=False)
        if endpoint_name == "order.create":
            product = str(args.get("product") or "未指定产品")
            order_id = "SO-2026-" + product[:6].upper().replace(" ", "")
            return json.dumps({"order_id": order_id, "status": "created",
                               "product": product, "qty": int(args.get("qty") or 1)},
                              ensure_ascii=False)
        return json.dumps({"error": f"mock-erp 未知端点 {endpoint_name}"}, ensure_ascii=False)

    return handler


def _rest_handler(record: ConnectorRecord, endpoint: dict):
    async def handler(arguments: str) -> str:
        import httpx

        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        method = (endpoint.get("method") or "GET").upper()
        url = _safe_url(record.base_url, endpoint.get("path") or "")
        headers = {}
        if record.api_key:
            from ..security_crypto import decrypt_secret

            headers[record.header_name or "Authorization"] = decrypt_secret(record.api_key)
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
            if method == "GET":
                resp = await client.request(method, url, params=args, headers=headers)
            else:
                resp = await client.request(method, url, json=args, headers=headers)
        return json.dumps({"status": resp.status_code,
                           "body": _safe_json(resp.text)}, ensure_ascii=False)

    return handler


def _safe_json(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text[:2000]


def _params_schema(endpoint: dict) -> dict:
    return endpoint.get("params") or {"type": "object", "properties": {}}


def endpoint_tool(record: ConnectorRecord, endpoint: dict) -> Tool:
    name = endpoint.get("tool_name") or f"conn.{record.name}.{endpoint.get('name', '')}"
    handler = _mock_erp_handler(endpoint.get("name", "")) if record.kind == "mock-erp" \
        else _rest_handler(record, endpoint)
    return Tool(
        name=name,
        description=endpoint.get("description") or f"连接器 {record.name} 端点 {name}",
        parameters=_params_schema(endpoint),
        handler=handler,
        requires_approval=bool(endpoint.get("requires_approval")),
    )


def load_connector_tools(record: ConnectorRecord) -> list[Tool]:
    """把单个连接器的全部端点包装为平台 Tool（未启用返回空）。"""
    if not record.enabled:
        return []
    return [endpoint_tool(record, ep) for ep in (record.endpoints or [])]


def load_enabled_connector_tools(db) -> list[Tool]:
    """全部启用连接器的工具合集（智能体工具池用）。"""
    from sqlalchemy import select

    records = db.scalars(select(ConnectorRecord).where(ConnectorRecord.enabled == True)).all()  # noqa: E712
    tools: list[Tool] = []
    for record in records:
        tools.extend(load_connector_tools(record))
    return tools
