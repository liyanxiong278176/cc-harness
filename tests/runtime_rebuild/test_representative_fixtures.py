from __future__ import annotations

import json
from pathlib import Path


FIXTURE_ROOT = Path(__file__).parent / "fixtures"


def test_login_register_fixture_and_capability_samples_are_frozen() -> None:
    login_root = FIXTURE_ROOT / "login_register"
    assert (login_root / "app" / "auth.py").is_file()
    assert (login_root / "tests" / "auth_spec.py").is_file()
    assert "register" in (login_root / "README.md").read_text(encoding="utf-8")

    context = json.loads(
        (FIXTURE_ROOT / "capabilities" / "context" / "session-001" / "manifest.json")
        .read_text(encoding="utf-8")
    )
    offload = json.loads(
        (FIXTURE_ROOT / "capabilities" / "offload" / "refs.json").read_text(encoding="utf-8")
    )
    memory = json.loads(
        (FIXTURE_ROOT / "capabilities" / "memory" / "checkpoint.json").read_text(encoding="utf-8")
    )
    assert context["coverage"]["working_state"] is True
    assert offload["complete"] is False
    assert memory["captured_count"] == 2
