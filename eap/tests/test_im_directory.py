"""M55-C IM 通讯录/群管理代理查询测试（docs/10 遗留「通讯录/群管理应用 API」）：

- 三平台代理出站统一注入 fake（模式同 test_im_remote.capture_outbound——monkeypatch
  runtime/im 的 get_json/post_json 原语），按 URL 特征返回确定性平台形态响应，
  断言平台无关归一化（user_id/name/department_ids + email/mobile 缺失省略）
- 语义：404 渠道不存在 / 400 缺应用凭据 / 502 上游错误（透传脱敏摘要，不含凭据）
- PII 纪律：审计 im.directory.query detail 零手机号/邮箱明文（仅记条数与查询摘要）
- 鉴权对齐渠道管理端点（ADMIN_DEP）：无凭证 401
真实平台联调（真实飞书/钉钉/企微凭证）= L3 外部条件，本文件全部离线确定性。
渠道名模块级唯一后缀（样板同 test_im.py._SFX），脏库可重入；审计断言按 target
（渠道名）过滤，跨运行不串扰。
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from .conftest import AUTH

HEADERS = {**AUTH, "Content-Type": "application/json"}

_SFX = uuid.uuid4().hex[:8]

# 捕获的代理出站调用 {method, url, params|payload, headers}（token 获取 + 业务查询）
CALLS: list[dict] = []

# 审计断言用的确定性 PII 样本（响应体可含、审计 detail 不可含）
_PII_MOBILE = "+8613800000001"
_PII_EMAIL = "zhang@example.com"


@pytest.fixture(autouse=True)
def fake_upstream(monkeypatch):
    """捕获并按 URL 特征应答三平台通讯录 API（离线确定性，真实平台联调=L3 外部条件）。"""
    CALLS.clear()
    from eap.runtime import im as im_rt

    async def fake_get(url, params=None, headers=None, timeout=10.0):
        CALLS.append({"method": "GET", "url": url, "params": params or {},
                      "headers": headers or {}})
        if "gettoken" in url:  # 钉钉 /gettoken、企微 /cgi-bin/gettoken
            return {"status": 200, "body": {"errcode": 0, "access_token": "tok-x"}}
        if "find_by_department" in url:  # 飞书通讯录用户
            return {"status": 200, "body": {"code": 0, "data": {"items": [
                {"user_id": "u-001", "name": "张三", "department_ids": ["1", "10"],
                 "email": _PII_EMAIL, "mobile": _PII_MOBILE},
                {"open_id": "ou-002", "name": "李四", "department_ids": ["2"]},
            ], "has_more": True, "page_token": "PT-NEXT"}}}
        if "/open-apis/im/v1/chats" in url:  # 飞书群列表
            return {"status": 200, "body": {"code": 0, "data": {"items": [
                {"chat_id": "oc-1", "name": "EAP 客户群"}], "has_more": False}}}
        if "/cgi-bin/user/list" in url:  # 企微通讯录用户
            return {"status": 200, "body": {"errcode": 0, "userlist": [
                {"userid": "wx-001", "name": "赵六", "department": [3],
                 "email": "z@example.com", "mobile": "13700000003"}],
                "next_cursor": "C2"}}
        if "/chat/list" in url:  # 钉钉群列表
            return {"status": 200, "body": {"errcode": 0, "results": [
                {"chatid": "cid-dt-1", "title": "钉钉值班群"}], "next_cursor": 7}}
        if "appchat/list" in url:  # 企微群列表
            return {"status": 200, "body": {"errcode": 0, "chatlist": [
                {"chatid": "cid-wx-1", "name": "企微售后群"}], "next_cursor": ""}}
        return {"status": 404, "body": {"errcode": 999, "errmsg": "not-found"}}

    async def fake_post(url, payload=None, timeout=10.0):
        CALLS.append({"method": "POST", "url": url, "payload": payload or {}, "headers": {}})
        if "tenant_access_token" in url:  # 飞书租户令牌
            return {"status": 200, "body": {"code": 0, "tenant_access_token": "tok-fs"}}
        if "topapi/v2/user/list" in url:  # 钉钉通讯录用户
            return {"status": 200, "body": {"errcode": 0, "result": {"list": [
                {"userid": "dt-001", "name": "王五", "dept_id_list": [1, 20],
                 "email": "w@example.com", "mobile": "13900000002"}],
                "has_more": True, "next_cursor": 42}}}
        return {"status": 200, "body": {"errcode": 0}}

    monkeypatch.setattr(im_rt, "get_json", fake_get)
    monkeypatch.setattr(im_rt, "post_json", fake_post)


def _create_channel(client: TestClient, name: str, platform: str, **kw) -> dict:
    body = {"name": name, "platform": platform, "agent": "faq-agent",
            "webhook_url": f"https://hook.example/{name}", **kw}
    r = client.post("/api/v1/im/channels", headers=HEADERS, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _cred_channel(client: TestClient, name: str, platform: str) -> dict:
    """带应用级凭据的渠道（app_secret 入库即 Fernet 加密，响应不回显）。"""
    return _create_channel(client, name, platform,
                           app_id=f"app-{platform}-{_SFX}", app_secret=f"sec-{_SFX}")


def _token_call() -> dict | None:
    return next((c for c in CALLS if "token" in c["url"]), None)


# ---------- 飞书：归一化 + 分页 + 页内过滤 ----------

def test_directory_users_feishu_normalized(client: TestClient):
    """飞书用户代理查询：token 前置获取 → Bearer 携带 → 平台无关归一化；
    email/mobile 缺失如实省略（李四无 email/mobile 键）。"""
    ch = _cred_channel(client, f"fs-dir-{_SFX}", "feishu")
    r = client.get(f"/api/v1/im/channels/{ch['name']}/directory/users",
                   headers=AUTH, params={"department_id": "10", "page_size": "20"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["next_page_token"] == "PT-NEXT"  # has_more=True → 透传不透明游标
    items = body["items"]
    assert items[0] == {"user_id": "u-001", "name": "张三", "department_ids": ["1", "10"],
                        "email": _PII_EMAIL, "mobile": _PII_MOBILE}
    # open_id 兜底 + 缺失字段如实省略
    assert items[1]["user_id"] == "ou-002" and "email" not in items[1] and "mobile" not in items[1]
    # 出站形态：先取 tenant_access_token，再携 Bearer 查询，参数透传
    tok = _token_call()
    assert tok is not None and "tenant_access_token" in tok["url"]
    assert tok["payload"]["app_id"] == ch["app_id"]
    q = next(c for c in CALLS if "find_by_department" in c["url"])
    assert q["headers"]["Authorization"] == "Bearer tok-fs"
    assert q["params"]["department_id"] == "10" and q["params"]["page_size"] == 20


def test_directory_users_feishu_keyword_page_local(client: TestClient):
    """keyword 页内过滤（三平台列表 API 无服务端关键字）：命中 1 条；
    平台分页游标仍透传（翻页语义不被页内过滤破坏）。"""
    ch = _cred_channel(client, f"fs-dir-kw-{_SFX}", "feishu")
    r = client.get(f"/api/v1/im/channels/{ch['name']}/directory/users",
                   headers=AUTH, params={"keyword": "李四"})
    assert r.status_code == 200
    body = r.json()
    assert [u["name"] for u in body["items"]] == ["李四"]
    assert body["next_page_token"] == "PT-NEXT"


def test_directory_chats_feishu(client: TestClient):
    ch = _cred_channel(client, f"fs-chat-{_SFX}", "feishu")
    r = client.get(f"/api/v1/im/channels/{ch['name']}/directory/chats", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == [{"chat_id": "oc-1", "name": "EAP 客户群"}]
    assert body["next_page_token"] == ""
    assert _token_call() is not None  # 群列表同样走应用级 token


# ---------- 钉钉 / 企微：平台差异处理 ----------

def test_directory_users_dingtalk(client: TestClient):
    """钉钉：GET gettoken → POST topapi/v2/user/list（token 在 query）；dept_id_list
    整数列表归一化为字符串；数字游标 next_cursor → next_page_token。"""
    ch = _cred_channel(client, f"dt-dir-{_SFX}", "dingtalk")
    r = client.get(f"/api/v1/im/channels/{ch['name']}/directory/users",
                   headers=AUTH, params={"page_token": "7"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"] == [{"user_id": "dt-001", "name": "王五",
                              "department_ids": ["1", "20"],
                              "email": "w@example.com", "mobile": "13900000002"}]
    assert body["next_page_token"] == "42"
    tok = next(c for c in CALLS if "gettoken" in c["url"])
    assert tok["params"]["appkey"] == ch["app_id"]
    q = next(c for c in CALLS if "topapi/v2/user/list" in c["url"])
    assert "access_token=tok-x" in q["url"]
    assert q["payload"] == {"dept_id": 1, "cursor": 7, "size": 50}  # 缺省根部门 + 游标透传


def test_directory_users_wecom(client: TestClient):
    """企微：corpid/corpsecret 取 token → user/list（fetch_child=1 含子部门）；
    department 列表归一化；next_cursor 透传。"""
    ch = _cred_channel(client, f"wx-dir-{_SFX}", "wecom")
    r = client.get(f"/api/v1/im/channels/{ch['name']}/directory/users",
                   headers=AUTH, params={"department_id": "3"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"][0]["user_id"] == "wx-001"
    assert body["items"][0]["department_ids"] == ["3"]
    assert body["next_page_token"] == "C2"
    tok = next(c for c in CALLS if "gettoken" in c["url"])
    assert tok["params"]["corpid"] == ch["app_id"]
    q = next(c for c in CALLS if "/cgi-bin/user/list" in c["url"])
    assert q["params"]["access_token"] == "tok-x"
    assert q["params"]["department_id"] == "3" and q["params"]["fetch_child"] == "1"


def test_directory_chats_dingtalk_and_wecom(client: TestClient):
    dt = _cred_channel(client, f"dt-chat-{_SFX}", "dingtalk")
    r = client.get(f"/api/v1/im/channels/{dt['name']}/directory/chats", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["items"] == [{"chat_id": "cid-dt-1", "name": "钉钉值班群"}]

    wx = _cred_channel(client, f"wx-chat-{_SFX}", "wecom")
    r = client.get(f"/api/v1/im/channels/{wx['name']}/directory/chats", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["items"] == [{"chat_id": "cid-wx-1", "name": "企微售后群"}]


# ---------- 失败语义 / 鉴权 / 停用渠道 ----------

def test_directory_missing_credentials_400(client: TestClient):
    """缺应用级凭据（未配 app_id/app_secret）→ 400 明确说明。"""
    ch = _create_channel(client, f"fs-nocred-{_SFX}", "feishu")
    r = client.get(f"/api/v1/im/channels/{ch['name']}/directory/users", headers=AUTH)
    assert r.status_code == 400
    assert "凭据" in r.json()["detail"]
    assert CALLS == []  # 未发起任何代理出站


def test_directory_unknown_channel_404(client: TestClient):
    for kind in ("users", "chats"):
        r = client.get(f"/api/v1/im/channels/no-such-{_SFX}/directory/{kind}", headers=AUTH)
        assert r.status_code == 404


def test_directory_upstream_error_502_sanitized(client: TestClient, monkeypatch):
    """上游 HTTP 错误 → 502 透传脱敏摘要：不含渠道凭据/URL（token 在钉钉 query 上）。"""
    from eap.runtime import im as im_rt

    ch = _cred_channel(client, f"dt-err-{_SFX}", "dingtalk")

    async def fail_get(url, params=None, headers=None, timeout=10.0):
        return {"status": 403, "body": {"errcode": 60020, "errmsg": "IP not in whitelist"}}

    monkeypatch.setattr(im_rt, "get_json", fail_get)
    r = client.get(f"/api/v1/im/channels/{ch['name']}/directory/users", headers=AUTH)
    assert r.status_code == 502, r.text
    detail = r.json()["detail"]
    assert "HTTP 403" in detail
    assert f"sec-{_SFX}" not in detail and ch["app_id"] not in detail  # 凭据不入错误摘要

    # 业务码非 0（HTTP 200）同样 502，msg 字段透传
    async def biz_fail(url, params=None, headers=None, timeout=10.0):
        return {"status": 200, "body": {"errcode": 60103, "errmsg": "dept not found"}}

    monkeypatch.setattr(im_rt, "get_json", biz_fail)
    r = client.get(f"/api/v1/im/channels/{ch['name']}/directory/chats", headers=AUTH)
    assert r.status_code == 502
    assert "60103" in r.json()["detail"]


def test_directory_requires_auth(client: TestClient):
    """鉴权对齐渠道管理端点（ADMIN_DEP）：无凭证 401。"""
    ch = _cred_channel(client, f"fs-auth-{_SFX}", "feishu")
    for kind in ("users", "chats"):
        r = client.get(f"/api/v1/im/channels/{ch['name']}/directory/{kind}")
        assert r.status_code == 401


def test_directory_disabled_channel_allowed(client: TestClient):
    """停用渠道允许代理查询：enabled 管消息流，不影响管理面只读查询。"""
    ch = _cred_channel(client, f"fs-off-{_SFX}", "feishu")
    client.post(f"/api/v1/im/channels/{ch['name']}/enabled?enabled=false", headers=HEADERS)
    r = client.get(f"/api/v1/im/channels/{ch['name']}/directory/users", headers=AUTH)
    assert r.status_code == 200


# ---------- 审计：落库 + 零 PII 明文 ----------

def test_directory_audit_no_pii(client: TestClient):
    """审计 im.directory.query：detail 记 platform/kind/department_id/has_keyword/条数；
    响应体中的手机号/邮箱绝不入审计（target 按渠道名过滤，脏库跨运行不串扰）。"""
    ch = _cred_channel(client, f"fs-audit-{_SFX}", "feishu")
    r = client.get(f"/api/v1/im/channels/{ch['name']}/directory/users",
                   headers=AUTH, params={"department_id": "10", "keyword": "张"})
    assert r.status_code == 200
    rows = client.get("/api/v1/audit", headers=AUTH,
                      params={"action": "im.directory.query",
                              "target": ch["name"]}).json()
    users_rows = [x for x in rows if x["detail"]["kind"] == "users"]
    assert users_rows, "代理查询应落审计"
    row = users_rows[0]
    assert row["target"] == ch["name"]
    assert row["detail"]["platform"] == "feishu"
    assert row["detail"]["department_id"] == "10" and row["detail"]["has_keyword"] is True
    assert row["detail"]["count"] == 1  # 页内过滤后条数
    # 零 PII 明文：手机号/邮箱/keyword 本身均不入审计 detail
    assert _PII_MOBILE not in json.dumps(row["detail"]) and _PII_EMAIL not in json.dumps(row["detail"])

    # 群列表同样落审计（kind=chats）
    client.get(f"/api/v1/im/channels/{ch['name']}/directory/chats", headers=AUTH)
    rows = client.get("/api/v1/audit", headers=AUTH,
                      params={"action": "im.directory.query",
                              "target": ch["name"]}).json()
    assert any(x["detail"]["kind"] == "chats" for x in rows)
