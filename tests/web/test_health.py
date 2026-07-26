"""/api/health 单测。"""
from fastapi.testclient import TestClient
from cc_harness.web.app import create_app


def test_health_returns_ok():
    app = create_app()
    client = TestClient(app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == 1
    assert "session_count" in body
