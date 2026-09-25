"""Workflow 环境保护规则测试（M55-E）：env-protection 策略 kind → publish/rollback 闸门。

覆盖：规则命中 403（allowed_actors 白名单内通过/外拒绝 + 空名单冻结）、require_confirm
两阶段确认（无确认 428 → 带确认成功）、rollback 同样受保护、多环境规则匹配精确度
（PROD 规则不影响 DEV）、config 校验、member 对策略管理 403、审计 workflow.env_protected
落库（detail 仅 env/规则摘要，零敏感值——不落 allowed_actors 名单）。

M52-D 可重入：策略名 uname 唯一化 + 模块级差量清理（样板=test_policies）；工作流名加
模块级 uuid 后缀（脏库不撞唯一约束），模块结束停用自造工作流（样板=test_workflow_versions）。
"""

from __future__ import annotations

import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from eap.db import SessionLocal
from eap.models import AuditLog, PolicyRecord, WorkflowRecord
from eap.runtime.tools import Tool
from eap.runtime.workflow import register_workflow_tool

from .conftest import AUTH
from .test_oidc import ISSUER, _make_id_token, fake_idp  # noqa: F401 复用模拟 IdP 夹具

HEADERS = {**AUTH, "Content-Type": "application/json"}
_SFX = uuid.uuid4().hex[:8]


def _uname(prefix: str) -> str:
    """策略唯一名（M52-D 脏库可重入：uuid 后缀跨运行不撞 policies.name 唯一约束）。"""
    return f"{prefix}-{_SFX}-{uuid.uuid4().hex[:6]}"

# 本模块自造的工作流（模块结束停用，避免残留 enabled 智能体）
F_DENY = f"envp-deny-flow-{_SFX}"
F_CONFIRM = f"envp-confirm-flow-{_SFX}"
F_MULTI = f"envp-multi-flow-{_SFX}"
F_FREEZE = f"envp-freeze-flow-{_SFX}"
F_JWT = f"envp-jwt-flow-{_SFX}"
_ALL_FLOWS = (F_DENY, F_CONFIRM, F_MULTI, F_FREEZE, F_JWT)


@pytest.fixture(scope="module", autouse=True)
def _cleanup_env_protection(client: TestClient):
    """模块结束：删除本模块新增的策略行 + 停用自造工作流（差量清理，样板=test_policies）。"""
    with SessionLocal() as db:
        before = set(db.scalars(select(PolicyRecord.id)).all())
    yield
    for name in _ALL_FLOWS:
        client.delete(f"/api/v1/workflows/{name}", headers=AUTH)  # 404/失败无害
    with SessionLocal() as db:
        for r in db.scalars(select(PolicyRecord)).all():
            if r.id not in before:
                db.delete(r)
        db.commit()


@pytest.fixture()
def _isolate_rules():
    """用例级隔离：结束后停用本用例新建的 env-protection 策略。

    多策略命中同一 env 取最严合并（conjunction），不隔离时上一用例的规则会
    叠加进下一用例的断言（如 test 环境残留 require_confirm 规则 → 意外 428）。
    只动本用例新建的行（快照差量），不碰其他模块/平台的既有策略。
    """
    with SessionLocal() as db:
        before = set(db.scalars(select(PolicyRecord.id)
                                .where(PolicyRecord.kind == "env-protection")).all())
    yield
    with SessionLocal() as db:
        for r in db.scalars(select(PolicyRecord)
                            .where(PolicyRecord.kind == "env-protection")).all():
            if r.id not in before:
                r.enabled = False
        db.commit()


# ---------- 工具与 DSL：handler 回显 marker（版本断言用版本端点而非执行输出，标记仅占位） ----------

async def _marker_handler(args: str) -> str:
    payload = json.loads(args) if args else {}
    return f"envp-marker:{payload.get('value', '')}"


register_workflow_tool("envp.marker", lambda: Tool(
    name="envp.marker", description="环境保护测试标记工具（测试）",
    parameters={"type": "object", "properties": {}}, handler=_marker_handler))


def _dsl(name: str, marker: str) -> dict:
    return {
        "name": name, "version": "1.0.0", "description": f"环境保护测试工作流（{marker}）",
        "steps": [{"id": "mark", "type": "tool", "tool_name": "envp.marker",
                   "tool_args": {"value": marker}}],
    }


def _make(client: TestClient, name: str) -> None:
    resp = client.post("/api/v1/workflows", headers=HEADERS, json=_dsl(name, name))
    assert resp.status_code == 200, resp.text


def _draft(client: TestClient, name: str) -> dict:
    resp = client.post(f"/api/v1/workflows/{name}/versions", headers=HEADERS, json={"note": ""})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _publish(client: TestClient, name: str, version_id: int, env: str, **extra) -> object:
    return client.post(f"/api/v1/workflows/{name}/versions/{version_id}/publish",
                       headers=HEADERS, json={"env": env, **extra})


def _rollback(client: TestClient, name: str, version_id: int, env: str, **extra) -> object:
    return client.post(f"/api/v1/workflows/{name}/versions/{version_id}/rollback",
                       headers=HEADERS, json={"env": env, **extra})


def _policy(client: TestClient, name: str, rules: list[dict]) -> object:
    return client.post("/api/v1/policies", headers=HEADERS,
                       json={"name": name, "kind": "env-protection", "config": {"rules": rules}})


def _env_protected_audits(flow: str) -> list[AuditLog]:
    """该工作流的 workflow.env_protected 审计行（创建序）。"""
    with SessionLocal() as db:
        rows = db.scalars(select(AuditLog)
                          .where(AuditLog.action == "workflow.env_protected",
                                 AuditLog.target == flow)
                          .order_by(AuditLog.id)).all()
        for r in rows:
            db.expunge(r)
        return list(rows)


# ---------- config 校验 ----------

def test_env_protection_config_validation(client, _isolate_rules):
    """rules 形状校验：env 枚举 / actors 字符串数组 / confirm 布尔 / rules 非空 / kind pattern。"""
    name = _uname("envp-bad")
    r = _policy(client, name, [{"env": "production", "allowed_actors": ["api-key"]}])
    assert r.status_code == 400 and "EAP-7102" in r.json()["detail"]
    r = _policy(client, name, [{"env": "prod", "allowed_actors": "api-key"}])
    assert r.status_code == 400 and "EAP-7102" in r.json()["detail"]
    r = _policy(client, name, [{"env": "prod", "allowed_actors": [1, 2]}])
    assert r.status_code == 400 and "EAP-7102" in r.json()["detail"]
    r = _policy(client, name, [{"env": "prod", "allowed_actors": [], "require_confirm": "yes"}])
    assert r.status_code == 400 and "EAP-7102" in r.json()["detail"]
    r = _policy(client, name, [])
    assert r.status_code == 400 and "EAP-7102" in r.json()["detail"]
    r = client.post("/api/v1/policies", headers=HEADERS,
                    json={"name": name, "kind": "env-protection", "config": {}})
    assert r.status_code == 400 and "EAP-7102" in r.json()["detail"]
    # 合法配置可创建（空名单 = 冻结语义是合法配置）
    r = _policy(client, _uname("envp-ok"), [{"env": "staging", "allowed_actors": []}])
    assert r.status_code == 200


def test_env_protection_policy_rbac(client: TestClient, fake_idp):  # noqa: F811
    """member 对策略管理 403（require_admin 守卫，EAP-3xxx RBAC 域）。"""
    member = _make_id_token({"iss": ISSUER, "sub": f"member-envp-{_SFX}@corp",
                             "exp": int(time.time()) + 600, "tenant_id": 1, "roles": ["member"]})
    r = client.post("/api/v1/policies", headers={"Authorization": f"Bearer {member}"},
                    json={"name": _uname("envp-member"), "kind": "env-protection",
                          "config": {"rules": [{"env": "prod", "allowed_actors": []}]}})
    assert r.status_code == 403 and "EAP-3" in r.json()["detail"]


# ---------- 白名单 403 + 审计 ----------

def test_allowlist_deny_403_publish_and_rollback(client, _isolate_rules):
    """操作者不在 allowed_actors：publish 与 rollback 均 403 EAP-3010，审计落库（零敏感值）。"""
    _make(client, F_DENY)
    v = _draft(client, F_DENY)
    # PROD 规则：只允许其他 JWT 用户 → api-key 通道被拒
    name = _uname("envp-deny")
    assert _policy(client, name, [{"env": "prod", "allowed_actors": ["jwt:someone-else@corp"]}]).status_code == 200
    # 白名单外的 dev 发布不受影响（多环境精确度：PROD 规则不影响 DEV）
    assert _publish(client, F_DENY, v["id"], "dev").status_code == 200
    r = _publish(client, F_DENY, v["id"], "prod")
    assert r.status_code == 403 and "EAP-3010" in r.json()["detail"] and name in r.json()["detail"]
    r = _rollback(client, F_DENY, v["id"], "prod")
    assert r.status_code == 403 and "EAP-3010" in r.json()["detail"]
    # 审计：两次拒绝各一行，detail 只含 env/op/outcome/policies，不含 allowed_actors 名单
    rows = _env_protected_audits(F_DENY)
    assert len(rows) == 2
    for row in rows:
        assert row.detail["outcome"] == "actor_not_allowed"
        assert row.detail["env"] == "prod" and row.detail["op"] in ("publish", "rollback")
        assert [name] == row.detail["policies"]
        assert "allowed_actors" not in json.dumps(row.detail)
    # 拒绝路径不产生状态变化：prod 无发布、版本仍在 dev
    with SessionLocal() as db:
        wf = db.scalar(select(WorkflowRecord).where(WorkflowRecord.name == F_DENY))
        assert wf is not None and wf.published_version_id is None


def test_empty_allowlist_freezes_env(client, _isolate_rules):
    """空名单 = 冻结该环境：任何操作者（含 api-key）都 403，其余环境不受影响。"""
    _make(client, F_FREEZE)
    v = _draft(client, F_FREEZE)
    assert _policy(client, _uname("envp-freeze"),
                   [{"env": "staging", "allowed_actors": []}]).status_code == 200
    r = _publish(client, F_FREEZE, v["id"], "staging")
    assert r.status_code == 403 and "EAP-3010" in r.json()["detail"]
    assert _publish(client, F_FREEZE, v["id"], "dev").status_code == 200


# ---------- require_confirm 两阶段确认 ----------

def test_require_confirm_two_phase_publish_and_rollback(client, _isolate_rules):
    """名单内操作者 + require_confirm：无确认 428 EAP-3011 → 带 confirm=true 成功（publish 与 rollback）。"""
    _make(client, F_CONFIRM)
    v1 = _draft(client, F_CONFIRM)
    assert _policy(client, _uname("envp-confirm"),
                   [{"env": "test", "allowed_actors": ["api-key"], "require_confirm": True}]).status_code == 200
    # 第一阶段：未带确认 → 428，状态不变
    r = _publish(client, F_CONFIRM, v1["id"], "test")
    assert r.status_code == 428 and "EAP-3011" in r.json()["detail"]
    # 第二阶段：显式确认 → 成功
    r = _publish(client, F_CONFIRM, v1["id"], "test", confirm=True)
    assert r.status_code == 200 and r.json()["env"] == "test" and r.json()["state"] == "published"
    # rollback 同样两阶段：先发布 v2 制造可回滚点
    v2 = _draft(client, F_CONFIRM)
    assert _publish(client, F_CONFIRM, v2["id"], "test", confirm=True).status_code == 200
    assert _rollback(client, F_CONFIRM, v2["id"], "test").status_code == 428
    r = _rollback(client, F_CONFIRM, v2["id"], "test", confirm=True)
    assert r.status_code == 200 and r.json()["state"] == "published"
    # confirm_required 与最终成功都留痕：env_protected 审计 2 行（publish + rollback 各一）
    rows = _env_protected_audits(F_CONFIRM)
    assert [row.detail["op"] for row in rows] == ["publish", "rollback"]
    assert all(row.detail["outcome"] == "confirm_required" for row in rows)


def test_jwt_admin_actor_in_allowlist(client: TestClient, fake_idp, _isolate_rules):  # noqa: F811
    """jwt:<user> 形态操作者与名单精确匹配：admin JWT 在名单内走两阶段确认后发布成功。"""
    _make(client, F_JWT)
    v = _draft(client, F_JWT)
    sub = f"admin-envp-{_SFX}@corp"
    admin = _make_id_token({"iss": ISSUER, "sub": sub, "exp": int(time.time()) + 600,
                            "tenant_id": 1, "roles": ["admin"]})
    h = {"Authorization": f"Bearer {admin}"}
    # 名单只有别人 → admin JWT 也 403（actor 精确匹配）
    name1 = _uname("envp-jwt")
    assert _policy(client, name1,
                   [{"env": "prod", "allowed_actors": ["jwt:other-admin@corp"]}]).status_code == 200
    r = client.post(f"/api/v1/workflows/{F_JWT}/versions/{v['id']}/publish",
                    headers=h, json={"env": "prod"})
    assert r.status_code == 403 and "EAP-3010" in r.json()["detail"]
    # 多策略按最严合并（conjunction）：并存时 name1 的 deny 仍生效，先停用再换名单
    assert client.post(f"/api/v1/policies/{name1}/enabled?enabled=false",
                       headers=HEADERS).json()["enabled"] is False
    # 名单换成 admin 的 jwt:<sub>（对齐 audit.actor_of 形态）+ require_confirm → 两阶段成功
    assert _policy(client, _uname("envp-jwt2"),
                   [{"env": "prod", "allowed_actors": [f"jwt:{sub}"], "require_confirm": True}]).status_code == 200
    r = client.post(f"/api/v1/workflows/{F_JWT}/versions/{v['id']}/publish", headers=h, json={"env": "prod"})
    assert r.status_code == 428 and "EAP-3011" in r.json()["detail"]
    r = client.post(f"/api/v1/workflows/{F_JWT}/versions/{v['id']}/publish",
                    headers=h, json={"env": "prod", "confirm": True})
    assert r.status_code == 200 and r.json()["env"] == "prod"


def test_multi_env_rule_precision(client, _isolate_rules):
    """多环境规则：逐规则精确匹配各自 env，最严者生效（deny 优先于 confirm）。"""
    _make(client, F_MULTI)
    v = _draft(client, F_MULTI)
    name = _uname("envp-multi")
    assert _policy(client, name, [
        {"env": "prod", "allowed_actors": ["jwt:release-manager@corp"]},   # api-key 不在名单 → deny
        {"env": "dev", "allowed_actors": ["api-key"], "require_confirm": False},  # api-key 在名单 → 放行
    ]).status_code == 200
    # dev 规则命中且在名单、无需确认 → 直接成功
    assert _publish(client, F_MULTI, v["id"], "dev").status_code == 200
    # prod 规则命中但不在名单 → 403（deny 不被 dev 规则稀释）
    assert _publish(client, F_MULTI, v["id"], "prod").status_code == 403
    # test 无任何规则命中 → 原样放行（无 confirm 要求）
    assert _publish(client, F_MULTI, v["id"], "test").status_code == 200
