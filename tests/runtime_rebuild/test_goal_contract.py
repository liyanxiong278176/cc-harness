import pytest

from cc_harness.goals import GoalContractService, GoalDecision
from cc_harness.durable_runtime import DurableRuntimeClient


def test_low_risk_goal_is_auto_accepted() -> None:
    service = GoalContractService()
    goal = service.build("add a parser", ["tests pass"])
    assessment = service.assess(goal)
    assert assessment.decision is GoalDecision.AUTO_ACCEPT
    assert assessment.accepted


def test_ambiguous_and_high_risk_goal_requires_decision() -> None:
    service = GoalContractService()
    goal = service.build("deploy something to production", ["it works"])
    assessment = service.assess(goal)
    assert assessment.decision is GoalDecision.HIGH_RISK_REVIEW
    assert not assessment.accepted
    assert assessment.questions


def test_official_benchmark_provenance_does_not_infer_user_high_risk() -> None:
    service = GoalContractService()
    goal = service.build(
        "configure a git server and push content to the webserver",
        ["the pushed file is served over HTTP"],
    )
    assessment = service.assess(goal, goal_provenance="official_benchmark")
    assert assessment.decision is GoalDecision.AUTO_ACCEPT
    assert assessment.accepted


def test_official_benchmark_provenance_accepts_fixture_paths_and_task_phrasing() -> None:
    service = GoalContractService()
    goal = service.build(
        "configure Nginx at /etc/nginx/nginx.conf and do something as needed",
        ["the request is addressed"],
    )
    assessment = service.assess(goal, goal_provenance="official_benchmark")
    assert assessment.decision is GoalDecision.AUTO_ACCEPT
    assert assessment.accepted


def test_untrusted_provenance_still_requires_high_risk_review() -> None:
    service = GoalContractService()
    goal = service.build("push content to production", ["deployment works"])
    assessment = service.assess(goal, goal_provenance="user")
    assert assessment.decision is GoalDecision.HIGH_RISK_REVIEW
    assert not assessment.accepted


@pytest.mark.asyncio
async def test_terminal_bench_provenance_is_explicit_and_audited(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CC_HARNESS_TERMINAL_BENCH", "1")
    monkeypatch.setenv("CC_HARNESS_TRUSTED_BENCHMARK_TASK", "1")
    client = await DurableRuntimeClient.create(tmp_path)
    try:
        run_id = await client.submit("push content to the task webserver")
        view = await client.coordinator.inspect(run_id)
        assert view.status.value == "queued"
        created = (await client.store.read(run_id)).events[0]
        assert created.payload["goal_provenance"] == "official_benchmark"
    finally:
        await client.close()
