from __future__ import annotations

from pathlib import Path

from eval.terminal_bench.error_classifier import classify_error
from eval.terminal_bench.infra_guard.base import GuardContext, GuardManager, GuardResult, RecoveryResult
from eval.terminal_bench.infra_guard.network_guard import NetworkGuard


class _FixtureGuard:
    name = "fixture"

    def pre_check(self, context: GuardContext) -> GuardResult:
        return GuardResult(self.name, "pre_check", details={"task": context.task_id})

    def prevent(self, context: GuardContext) -> GuardResult:
        return GuardResult(self.name, "prevent", actions_taken=["fixture"], details={})

    def recover(self, context: GuardContext, error_text: str) -> RecoveryResult:
        return RecoveryResult(self.name, recovered=True, details={"error": error_text})

    def post_cleanup(self, context: GuardContext) -> dict[str, object]:
        return {"ok": True}


def test_guard_manager_runs_all_phases_in_order(tmp_path: Path) -> None:
    context = GuardContext(tmp_path, tmp_path / "attempt", "task-1", "terminal-bench/terminal-bench-2-1@sha256:x", "0.20.0")
    manager = GuardManager(context, (_FixtureGuard(),))
    assert manager.pre_check()["ready"] is True
    assert manager.prevent()["results"][0]["actions_taken"] == ["fixture"]
    assert manager.recover("network timeout")["recovered"] is True
    assert manager.post_cleanup()["results"][0]["ok"] is True


def test_error_classifier_keeps_pre_model_docker_failure_out_of_task_grade() -> None:
    result = classify_error("all predefined address pools are fully subnetted")
    assert result.category == "docker"
    assert result.canonical_class == "environment_not_ready"
    assert result.verifier_executed is False
    assert result.severity == "recoverable"


def test_error_classifier_marks_verifier_assertion_as_deterministic_task_failure() -> None:
    result = classify_error("collected 3 items\n1 failed, 2 passed")
    assert result.category == "task"
    assert result.canonical_class == "task_failure"
    assert result.severity == "deterministic"
    assert result.verifier_executed is True


def test_error_classifier_marks_unknown_and_provider_failures_canonically() -> None:
    unknown = classify_error("launcher exited without a result")
    assert unknown.canonical_class == "outcome_unknown"
    provider = classify_error("connection timed out while calling provider")
    assert provider.canonical_class == "provider_transport"
    assert provider.to_dict()["canonical_class"] == "provider_transport"


def test_network_guard_probes_pypi_wheel_cdn_before_model_admission(monkeypatch) -> None:
    observed: list[str] = []

    def probe(url: str) -> dict[str, object]:
        observed.append(url)
        return {"url": url, "host": url.split("//", 1)[-1], "dns": True, "http_reachable": True}

    monkeypatch.setattr(NetworkGuard, "_probe_url", staticmethod(probe))
    result = NetworkGuard().pre_check(None)  # type: ignore[arg-type]

    assert result.ready is True
    assert "https://files.pythonhosted.org" in observed
