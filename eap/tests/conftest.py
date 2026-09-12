"""智能体平台测试公共夹具：测试库隔离 + TestClient。"""

from __future__ import annotations

import os
import tempfile

# 必须在导入 eap 之前设置（config 使用 lru_cache）
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
