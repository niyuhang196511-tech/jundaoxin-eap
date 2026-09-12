import pytest
from fastapi.testclient import TestClient

from eap.static import console as _  # noqa: F401  确认静态包随包分发


def test_console_served(client: TestClient):
    """React 控制台构建产物经 /console 托管。"""
    resp = client.get("/console")
    assert resp.status_code == 200
    assert "EAP" in resp.text
    # 构建产物引用 /sdk/console/ 下的资源
    assert "/sdk/console/" in resp.text


def test_widget_still_served(client: TestClient):
    resp = client.get("/sdk/eap-widget.js")
    assert resp.status_code == 200
