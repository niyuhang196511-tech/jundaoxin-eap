from fastapi.testclient import TestClient


def test_widget_still_served(client: TestClient):
    """嵌入外链 widget 仍由后端 /sdk 托管；控制台前端已独立部署（frontend/ → Next.js）。"""
    resp = client.get("/sdk/eap-widget.js")
    assert resp.status_code == 200
