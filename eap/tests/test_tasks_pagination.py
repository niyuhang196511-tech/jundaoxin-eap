"""任务列表分页参数测试（M52-B）：GET /api/v1/tasks 的 limit/offset 行为 + 钳制 + 默认兼容。

脏库可重入约定：库里存在种子数据与历史残留，任何断言不依赖全局计数——
每轮测试用唯一 state 标记（uuid 后缀）直写 TaskRecord，经 ?state=<标记> 过滤后
窗口内只剩自己的数据，顺序由互异 created_at 决定（倒序），可做精确子集/相对断言。
自造行模块结束即清理（M52-D）：非标准 state 的残留行会击穿 test_metrics 的
task_queue_depth「导出面==DB 计数」断言面（gauge 按设计只导出 TASK_STATES 已知
state，标记 state 永不在导出面）——tasks 表属「会污染别人」的表，测后自删。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from .conftest import AUTH

from eap.db import SessionLocal
from eap.models import TaskRecord

# 本模块直写过的 state 标记（teardown 按标记删自造行）
_SEEDED_STATES: set[str] = set()


@pytest.fixture(scope="module", autouse=True)
def _cleanup_seeded_tasks():
    """模块结束后删除本模块直写的全部 TaskRecord 行（按唯一 state 标记）。"""
    yield
    with SessionLocal() as db:
        db.execute(delete(TaskRecord).where(TaskRecord.state.in_(_SEEDED_STATES)))
        db.commit()


def _tag() -> str:
    """唯一 state 标记（≤ String(16)）：既隔离脏库残留，又天然避免重跑主键/断言冲突。"""
    return f"M52B{uuid.uuid4().hex[:8].upper()}"


def _seed(n: int, state: str) -> list[str]:
    """直写 n 条 TaskRecord（created_at 逐秒递增、互不相同 → 倒序确定），
    返回按接口预期顺序（created_at desc，即 seq 从大到小）的 task_id 列表。"""
    _SEEDED_STATES.add(state)  # teardown 按标记删自造行
    base = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=n + 120)
    ids: list[str] = []
    with SessionLocal() as db:
        for i in range(n):
            tid = f"m52b-{state.lower()}-{i:04d}"
            db.add(TaskRecord(id=tid, type="generic", state=state,
                              payload={"marker": state, "seq": i}, result={},
                              created_at=base + timedelta(seconds=i),
                              updated_at=base + timedelta(seconds=i)))
            ids.append(tid)
        db.commit()
    return list(reversed(ids))


def _ids(client: TestClient, **params) -> list[str]:
    resp = client.get("/api/v1/tasks", headers=AUTH, params=params)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data, list)
    return [t["task_id"] for t in data]


def test_limit_offset_window(client: TestClient):
    """a) limit/offset 基本行为：窗口条数与相对顺序（created_at 倒序）精确匹配。"""
    state = _tag()
    expected = _seed(5, state)  # 新 → 旧

    # state 过滤即得全量自有数据：顺序 = created_at 倒序
    assert _ids(client, state=state) == expected
    # limit 截头部
    assert _ids(client, state=state, limit=2) == expected[:2]
    # limit + offset 取中间窗口
    assert _ids(client, state=state, limit=2, offset=2) == expected[2:4]
    # offset 越过尾部剩余不足 limit → 只回剩余
    assert _ids(client, state=state, limit=10, offset=4) == expected[4:]
    # offset ≥ 总数 → 空窗口
    assert _ids(client, state=state, limit=5, offset=5) == []


def test_clamp(client: TestClient):
    """b) 钳制：limit=0 → 按 1、offset<0 → 按 0、limit=9999 → 按上限 200。"""
    state = _tag()
    expected = _seed(3, state)

    assert _ids(client, state=state, limit=0) == expected[:1]
    assert _ids(client, state=state, limit=2, offset=-5) == expected[:2]

    # 上限 200：造 205 条，limit=9999 只回最新 200 条
    state_big = _tag()
    expected_big = _seed(205, state_big)
    got = _ids(client, state=state_big, limit=9999)
    assert len(got) == 200
    assert got == expected_big[:200]


def test_default_limit_50_backward_compatible(client: TestClient):
    """c) 默认 limit=50 向后兼容：不传分页参数 = 旧版固定 50 条（取最新 50）。"""
    state = _tag()
    expected = _seed(55, state)

    assert _ids(client, state=state) == expected[:50]

    # 完全无参调用（含不带 state）：仍 200、数组、不超默认 50 条（子集断言，不依赖全局计数）
    resp = client.get("/api/v1/tasks", headers=AUTH)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data, list) and len(data) <= 50
