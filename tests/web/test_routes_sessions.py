"""/api/sessions 路由单测。"""
import pytest
from fastapi.testclient import TestClient

from cc_harness.web.app import create_app
from cc_harness.web.sessions import SessionManager


class FakeLLM:
    async def chat(self, *a, **k): raise NotImplementedError


class FakeMCPFactory:
    async def __call__(self): return None


@pytest.fixture
def client():
    sm = SessionManager(llm=FakeLLM(), mcp_factory=FakeMCPFactory(), max_sessions=4)
    app = create_app(session_manager=sm)
    return TestClient(app), sm


def test_create_session(client, tmp_path):
    c, sm = client
    resp = c.post("/api/sessions", json={"cwd": str(tmp_path), "mode": "coding"})
    assert resp.status_code == 201
    body = resp.json()
    assert "session_id" in body
    assert body["mode"] == "coding"


def test_list_sessions(client, tmp_path):
    c, sm = client
    c.post("/api/sessions", json={"cwd": str(tmp_path), "mode": "coding"})
    c.post("/api/sessions", json={"cwd": str(tmp_path), "mode": "plan"})
    resp = c.get("/api/sessions")
    assert resp.status_code == 200
    assert len(resp.json()["sessions"]) == 2


def test_delete_session(client, tmp_path):
    c, sm = client
    r = c.post("/api/sessions", json={"cwd": str(tmp_path), "mode": "coding"})
    sid = r.json()["session_id"]
    resp = c.delete(f"/api/sessions/{sid}")
    assert resp.status_code == 204
    assert c.get(f"/api/sessions/{sid}").status_code == 404


def test_max_sessions_returns_422(client, tmp_path):
    c, sm = client
    sm.max_sessions = 2  # 强制上限 2(brief bug fix:字段名是 max_sessions 不是 _max)
    c.post("/api/sessions", json={"cwd": str(tmp_path), "mode": "coding"})
    c.post("/api/sessions", json={"cwd": str(tmp_path), "mode": "coding"})
    resp = c.post("/api/sessions", json={"cwd": str(tmp_path), "mode": "coding"})
    assert resp.status_code == 422
