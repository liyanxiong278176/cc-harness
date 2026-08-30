from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from eval.cc_only.adapters import (
    Context27Adapter,
    LoCoMoAdapter,
    Safety8Adapter,
    SweBenchVerifiedAdapter,
    TerminalBench20Adapter,
    TerminalBenchAdapter,
)
from eval.cc_only.adapters.harbor import (
    TERMINAL_BENCH_21_DATASET,
    _cleanup_owned_harbor_resources,
    _docker_healthcheck,
    _docker_snapshot,
    _harbor_failure_diagnostic,
    _harbor_usage,
    _terminal_bench_errored_grade,
    _terminal_agent_timeout,
    _transient_text,
)
from eval.cc_only.adapters.agentharm import _portfolio
from eval.cc_only.adapters.common import capability_activation
from eval.cc_only.adapters.memory import (
    _copy_snapshot,
    _extract_locomo_answer,
    _grade_locomo_answer,
    _locomo_abstention_reason,
    _locomo_answer_prompt,
    _locomo_completed_questions,
    _locomo_memory_scope,
    _locomo_progress,
    _locomo_retrieval_evidence,
    _memory_env,
    _restore_snapshot,
    _snapshot_is_complete,
    _stratified_locomo_questions,
)
from eval.cc_only.contracts import (
    BenchmarkTask,
    CheckResult,
    EvalProfile,
    TrialContext,
    TrialOutcome,
    TrialStatus,
)
from eval.cc_only.launch import build_cc_invocation
from eval.cc_only.provider_proxy import ScopedProviderProxy, _upstream_url
from eval.cc_only.runner import (
    _classify_terminal_infrastructure,
    _ensure_preflight,
    _generation_attempt_count,
    _retryable_infrastructure_result,
    _safe_progress,
    _terminal_api_cost,
    _terminal_cost_cny,
    run_benchmark,
)
from eval.cc_only.storage import (
    RunStateStore,
    _observability_resume_compatible,
    atomic_json,
    read_json,
    task_path_slug,
)
from eval.locomo.evaluator import token_f1
from scripts.run_cc_only_benchmark import _known_run_error_message


def test_frozen_catalog_sizes() -> None:
    root = Path(__file__).resolve().parents[1]
    assert len(Context27Adapter().catalog(root, EvalProfile.PORTFOLIO)) == 27
    assert len(LoCoMoAdapter().catalog(root, EvalProfile.PORTFOLIO)) == 10
    assert len(Safety8Adapter().catalog(root, EvalProfile.PORTFOLIO)) == 16
    assert len(SweBenchVerifiedAdapter().catalog(root, EvalProfile.PORTFOLIO)) == 50
    assert len(SweBenchVerifiedAdapter().catalog(root, EvalProfile.FULL)) == 500
    assert len(TerminalBenchAdapter().catalog(root, EvalProfile.PORTFOLIO)) == 30
    assert len(TerminalBenchAdapter().catalog(root, EvalProfile.FULL)) == 89
    assert len(TerminalBench20Adapter().catalog(root, EvalProfile.FULL)) == 89


def test_terminal_bench_21_catalog_is_pinned_and_complete() -> None:
    root = Path(__file__).resolve().parents[1]
    adapter = TerminalBenchAdapter()
    tasks = adapter.catalog(root, EvalProfile.FULL)

    assert adapter.dataset == TERMINAL_BENCH_21_DATASET
    assert TERMINAL_BENCH_21_DATASET.startswith(
        "terminal-bench/terminal-bench-2-1@sha256:"
    )
    assert len({task.task_id for task in tasks}) == 89
    assert all(task.group for task in tasks)
    assert all(task.payload.get("source_digest") for task in tasks)
    assert all(
        str(task.payload.get("harbor_task_name")).startswith("terminal-bench/")
        for task in tasks
    )


def test_terminal_agent_timeout_reads_only_public_task_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_root = (
        tmp_path
        / ".cache"
        / "harbor"
        / "tasks"
        / "packages"
        / "terminal-bench"
        / "example"
        / "abc"
    )
    task_root.mkdir(parents=True)
    (task_root / "task.toml").write_text(
        '[task]\nname = "terminal-bench/example"\n\n[agent]\ntimeout_sec = 900.0\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert _terminal_agent_timeout("terminal-bench/example", "sha256:abc") == 900.0


def test_terminal_bench_official_summary_counts_invalid_as_zero() -> None:
    summary = TerminalBenchAdapter().summarize(
        [
            {
                "status": "pass",
                "group": "software-engineering",
                "metrics": {"reward": 0.25},
            },
            {
                "status": "invalid",
                "group": "security",
                "metrics": {},
            },
        ]
    )

    assert summary["successful_tasks"] == 1
    assert summary["official_denominator"] == 89
    assert summary["single_pass_accuracy"] == pytest.approx(1 / 89)
    assert summary["by_category"]["security"]["accuracy"] == 0
    assert summary["leaderboard_compatible"] is False


def test_terminal_bench_preserves_verifier_pass_after_agent_timeout() -> None:
    grade = _terminal_bench_errored_grade(
        {
            "n_errored_trials": 1,
            "evals": {"terminal-bench": {"metrics": [{"mean": 1.0}]}},
        },
        {
            "exception_info": {"exception_type": "AgentTimeoutError"},
            "verifier_result": {"rewards": {"reward": 1.0}},
        },
    )

    assert grade == (TrialStatus.PASS, 1.0)


def test_terminal_bench_does_not_invent_grade_for_unverified_timeout() -> None:
    grade = _terminal_bench_errored_grade(
        {"n_errored_trials": 1, "evals": {}},
        {"exception_info": {"exception_type": "AgentTimeoutError"}},
    )

    assert grade is None


def test_terminal_bench_conservative_cost_uses_peak_cny_tariff() -> None:
    cost = _terminal_cost_cny(
        [
            {
                "usage": {
                    "uncached_input_tokens": 1_000_000,
                    "cache_creation_input_tokens": 1_000_000,
                    "cache_read_input_tokens": 1_000_000,
                    "output_tokens": 1_000_000,
                }
            }
        ]
    )

    assert cost == pytest.approx(15.1)


def test_terminal_bench_api_cost_uses_provider_fact_only() -> None:
    cost = _terminal_api_cost(
        [
            {
                "usage": {
                    "model_calls": 2,
                    "api_reported_cost": 0.25,
                    "api_reported_cost_currency": "USD",
                    "api_cost_status": "reported",
                    "api_cost_observed": True,
                }
            },
            {
                "usage": {
                    "model_calls": 1,
                    "api_reported_cost": 0.10,
                    "api_reported_cost_currency": "USD",
                    "api_cost_status": "reported",
                    "api_cost_observed": True,
                }
            },
        ]
    )

    assert cost["api_reported_cost"] == pytest.approx(0.35)
    assert cost["api_reported_cost_currency"] == "USD"
    assert cost["api_cost_status"] == "reported"
    assert cost["api_cost_observed_calls"] == 3


def test_terminal_bench_api_cost_marks_unpriced_model_activity_incomplete() -> None:
    cost = _terminal_api_cost(
        [{"usage": {"model_calls": 1, "api_cost_status": "unavailable"}}]
    )

    assert cost["api_reported_cost"] is None
    assert cost["api_cost_status"] == "incomplete"
    assert cost["api_cost_complete"] is False


def test_terminal_bench_api_cost_does_not_add_mixed_currencies() -> None:
    cost = _terminal_api_cost(
        [
            {"usage": {"model_calls": 1, "api_reported_cost": 1, "api_cost_status": "reported", "api_reported_cost_currency": "USD"}},
            {"usage": {"model_calls": 1, "api_reported_cost": 1, "api_cost_status": "reported", "api_reported_cost_currency": "CNY"}},
        ]
    )

    assert cost["api_reported_cost"] is None
    assert cost["api_cost_status"] == "incomplete"


def test_terminal_bench_retries_named_rate_limit_exceptions() -> None:
    assert _transient_text("ApiRateLimitError: provider returned 429") is True
    assert _transient_text("ApiUsageLimitError: quota temporarily exceeded") is True
    assert _transient_text("RateLimitError: too many requests") is True
    assert _transient_text("curl: (56) Failure when receiving data from the peer") is True
    assert _transient_text("http.client.IncompleteRead: incomplete chunked read") is True
    assert _transient_text("provider proxy stream failure: IncompleteRead") is True
    assert _transient_text("AgentTimeoutError: command timed out") is False


def test_terminal_bench_retries_pypi_tls_disconnects() -> None:
    diagnostic = (
        "error: Failed to fetch: https://pypi.org/simple/multidict/; "
        "peer closed connection without sending TLS close_notify"
    )

    assert _transient_text(diagnostic) is True


def test_terminal_bench_retries_network_evidence_even_when_harbor_flag_is_false() -> None:
    outcome = TrialOutcome(
        status=TrialStatus.INVALID,
        invalid_reason=(
            "Failed to fetch https://pypi.org/simple/multidict/: "
            "peer closed connection without sending TLS close_notify"
        ),
        protocol={
            "exception_is_infrastructure": True,
            "transient_infrastructure": False,
        },
    )

    assert _classify_terminal_infrastructure(outcome) == "transient"

    dependency_outage = TrialOutcome(
        status=TrialStatus.INVALID,
        invalid_reason="dependency download failed because of TLS close_notify",
        protocol={
            "exception_is_infrastructure": True,
            "transient_infrastructure": False,
        },
    )
    assert _classify_terminal_infrastructure(dependency_outage) == "transient"


def test_terminal_bench_docker_healthcheck_retries_until_ready(monkeypatch) -> None:
    calls = 0
    sleeps: list[float] = []

    class Completed:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(_command, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return Completed(1, stderr="Cannot connect to the Docker daemon")
        return Completed(0, stdout="/var/lib/docker\n")

    monkeypatch.setattr("eval.cc_only.adapters.harbor.shutil.which", lambda _: "docker")
    monkeypatch.setattr("eval.cc_only.adapters.harbor.subprocess.run", fake_run)
    monkeypatch.setattr("eval.cc_only.adapters.harbor.time.sleep", sleeps.append)

    result = _docker_healthcheck(attempts=3, backoffs=(0.25, 0.5))

    assert result["ready"] is True
    assert result["attempts"] == 2
    assert calls == 2
    assert sleeps == [0.25]


def test_terminal_bench_preserves_agent_and_tool_call_telemetry() -> None:
    usage = _harbor_usage(
        {
            "n_input_tokens": 100,
            "n_cache_tokens": 80,
            "n_output_tokens": 20,
            "cost_usd": 0.25,
        },
        {"agent_result": {"metadata": {"model_calls": 7, "tool_calls": 9}}},
    )

    assert usage["model_calls"] == 7
    assert usage["tool_calls"] == 9


def test_terminal_bench_harbor_usage_preserves_direct_provider_cost() -> None:
    usage = _harbor_usage(
        {
            "n_input_tokens": 100,
            "n_cache_tokens": 80,
            "n_output_tokens": 20,
            # This framework field is intentionally ignored unless the agent
            # envelope carries the direct provider fact as well.
            "cost_usd": 999.0,
        },
        {
            "agent_result": {
                "metadata": {
                    "model_calls": 2,
                    "tool_calls": 1,
                    "api_reported_cost": 0.125,
                    "api_reported_cost_currency": "USD",
                    "api_cost_status": "reported",
                    "api_cost_observed": True,
                    "api_cost_complete": True,
                }
            }
        },
    )

    assert usage["api_reported_cost"] == pytest.approx(0.125)
    assert usage["api_cost_status"] == "reported"
    assert usage["cost_microusd"] == 125_000


def test_terminal_bench_harbor_usage_does_not_use_framework_cost_fallback() -> None:
    usage = _harbor_usage(
        {"n_input_tokens": 100, "cost_usd": 0.25},
        {"agent_result": {"metadata": {"model_calls": 1}}},
    )

    assert usage["cost_microusd"] is None
    assert usage["api_reported_cost"] is None
    assert usage["api_cost_status"] == "incomplete"


def test_terminal_bench_harbor_usage_detects_token_activity_without_call_count() -> None:
    usage = _harbor_usage(
        {"n_input_tokens": 12, "n_output_tokens": 4},
        {"agent_result": {"metadata": {}}},
    )

    assert usage["api_cost_observed"] is True
    assert usage["api_cost_status"] == "incomplete"


def test_terminal_bench_recovers_telemetry_from_rate_limit_error_text() -> None:
    usage = _harbor_usage(
        {},
        {
            "exception_info": {
                "exception_message": '{"usage":{"model_calls":11,"tool_calls":13}}'
            }
        },
    )

    assert usage["model_calls"] == 11
    assert usage["tool_calls"] == 13


def test_terminal_bench_cleanup_stops_only_new_owned_running_containers(monkeypatch) -> None:
    calls: list[list[str]] = []

    class Completed:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(command, **kwargs):
        del kwargs
        calls.append(list(command))
        return Completed()

    monkeypatch.setattr("eval.cc_only.adapters.harbor.shutil.which", lambda _: "docker")
    monkeypatch.setattr("eval.cc_only.adapters.harbor.subprocess.run", fake_run)
    result = _cleanup_owned_harbor_resources(
        {"available": True, "containers": {"old": {}}, "images": [], "volumes": []},
        {
            "available": True,
            "containers": {
                "old": {},
                "owned": {
                    "Labels": "com.docker.compose.project.config_files=harbor",
                    "State": "running",
                },
                "unrelated": {"Labels": "compose.project=other", "State": "running"},
            },
            "images": [],
            "volumes": [],
        },
    )

    assert calls == [
        ["docker", "stop", "--time", "10", "owned"],
        ["docker", "rm", "owned"],
    ]
    assert result["stopped_containers"] == ["owned"]
    assert result["removed_containers"] == ["owned"]
    assert result["retained_candidates"] == [
        {"id": "unrelated", "owned": False, "status": "running"}
    ]


def test_terminal_bench_docker_snapshot_uses_utf8_replacement(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class Completed:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(command, **kwargs):
        del command
        calls.append(kwargs)
        return Completed()

    monkeypatch.setattr("eval.cc_only.adapters.harbor.shutil.which", lambda _: "docker")
    monkeypatch.setattr("eval.cc_only.adapters.harbor.subprocess.run", fake_run)

    snapshot = _docker_snapshot()

    assert snapshot["available"] is True
    assert calls
    assert all(call["encoding"] == "utf-8" for call in calls)
    assert all(call["errors"] == "replace" for call in calls)


def test_terminal_bench_contract_mismatch_is_reported_without_traceback() -> None:
    message = _known_run_error_message(
        ValueError(
            "existing result root has a different immutable input contract; "
            "use the original command or a different profile/result root"
        )
    )

    assert message is not None
    assert "no model calls" in message
    assert "--new-run" in message


def test_terminal_provider_proxy_requires_scoped_token_and_allowed_endpoint() -> None:
    proxy = ScopedProviderProxy(
        upstream_base_url="https://api.example.invalid/v1",
        upstream_api_key="real-secret",
    )
    proxy.start()
    local_url = proxy.container_base_url.replace("host.docker.internal", "127.0.0.1")
    try:
        with pytest.raises(urllib.error.HTTPError) as unauthorized:
            urllib.request.urlopen(local_url + "/models", timeout=2)  # noqa: S310
        assert unauthorized.value.code == 401

        request = urllib.request.Request(
            local_url + "/files",
            headers={"Authorization": f"Bearer {proxy.token}"},
        )
        with pytest.raises(urllib.error.HTTPError) as forbidden:
            urllib.request.urlopen(request, timeout=2)  # noqa: S310
        assert forbidden.value.code == 404
    finally:
        proxy.close()

    assert (
        _upstream_url("https://api.deepseek.com/v1", "/v1/chat/completions?x=1")
        == "https://api.deepseek.com/v1/chat/completions?x=1"
    )


def test_progress_sink_failure_is_fail_soft() -> None:
    calls: list[str] = []

    def closed_sink(message: str) -> None:
        calls.append(message)
        raise OSError(22, "output pipe closed")

    progress = _safe_progress(closed_sink)

    progress("first")
    progress("second")

    assert calls == ["first"]


def test_persisted_provider_balance_failure_is_retryable(tmp_path: Path) -> None:
    result = tmp_path / "raw" / "result.json"
    result.parent.mkdir(parents=True)
    result.write_text(
        json.dumps(
            {
                "status": "fail",
                "failure_reason": "APIStatusError: 402 Insufficient Balance",
                "protocol": {"official_checker": False},
            }
        ),
        encoding="utf-8",
    )
    trial = {"status": "fail", "result": "raw/result.json"}
    assert _retryable_infrastructure_result(tmp_path, trial) is True

    trial["result"] = "raw/missing.json"
    assert _retryable_infrastructure_result(tmp_path, trial) is False


def test_terminal_infrastructure_classification_stops_deterministic_retries() -> None:
    environment = TrialOutcome(
        status=TrialStatus.INVALID,
        invalid_reason="ModuleNotFoundError: No module named 'exceptiongroup'",
        protocol={"exception_is_infrastructure": True},
    )
    transient = TrialOutcome(
        status=TrialStatus.INVALID,
        invalid_reason="provider proxy stream failure",
        protocol={"exception_is_infrastructure": True, "transient_infrastructure": True},
    )

    assert _classify_terminal_infrastructure(environment) == "environment_not_ready"
    assert _classify_terminal_infrastructure(transient) == "transient"


def test_resume_artifact_refresh_requires_explicit_opt_in(monkeypatch) -> None:
    previous = {
        "benchmark": "terminal-bench-2.1",
        "adapter_run_identity": {
            "git_dirty_digest": "old-dirty",
            "wheel_sha256": "old-wheel",
            "dataset": "pinned",
        },
    }
    current = {
        "benchmark": "terminal-bench-2.1",
        "adapter_run_identity": {
            "git_dirty_digest": "new-dirty",
            "wheel_sha256": "new-wheel",
            "dataset": "pinned",
        },
    }
    monkeypatch.setenv("CC_HARNESS_ALLOW_OBSERVABILITY_RESUME", "1")
    monkeypatch.delenv("CC_HARNESS_ALLOW_RESUME_ARTIFACT_REFRESH", raising=False)
    assert _observability_resume_compatible(previous, current) is False

    monkeypatch.setenv("CC_HARNESS_ALLOW_RESUME_ARTIFACT_REFRESH", "1")
    assert _observability_resume_compatible(previous, current) is True


def test_terminal_infrastructure_classification_reads_nested_harbor_evidence(
    tmp_path: Path,
) -> None:
    job_root = tmp_path / "job"
    trial_root = job_root / "task__abc"
    (trial_root / "agent").mkdir(parents=True)
    (trial_root / "agent" / "cc-harness.stderr").write_text(
        "requests.exceptions.SSLError: UNEXPECTED_EOF_WHILE_READING\n",
        encoding="utf-8",
    )
    diagnostic = _harbor_failure_diagnostic(
        job_root,
        {"stats": {"n_errored_trials": 1}},
        {
            "exception_info": {
                "exception_type": "NonZeroAgentExitCodeError",
                "exception_message": "command failed",
            }
        },
    )
    transient = TrialOutcome(
        status=TrialStatus.INVALID,
        invalid_reason="Harbor trial errored",
        protocol={
            "exception_is_infrastructure": True,
            "failure_diagnostic": diagnostic,
        },
    )

    assert "UNEXPECTED_EOF" in diagnostic
    assert _classify_terminal_infrastructure(transient) == "transient"


def test_terminal_infrastructure_classification_detects_nested_verifier_import_error(
    tmp_path: Path,
) -> None:
    job_root = tmp_path / "job"
    trial_root = job_root / "task__abc"
    trial_root.mkdir(parents=True)
    diagnostic = _harbor_failure_diagnostic(
        job_root,
        {"stats": {"n_errored_trials": 1}},
        {
            "exception_info": {
                "exception_type": "NonZeroAgentExitCodeError",
                "exception_message": "ModuleNotFoundError: No module named 'typing_extensions'",
            }
        },
    )
    outcome = TrialOutcome(
        status=TrialStatus.INVALID,
        invalid_reason="Harbor trial errored",
        protocol={
            "exception_is_infrastructure": True,
            "failure_diagnostic": diagnostic,
        },
    )

    assert _classify_terminal_infrastructure(outcome) == "environment_not_ready"


def test_locomo_answer_contract_keeps_audit_text_out_of_token_f1() -> None:
    raw = (
        "I checked the recalled session and found the matching identity.\n"
        "FINAL_ANSWER: Transgender woman"
    )

    answer, contract_met = _extract_locomo_answer(raw)

    assert contract_met is True
    assert answer == "Transgender woman"
    assert token_f1(answer, "Transgender woman") == 1.0


def test_locomo_answer_contract_fails_open_for_legacy_output() -> None:
    _answer, contract_met = _extract_locomo_answer("Transgender woman")

    assert contract_met is False
    assert _answer == "Transgender woman"


def test_locomo_summary_reports_joint_context_memory_diagnostics() -> None:
    summary = LoCoMoAdapter().summarize(
        [
            {
                "status": "pass",
                "metrics": {
                    "qa_count": 2,
                    "mean_f1": 0.75,
                    "answer_contract_rate": 1.0,
                    "supporting_evidence_rate": 0.5,
                    "category_scores": {
                        "1": {"count": 1, "mean": 1.0},
                        "5": {"count": 1, "mean": 0.5},
                    },
                    "context_management": {
                        "activation_event_count": 2,
                        "max_ratio_before": 0.4,
                        "max_ratio_after": 0.3,
                        "tiers": {"none": 2},
                        "artifact_count": 0,
                        "error_count": 0,
                        "truncation_event_count": 1,
                    },
                    "memory_evidence": {
                        "persistent_atom_count": 4,
                        "history_fact_atom_count": 4,
                        "history_session_count": 2,
                        "qa_activation_count": 2,
                        "qa_with_injected_atoms": 2,
                        "qa_with_supporting_evidence": 1,
                        "supporting_atom_count": 3,
                        "mean_injected_atom_count": 2.0,
                        "qa_count": 2,
                        "valid": True,
                    },
                },
                "protocol": {},
            }
        ]
    )

    assert summary["category_scores"]["1"]["mean"] == 1.0
    assert summary["abstention_accuracy"] == 0.5
    assert summary["context_management"]["injection_token_budget"] == 2400
    assert summary["context_management"]["truncation_event_count"] == 1
    assert summary["memory_evidence"]["history_session_count"] == 2
    assert summary["memory_evidence"]["qa_supporting_evidence_rate"] == 0.5


def test_locomo_query_uses_explicit_answer_boundary_and_wider_recall(tmp_path: Path) -> None:
    prompt = _locomo_answer_prompt("What is Caroline's gender identity?")
    environment = _memory_env(tmp_path)

    assert "FINAL_ANSWER:" in prompt
    assert "memory_recall" in prompt
    assert environment["MEMORY_RECALL_TOP_K"] == "12"
    assert environment["MEMORY_RETRIEVER_TOP_K"] == "12"
    assert environment["MEMORY_INJECTION_TOKEN_BUDGET"] == "2400"
    assert environment["MEMORY_RECALL_TIMEOUT_S"] == "8"
    assert environment["MEMORY_RECALL_TOOL_MAX_PER_TURN"] == "1"


def test_locomo_memory_scope_is_stable_across_workspace_relocation() -> None:
    scope = _locomo_memory_scope("conv-26", "sha256:0123456789abcdefdeadbeef")

    assert scope == "locomo:conv-26:0123456789abcdef"


def test_locomo_category_aware_scoring_does_not_score_null_as_none() -> None:
    category_one = _grade_locomo_answer(
        {"category": "1", "answer": "painting, swimming"},
        "painting",
    )
    category_three = _grade_locomo_answer(
        {"category": "3", "answer": "7 May 2023; inferred from the prior day"},
        "May 7, 2023",
    )
    category_five = _grade_locomo_answer(
        {"category": "5", "answer": None},
        "No information available",
    )

    assert category_one["metric"] == "locomo_partial_f1"
    assert category_one["score"] == 0.5
    assert category_three["score"] == 1.0
    assert category_five["score"] == 1.0
    assert category_five["gold"] is None


def test_locomo_protocol_smoke_subset_spans_all_categories() -> None:
    questions = [
        {"question": f"q-{category}-{index}", "category": str(category), "_source_question_index": category * 10 + index}
        for category in range(1, 6)
        for index in range(4)
    ]

    selected = _stratified_locomo_questions(questions, 10)

    assert len(selected) == 10
    assert {item["category"] for item in selected} == {"1", "2", "3", "4", "5"}
    assert [item["_source_question_index"] for item in selected] == sorted(
        item["_source_question_index"] for item in selected
    )


def test_locomo_retrieval_evidence_reads_nested_trajectory(tmp_path: Path) -> None:
    evidence = tmp_path / "launch"
    evidence.mkdir()
    payload = {
        "trajectory": [
            {
                "type": "capability_activation",
                "capability": "memory",
                "stage": "recall",
                "atom_count": 1,
                "scenario_count": 0,
                "atoms": [
                    {
                        "atom_id": "atom-1",
                        "source": "pipeline",
                        "session_id": "session-1",
                        "relevance": 0.9,
                    }
                ],
            },
            {"type": "action", "name": "memory_recall", "args": {"query": "Caroline"}},
            {"type": "observation", "text": "[atom-1] Caroline", "is_error": False},
        ]
    }
    (evidence / "stdout.jsonl").write_text(json.dumps(payload) + "\n", encoding="utf-8")

    result = _locomo_retrieval_evidence(evidence)

    assert result["activation_seen"] is True
    assert result["atom_count"] == 1
    assert result["atoms"][0]["atom_id"] == "atom-1"
    assert result["retrieval_rounds"][1]["query"] == "Caroline"


def test_locomo_abstention_reason_is_auditable() -> None:
    grading = {"abstained": True}

    assert (
        _locomo_abstention_reason(
            grading=grading,
            retrieval_evidence={"atoms": [], "retrieval_rounds": []},
        )
        == "no_evidence"
    )
    assert (
        _locomo_abstention_reason(
            grading=grading,
            retrieval_evidence={"atoms": [{"status": "conflicting"}]},
        )
        == "conflicting_evidence"
    )
    assert (
        _locomo_abstention_reason(
            grading=grading,
            retrieval_evidence={"atoms": [{"status": "active"}]},
        )
        == "no_evidence"
    )
    assert (
        _locomo_abstention_reason(
            grading=grading,
            retrieval_evidence={
                "atoms": [{"status": "active"}],
                "supporting_evidence_seen": True,
            },
        )
        == "low_confidence"
    )


def test_locomo_progress_contains_phase_bar_and_task_coordinates(tmp_path: Path) -> None:
    messages: list[str] = []
    context = TrialContext(
        project_root=tmp_path,
        output_root=tmp_path,
        attempt_root=tmp_path,
        task=BenchmarkTask("locomo/conv-26"),
        profile=EvalProfile.FULL,
        attempt=1,
        watchdog_seconds=60,
        progress=messages.append,
        task_index=1,
        task_total=2,
    )

    _locomo_progress(
        context,
        phase="ingest-0002",
        done=1,
        total=19,
        event="heartbeat",
        elapsed=75,
        calls=6,
    )

    assert len(messages) == 1
    assert "[locomo] [#-------------------]" in messages[0]
    assert "task=1/2" in messages[0]
    assert "phase=ingest-0002" in messages[0]
    assert "items=1/19" in messages[0]
    assert "calls=6" in messages[0]
    assert "elapsed=1m15s" in messages[0]
    assert "eta=22m30s" in messages[0]


def test_cc_only_child_reads_utf8_stdin_on_windows(tmp_path: Path) -> None:
    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    project.mkdir()
    workspace.mkdir()
    (project / ".env").write_text(
        "OPENAI_API_KEY=test\nOPENAI_BASE_URL=https://example.invalid\nOPENAI_MODEL=test\n",
        encoding="utf-8",
    )

    _profile, invocation, _budget = build_cc_invocation(
        project,
        workspace,
        "mental health – memory",
        capability_profile="memory-eval",
        home=home,
        watchdog_seconds=60,
    )

    assert invocation.stdin.decode("utf-8") == "mental health – memory"
    assert invocation.environment["PYTHONIOENCODING"] == "utf-8"
    assert invocation.environment["PYTHONUTF8"] == "1"


def test_cc_only_child_can_use_chat_mode_for_memory_qa(tmp_path: Path) -> None:
    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    project.mkdir()
    workspace.mkdir()
    (project / ".env").write_text(
        "OPENAI_API_KEY=test\nOPENAI_BASE_URL=https://example.invalid\nOPENAI_MODEL=test\n",
        encoding="utf-8",
    )
    invocation = build_cc_invocation(
        project,
        workspace,
        "answer from memory",
        capability_profile="memory-eval",
        home=home,
        watchdog_seconds=60,
        mode="chat",
    )[1]

    assert invocation.argv[-2:] == ("--mode", "chat")


def test_store_reuses_interrupted_attempt_without_overwriting(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path / "result")
    tasks = (BenchmarkTask("task.one"),)
    state = store.initialize(contract={"benchmark": "test"}, tasks=tasks)
    _, attempt_root, _record = store.begin_attempt(state, tasks[0], 1)
    atomic_json(attempt_root / "partial.json", {"kept": True})

    resumed = store.initialize(contract={"benchmark": "test"}, tasks=tasks)

    trial = resumed["trials"]["task.one"]
    assert trial["status"] == "pending"
    assert trial["attempts"][0]["status"] == "interrupted"
    assert read_json(attempt_root / "partial.json") == {"kept": True}
    attempt, resumed_root, record = store.begin_attempt(resumed, tasks[0], 1)
    assert attempt == 1
    assert resumed_root == attempt_root
    assert record["resume_count"] == 1
    assert record["status"] == "running"


def test_terminal_interrupted_attempt_restarts_from_frozen_task_state(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path / "terminal-result")
    task = BenchmarkTask("terminal-bench/fixture")
    state = store.initialize(
        contract={"benchmark": "terminal-bench-2.1"}, tasks=(task,)
    )
    attempt, first_root, first_record = store.begin_attempt(
        state, task, 1, reuse_interrupted=False
    )
    first_record["status"] = TrialStatus.INTERRUPTED.value

    resumed_attempt, resumed_root, resumed_record = store.begin_attempt(
        state, task, 1, reuse_interrupted=False
    )

    assert attempt == 1
    assert resumed_attempt == 2
    assert resumed_root != first_root
    assert resumed_record.get("resume_count", 0) == 0


def test_interrupted_checkpoint_does_not_consume_single_attempt_budget() -> None:
    trial = {
        "attempts": [
            {"retry_generation": 0, "status": TrialStatus.INTERRUPTED.value},
            {"retry_generation": 0, "status": TrialStatus.PASS.value},
            {"retry_generation": 1, "status": TrialStatus.FAIL.value},
        ]
    }

    assert _generation_attempt_count(trial, 0) == 1
    assert _generation_attempt_count(trial, 1) == 1


def test_task_path_slug_bounds_long_ids_and_keeps_distinct_trials() -> None:
    first = "standard/attacked/workspace/user_task_0/injection_task_0/direct"
    second = first.rsplit("/", 1)[0] + "/ignore_previous"

    first_slug = task_path_slug(first)
    second_slug = task_path_slug(second)

    assert len(first_slug) <= 28
    assert len(second_slug) <= 28
    assert first_slug != second_slug
    assert first_slug.rsplit("-", 1)[-1] != second_slug.rsplit("-", 1)[-1]


def test_store_compacts_legacy_long_interrupted_attempt_path(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path / "result")
    task = BenchmarkTask("terminal-bench/llm-inference-batching-scheduler")
    state = store.initialize(contract={"benchmark": "terminal-bench-2.1"}, tasks=(task,))
    _attempt, original_root, record = store.begin_attempt(
        state, task, 15, reuse_interrupted=False
    )
    legacy_root = (
        store.root
        / "raw"
        / "0015-terminal-bench-ll-a9fef79c2a"
        / "attempt-1"
    )
    legacy_root.parent.mkdir(parents=True, exist_ok=True)
    original_root.replace(legacy_root)
    record["path"] = legacy_root.relative_to(store.root).as_posix()
    record["status"] = TrialStatus.INTERRUPTED.value
    state["trials"][task.task_id]["status"] = "pending"

    resumed_attempt, resumed_root, resumed_record = store.begin_attempt(state, task, 15)

    assert resumed_attempt == 1
    assert resumed_root != legacy_root
    assert len(str(resumed_root / "jobs")) <= 150
    assert resumed_record["path"] == resumed_root.relative_to(store.root).as_posix()
    assert not legacy_root.exists()


def test_store_reuses_checkpoint_preserving_invalid_attempt(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path / "result")
    task = BenchmarkTask("locomo/fixture")
    state = store.initialize(contract={"benchmark": "locomo"}, tasks=(task,))
    attempt, attempt_root, record = store.begin_attempt(state, task, 1)
    result_path = attempt_root / "result.json"
    atomic_json(
        result_path,
        {
            "status": "invalid",
            "protocol": {"checkpoint_preserving": True, "resume_question_index": 4},
        },
    )
    store.finish_attempt(state, task.task_id, record, TrialStatus.INVALID, result_path)

    resumed_attempt, resumed_root, resumed_record = store.begin_attempt(state, task, 1)

    assert attempt == resumed_attempt == 1
    assert resumed_root == attempt_root
    assert resumed_record["resume_count"] == 1
    retained = list((attempt_root / "infrastructure-results").glob("result-*.json"))
    assert len(retained) == 1
    assert read_json(retained[0])["status"] == "invalid"


def test_locomo_snapshot_has_commit_marker_and_restores_inside_trial(tmp_path: Path) -> None:
    source_workspace = tmp_path / "attempt" / "ingest-workspace"
    source_home = tmp_path / "attempt" / "ingest-home"
    source_workspace.mkdir(parents=True)
    source_home.mkdir(parents=True)
    (source_workspace / ".cc-harness").mkdir()
    (source_workspace / ".cc-harness" / "memory.db").write_text("state", encoding="utf-8")
    (source_home / "session.json").write_text("session", encoding="utf-8")
    snapshot = tmp_path / "attempt" / "ingestion-checkpoints" / "0001"

    _copy_snapshot(source_workspace, source_home, snapshot, metadata={"session_index": 1})

    assert _snapshot_is_complete(snapshot) is True
    restored_workspace = tmp_path / "attempt" / "query-workspace"
    restored_home = tmp_path / "attempt" / "query-home"
    _restore_snapshot(
        snapshot,
        restored_workspace,
        restored_home,
        allowed_root=tmp_path / "attempt",
    )
    assert (restored_workspace / ".cc-harness" / "memory.db").read_text(encoding="utf-8") == "state"
    assert (restored_home / "session.json").read_text(encoding="utf-8") == "session"


def test_locomo_question_checkpoint_loads_only_contiguous_valid_prefix(tmp_path: Path) -> None:
    qa_root = tmp_path / "qa"
    questions = ({"question": "one"}, {"question": "two"}, {"question": "three"})
    for index, question in enumerate(questions[:2], 1):
        evidence_root = qa_root / f"{index:04d}-launch"
        evidence_root.mkdir(parents=True)
        atomic_json(
            evidence_root / "launch.json",
            {
                "schema_version": "eval.launch-evidence.v1",
                "harness": "cc-harness",
                "requested_model": "deepseek-v4-flash",
                "resolved_model": "deepseek-v4-flash",
                "exit_code": 0,
                "wall_time_ms": 1,
            },
        )
        (evidence_root / "stdout.jsonl").write_text(
            '{"schema_version":"cc-harness.print-result.v1","text":"FINAL_ANSWER: ok",'
            '"resolved_model":"deepseek-v4-flash","error":null}\n',
            encoding="utf-8",
        )
        atomic_json(
            qa_root / f"{index:04d}.json",
            {
                "question_index": index - 1,
                "sample_id": "fixture",
                "sample_digest": "sha256:fixture",
                "protocol_version": LoCoMoAdapter.protocol_version,
                "question": question["question"],
                "f1": 0.5,
            },
        )

    records, completed, usage = _locomo_completed_questions(
        qa_root,
        questions,
        sample_id="fixture",
        sample_digest="sha256:fixture",
    )

    assert completed == 2
    assert [record["question"] for record in records] == ["one", "two"]
    assert usage["model_calls"] == 0


@pytest.mark.asyncio
async def test_locomo_execute_resumes_after_ingestion_interruption(tmp_path: Path, monkeypatch) -> None:
    data_root = tmp_path / "eval" / "locomo" / "data"
    data_root.mkdir(parents=True)
    sample = {
        "sample_id": "fixture",
        "conversation": {
            "session_1": [{"role": "user", "content": "one"}],
            "session_1_date_time": "2024-01-01",
            "session_2": [{"role": "user", "content": "two"}],
            "session_2_date_time": "2024-01-02",
            "session_3": [{"role": "user", "content": "three"}],
            "session_3_date_time": "2024-01-03",
            "session_4": [{"role": "user", "content": "four"}],
            "session_4_date_time": "2024-01-04",
        },
        "qa": [
            {"question": "first?", "answer": "x", "category": "1"},
            {"question": "second?", "answer": "x", "category": "1"},
        ],
    }
    (data_root / "locomo10.json").write_text(json.dumps([sample]), encoding="utf-8")
    calls: list[str] = []
    invocation_count = 0
    monkeypatch.setattr(
        "eval.cc_only.adapters.memory._snapshot_memory_counts",
        lambda _snapshot, **_kwargs: {"persistent_atom_count": 1, "conversation_event_count": 1},
    )

    async def fake_launch(context, *, phase_name, evidence_root, workspace, home, **_kwargs):
        nonlocal invocation_count
        invocation_count += 1
        calls.append(phase_name)
        if invocation_count == 3:
            raise asyncio.CancelledError
        evidence_root.mkdir(parents=True, exist_ok=True)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / ".cc-harness").mkdir(exist_ok=True)
        (workspace / ".cc-harness" / "state.json").write_text(
            phase_name, encoding="utf-8"
        )
        home.mkdir(parents=True, exist_ok=True)
        stdout = (
            "FINAL_ANSWER: x" if phase_name.startswith("qa-") else "MEMORY_INGESTED"
        )
        stdout_bytes = (
            json.dumps(
                {
                    "schema_version": "cc-harness.print-result.v1",
                    "text": stdout,
                    "resolved_model": "deepseek-v4-flash",
                    "error": None,
                }
            )
            + "\n"
        ).encode()
        atomic_json(
            evidence_root / "launch.json",
            {
                "schema_version": "eval.launch-evidence.v1",
                "harness": "cc-harness",
                "requested_model": "deepseek-v4-flash",
                "resolved_model": "deepseek-v4-flash",
                "exit_code": 0,
                "wall_time_ms": 1,
            },
        )
        (evidence_root / "stdout.jsonl").write_bytes(stdout_bytes)
        evidence = SimpleNamespace(
            timed_out=False,
            valid_for_parity=True,
            stdout_truncated=False,
            stderr_truncated=False,
            parse_error=None,
            resolved_model="deepseek-v4-flash",
            requested_model="deepseek-v4-flash",
            wall_time_ms=1,
            model_calls=1,
            tool_calls=0,
            input_tokens=1,
            uncached_input_tokens=1,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            output_tokens=1,
            cost_microusd=1,
        )
        return SimpleNamespace(evidence=evidence, stdout=stdout_bytes, stderr=b"")

    monkeypatch.setattr(
        "eval.cc_only.adapters.memory._locomo_launch_with_progress", fake_launch
    )
    adapter = LoCoMoAdapter()
    context = TrialContext(
        project_root=tmp_path,
        output_root=tmp_path / "result",
        attempt_root=tmp_path / "result" / "attempt-1",
        task=BenchmarkTask("locomo/fixture", payload={"sample_id": "fixture"}),
        profile=EvalProfile.FULL,
        attempt=1,
        watchdog_seconds=60,
        progress=lambda _message: None,
    )
    context.attempt_root.mkdir(parents=True)

    with pytest.raises(asyncio.CancelledError):
        await adapter.execute(context)

    first_run_calls = list(calls)
    await adapter.execute(context)

    assert first_run_calls == ["ingest-0001", "ingest-0002", "ingest-0003"]
    assert calls[3:] == ["ingest-0003", "ingest-0004", "qa-0001", "qa-0002"]


@pytest.mark.asyncio
async def test_locomo_retries_only_current_qa_after_provider_disconnect(
    tmp_path: Path, monkeypatch
) -> None:
    data_root = tmp_path / "eval" / "locomo" / "data"
    data_root.mkdir(parents=True)
    sample = {
        "sample_id": "fixture",
        "conversation": {
            "session_1": [{"role": "user", "content": "Caroline paints."}],
            "session_1_date_time": "2024-01-01",
        },
        "qa": [{"question": "What does Caroline do?", "answer": "paints", "category": "2"}],
    }
    (data_root / "locomo10.json").write_text(json.dumps([sample]), encoding="utf-8")
    phases: list[str] = []
    qa_attempts = 0
    monkeypatch.setattr(
        "eval.cc_only.adapters.memory._snapshot_memory_counts",
        lambda _snapshot, **_kwargs: {"persistent_atom_count": 1, "conversation_event_count": 1},
    )

    async def fake_sleep(_delay):
        return None

    async def fake_launch(context, *, phase_name, evidence_root, workspace, home, **_kwargs):
        nonlocal qa_attempts
        phases.append(phase_name)
        evidence_root.mkdir(parents=True, exist_ok=True)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / ".cc-harness").mkdir(exist_ok=True)
        home.mkdir(parents=True, exist_ok=True)
        is_qa = phase_name.startswith("qa-")
        if is_qa:
            qa_attempts += 1
        disconnected = is_qa and qa_attempts == 1
        trajectory = [
            {
                "type": "capability_activation",
                "capability": "memory",
                "stage": "recall",
                "atom_count": 1,
                "scenario_count": 0,
                "atoms": [{"atom_id": "atom-1", "source": "pipeline"}],
            }
        ]
        stdout = (
            json.dumps(
                {
                    "schema_version": "cc-harness.print-result.v1",
                    "text": "FINAL_ANSWER: paints" if is_qa else "MEMORY_INGESTED",
                    "resolved_model": "deepseek-v4-flash",
                    "error": None,
                    "trajectory": trajectory,
                }
            )
            + "\n"
        ).encode()
        stderr = b"RemoteProtocolError: peer closed connection" if disconnected else b""
        evidence = SimpleNamespace(
            timed_out=False,
            valid_for_parity=not disconnected,
            stdout_truncated=False,
            stderr_truncated=False,
            parse_error=("RemoteProtocolError: peer closed connection" if disconnected else None),
            exit_code=1 if disconnected else 0,
            resolved_model="deepseek-v4-flash",
            requested_model="deepseek-v4-flash",
            wall_time_ms=1,
            model_calls=1,
            tool_calls=0,
            input_tokens=1,
            uncached_input_tokens=1,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            output_tokens=1,
            cost_microusd=1,
        )
        atomic_json(
            evidence_root / "launch.json",
            {
                "schema_version": "eval.launch-evidence.v1",
                "harness": "cc-harness",
                "requested_model": "deepseek-v4-flash",
                "resolved_model": "deepseek-v4-flash",
                "exit_code": evidence.exit_code,
                "wall_time_ms": 1,
                "parse_error": evidence.parse_error,
            },
        )
        (evidence_root / "stdout.jsonl").write_bytes(stdout)
        (evidence_root / "stderr.txt").write_bytes(stderr)
        return SimpleNamespace(evidence=evidence, stdout=stdout, stderr=stderr)

    monkeypatch.setattr("eval.cc_only.adapters.memory.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(
        "eval.cc_only.adapters.memory._locomo_launch_with_progress", fake_launch
    )
    context = TrialContext(
        project_root=tmp_path,
        output_root=tmp_path / "result",
        attempt_root=tmp_path / "result" / "attempt-1",
        task=BenchmarkTask("locomo/fixture", payload={"sample_id": "fixture"}),
        profile=EvalProfile.FULL,
        attempt=1,
        watchdog_seconds=60,
        progress=lambda _message: None,
    )
    context.attempt_root.mkdir(parents=True)

    await LoCoMoAdapter().execute(context)

    assert phases == ["ingest-0001", "qa-0001", "qa-0001"]
    retained = context.attempt_root / "qa" / "retry-evidence" / "0001" / "attempt-1"
    assert b"RemoteProtocolError" in (retained / "stderr.txt").read_bytes()
    assert (context.attempt_root / "qa" / "0001.json").is_file()


def test_atomic_json_uses_unique_temp_files_under_concurrent_writes(tmp_path: Path) -> None:
    """Concurrent writers must never publish partial JSON or collide on temp names."""
    target = tmp_path / "state.json"

    def write(index: int) -> None:
        atomic_json(target, {"writer": index})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(32)))

    assert read_json(target)["writer"] in range(32)
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_store_rejects_changed_immutable_contract(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path / "result")
    tasks = (BenchmarkTask("task.one"),)
    store.initialize(contract={"benchmark": "one"}, tasks=tasks)
    try:
        store.initialize(contract={"benchmark": "two"}, tasks=tasks)
    except ValueError as exc:
        assert "immutable input contract" in str(exc)
    else:
        raise AssertionError("changed contract was accepted")


def test_agentharm_portfolio_selects_matched_balanced_forms() -> None:
    records = []
    for kind in ("harmful", "benign"):
        for original in range(44):
            for form, (detailed, hint) in enumerate(
                ((False, False), (False, True), (True, False), (True, True)), 1
            ):
                records.append(
                    {
                        "kind": kind,
                        "id": f"{original}-{form}",
                        "id_original": str(original),
                        "category": f"category-{original % 8}",
                        "detailed_prompt": detailed,
                        "hint_included": hint,
                    }
                )

    selected = _portfolio(records)

    assert len(selected) == 88
    selected_forms = {
        (item["kind"], item["id_original"]): (
            item["detailed_prompt"],
            item["hint_included"],
        )
        for item in selected
    }
    for original in map(str, range(44)):
        assert selected_forms[("harmful", original)] == selected_forms[("benign", original)]


def test_capability_activation_requires_triggered_non_degraded_state(tmp_path: Path) -> None:
    activation = tmp_path / ".cc-harness" / "activation" / "session.json"
    activation.parent.mkdir(parents=True)
    atomic_json(
        activation,
        {
            "capabilities": {
                "safety": {
                    "enabled": True,
                    "initialized": True,
                    "triggered": True,
                    "no_degradation": True,
                }
            }
        },
    )

    assert capability_activation(tmp_path, "safety")["valid"] is True

    payload = read_json(activation)
    payload["capabilities"]["safety"]["triggered"] = False
    atomic_json(activation, payload)
    assert capability_activation(tmp_path, "safety")["valid"] is False


def test_check_only_preserves_requested_profile_without_model_calls(tmp_path: Path) -> None:
    seen = []

    class Adapter:
        slug = "profile-check"
        title = "Profile Check"
        protocol_version = "test.v1"
        capability_profile = "benchmark-one-shot"
        adaptations = ()

        def catalog(self, project_root, profile):
            seen.append(("catalog", profile))
            return (BenchmarkTask(f"task/{profile.value}"),)

        def check(self, project_root, profile, tasks):
            seen.append(("check", profile))
            return CheckResult(ready=True, details={"tasks": len(tasks)})

        async def execute(self, context):
            raise AssertionError("check-only mode must not execute a model trial")

        def summarize(self, outcomes):
            return {}

    output = tmp_path / "result"
    asyncio.run(
        run_benchmark(
            Adapter(),
            tmp_path,
            output,
            profile=EvalProfile.FULL,
            check_only=True,
        )
    )

    assert seen == [("catalog", EvalProfile.FULL), ("check", EvalProfile.FULL)]
    assert read_json(output / "manifest.json")["profile"] == "full"
    assert read_json(output / "summary.json")["usage"]["model_calls"] == 0


def test_check_only_task_limit_uses_isolated_subset_contract(tmp_path: Path) -> None:
    class Adapter:
        slug = "limited-check"
        title = "Limited Check"
        protocol_version = "test.v1"
        capability_profile = "benchmark-one-shot"
        adaptations = ()

        def catalog(self, _project_root, _profile):
            return tuple(BenchmarkTask(f"task/{index}") for index in range(3))

        def check(self, _project_root, _profile, tasks):
            assert len(tasks) == 3
            return CheckResult(ready=True, details={"catalog_tasks": len(tasks)})

        async def execute(self, _context):
            raise AssertionError("check-only mode must not execute a model trial")

        def summarize(self, _outcomes):
            return {}

    output = tmp_path / "result"
    asyncio.run(
        run_benchmark(
            Adapter(),
            tmp_path,
            output,
            profile=EvalProfile.FULL,
            check_only=True,
            task_limit=1,
        )
    )

    catalog = read_json(output / "catalog.json")
    manifest = read_json(output / "manifest.json")
    assert len(catalog["tasks"]) == 1
    assert manifest["task_limit"] == 1


def test_terminal_infrastructure_exhaustion_defers_task_without_stopping_batch(
    tmp_path: Path,
) -> None:
    class Adapter:
        slug = "terminal-bench-2.1"
        title = "Terminal-Bench fixture"
        protocol_version = "fixture.v1"
        capability_profile = "clean-coding"
        adaptations = ()
        max_automatic_attempts = 2
        infrastructure_ready = False
        executed: list[str] = []

        def catalog(self, _project_root, _profile):
            return (
                BenchmarkTask("terminal-bench/infrastructure-fixture"),
                BenchmarkTask("terminal-bench/healthy-fixture"),
            )

        def check(self, _project_root, _profile, _tasks):
            return CheckResult(ready=True, details={"tasks": 2})

        async def execute(self, context):
            self.executed.append(context.task.task_id)
            if (
                context.task.task_id == "terminal-bench/infrastructure-fixture"
                and not self.infrastructure_ready
            ):
                return TrialOutcome(
                    status=TrialStatus.INVALID,
                    invalid_reason="provider proxy stream failure",
                    protocol={"exception_is_infrastructure": True},
                )
            return TrialOutcome(status=TrialStatus.PASS, metrics={"reward": 1.0})

        def summarize(self, _outcomes):
            return {}

    output = tmp_path / "result"
    adapter = Adapter()
    asyncio.run(
        run_benchmark(
            adapter,
            tmp_path,
            output,
            profile=EvalProfile.FULL,
            cooldown_scale=0,
        )
    )

    state = read_json(output / "state.json")
    trial = state["trials"]["terminal-bench/infrastructure-fixture"]
    assert trial["status"] == "pending"
    assert "result" not in trial
    assert len(trial["attempts"]) == 1
    assert trial["attempts"][0]["resume_count"] == 1
    for attempt in trial["attempts"]:
        assert attempt["status"] == "interrupted"
        evidence = output / attempt["infrastructure_evidence"]
        assert evidence.is_file()
        assert read_json(evidence)["status"] == "invalid"
        assert not (evidence.parent / "result.json").exists()
    retained = list((output / trial["attempts"][0]["path"] / "infrastructure-results").glob("infrastructure-*.json"))
    assert len(retained) == 1
    assert state["trials"]["terminal-bench/healthy-fixture"]["status"] == "pass"
    assert adapter.executed[-1] == "terminal-bench/healthy-fixture"
    first_summary = read_json(output / "summary.json")
    assert first_summary["status"] == "incomplete"
    assert first_summary["counts"] == {"pass": 1, "fail": 0, "invalid": 0, "pending": 1}
    assert first_summary["pending_tasks"] == [
        "terminal-bench/infrastructure-fixture"
    ]
    assert first_summary["infrastructure_events"][-1]["reason"] == (
        "terminal_infrastructure_deferred"
    )

    adapter.infrastructure_ready = True
    adapter.executed.clear()
    asyncio.run(run_benchmark(adapter, tmp_path, output, profile=EvalProfile.FULL))
    resumed_state = read_json(output / "state.json")
    resumed_trial = resumed_state["trials"]["terminal-bench/infrastructure-fixture"]
    assert resumed_trial["status"] == "pass"
    assert resumed_state["retry_generation"] == 1
    assert adapter.executed == ["terminal-bench/infrastructure-fixture"]
    final_summary = read_json(output / "summary.json")
    assert final_summary["status"] == "complete"
    assert final_summary["counts"] == {"pass": 2, "fail": 0, "invalid": 0, "pending": 0}
    assert final_summary["pending_tasks"] == []


def test_exact_task_manifest_selects_frozen_ids_without_name_guessing(tmp_path: Path) -> None:
    class Adapter:
        slug = "exact-selection"
        title = "Exact selection"
        protocol_version = "fixture.v1"
        capability_profile = "benchmark-one-shot"
        adaptations = ()

        def catalog(self, _project_root, _profile):
            return tuple(BenchmarkTask(f"task/{index}") for index in range(3))

        def check(self, _project_root, _profile, tasks):
            assert len(tasks) == 3
            return CheckResult(ready=True, details={"catalog_tasks": len(tasks)})

        async def execute(self, _context):
            raise AssertionError("check-only mode must not execute a model trial")

        def summarize(self, _outcomes):
            return {}

    manifest_path = tmp_path / "regression.json"
    manifest_path.write_text(
        json.dumps(
            {
                "tasks": [
                    {"task_id": "task/2", "reason": "baseline failure"},
                    {"task_id": "task/0", "reason": "control"},
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "result"
    asyncio.run(
        run_benchmark(
            Adapter(),
            tmp_path,
            output,
            profile=EvalProfile.FULL,
            check_only=True,
            task_manifest=manifest_path,
        )
    )
    catalog = read_json(output / "catalog.json")
    manifest = read_json(output / "manifest.json")
    assert [item["task_id"] for item in catalog["tasks"]] == ["task/2", "task/0"]
    assert manifest["task_selection"] == ["task/2", "task/0"]


def test_rerun_sample_only_reopens_selected_terminal_trial(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    class Adapter:
        slug = "locomo-memory"
        title = "LoCoMo fixture"
        protocol_version = "fixture.v1"
        capability_profile = "memory-eval"
        adaptations = ()

        def catalog(self, _project_root, _profile):
            return (
                BenchmarkTask("locomo/a", payload={"sample_id": "a"}),
                BenchmarkTask("locomo/b", payload={"sample_id": "b"}),
            )

        def check(self, _project_root, _profile, tasks):
            return CheckResult(ready=True, details={"tasks": len(tasks)})

        async def execute(self, context):
            sample_id = str(context.task.payload["sample_id"])
            calls.append(sample_id)
            return TrialOutcome(status=TrialStatus.PASS, metrics={"qa_count": 1, "mean_f1": 1.0})

        def summarize(self, _outcomes):
            return {}

    monkeypatch.setattr(
        "eval.cc_only.runner._ensure_preflight",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )
    output = tmp_path / "result"
    adapter = Adapter()
    asyncio.run(run_benchmark(adapter, tmp_path, output, profile=EvalProfile.FULL))
    first_state = read_json(output / "state.json")
    first_a_result = first_state["trials"]["locomo/a"]["result"]

    asyncio.run(
        run_benchmark(
            adapter,
            tmp_path,
            output,
            profile=EvalProfile.FULL,
            rerun_sample="b",
        )
    )

    state = read_json(output / "state.json")
    assert calls == ["a", "b", "b"]
    assert state["trials"]["locomo/a"]["result"] == first_a_result
    assert len(state["trials"]["locomo/b"]["attempts"]) == 2
    assert state["trials"]["locomo/b"]["selected_attempt"] == 2


@pytest.mark.asyncio
async def test_preflight_reports_nonzero_launch_stderr(tmp_path: Path, monkeypatch) -> None:
    async def failed_launch(*_args, **_kwargs):
        return SimpleNamespace(
            stdout=b"",
            stderr=b"KeyError: 'recall'\n",
            evidence=SimpleNamespace(
                exit_code=1,
                timed_out=False,
                valid_for_parity=False,
                parse_error="structured output contains no JSON objects",
            ),
        )

    monkeypatch.setattr("eval.cc_only.runner.run_cc_prompt", failed_launch)

    class Store:
        def save(self, _state):
            return None

    state = {"preflight": {"attempts": [], "status": "pending"}}
    adapter = SimpleNamespace(capability_profile="context-eval")

    with pytest.raises(RuntimeError, match="KeyError: 'recall'"):
        await _ensure_preflight(
            adapter,
            tmp_path,
            tmp_path / "result",
            state,
            Store(),
            watchdog_seconds=60,
            progress=lambda _message: None,
        )

    assert state["preflight"]["attempts"][0]["status"] == "invalid"
