import pytest
from fastapi.testclient import TestClient


def test_console_served(client: TestClient):
    """React 控制台构建产物经 /console 托管（未构建则跳过）。"""
    resp = client.get("/console")
    if resp.status_code == 503:
        pytest.skip("控制台未构建（cd frontend && npm run build）")
    assert resp.status_code == 200
    assert "EAP" in resp.text
    assert "/sdk/console/" in resp.text


def test_widget_still_served(client: TestClient):
    resp = client.get("/sdk/eap-widget.js")
    assert resp.status_code == 200
