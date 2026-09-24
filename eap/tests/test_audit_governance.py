"""审计日志治理测试（M47-B）：导出（CSV/JSON/过滤/行数上限/RBAC/审计留痕）+ 保留期清理。

导出：GET /api/v1/audit/export（admin，复用查询端点过滤语义，流式 + 硬上限防拖库，
导出动作落审计 audit.export 仅条数与过滤条件）。
保留期：POST /api/v1/audit/purge（admin，对齐 memory /purge 模式；0=永久保留不删，
动作落审计 audit.retention.purge 仅条数）。
"""

from __future__ import annotations

import csv as csv_mod
import io
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from .conftest import AUTH
from .test_jwt_auth import _access_token
from .test_oidc import fake_idp  # noqa: F401  复用模拟 IdP 夹具（fixture 再导出）

from eap.db import SessionLocal
from eap.models import AuditLog


def _insert(action: str, target: str, *, detail: dict | None = None,
            created_at: datetime | None = None) -> int:
    """直接造审计行（含保留期测试所需的超龄 created_at）。"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with SessionLocal() as db:
        row = AuditLog(action=action, actor="m47b", target=target, trace_id="m47b",
                       detail=detail or {}, created_at=created_at or now)
        db.add(row)
        db.commit()
        return row.id


# ---------- 动作目录（M50-B2）：GET /api/v1/audit/actions ----------


def test_actions_catalog_shape(client: TestClient):
    """动作目录端点：扁平数组 = AUDIT_ACTIONS 原样（文档性质，供过滤 datalist 提示）。"""
    from eap.observability.audit_actions import AUDIT_ACTIONS

    resp = client.get("/api/v1/audit/actions", headers=AUTH)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data, list) and data == list(AUDIT_ACTIONS)
    # 代表性动作抽查（各域均有登记）
    assert {"model.register", "release.promote", "trigger.create", "audit.export",
            "eval.review.submit", "shadow.create", "memory.purge",
            "workflow.version.publish"} <= set(data)


def test_actions_requires_admin(client: TestClient, fake_idp):  # noqa: F811  fixture 再导出
    """目录端点鉴权与该 router 查询端点一致：member JWT → 403。"""
    member = {"Authorization": f"Bearer {_access_token(roles=['member'])}"}
    assert client.get("/api/v1/audit/actions", headers=member).status_code == 403


# ---------- 导出：形态 ----------


def test_export_csv_shape(client: TestClient):
    """CSV 导出：附件下载头 + UTF-8 BOM + 表头列序 + detail 为 JSON 字符串 + 过滤生效。"""
    _insert("m47b.csv.op", "m47b-csv", detail={"k": "中文值", "n": 1})
    resp = client.get("/api/v1/audit/export", headers=AUTH,
                      params={"format": "csv", "action": "m47b.csv.op"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    dispo = resp.headers["content-disposition"]
    assert dispo.startswith("attachment") and "audit-export-" in dispo and dispo.endswith('.csv"')
    body = resp.content.decode("utf-8")
    assert body.startswith("\ufeff")  # BOM：Excel 直开中文不乱码
    rows = list(csv_mod.DictReader(io.StringIO(body.lstrip("\ufeff"))))
    assert rows, "至少导出一条"
    assert list(rows[0].keys()) == ["id", "created_at", "action", "actor", "target",
                                    "trace_id", "detail"]
    assert all(r["action"] == "m47b.csv.op" for r in rows)  # 过滤条件生效
    assert any(r["target"] == "m47b-csv" for r in rows)
    assert any(r["trace_id"] == "m47b" for r in rows)
    detail = json.loads(rows[0]["detail"])  # detail 列为 JSON 字符串
    assert isinstance(detail, dict)


def test_export_json_shape(client: TestClient):
    """JSON 导出：对象数组，含全部导出列，detail 为对象。"""
    _insert("m47b.json.op", "m47b-json", detail={"k": "v"})
    resp = client.get("/api/v1/audit/export", headers=AUTH,
                      params={"format": "json", "action": "m47b.json.op"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.headers["content-disposition"].endswith('.json"')
    data = resp.json()
    assert isinstance(data, list) and data
    assert {"id", "created_at", "action", "actor", "target", "trace_id", "detail"} <= set(data[0])
    assert all(r["action"] == "m47b.json.op" for r in data)
    assert any(r["detail"] == {"k": "v"} for r in data)


# ---------- 导出：过滤语义与上限 ----------


def test_export_filters(client: TestClient):
    """导出过滤复用查询端点语义：target 前缀 / since 边界 / 非法时间 400 / 非法 format 422。"""
    _insert("m47b.filter.op", "m47b-f-alpha")
    _insert("m47b.filter.op", "m47b-f-beta")
    # target 前缀匹配
    data = client.get("/api/v1/audit/export", headers=AUTH,
                      params={"format": "json", "target": "m47b-f-"}).json()
    assert {r["target"] for r in data} >= {"m47b-f-alpha", "m47b-f-beta"}
    data = client.get("/api/v1/audit/export", headers=AUTH,
                      params={"format": "json", "target": "m47b-f-al"}).json()
    assert {r["target"] for r in data} == {"m47b-f-alpha"}
    # since 在未来 → 空
    data = client.get("/api/v1/audit/export", headers=AUTH,
                      params={"format": "json", "since": "2099-01-01"}).json()
    assert data == []
    # 非法时间 → 400（与查询端点一致）
    assert client.get("/api/v1/audit/export", headers=AUTH,
                      params={"since": "not-a-date"}).status_code == 400
    # 非法 format → 422
    assert client.get("/api/v1/audit/export", headers=AUTH,
                      params={"format": "xml"}).status_code == 422


def test_export_row_limit(client: TestClient, monkeypatch):
    """行数上限：请求 limit 取更小值生效；EAP_AUDIT_EXPORT_LIMIT 为硬上限（monkeypatch 模拟收紧）。"""
    for i in range(5):
        _insert("m47b.limit.op", f"m47b-limit-{i}")
    # 显式 limit=2 → 恰好 2 条（最新优先）
    data = client.get("/api/v1/audit/export", headers=AUTH,
                      params={"format": "json", "action": "m47b.limit.op", "limit": 2}).json()
    assert len(data) == 2
    # limit=0 视为未指定 → 回落配置上限（默认 50000，5 条全出）
    data = client.get("/api/v1/audit/export", headers=AUTH,
                      params={"format": "json", "action": "m47b.limit.op", "limit": 0}).json()
    assert len(data) == 5
    # 硬上限不可关闭/突破：配置 3 → 即使不传 limit 也最多 3 条
    from eap.api.v1 import audit as audit_api

    class _Stub:
        audit_export_limit = 3

    monkeypatch.setattr(audit_api, "get_settings", lambda: _Stub())
    data = client.get("/api/v1/audit/export", headers=AUTH,
                      params={"format": "json", "action": "m47b.limit.op"}).json()
    assert len(data) == 3


# ---------- 导出：RBAC 与审计留痕 ----------


def test_export_requires_admin(client: TestClient, fake_idp):  # noqa: F811  fixture 再导出
    """导出为 admin 语义：member JWT → 403（与查询端点一致）；purge 同。"""
    member = {"Authorization": f"Bearer {_access_token(roles=['member'])}"}
    assert client.get("/api/v1/audit/export", headers=member).status_code == 403
    assert client.post("/api/v1/audit/purge", headers=member).status_code == 403


def test_export_action_audited(client: TestClient):
    """导出动作自身落审计 audit.export：仅条数与过滤条件（format/count/limit），不含导出内容。"""
    _insert("m47b.audited.op", "m47b-audited")
    resp = client.get("/api/v1/audit/export", headers=AUTH,
                      params={"format": "json", "action": "m47b.audited.op"})
    assert resp.status_code == 200
    logs = client.get("/api/v1/audit", headers=AUTH, params={"action": "audit.export"}).json()
    assert logs
    entry = logs[0]  # 最新一条即本次导出
    assert entry["detail"]["format"] == "json"
    assert entry["detail"]["count"] == 1
    assert entry["detail"]["action"] == "m47b.audited.op"
    assert entry["actor"] == "api-key"


# ---------- 保留期清理 ----------


def test_retention_purge_deletes_old_rows(client: TestClient):
    """造超龄行（created_at < now-365d）→ purge 后消失；新行保留；返回清理条数。"""
    _insert("m47b.legacy.op", "m47b-old", created_at=datetime.now(timezone.utc)
            .replace(tzinfo=None) - timedelta(days=400))
    _insert("m47b.recent.op", "m47b-recent")
    resp = client.post("/api/v1/audit/purge", headers=AUTH, params={"retention_days": 365})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["retention_days"] == 365 and body["deleted"] >= 1
    assert client.get("/api/v1/audit", headers=AUTH,
                      params={"target": "m47b-old"}).json() == []
    assert client.get("/api/v1/audit", headers=AUTH, params={"target": "m47b-recent"}).json()


def test_retention_zero_disables_purge(client: TestClient):
    """retention=0 = 永久保留：不删任何行。"""
    _insert("m47b.keep.op", "m47b-keep-old", created_at=datetime.now(timezone.utc)
            .replace(tzinfo=None) - timedelta(days=400))
    resp = client.post("/api/v1/audit/purge", headers=AUTH, params={"retention_days": 0})
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 0, "retention_days": 0}
    assert client.get("/api/v1/audit", headers=AUTH, params={"target": "m47b-keep-old"}).json()


def test_retention_purge_audited(client: TestClient):
    """purge 动作落审计 audit.retention.purge（仅条数与保留参数，不含内容）。"""
    _insert("m47b.legacy.op", "m47b-old-2", created_at=datetime.now(timezone.utc)
            .replace(tzinfo=None) - timedelta(days=400))
    resp = client.post("/api/v1/audit/purge", headers=AUTH, params={"retention_days": 365})
    deleted = resp.json()["deleted"]
    logs = client.get("/api/v1/audit", headers=AUTH,
                      params={"action": "audit.retention.purge"}).json()
    assert logs
    entry = logs[0]  # 最新一条即本次 purge
    assert entry["detail"] == {"retention_days": 365, "deleted": deleted}
    assert entry["actor"] == "api-key"
