"""/api/sessions/{sid}/files + /file 路由单测。"""
import pytest
from fastapi.testclient import TestClient

from cc_harness.web.app import create_app
from cc_harness.web.sessions import SessionManager


class FakeLLM:
    async def chat(self, *a, **k): raise NotImplementedError


class FakeMCPFactory:
    async def __call__(self): return None


@pytest.fixture
def client_with_cwd(tmp_path):
    # 在 tmp_path 建几个文件
    (tmp_path / "hello.py").write_text("print('hi')", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "data.json").write_text("{}", encoding="utf-8")
    sm = SessionManager(llm=FakeLLM(), mcp_factory=FakeMCPFactory())
    app = create_app(session_manager=sm)
    return TestClient(app), sm, tmp_path


async def test_list_files_root(client_with_cwd):
    c, sm, cwd = client_with_cwd
    r = await _create(c, cwd)
    sid = r["session_id"]
    resp = c.get(f"/api/sessions/{sid}/files?path=.")
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    names = {e["name"] for e in entries}
    assert "hello.py" in names
    assert "sub" in names


async def test_read_file(client_with_cwd):
    c, sm, cwd = client_with_cwd
    r = await _create(c, cwd)
    sid = r["session_id"]
    resp = c.get(f"/api/sessions/{sid}/file?path=hello.py")
    assert resp.status_code == 200
    body = resp.json()
    assert "print" in body["content"]
    assert body["language"] == "python"


async def test_read_path_traversal_blocked(client_with_cwd):
    """拒绝 ../ 跳出 cwd。"""
    c, sm, cwd = client_with_cwd
    r = await _create(c, cwd)
    sid = r["session_id"]
    resp = c.get(f"/api/sessions/{sid}/file?path=../../etc/passwd")
    assert resp.status_code in (400, 403)


async def _create(c, cwd):
    """同步调 async POST。TestClient 自动处理。"""
    return c.post("/api/sessions", json={"cwd": str(cwd), "mode": "coding"}).json()
