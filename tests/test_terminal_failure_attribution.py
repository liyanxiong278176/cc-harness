from __future__ import annotations

from eval.cc_only.adapters.harbor import (
    _terminal_failure_class,
    _terminal_official_zero_reward_status,
)
from eval.cc_only.contracts import TrialOutcome, TrialStatus
from eval.cc_only.infrastructure import (
    verifier_bootstrap_failure_text,
    verifier_execution_observed,
)
from eval.cc_only.runner import _classify_terminal_infrastructure


def test_verifier_bootstrap_network_failure_is_not_a_task_failure() -> None:
    output = """
    curl: (28) Operation timed out after 300001 milliseconds
    /tests/test.sh: line 12: uvx: command not found
    """

    assert verifier_bootstrap_failure_text(output) is True
    assert _terminal_failure_class({}, verifier_diagnostic=output) == "verifier_infrastructure"


def test_real_pytest_failure_takes_precedence_over_network_words() -> None:
    output = """
    ============================= test session starts =============================
    collected 6 items
    tests/test_solution.py .....F
    ========================= 1 failed, 5 passed in 0.12s =========================
    AssertionError: expected two cancelled tasks after a connection timeout
    """

    assert verifier_execution_observed(output) is True
    assert verifier_bootstrap_failure_text(output) is False
    assert _terminal_failure_class({}, verifier_diagnostic=output) == "task_failure"


def test_agent_and_verifier_failures_are_reported_as_mixed() -> None:
    trial = {
        "exception_info": {
            "exception_type": "NonZeroAgentExitCodeError",
            "exception_message": "durable run ended with status failed_recoverable",
        }
    }
    output = "curl: (28) Failed to download CPython before tests started"

    assert _terminal_failure_class(trial, verifier_diagnostic=output) == "mixed"


def test_agent_exception_without_verifier_bootstrap_failure_is_runtime_failure() -> None:
    trial = {"exception_info": {"exception_type": "AgentTimeoutError"}}

    assert _terminal_failure_class(trial, verifier_diagnostic="") == "agent_runtime"


def test_provider_transport_error_is_not_published_as_task_failure() -> None:
    trial = {
        "exception_info": {
            "exception_type": "APIConnectionError",
            "exception_message": "connection reset by peer",
        }
    }

    assert _terminal_failure_class(trial, verifier_diagnostic="") == "provider_transport"
    assert _terminal_official_zero_reward_status("provider_transport") is TrialStatus.INVALID


def test_verifier_infrastructure_zero_reward_is_not_published_as_task_fail() -> None:
    assert (
        _terminal_official_zero_reward_status("verifier_infrastructure")
        is TrialStatus.INVALID
    )
    assert _terminal_official_zero_reward_status("mixed") is TrialStatus.INVALID
    assert _terminal_official_zero_reward_status("task_failure") is TrialStatus.FAIL
    assert _terminal_official_zero_reward_status("agent_runtime") is TrialStatus.FAIL


def test_verifier_infrastructure_does_not_trigger_a_second_model_trial() -> None:
    outcome = TrialOutcome(
        status=TrialStatus.INVALID,
        invalid_reason="official verifier did not execute",
        protocol={
            "exception_is_infrastructure": True,
            "verifier_infrastructure": True,
            "transient_infrastructure": True,
            "failure_diagnostic": "curl: (28) connection timed out",
        },
    )

    assert _classify_terminal_infrastructure(outcome) == "environment_not_ready"
