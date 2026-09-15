from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from cc_harness.webui import WebRuntimeManager, WebSettingsStore, create_web_app


def _client(tmp_path: Path) -> TestClient:
    manager = WebRuntimeManager(initial_cwd=tmp_path, data_root=tmp_path / "runtime-data")
    manager.settings = WebSettingsStore(tmp_path / "settings.json")
    return TestClient(create_web_app(manager))


def test_versioned_web_contract_exposes_runtime_capabilities(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        health = client.get("/api/web/v1/health")
        assert health.status_code == 200
        assert health.json()["runtime"] == "durable"

        capabilities = client.get("/api/web/v1/capabilities")
        assert capabilities.status_code == 200
        body = capabilities.json()
        assert body["version"] == "v1"
        assert body["transport"] == {"commands": "rest", "events": "sse", "reconnect": "cursor"}
        assert body["features"]["approvals"]["reject_continues"] is True


def test_versioned_alias_preserves_project_gate_and_bootstrap(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        bootstrap = client.get("/api/web/v1/bootstrap")
        assert bootstrap.status_code == 200
        assert bootstrap.json()["project"] is None

        response = client.post("/api/web/v1/messages", json={"text": "检查项目"})
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "project_required"


def test_versioned_settings_alias_uses_same_redaction_contract(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/api/web/v1/settings",
            json={
                "base_url": "https://provider.example/v1",
                "model": "deepseek-v4-flash",
                "api_key": "sk-versioned-secret",
            },
        )
        assert response.status_code == 200
        public = client.get("/api/web/v1/settings").json()
        assert public["has_api_key"] is True
        assert "api_key" not in public
        assert public["api_key_masked"] != "sk-versioned-secret"

