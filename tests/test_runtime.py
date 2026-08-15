import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cc_harness.activation import ActivationManifest, CapabilityProfile
from cc_harness.config import L2Config
from cc_harness.l2 import ScanResult
from cc_harness.runtime import SessionRuntime, _effective_resume_mode
from cc_harness.tokens import TurnTokenStats, UsageRecord


def test_explicit_mode_wins_when_resuming_saved_session() -> None:
    """A benchmark can change mode without inheriting an old coding session."""
    assert _effective_resume_mode("chat", "coding") == "chat"
    assert _effective_resume_mode(None, "coding") == "coding"
    assert _effective_resume_mode(None, None) == "coding"


def test_activation_manifest_records_four_part_capability_evidence(tmp_path):
    path = tmp_path / "activation.json"
    manifest = ActivationManifest(
        path,
        session_id="session-test",
        project_root=tmp_path,
        profile=CapabilityProfile.named("context-eval"),
        requested_model="deepseek-v4-flash",
    )

    manifest.initialize("context", context_window=128_000)
    manifest.trigger("context", tier="summary")
    manifest.add_artifact("context", tmp_path / "summary-v1.json")

    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    state = payload["capabilities"]["context"]
    assert payload["schema_version"] == "cc-harness.activation.v1"
    assert payload["profile"]["name"] == "context-eval"
    assert state["initialized"] is True
    assert state["triggered"] is True
    assert state["artifacts"] == [str(tmp_path / "summary-v1.json")]
    assert state["no_degradation"] is True


def test_activation_manifest_retries_transient_windows_replace_failure(
    tmp_path, monkeypatch
):
    path = tmp_path / "activation.json"
    manifest = ActivationManifest(
        path,
        session_id="session-test",
        project_root=tmp_path,
        profile=CapabilityProfile.named("memory-eval"),
        requested_model="deepseek-v4-flash",
    )
    real_replace = os.replace
    calls = 0

    def flaky_replace(source, target):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise PermissionError(13, "transient Windows sharing violation")
        return real_replace(source, target)

    monkeypatch.setattr(os, "replace", flaky_replace)

    manifest.initialize("runtime", entrypoint="SessionRuntime")

    assert calls == 3
    assert path.is_file()
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.asyncio
async def test_runtime_create_closes_owned_resources_when_initialization_fails(
    tmp_path, monkeypatch
):
    session_store_closed = AsyncMock()
    memory_worker = SimpleNamespace(close=AsyncMock())
    memory_store = SimpleNamespace(close=AsyncMock())
    llm = SimpleNamespace(aclose=AsyncMock())
    mcp = SimpleNamespace(
        _sessions={},
        _tools={},
        start=AsyncMock(),
        shutdown=AsyncMock(),
    )
    executor_shutdown = AsyncMock()

    class FakeSessionStore:
        def __init__(self, cwd):
            self.db_path = Path(cwd) / ".cc-harness" / "sessions.db"

        async def open(self):
            return self

        async def import_legacy(self, _path):
            return 0

        async def close(self):
            await session_store_closed()

    async def build_context(_self):
        return None

    async def build_memory(self):
        self.memory_config = SimpleNamespace(enabled=True)
        self.state.mem_deps = {
            "worker": memory_worker,
            "store": memory_store,
            "service": SimpleNamespace(
                embedder=None,
                decider=SimpleNamespace(_llm=None),
            ),
        }

    original_initialize = ActivationManifest.initialize

    def fail_at_safety(self, name, **details):
        if name == "safety":
            raise PermissionError(13, "activation replace failed")
        return original_initialize(self, name, **details)

    config = SimpleNamespace(
        openai_api_key="test-key",
        openai_base_url="https://example.invalid",
        openai_model="deepseek-v4-flash",
        mcp_servers={},
        runtime_environment={},
    )
    monkeypatch.setattr("cc_harness.runtime.load_layered_config", lambda _cwd: config)
    monkeypatch.setattr("cc_harness.runtime.LLMClient", lambda **_kwargs: llm)
    monkeypatch.setattr("cc_harness.runtime.MCPClient", lambda _servers: mcp)
    monkeypatch.setattr("cc_harness.runtime.SessionStore", FakeSessionStore)
    monkeypatch.setattr(SessionRuntime, "_build_context_services", build_context)
    monkeypatch.setattr(SessionRuntime, "_build_memory_services", build_memory)
    monkeypatch.setattr(ActivationManifest, "initialize", fail_at_safety)
    monkeypatch.setattr("cc_harness.runtime.init_session_executor", lambda *_args: None)
    monkeypatch.setattr(
        "cc_harness.runtime.shutdown_session_executor", executor_shutdown
    )

    with pytest.raises(PermissionError, match="activation replace failed"):
        await SessionRuntime.create(
            tmp_path,
            capability_profile="memory-eval",
            host_execution=True,
        )

    session_store_closed.assert_awaited_once()
    memory_worker.close.assert_awaited_once()
    memory_store.close.assert_awaited_once()
    mcp.shutdown.assert_awaited_once()
    llm.aclose.assert_awaited_once()
    executor_shutdown.assert_awaited_once()


def test_bare_and_conflicting_capability_profile_are_rejected():
    with pytest.raises(ValueError, match="cannot be combined"):
        # The conflict is validated before any external dependency is created.
        import asyncio

        asyncio.run(
            SessionRuntime.create(
                Path.cwd(),
                bare=True,
                capability_profile="memory-eval",
            )
        )


def test_hardened_safety_rejects_host_execution():
    import asyncio

    with pytest.raises(ValueError, match="cannot be combined with host execution"):
        asyncio.run(
            SessionRuntime.create(
                Path.cwd(),
                capability_profile="hardened-safety",
                host_execution=True,
            )
        )


def test_benchmark_one_shot_profile_disables_agent_subsystems():
    profile = CapabilityProfile.named("benchmark-one-shot")

    assert profile.one_shot is True
    assert profile.mcp is False
    assert profile.tools is False
    assert profile.loop_control is False
    assert profile.safety is False


@pytest.mark.asyncio
async def test_run_user_turn_passes_runtime_iteration_budget_to_agent(tmp_path, monkeypatch):
    observed = {}

    async def fake_run_turn(messages, llm, mcp, **kwargs):
        del llm, mcp
        observed.update(kwargs)
        messages.append({"role": "assistant", "content": "done"})
        return TurnTokenStats(iter_count=1)

    monkeypatch.setattr("cc_harness.runtime.run_turn", fake_run_turn)

    runtime = SessionRuntime()
    runtime.cwd = tmp_path
    runtime.llm = object()
    runtime.mcp = object()
    runtime.max_iterations = 37
    runtime.memory_config = SimpleNamespace(layered_inject=False, offload_enabled=False)

    async def emit(_event):
        return None

    async def confirm(_tool, _args, _reason):
        return "yes"

    await runtime.run_user_turn(
        "finish the task",
        event_emitter=emit,
        confirm_handler=confirm,
    )

    assert observed["max_iter"] == 37


@pytest.mark.asyncio
async def test_context_only_runtime_does_not_require_long_term_memory_deps(tmp_path, monkeypatch):
    observed = {}

    async def fake_run_turn(messages, llm, mcp, **kwargs):
        del llm, mcp
        observed.update(kwargs)
        messages.append({"role": "assistant", "content": "done"})
        return TurnTokenStats(iter_count=1)

    after_turn_memory = AsyncMock()
    monkeypatch.setattr("cc_harness.runtime.run_turn", fake_run_turn)
    monkeypatch.setattr("cc_harness.runtime._after_turn_memory", after_turn_memory)

    runtime = SessionRuntime()
    runtime.cwd = tmp_path
    runtime.llm = object()
    runtime.mcp = object()
    runtime.capability_profile = CapabilityProfile.named("context-eval")
    runtime.memory_config = SimpleNamespace(
        enabled=True,
        layered_inject=True,
        offload_enabled=True,
    )
    context_deps = {
        "refs_dir": tmp_path / "refs",
        "manifest_path": tmp_path / "manifest.jsonl",
    }
    runtime.state.mem_deps = context_deps

    async def emit(_event):
        return None

    async def confirm(_tool, _args, _reason):
        return "yes"

    await runtime.run_user_turn(
        "inspect the context",
        event_emitter=emit,
        confirm_handler=confirm,
    )

    assert observed["memory_layer"] is None
    assert observed["offload_deps"] is context_deps
    after_turn_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_shot_runtime_does_not_require_memory_config(tmp_path, monkeypatch):
    observed = {}

    async def fake_run_turn(messages, llm, mcp, **kwargs):
        del llm, mcp
        observed.update(kwargs)
        messages.append({"role": "assistant", "content": "done"})
        return TurnTokenStats(iter_count=1)

    monkeypatch.setattr("cc_harness.runtime.run_turn", fake_run_turn)

    runtime = SessionRuntime()
    runtime.cwd = tmp_path
    runtime.llm = object()
    runtime.mcp = object()
    runtime.capability_profile = CapabilityProfile.named("benchmark-one-shot")
    runtime.memory_config = None
    runtime.state.mem_deps = None

    async def emit(_event):
        return None

    async def confirm(_tool, _args, _reason):
        return "yes"

    await runtime.run_user_turn(
        "answer once",
        event_emitter=emit,
        confirm_handler=confirm,
    )

    assert observed["memory_layer"] is None
    assert observed["offload_deps"] is None
    assert observed["mode"] == "plan"
    assert observed["max_iter"] == 1
    assert observed["refresh_system_prompt"] is False
    assert observed["retry_empty_response"] is False
    assert observed["l5"] is None
    assert observed["loop_control_config"].enabled is False
    assert observed["context_config"] is None
    assert observed["context_projection"] is None


@pytest.mark.asyncio
async def test_bare_runtime_passes_only_available_prompt_capabilities(tmp_path, monkeypatch):
    observed = {}

    async def fake_run_turn(messages, llm, mcp, **kwargs):
        del llm, mcp
        observed.update(kwargs)
        messages.append({"role": "assistant", "content": "done"})
        return TurnTokenStats(iter_count=1)

    monkeypatch.setattr("cc_harness.runtime.run_turn", fake_run_turn)
    runtime = SessionRuntime()
    runtime.cwd = tmp_path
    runtime.llm = object()
    runtime.mcp = object()
    runtime.bare = True
    runtime.memory_config = SimpleNamespace(layered_inject=False, offload_enabled=False)

    async def emit(_event):
        return None

    async def confirm(_tool, _args, _reason):
        return "no"

    await runtime.run_user_turn("fix parser.py", event_emitter=emit, confirm_handler=confirm)

    assert observed["prompt_capabilities"] == {
        "todo_available": False,
        "subagent_available": False,
        "visible_thought_required": False,
    }
    assert observed["e1_decompose_enabled"] is False


@pytest.mark.asyncio
async def test_run_user_turn_passes_unbounded_iteration_contract_to_agent(tmp_path, monkeypatch):
    observed = {}

    async def fake_run_turn(messages, llm, mcp, **kwargs):
        del llm, mcp
        observed.update(kwargs)
        messages.append({"role": "assistant", "content": "done"})
        return TurnTokenStats(iter_count=1)

    monkeypatch.setattr("cc_harness.runtime.run_turn", fake_run_turn)
    runtime = SessionRuntime()
    runtime.cwd = tmp_path
    runtime.llm = object()
    runtime.mcp = object()
    runtime.max_iterations = None
    runtime.memory_config = SimpleNamespace(layered_inject=False, offload_enabled=False)

    async def emit(_event):
        return None

    async def confirm(_tool, _args, _reason):
        return "yes"

    await runtime.run_user_turn(
        "finish the task",
        event_emitter=emit,
        confirm_handler=confirm,
    )

    assert observed["max_iter"] is None


@pytest.mark.asyncio
async def test_run_user_turn_emits_structured_policy_block_reason(tmp_path, monkeypatch):
    async def blocked(*_args, **_kwargs):
        return ScanResult(allowed=False, reason="judge:injection;confirmed:judge:injection")

    monkeypatch.setattr("cc_harness.runtime.scan_user_input", blocked)
    runtime = SessionRuntime()
    runtime.cwd = tmp_path
    runtime.llm = object()
    runtime.mcp = object()
    runtime.l2_config = L2Config(enabled=True)
    runtime.l2_client = object()
    runtime.l2_model = "judge"
    events = []

    async def emit(event):
        events.append(event)

    async def confirm(_tool, _args, _reason):
        return "yes"

    result = await runtime.run_user_turn(
        "resume work",
        event_emitter=emit,
        confirm_handler=confirm,
    )

    assert result is None
    assert events[0] == {
        "type": "policy_block",
        "stage": "user_input",
        "reason": "judge:injection;confirmed:judge:injection",
    }
    assert events[1]["type"] == "result"
    assert events[1]["blocked"] is True


@pytest.mark.asyncio
async def test_run_user_turn_accounts_for_l2_auxiliary_model_usage(tmp_path, monkeypatch):
    async def allowed(*_args, **_kwargs):
        return ScanResult(
            allowed=True,
            reason="judge:benign",
            wrapped_text="<user_input>work</user_input>",
            model_calls=1,
            usage=UsageRecord(prompt_tokens=10, completion_tokens=2, total_tokens=12),
        )

    async def fake_run_turn(messages, llm, mcp, **_kwargs):
        del llm, mcp
        messages.append({"role": "assistant", "content": "done"})
        return TurnTokenStats(
            api_prompt_tokens=100,
            api_uncached_prompt_tokens=100,
            api_completion_tokens=20,
            api_total_tokens=120,
            iter_count=2,
            api_reported=True,
        )

    monkeypatch.setattr("cc_harness.runtime.scan_user_input", allowed)
    monkeypatch.setattr("cc_harness.runtime.run_turn", fake_run_turn)
    runtime = SessionRuntime()
    runtime.cwd = tmp_path
    runtime.llm = object()
    runtime.mcp = object()
    runtime.l2_config = L2Config(enabled=True)
    runtime.l2_client = object()
    runtime.l2_model = "judge"
    runtime.memory_config = SimpleNamespace(layered_inject=False, offload_enabled=False)

    async def emit(_event):
        return None

    async def confirm(_tool, _args, _reason):
        return "yes"

    stats = await runtime.run_user_turn(
        "work",
        event_emitter=emit,
        confirm_handler=confirm,
    )

    assert stats.iter_count == 2
    assert stats.auxiliary_model_calls == 1
    assert stats.api_prompt_tokens == 110
    assert stats.api_completion_tokens == 22
    assert stats.api_total_tokens == 132


@pytest.mark.asyncio
async def test_close_releases_every_owned_resource_even_when_one_fails(monkeypatch):
    executor_close = AsyncMock()
    monkeypatch.setattr("cc_harness.runtime.shutdown_session_executor", executor_close)

    embedder = SimpleNamespace(aclose=AsyncMock(side_effect=RuntimeError("embedder failed")))
    decider_llm = SimpleNamespace(aclose=AsyncMock())
    store = SimpleNamespace(close=AsyncMock())
    runtime = SessionRuntime()
    runtime.state.mem_deps = {
        "service": SimpleNamespace(
            embedder=embedder,
            decider=SimpleNamespace(_llm=decider_llm),
        ),
        "store": store,
    }
    runtime.session_store = SimpleNamespace(close=AsyncMock())
    runtime.mcp = SimpleNamespace(shutdown=AsyncMock())
    runtime.l2_client = SimpleNamespace(close=AsyncMock())
    runtime.judge_llm = SimpleNamespace(aclose=AsyncMock())
    runtime.llm = SimpleNamespace(aclose=AsyncMock())

    await runtime.close()

    embedder.aclose.assert_awaited_once()
    decider_llm.aclose.assert_awaited_once()
    store.close.assert_awaited_once()
    runtime.session_store.close.assert_awaited_once()
    runtime.mcp.shutdown.assert_awaited_once()
    runtime.l2_client.close.assert_awaited_once()
    runtime.judge_llm.aclose.assert_awaited_once()
    runtime.llm.aclose.assert_awaited_once()
    executor_close.assert_awaited_once()
