"""交互引擎运行时（docs/unfinished v0.5-③④⑧）。

- AI UI Schema：Agent 向用户请求结构化输入（text/textarea/number/select/multiselect/
  radio/checkbox/confirmation/form 9 种控件 + 动态 data_source）
- 挂起/恢复：ctx.interact(schema) 抛 InteractionRequested —— 任务通道由 TaskEngine
  捕获落 WAITING_INPUT 快照（interact 端点提交后续跑）；聊天通道由 registry.invoke
  捕获返回 InvokeResult.interaction（interactions 表 + submit 端点恢复）
- 动态数据源：select 系控件的 options 可声明 {type:"tool", tool, args, label_field,
  value_field, cascade}，服务端调已注册 Tool 实时取值（写审计）
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import InteractionRecord

# UI Schema 支持的控件类型（v0.5 核心集）
INTERACTION_TYPES = {
    "text", "textarea", "number", "select", "multiselect",
    "radio", "checkbox", "confirmation", "form",
}

# data_source 选项字段的合法键（白名单校验）
_DATA_SOURCE_KEYS = {"type", "tool", "args", "label_field", "value_field", "cascade"}


def validate_interaction_schema(schema: dict) -> dict:
    """发布/下发前的 UI Schema 结构校验（白名单控件 + data_source 键）。"""
    if not isinstance(schema, dict):
        raise ValueError("interaction schema 必须是对象")
    if schema.get("type") != "form":
        raise ValueError('interaction schema 顶层必须为 {"type": "form", "fields": [...]}')
    fields = schema.get("fields")
    if not isinstance(fields, list) or not fields:
        raise ValueError("form.fields 必须是非空数组")

    def _check_field(f: dict) -> None:
        if not isinstance(f, dict):
            raise ValueError("field 必须是对象")
        ftype = f.get("type")
        if ftype not in INTERACTION_TYPES:
            raise ValueError(f"不支持的控件类型: {ftype}（允许: {sorted(INTERACTION_TYPES)}）")
        if ftype == "form":
            raise ValueError("fields 不允许内嵌 form（顶层已是 form）")
        fid = f.get("id")
        if not isinstance(fid, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", fid):
            raise ValueError(f"field.id 非法: {fid!r}（小写字母开头的标识符）")
        ds = f.get("data_source")
        if ds is not None:
            if ftype not in ("select", "multiselect", "radio"):
                raise ValueError(f"控件 {ftype} 不支持 data_source（仅 select 系）")
            if not isinstance(ds, dict) or ds.get("type") != "tool" or not ds.get("tool"):
                raise ValueError('data_source 须为 {"type": "tool", "tool": "...", ...}')
            unknown = set(ds) - _DATA_SOURCE_KEYS
            if unknown:
                raise ValueError(f"data_source 含未知键: {sorted(unknown)}")
            for cascade in ds.get("cascade") or []:
                if not isinstance(cascade, dict) or not cascade.get("field") or not cascade.get("param"):
                    raise ValueError('cascade 项须为 {"field": "...", "param": "..."}')

    for f in fields:
        _check_field(f)
    return schema


def _extract_options(raw: str, label_field: str, value_field: str) -> list[dict]:
    """工具输出（JSON）→ [{label, value}]；支持 {options: [...]} 包裹 / 数组 / 单对象。"""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, dict):
        parsed = parsed.get("options") or parsed.get("items") or parsed
    options: list[dict] = []
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                options.append({"label": str(item.get(label_field, item.get("label", ""))),
                                "value": item.get(value_field, item.get("value"))})
            else:
                options.append({"label": str(item), "value": item})
    elif isinstance(parsed, dict):
        if label_field in parsed:
            options = [{"label": str(parsed.get(label_field, "")),
                        "value": parsed.get(value_field, parsed.get(label_field))}]
        else:
            options = [{"label": str(k), "value": v} for k, v in parsed.items()]
    return options[:50]


async def aresolve_options(db: Session, data_source: dict, values: dict | None = None,
                           agent_name: str = "") -> list[dict]:
    """解析动态选项：调已注册 Tool（连接器/工作流/插件池），返回 [{label, value}]。

    cascade：按 values 里已填的父字段值填充 tool 参数（param → values[field]）。
    """
    tool_name = str(data_source.get("tool") or "")
    label_field = str(data_source.get("label_field") or "label")
    value_field = str(data_source.get("value_field") or "value")

    tool = find_tool_for_options(db, tool_name)
    if tool is None:
        raise ValueError(f"选项数据源工具 {tool_name} 不可用")
    args = dict(data_source.get("args") or {})
    for cascade in data_source.get("cascade") or []:
        parent_value = (values or {}).get(cascade["field"])
        if parent_value is not None:
            args[cascade["param"]] = parent_value
    raw = await tool.handler(json.dumps(args, ensure_ascii=False))

    from ..observability import audit

    audit.record("interaction.options", actor="system", target=f"{agent_name}:{tool_name}",
                 detail={"args": args})
    return _extract_options(raw, label_field, value_field)


def collect_option_tools(db: Session) -> list:
    """收集可作选项数据源的工具池：工作流/插件工具 + 连接器工具。"""
    from ..models import ConnectorRecord
    from ..runtime.connectors import load_connector_tools
    from ..runtime.workflow import _WORKFLOW_TOOLS

    tools: list = []
    for factory in _WORKFLOW_TOOLS.values():
        try:
            tools.append(factory())
        except Exception:
            continue
    for record in db.scalars(select(ConnectorRecord)
                             .where(ConnectorRecord.enabled == True)).all():  # noqa: E712
        tools.extend(load_connector_tools(record))
    return tools


def find_tool_for_options(db: Session, tool_name: str):
    """动态选项可调用的工具解析：工作流/插件工具 + 连接器 + MCP（经由统一注册点）。"""
    from ..runtime.tools import find_tool

    return find_tool(collect_option_tools(db), tool_name)


@dataclass
class InteractionRequest:
    """Agent 发出的交互请求载荷（挂起快照与 SSE result 帧共用）。"""

    schema: dict
    key: str = "input"
    title: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        validate_interaction_schema(self.schema)

    async def aresolved_schema(self, db: Session, values: dict | None = None,
                               agent_name: str = "") -> dict:
        """schema 的 options 解析副本：静态 options 原样；data_source 字段实时取值。"""
        import copy

        resolved = copy.deepcopy(self.schema)
        for f in resolved.get("fields", []):
            ds = f.get("data_source")
            if ds:
                f["options"] = await aresolve_options(db, ds, values=values, agent_name=agent_name)
        return resolved


class InteractionRequested(Exception):
    """Agent 等待用户结构化输入（携带可恢复的交互请求）。

    任务通道：TaskEngine 捕获 → WAITING_INPUT + 快照落库；
    聊天通道：registry.invoke 捕获 → InvokeResult.interaction + interactions 表。
    """

    def __init__(self, request: InteractionRequest, messages: list[dict] | None = None):
        self.request = request
        self.messages = messages or []
        super().__init__(f"等待用户输入: {request.title or request.key}")
