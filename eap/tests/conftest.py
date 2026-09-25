"""智能体平台测试公共夹具：测试库隔离 + TestClient。"""

from __future__ import annotations

import os
import tempfile
import uuid

# 必须在导入 eap 之前设置（config 使用 lru_cache）
# M49-C：尊重预设 EAP_TEST_DB_URL（CI/本地指向真实 PostgreSQL 实测）；
# 未预设时维持 SQLite 临时库（开发默认，完全向后兼容）。
_TEST_DB_URL = os.environ.get("EAP_TEST_DB_URL")
if _TEST_DB_URL:
    os.environ["EAP_DB_URL"] = _TEST_DB_URL
else:
    _TMPDIR = tempfile.mkdtemp(prefix="eap-test-")
    os.environ["EAP_DB_URL"] = f"sqlite:///{_TMPDIR}/test-eap.db"
os.environ.pop("EAP_OPENAI_BASE_URL", None)  # 测试仅用 mock，离线确定
os.environ.pop("EAP_OPENAI_API_KEY", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from eap.main import create_app  # noqa: E402

AUTH = {"Authorization": "Bearer dev-key-1"}


@pytest.fixture(scope="session")
def client():
    app = create_app()
    with TestClient(app) as c:  # with 触发 lifespan（建库/种子/注册引导）
        yield c


@pytest.fixture(scope="session")
def uname():
    """唯一名生成器（M52-D）：脏库可重入——固定名重跑撞唯一约束，uuid 后缀保证跨运行不撞。
    样板=test_webhooks._name。"""
    def _make(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:8]}"
    return _make
