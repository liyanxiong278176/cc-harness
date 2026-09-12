import pytest

from cc_harness.goals import GoalContractService, GoalDecision
from cc_harness.coordinator import RunCoordinator, RunRequest
from cc_harness.durable_runtime import DurableRuntimeClient
from cc_harness.run_store import RunStore


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


def test_explicit_user_confirmation_accepts_high_risk_goal() -> None:
    service = GoalContractService()
    goal = service.build("build a production payment service", ["tests pass"])
    assessment = service.assess(goal, goal_provenance="user_confirmed")
    assert assessment.decision is GoalDecision.AUTO_ACCEPT
    assert assessment.accepted


def test_production_shaped_project_with_simulated_integrations_is_not_blocked() -> None:
    """Feature requirements must not be mistaken for live external actions."""

    service = GoalContractService()
    goal = service.build(
        """请完成一个生产级校园外卖平台。
        支付、短信、地图等外部服务使用可替换的模拟适配器，不需要真实上线。
        提供 Docker Compose、本地测试和启动脚本。""",
        ["核心功能实现并通过测试"],
    )
    assessment = service.assess(goal)
    assert assessment.decision is GoalDecision.AUTO_ACCEPT
    assert assessment.accepted


def test_explicit_deployment_still_requires_goal_decision() -> None:
    service = GoalContractService()
    goal = service.build("deploy the completed service to production", ["it is live"])
    assessment = service.assess(goal)
    assert assessment.decision is GoalDecision.HIGH_RISK_REVIEW
    assert not assessment.accepted


@pytest.mark.asyncio
async def test_goal_review_block_can_be_explicitly_resumed(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    try:
        coordinator = RunCoordinator(store)
        handle = await coordinator.submit(
            RunRequest("deploy the completed service to production", ("it is live",))
        )
        assert (await coordinator.inspect(handle.run_id)).status.value == "blocked"

        receipt = await coordinator.resume(handle.run_id, "用户确认目标范围，可继续执行")
        assert receipt.status.value == "queued"
        events = (await store.read(handle.run_id, limit=100)).events
        assert any(event.event_type == "GoalContractAccepted" for event in events)
        assert any(event.event_type == "RunResumed" for event in events)
        assert (await coordinator.inspect(handle.run_id)).projection.outcome is None
    finally:
        await store.close()


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
