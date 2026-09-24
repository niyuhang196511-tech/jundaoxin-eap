"""知识库文档级 ACL（M36/L6，设计 docs/17）：租户隔离之上的文档/角色级访问控制。

- 判定语义（docs/17 §三，纯函数 is_allowed）：deny 优先 → allow → 默认可见（存量零破坏，
  与租户过滤叠加而非替代）
- 主体：role（JWT claim / API Key=admin / embed=embed）/ user；"*" 通配
- 过滤点：knowledge/service._retrieve_inner 在 RRF 融合后、池选取前单点过滤（三路全生效）
- 上下文：resolve_tenant 单点下发（JWT/API Key/embed 三通道），任务通道经 payload._acl
  快照重建（与 M24 _tenant_id 同法）
- 规则缓存：per kb_id 进程内 TTL 30s（企业规则量级小；管理端写入即失效）
"""

from __future__ import annotations

import contextvars
import time
from dataclasses import dataclass

from sqlalchemy import select

DENY = "deny"
ALLOW = "allow"


@dataclass(frozen=True)
class AclContext:
    """请求主体上下文：检索过滤的判定依据（无上下文 = 默认可见，测试/内部路径）。"""

    tenant_id: int | None = None
    user_id: str | None = None
    roles: tuple[str, ...] = ()

    def subjects(self) -> tuple[str, ...]:
        """主体的 subject 值集合：角色 + 用户 id + 通配（user 通道同时带 user id 主体）。"""
        subs = ["*"]
        if self.user_id:
            subs.append(self.user_id)
        subs.extend(self.roles)
        return tuple(subs)


_acl_ctx: contextvars.ContextVar[AclContext | None] = contextvars.ContextVar(
    "eap_acl_ctx", default=None)


def set_acl_context(ctx: AclContext) -> contextvars.Token:
    return _acl_ctx.set(ctx)


def reset_acl_context(token: contextvars.Token) -> None:
    _acl_ctx.reset(token)


def current_acl() -> AclContext | None:
    return _acl_ctx.get()


def acl_from_payload(payload: dict) -> AclContext | None:
    """任务通道：payload._acl 快照 → AclContext（M24 _tenant_id 同法；无快照返回 None）。"""
    acl = payload.get("_acl")
    if not isinstance(acl, dict):
        return None
    roles = acl.get("roles") or []
    return AclContext(tenant_id=acl.get("tenant_id"),
                      user_id=acl.get("user_id"),
                      roles=tuple(roles))


def is_allowed(rules: list[dict], ctx: AclContext) -> bool:
    """纯函数判定（docs/17 §三）：deny 命中 → 拒绝；allow 命中 → 允许；未命中 → 默认可见。

    allow 规则在单规则集内不改变结果（默认可见语义）——它的结构作用在 filter_doc_ids：
    文档级规则集整体覆盖 KB 级默认规则集（KB deny-all + 文档 allow = 定向放行）。
    rules：[{effect, subject_type, subject}]；user 主体要求 user_id 精确相等，
    role 主体要求角色命中，subject="*" 通配所有主体。
    """
    for r in rules:
        st, subject, effect = r.get("subject_type"), r.get("subject"), r.get("effect")
        # 通配 "*" 独立于主体类型；user/role 各自精确命中
        if subject != "*":
            if st == "user":
                if not ctx.user_id or subject != ctx.user_id:
                    continue
            elif st == "role":
                if subject not in ctx.roles:
                    continue
        if effect == DENY:
            return False
        if effect == ALLOW:
            return True
    return True  # 默认可见（无规则或全部未命中）


# ---------- 规则加载（TTL 缓存） ----------

_RULES_TTL = 30.0
_cache: dict[int, tuple[float, list[dict]]] = {}  # kb_id → ( Monotonic 时间戳, rules )


def invalidate(kb_id: int | None = None) -> None:
    """规则缓存失效（管理端写入/删除时调用；None=全清，测试用）。"""
    if kb_id is None:
        _cache.clear()
    else:
        _cache.pop(kb_id, None)


def load_rules(db, kb_id: int) -> list[dict]:
    """kb 的规则集（含 KB 级默认 document_id=None），TTL 缓存。"""
    from ..models import DocumentACL

    now = time.monotonic()
    cached = _cache.get(kb_id)
    if cached and now - cached[0] < _RULES_TTL:
        return cached[1]
    rows = db.scalars(select(DocumentACL).where(DocumentACL.kb_id == kb_id)).all()
    rules = [{"effect": r.effect, "subject_type": r.subject_type, "subject": r.subject,
              "document_id": r.document_id} for r in rows]
    _cache[kb_id] = (now, rules)
    return rules


def filter_doc_ids(db, kb_id: int, doc_ids: list[int | None],
                   ctx: "AclContext | None" = None) -> tuple[set[int], int]:
    """按 ACL 过滤 document_id 集合 → (允许的 doc_id 集, 被过滤计数)。

    ctx 显式传入优先（sync 端点从 request.state 构建）；否则读 ContextVar（异步链路）；
    两者皆无（内部/测试路径）→ 全部允许，计数 0（存量兼容）。
    KB 级默认规则（document_id=None）作用于全部文档；文档级规则覆盖默认。
    """
    ctx = ctx or current_acl()
    doc_set = {d for d in doc_ids if d is not None}
    if ctx is None or not doc_set:
        return doc_set, 0
    rules = load_rules(db, kb_id)
    if not rules:
        return doc_set, 0
    kb_rules = [r for r in rules if r.get("document_id") is None]
    doc_rules: dict[int, list[dict]] = {}
    for r in rules:
        if r.get("document_id") is not None:
            doc_rules.setdefault(r["document_id"], []).append(r)
    allowed: set[int] = set()
    filtered = 0
    for d in doc_set:
        effective = doc_rules.get(d, kb_rules)  # 文档级规则存在则覆盖 KB 级默认
        if is_allowed(effective, ctx):
            allowed.add(d)
        else:
            filtered += 1
    return allowed, filtered
