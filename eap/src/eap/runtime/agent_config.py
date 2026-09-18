"""Agent 配置版本层运行时（docs/unfinished v0.5-①）。

- 版本流水线：draft → publish（写 AgentRecord.published_version 指针，旧发布版归档）
  → rollback（重发布最近归档版）→ deprecate（下线过渡）
- 运行时覆盖层：registry.invoke 按 (agent, published_version) 解析 config（进程内缓存，
  已发布版本不可变故缓存安全），经 ContextVar 下发给 SDK 消费点
  （ctx.chat / ctx.run_loop / ctx.retriever / ctx.system_role / ctx.skill_context）——
  未发布配置版本的智能体行为与纯代码定义完全一致（向后兼容）。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AgentRecord, AgentVersionRecord

# config 白名单键 → 类型（未列出的键拒绝；temperature/max_steps 有额外区间校验）
CONFIG_KEYS: dict[str, type | tuple[type, ...]] = {
    "system_prompt": str,
    "model_prefer": str,
    "temperature": (int, float),
    "tools": list,
    "knowledge": list,
    "skills": list,
    "max_steps": int,
    "output_schema": dict,
    "interaction_schema": dict,
}

_overlay: ContextVar[dict] = ContextVar("eap_agent_overlay", default={})
_cache: dict[tuple[str, str], dict] = {}  # (agent_name, version) -> config


def current_overlay() -> dict:
    """当前调用的配置覆盖层（未发布版本时空 dict）。"""
    return _overlay.get()


@contextmanager
def apply_overlay(config: dict | None):
    """覆盖层作用域：invoke 期间对 SDK 消费点生效，结束复原。"""
    token = _overlay.set(config or {})
    try:
        yield
    finally:
        _overlay.reset(token)


def validate_config(config: dict) -> dict:
    """白名单键 + 类型 + 区间校验；output_schema 必须是合法 JSON Schema。"""
    if not isinstance(config, dict):
        raise ValueError("config 必须是对象")
    unknown = set(config) - set(CONFIG_KEYS)
    if unknown:
        raise ValueError(f"config 含未知键: {sorted(unknown)}，允许: {sorted(CONFIG_KEYS)}")
    for key, value in config.items():
        if key == "temperature":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= 2:
                raise ValueError("config.temperature 必须是 0-2 的数值")
        elif key == "max_steps":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 64:
                raise ValueError("config.max_steps 必须是 1-64 的整数")
        elif isinstance(value, bool) or not isinstance(value, CONFIG_KEYS[key]):
            expected = {str: "字符串", list: "数组", dict: "对象", int: "整数",
                        (int, float): "数值"}[CONFIG_KEYS[key]]
            raise ValueError(f"config.{key} 类型错误：应为{expected}")
    if any(not isinstance(t, str) or not t for t in config.get("tools", [])):
        raise ValueError("config.tools 必须是非空字符串数组")
    if any(not isinstance(k, str) or not k for k in config.get("knowledge", [])):
        raise ValueError("config.knowledge 必须是非空字符串数组")
    if any(not isinstance(s, str) or not s for s in config.get("skills", [])):
        raise ValueError("config.skills 必须是非空字符串数组")
    if config.get("output_schema") is not None:
        check_json_schema(config["output_schema"], field="config.output_schema")
    return config


def check_json_schema(schema, field: str) -> None:
    """校验 schema 本身是合法 JSON Schema（发布期防线，M19 结构化输出共用）。"""
    import jsonschema

    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as e:
        raise ValueError(f"{field} 不是合法的 JSON Schema: {e.message}") from e


def resolve_published_config(db: Session, agent_name: str) -> tuple[str | None, dict | None]:
    """解析 agent 的已发布配置版本 → (version, config)；未发布返回 (None, None)。"""
    record = db.scalar(select(AgentRecord).where(AgentRecord.name == agent_name))
    version = record.published_version if record else None
    if not version:
        return None, None
    cached = _cache.get((agent_name, version))
    if cached is not None:
        return version, cached
    row = db.scalar(select(AgentVersionRecord)
                    .where(AgentVersionRecord.agent_name == agent_name,
                           AgentVersionRecord.version == version))
    # deprecated = 下线过渡态：指针未移交前仍生效（语义同 docs/05 §3 生命周期）
    if row is None or row.state not in ("published", "deprecated"):
        return None, None
    _cache[(agent_name, version)] = row.config or {}
    return version, row.config or {}


def filter_tools(tools: list, whitelist: list | None) -> list:
    """工具白名单过滤：overlay.tools 未设置/为空时原样返回。"""
    if not whitelist:
        return tools
    allowed = set(whitelist)
    return [t for t in tools if getattr(t, "name", "") in allowed]


# ---------- 版本流水线（语义同 runtime/prompts.py） ----------

def create_version(db: Session, agent_name: str, version: str, config: dict, notes: str) -> AgentVersionRecord:
    exists = db.scalar(select(AgentVersionRecord)
                       .where(AgentVersionRecord.agent_name == agent_name,
                              AgentVersionRecord.version == version))
    if exists:
        raise ValueError(f"EAP-2002 Agent {agent_name}@{version} 配置版本已存在")
    record = AgentVersionRecord(agent_name=agent_name, version=version,
                                config=validate_config(config), notes=notes, state="draft")
    db.add(record)
    db.flush()
    return record


def update_draft(db: Session, agent_name: str, version: str,
                 config: dict | None = None, notes: str | None = None) -> AgentVersionRecord:
    target = _get(db, agent_name, version)
    if target.state != "draft":
        raise ValueError(f"EAP-4006 仅 draft 版本可修改（当前 {target.state}）")
    if config is not None:
        target.config = validate_config(config)
    if notes is not None:
        target.notes = notes
    db.flush()
    return target


def publish_version(db: Session, agent_name: str, version: str) -> AgentVersionRecord:
    """发布：校验通过后写入发布指针，旧发布版归档。"""
    target = _get(db, agent_name, version)
    if target.state not in ("draft", "deprecated", "archived"):
        raise ValueError(f"EAP-4006 版本 {version} 当前为 {target.state}，不可发布")
    validate_config(target.config)
    for row in db.scalars(select(AgentVersionRecord)
                          .where(AgentVersionRecord.agent_name == agent_name,
                                 AgentVersionRecord.state == "published")).all():
        if row.version != version:
            row.state = "archived"
    target.state = "published"
    pointer = db.scalar(select(AgentRecord).where(AgentRecord.name == agent_name))
    if pointer is not None:
        pointer.published_version = version
    db.flush()
    return target


def deprecate_version(db: Session, agent_name: str, version: str) -> AgentVersionRecord:
    """下线过渡：published → deprecated（指针保留，回滚/重发布可恢复）。"""
    target = _get(db, agent_name, version)
    if target.state != "published":
        raise ValueError(f"EAP-4006 仅 published 版本可下线（当前 {target.state}）")
    target.state = "deprecated"
    db.flush()
    return target


def rollback_version(db: Session, agent_name: str) -> AgentVersionRecord | None:
    """回滚：把最近一个 archived/deprecated 版本重新发布。"""
    previous = db.scalars(
        select(AgentVersionRecord)
        .where(AgentVersionRecord.agent_name == agent_name,
               AgentVersionRecord.state.in_(("archived", "deprecated")))
        .order_by(AgentVersionRecord.updated_at.desc())).first()
    if previous is None:
        return None
    return publish_version(db, agent_name, previous.version)


def _get(db: Session, agent_name: str, version: str) -> AgentVersionRecord:
    row = db.scalar(select(AgentVersionRecord)
                    .where(AgentVersionRecord.agent_name == agent_name,
                           AgentVersionRecord.version == version))
    if row is None:
        raise KeyError(f"Agent {agent_name}@{version} 配置版本不存在")
    return row
