"""A2A Client（M30，docs/04 §5 反向通道）：平台作为 A2A Client 委派外部 Agent。

边界口诀：MCP = Agent ↔ 工具/数据；A2A = Agent ↔ Agent。平台对内是 A2A Server
（`api/v1/a2a.py`），对外经本模块作为 A2A Client：

- `discover()`  拉取外部 Agent Card（/.well-known/agent-card.json）
- `send()`      JSON-RPC message/send（同步委派，返回 A2A Task）
- `send_stream()`  JSON-RPC message/stream（SSE 逐帧产出 Task 事件）

离线确定性：出站 HTTP 收敛在 Transport（默认 httpx.AsyncClient，禁重定向），
构造参数 / `set_transport_factory` 可注入 fake，测试不触网、不起子进程。

委派边界（跨租户 fail-closed）：外部 endpoint 委派默认拒绝，须租户策略
a2a-delegate-allowlist 放行（`runtime/policy.py::check_a2a_delegate`），
与 M24 内部委派的 agent-allowlist（默认放行）语义互补；每次委派落审计
`a2a.delegate`（目标 endpoint/agent/状态）。
"""

from __future__ import annotations

import itertools
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import httpx

from .tools import Tool

_TIMEOUT = 10.0

# ---------- 传输层（出站 HTTP 唯一出口；测试注入点） ----------


@dataclass
class TransportResponse:
    """传输层回包：状态码 + 文本体（JSON 解析交给上层）。"""

    status_code: int
    text: str = ""


class A2AError(RuntimeError):
    """A2A 委派失败基类（传输/协议层错误）。"""


class A2ATransportError(A2AError):
    """网络/超时/HTTP 状态错误（对方不可达或非 200）。"""


class A2AProtocolError(A2AError):
    """协议错误：回包非 JSON / JSON-RPC error 帧 / 名片缺关键字段。"""


class _CallableTransport:
    """把裸 async callable(method, url, *, headers, content, timeout) → TransportResponse
    适配为传输对象（简单 fake 的最简注入形态；不支持流式）。"""

    def __init__(self, fn: Callable[..., Awaitable[TransportResponse]]) -> None:
        self._fn = fn

    async def request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                      content: bytes | None = None, timeout: float = _TIMEOUT) -> TransportResponse:
        return await self._fn(method, url, headers=headers, content=content, timeout=timeout)

    def stream_events(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                      content: bytes | None = None, timeout: float = _TIMEOUT) -> AsyncIterator[str]:
        raise A2AError("注入的 callable 传输不支持流式（stream_events），请传入传输对象")


class HttpxTransport:
    """默认出站传输：httpx.AsyncClient，单次请求一客户端（禁重定向，SSRF 基线同 connectors）。"""

    async def request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                      content: bytes | None = None, timeout: float = _TIMEOUT) -> TransportResponse:
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                resp = await client.request(method, url, headers=headers, content=content)
            return TransportResponse(status_code=resp.status_code, text=resp.text)
        except httpx.HTTPError as e:
            raise A2ATransportError(f"A2A 出站请求失败 {method} {url}: {e}") from e

    async def stream_events(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                            content: bytes | None = None, timeout: float = _TIMEOUT) -> AsyncIterator[str]:
        """SSE 逐帧产出 data 载荷（已剥离 "data: " 前缀，多行 data 以 \n 连接）。"""
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                async with client.stream(method, url, headers=headers, content=content) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        raise A2ATransportError(f"A2A 流式请求失败 {url}: HTTP {resp.status_code} {body[:200]}")
                    data_lines: list[str] = []
                    async for line in resp.aiter_lines():
                        if line.startswith(":"):  # SSE 注释/心跳
                            continue
                        if line.startswith("data:"):
                            data_lines.append(line[5:].lstrip(" "))
                            continue
                        if not line and data_lines:  # 空行 = 帧结束
                            yield "\n".join(data_lines)
                            data_lines = []
                    if data_lines:
                        yield "\n".join(data_lines)
        except httpx.HTTPError as e:
            raise A2ATransportError(f"A2A 流式请求失败 {method} {url}: {e}") from e


# 模块级传输工厂：工具路径（无法逐调用传参）的测试注入点；None = 默认 httpx
_transport_factory: Callable[[], object] | None = None


def set_transport_factory(factory: Callable[[], object] | None) -> None:
    """替换默认传输工厂（测试注入 fake；传 None 恢复默认 httpx）。"""
    global _transport_factory
    _transport_factory = factory


def default_transport() -> object:
    """当前默认传输实例（工厂注册优先，否则 HttpxTransport）。"""
    if _transport_factory is not None:
        return _transport_factory()
    return HttpxTransport()


def _coerce_transport(transport: object) -> object:
    """注入形态归一：带 request 方法的对象直接用；裸 callable 适配为传输对象。"""
    if transport is None:
        return default_transport()
    if callable(transport) and not hasattr(transport, "request"):
        return _CallableTransport(transport)
    return transport


# ---------- A2A Client ----------


class A2AClient:
    """外部 Agent 的 A2A 客户端（A2A 1.0 子集：discover / message/send / message/stream）。

    endpoint 约定：外部 Agent 的服务基址；discover() 拉取名片后以 card.url 为
    message/send 目标；未 discover 时直接以 endpoint 作为 JSON-RPC 端点
    （允许 endpoint 直接是 .../a2a/rpc 形态）。
    """

    def __init__(self, endpoint: str, transport: object | None = None, timeout: float = _TIMEOUT) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.transport = _coerce_transport(transport)
        self.timeout = timeout
        self._service_url = self.endpoint
        self._ids = itertools.count(1)

    # -- 名片与端点 --

    @property
    def card_url(self) -> str:
        if "/.well-known/agent-card.json" in self.endpoint:
            return self.endpoint
        return self.endpoint + "/.well-known/agent-card.json"

    async def discover(self) -> dict:
        """拉取并返回 Agent Card（以 card.url 校准后续 message/send 目标）。"""
        resp = await self._request("GET", self.card_url)
        if resp.status_code >= 400:
            raise A2ATransportError(f"拉取 Agent Card 失败 {self.card_url}: HTTP {resp.status_code}")
        try:
            card = json.loads(resp.text)
        except (json.JSONDecodeError, TypeError) as e:
            raise A2AProtocolError(f"Agent Card 非 JSON：{resp.text[:200]}") from e
        if not isinstance(card, dict) or not card.get("name"):
            raise A2AProtocolError("Agent Card 缺少 name 字段")
        if card.get("url"):
            self._service_url = str(card["url"]).rstrip("/")
        return card

    # -- JSON-RPC --

    def _rpc_body(self, method: str, params: dict) -> dict:
        return {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}

    async def _post_rpc(self, method: str, params: dict) -> dict:
        body = json.dumps(self._rpc_body(method, params), ensure_ascii=False)
        resp = await self._request("POST", self._service_url, content=body.encode("utf-8"),
                                   headers={"Content-Type": "application/json"})
        if resp.status_code >= 400:
            raise A2ATransportError(f"A2A {method} 失败 {self._service_url}: HTTP {resp.status_code} {resp.text[:200]}")
        try:
            frame = json.loads(resp.text)
        except (json.JSONDecodeError, TypeError) as e:
            raise A2AProtocolError(f"A2A {method} 回包非 JSON：{resp.text[:200]}") from e
        if not isinstance(frame, dict):
            raise A2AProtocolError(f"A2A {method} 回包形态非法")
        if frame.get("error"):
            err = frame["error"]
            raise A2AProtocolError(f"A2A {method} JSON-RPC 错误 {err.get('code')}: {err.get('message')}")
        return frame.get("result") or {}

    async def send(self, message: str | dict, *, metadata: dict | None = None) -> dict:
        """message/send：同步委派，返回 A2A Task（completed 状态）。"""
        params: dict = {"message": self._as_message(message)}
        if metadata:
            params["metadata"] = metadata
        return await self._post_rpc("message/send", params)

    async def send_stream(self, message: str | dict, *, metadata: dict | None = None) -> AsyncIterator[dict]:
        """message/stream：SSE 逐帧 yield JSON-RPC result（status-update / artifact-update / Task）。"""
        params: dict = {"message": self._as_message(message)}
        if metadata:
            params["metadata"] = metadata
        body = json.dumps(self._rpc_body("message/stream", params), ensure_ascii=False)
        try:
            async for payload in self.transport.stream_events(  # type: ignore[union-attr]
                    "POST", self._service_url, content=body.encode("utf-8"),
                    headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
                    timeout=self.timeout):
                try:
                    frame = json.loads(payload)
                except (json.JSONDecodeError, TypeError) as e:
                    raise A2AProtocolError(f"A2A 流式帧非 JSON：{payload[:200]}") from e
                if frame.get("error"):
                    err = frame["error"]
                    raise A2AProtocolError(f"A2A 流式 JSON-RPC 错误 {err.get('code')}: {err.get('message')}")
                if frame.get("result") is not None:
                    yield frame["result"]
        except httpx.HTTPError as e:  # 仅兜底传输层异常（协议错误直接上抛）
            raise A2ATransportError(f"A2A 流式请求失败 {self._service_url}: {e}") from e

    async def _request(self, method: str, url: str, *, content: bytes | None = None,
                       headers: dict[str, str] | None = None) -> TransportResponse:
        return await self.transport.request(  # type: ignore[union-attr]
            method, url, headers=headers, content=content, timeout=self.timeout)

    @staticmethod
    def _as_message(message: str | dict) -> dict:
        """入参归一：字符串 → role=user 的单 text part；dict 原样透传。"""
        if isinstance(message, str):
            return {"role": "user", "parts": [{"kind": "text", "text": message}]}
        return message

    # -- 回包解析（委派结果 → 文本/结构） --

    @staticmethod
    def task_state(task: dict) -> str:
        """Task 状态机当前态（working/completed/failed/...）。"""
        return str((task.get("status") or {}).get("state") or "")

    @staticmethod
    def task_text(task: dict) -> str:
        """Task 回包 → 文本：artifacts 文本 parts 优先，回退 status.message。"""
        chunks: list[str] = []
        for artifact in task.get("artifacts") or []:
            for part in (artifact or {}).get("parts") or []:
                if part.get("kind") == "text" and part.get("text"):
                    chunks.append(part["text"])
        if chunks:
            return "\n".join(chunks)
        msg = (task.get("status") or {}).get("message") or {}
        return "\n".join(p.get("text") or "" for p in msg.get("parts") or []
                         if p.get("kind") == "text")

    @staticmethod
    def task_json(task: dict) -> dict | None:
        """Task 回包 → 结构：首个 artifact 文本能解析为 JSON 对象时返回，否则 None。"""
        text = A2AClient.task_text(task)
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None
        return data if isinstance(data, dict) else None


# ---------- 原生工具：a2a.delegate（外部委派，走 M24 delegate 模式） ----------

_TOOL_TIMEOUT_S = 60.0


def a2a_delegate_tool(transport: object | None = None) -> Tool:
    """A2A 外部委派工具：模型经工具调用把任务委派给外部 Agent（A2A message/send）。

    复用 M24 delegate 模式：委派深度护栏（防循环）+ 策略边界 + 审计；区别于
    agent.<name>（平台内部、默认放行），外部 endpoint 跨租户边界 fail-closed，
    须 a2a-delegate-allowlist 策略放行。risk_level=medium（数据出边界，
    可被 tool-allowlist / tool-risk-approval 进一步治理）。
    """

    async def handler(arguments: str) -> str:
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        endpoint = str(args.get("endpoint") or "").strip()
        target_agent = str(args.get("target_agent") or "").strip()
        message = str(args.get("message") or args.get("input") or "").strip()
        if not endpoint or not message:
            return json.dumps({"error": "缺少 endpoint / message 参数"}, ensure_ascii=False)

        from ..db import SessionLocal
        from ..observability.audit import record as audit_record
        from .multi_agent import _MAX_DELEGATION_DEPTH, current_depth, pop_depth, push_depth
        from .policy import PolicyDenied, check_a2a_delegate

        depth = current_depth()
        if depth >= _MAX_DELEGATION_DEPTH:
            return json.dumps({"error": f"委派深度已达上限 {_MAX_DELEGATION_DEPTH}，请自行作答"},
                              ensure_ascii=False)
        token = push_depth()
        status = "ok"
        task: dict = {}
        try:
            with SessionLocal() as db:
                try:
                    # fail-closed：外部边界先于任何出站请求（拒绝时不触网）；
                    # agents 名单匹配工具声明的 target_agent，未声明时按 endpoint 门控
                    check_a2a_delegate(db, endpoint, target_agent)
                except PolicyDenied as e:
                    status = "denied"
                    return json.dumps({"error": f"外部委派被策略拒绝: {e}"}, ensure_ascii=False)
                client = A2AClient(endpoint, transport=transport)
                card: dict = {}
                try:
                    card = await client.discover()  # A2A 流程：先拉名片（card.url 校准 send 目标）
                except A2AError:
                    pass  # 名片不可达时回退直连 endpoint（仅暴露 JSON-RPC 端点的部署）
                task = await client.send(message)
                return json.dumps({
                    "agent": target_agent or str(card.get("name") or "external"),
                    "endpoint": endpoint,
                    "task_id": task.get("id"),
                    "state": A2AClient.task_state(task),
                    "output": A2AClient.task_text(task),
                    "data": A2AClient.task_json(task),
                }, ensure_ascii=False)
        except A2AError as e:
            status = "error"
            return json.dumps({"error": f"外部委派失败: {e}"}, ensure_ascii=False)
        except PolicyDenied as e:  # 循环内 policy 校验等其他拒绝路径
            status = "denied"
            return json.dumps({"error": f"外部委派被策略拒绝: {e}"}, ensure_ascii=False)
        except Exception as e:  # 深度/会话等兜底，不向循环抛裸异常
            status = "error"
            return json.dumps({"error": f"外部委派失败: {e}"}, ensure_ascii=False)
        finally:
            # 每次委派（含拒绝/失败）落审计：目标 endpoint/agent/状态（敏感字段自动脱敏）
            try:
                audit_record("a2a.delegate", actor="system", target=endpoint,
                             detail={"agent": target_agent, "status": status,
                                     "task_state": A2AClient.task_state(task) if task else ""})
            except Exception:
                pass  # 审计失败不阻断业务
            pop_depth(token)

    return Tool(
        name="a2a.delegate",
        description="把任务委派给外部 Agent（A2A 协议）：给出对方服务地址 endpoint 与任务 message，"
                    "返回其回答文本。默认被策略拒绝，仅在租户放行名单内可用",
        parameters={
            "type": "object",
            "properties": {
                "endpoint": {"type": "string", "description": "外部 Agent 的 A2A 服务地址（http/https）"},
                "target_agent": {"type": "string", "description": "目标 Agent 名（可选，用于策略匹配与审计）"},
                "message": {"type": "string", "description": "要委派的任务/问题"},
            },
            "required": ["endpoint", "message"],
        },
        handler=handler,
        emits_citations=False,
        risk_level="medium",
        timeout_s=_TOOL_TIMEOUT_S,
    )
