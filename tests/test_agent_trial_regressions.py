"""Deterministic reproductions of the real-provider local trial failures."""
import json

import pytest

from cc_harness.loop_control import completion_contract_from_instruction
from cc_harness.memory.offload.offload import maybe_offload
from cc_harness.memory.store import MemoryStore
from cc_harness.worker import _is_verification_command


def test_environment_inspection_is_not_completion_verification():
    assert not _is_verification_command("run_command", "python --version; git status --porcelain")
    assert not _is_verification_command("run_command", "python -m pytest | Select-Object -Last 10")
    assert _is_verification_command("run_command", "python verify_audit.py")
    assert _is_verification_command("run_command", "python scripts/check.py manifest.json")


def test_local_output_contract_catches_relative_artifacts_without_requiring_input():
    contract = completion_contract_from_instruction(
        "Read orders.jsonl. Implement pipeline.py and output totals.json and diagnostics.json."
    )
    assert set(contract.required_paths) == {"pipeline.py", "totals.json", "diagnostics.json"}


def test_context_sync_rebuilds_changed_prefix_even_when_message_count_is_equal():
    from cc_harness.context import ContextProjection
    old = [{"role": "system", "content": "policy"}, {"role": "user", "content": "old control"}]
    new = [{"role": "system", "content": "policy"}, {"role": "user", "content": "new control"}]
    projection = ContextProjection(old)
    projection.sync(new)
    assert projection.messages == new


def test_trial_grader_accepts_envelopes_but_rejects_conflicts_and_duplicate_regions():
    from scripts.local_agent_trials import region_totals
    expected = {"eu": 5, "sg": 7}
    assert region_totals({"regions": expected, "currency_unit": "cents"}, expected) == expected
    assert region_totals({"totals": [{"region": "eu", "total_cents": 5}, {"region": "sg", "total_cents": 7}]}, expected) == expected
    assert region_totals({"a": expected, "b": {"eu": 0, "sg": 0}}, expected) is None
    assert region_totals([{"region": "eu", "total_cents": 5}, {"region": "eu", "total_cents": 7}], expected) is None


@pytest.mark.asyncio
async def test_forget_blocks_recalled_derivatives_in_other_sessions_and_keeps_raw_history(tmp_path):
    store = MemoryStore(tmp_path / "memory.db", 3)
    await store.init_schema()
    try:
        text = "For Borealis use Chinese and marker BOR-7e92c1"
        assert (await (await store._db.execute("PRAGMA journal_mode")).fetchone())[0] == "wal"
        memory = await store.add(text, [1., 0., 0.], "pipeline", session_id="teach")
        await store.add_conversation("teach", 1, "user", text, 1.)
        await store.add_conversation("recall", 1, "tool", "[L1] " + text, 2.)
        await store.add_conversation("recall", 2, "assistant", "Borealis marker: BOR-7e92c1", 3.)
        await store.forget(memory.id)
        assert not await store.search_conversation("Borealis")
        assert (await (await store._db.execute("SELECT COUNT(*) FROM conversation")).fetchone())[0] == 3
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_pointer_and_bounded_reference_reads_are_never_offloaded_again(tmp_path):
    class Counter:
        def count_text(self, text):
            return len(text)
    for tool, text in [("Read", "[offloaded node=original source_ref=node:original digest=sha256:abc]"),
                       ("read_ref", "original receipt " * 100)]:
        assert await maybe_offload(text, tool, {}, 1, tmp_path, None, Counter()) is None


@pytest.mark.asyncio
async def test_failed_command_can_retry_after_repair_and_finish(tmp_path):
    from cc_harness.coordinator import RunCoordinator, RunRequest
    from cc_harness.run_kernel import ModelSegment, ReActKernel
    from cc_harness.run_model import ActionStatus
    from cc_harness.run_store import RunStore
    from cc_harness.worker import ActionExecutionResult, RunWorker

    class Model:
        calls = 0
        async def complete(self, messages, tools):
            self.calls += 1
            actions = [("run_command", {"command": "python build.py"}),
                       ("Write", {"path": "settings.json", "content": "{}"}),
                       ("run_command", {"command": "python build.py"})]
            if self.calls > len(actions):
                return ModelSegment(text="Done, build verified.")
            name, args = actions[self.calls - 1]
            return ModelSegment(tool_calls=({"id": f"call-{self.calls}", "name": name, "arguments": args},))

    executed = []
    async def execute(request):
        executed.append(request.tool_name)
        if len(executed) == 1:
            return ActionExecutionResult(ActionStatus.FAILED, error_kind="tool_error")
        if request.tool_name == "Write":
            (tmp_path / "settings.json").write_text("{}")
            return ActionExecutionResult(ActionStatus.SUCCEEDED, modified_paths=("settings.json",))
        (tmp_path / "receipt.json").write_text(json.dumps({"ok": True}))
        return ActionExecutionResult(ActionStatus.SUCCEEDED)

    store = RunStore(tmp_path, data_root=tmp_path / "runtime")
    await store.open()
    try:
        handle = await RunCoordinator(store).submit(RunRequest("Repair settings.json and write receipt.json", ("build succeeds",)))
        worker = RunWorker(store, ReActKernel(Model()), worker_id="regression", action_executor=execute,
                           permission_mode="bypass-prompts")
        await worker.execute(await worker.claim(handle.run_id))
        p = await store.load_projection(handle.run_id)
        assert executed == ["run_command", "Write", "run_command"]
        assert p.status.value == "completed"
        assert not p.working_state.unresolved_errors
        assert any(e.event_type == "FailureResolved" for e in (await store.read(handle.run_id)).events)
    finally:
        await store.close()


def test_new_workspace_version_never_retries_an_unknown_effect(tmp_path):
    from dataclasses import replace
    from cc_harness.run_kernel import ActionRequest
    from cc_harness.run_model import ActionAttempt, ActionStatus, EffectClass
    from cc_harness.run_projection import RunProjection, WorkingStateProjection
    from cc_harness.run_store import RunStore
    from cc_harness.worker import RunWorker
    worker = RunWorker(RunStore(tmp_path), object(), worker_id="test")
    request = ActionRequest("new", "run_command", {"command": "python build.py"})
    action = ActionAttempt("old", "run", "run_command", request.normalized_args_digest,
                           EffectClass.UNKNOWN, "sha256:args", "sha256:contract", "worker", 1,
                           idempotency_key="verify:old-inputs", status=ActionStatus.OUTCOME_UNKNOWN)
    p = replace(RunProjection.empty("run"), actions=(action,), working_state=WorkingStateProjection(last_mutation_sequence=20))
    requests = worker._refresh_completed_reads(p, (request,))
    chosen, blocked = worker._deduplicate_action_requests(p, requests)
    assert not chosen and blocked


@pytest.mark.asyncio
async def test_slow_offload_summary_falls_back_without_losing_original(tmp_path, monkeypatch):
    import asyncio
    import cc_harness.memory.offload.offload as offload_module
    class Slow:
        async def chat(self, *args, **kwargs):
            await asyncio.Event().wait()
            yield None
    class Counter:
        def count_text(self, text):
            return len(text)
    monkeypatch.setattr(offload_module, "_SUMMARY_TIMEOUT_SECONDS", .01)
    body = "original exact receipt: 192837" * 10
    result = await maybe_offload(body, "Read", {}, 1, tmp_path, Slow(), Counter())
    from pathlib import Path
    assert Path(result.refs_path).read_text(encoding="utf-8") == body


@pytest.mark.asyncio
async def test_offload_manifest_lock_does_not_block_event_loop(tmp_path, monkeypatch):
    import asyncio
    import threading
    import cc_harness.memory.offload.offload as module
    released = threading.Event()
    progressed = False
    def locked_commit(*args):
        assert released.wait(1)
    async def release_from_loop():
        nonlocal progressed
        await asyncio.sleep(.01)
        progressed = True
        released.set()
    class Counter:
        def count_text(self, text):
            return len(text)
    monkeypatch.setattr(module, "_commit_sqlite_manifest", locked_commit)
    # Prevent a broken synchronous implementation from hanging the test.
    watchdog = threading.Timer(.2, released.set)
    watchdog.start()
    release = asyncio.create_task(release_from_loop())
    try:
        await maybe_offload("exact original" * 50, "Read", {}, 1, tmp_path / "refs", None, Counter(),
                            manifest_path=tmp_path / "nodes.jsonl", state_db_path=tmp_path / "runtime.db")
        assert progressed
    finally:
        watchdog.cancel()
        await release


@pytest.mark.asyncio
async def test_historical_alias_read_reaches_original_and_rejects_corruption(tmp_path):
    import hashlib
    from cc_harness.memory.offload.read_ref import read_ref_handler
    refs = tmp_path / "refs"
    refs.mkdir()
    manifest = tmp_path / "nodes.jsonl"
    contents = {"source": "AUTHORITATIVE_RECEIPT exact-original-192837",
                "alias": "[offloaded node=source source_ref=node:source]"}
    for node, text in contents.items():
        path = refs / f"{node}.md"
        path.write_text(text, encoding="utf-8")
        with manifest.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"node_id": node, "result_ref": str(path),
                                "content_digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}) + "\n")
    result = await read_ref_handler({"node_id": "alias"}, cwd=str(tmp_path), refs_dir=refs, manifest_path=manifest)
    assert not result.is_error and contents["source"] in result.llm_text
    (refs / "source.md").write_text("tampered")
    result = await read_ref_handler({"node_id": "alias"}, cwd=str(tmp_path), refs_dir=refs, manifest_path=manifest)
    assert result.is_error and "digest mismatch" in result.llm_text


@pytest.mark.asyncio
async def test_worker_and_controls_serialize_parallel_event_sequences(tmp_path):
    import asyncio
    from cc_harness.coordinator import RunCoordinator, RunRequest
    from cc_harness.run_store import RunStore
    from cc_harness.worker import RunWorker
    store = RunStore(tmp_path, data_root=tmp_path / "runtime")
    await store.open()
    try:
        handle = await RunCoordinator(store).submit(RunRequest("Inspect", ("report",)))
        worker = RunWorker(store, object(), worker_id="parallel")
        lease = await worker.claim(handle.run_id)
        before = (await store.load_projection(handle.run_id)).sequence
        await asyncio.gather(*[worker._append(lease, "StallDiagnosisRecorded", {"diagnosis": f"probe {i}"}) for i in range(10)],
                             *[worker.execution.append(handle.run_id, "TaskKernelPhase", {"phase": "act", "timing": "before"}) for _ in range(10)])
        assert (await store.load_projection(handle.run_id)).sequence == before + 20
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_shell_parse_rejection_is_retained_but_does_not_block_verified_goal(tmp_path):
    from cc_harness.coordinator import RunCoordinator, RunRequest
    from cc_harness.tool_observation import make_observation
    from cc_harness.run_kernel import ModelSegment, ReActKernel
    from cc_harness.run_model import ActionStatus
    from cc_harness.run_store import RunStore
    from cc_harness.worker import ActionExecutionResult, RunWorker
    class Model:
        calls = 0
        async def complete(self, messages, tools):
            self.calls += 1
            if self.calls > 2:
                return ModelSegment(text="Verified")
            return ModelSegment(tool_calls=({"id": f"call-{self.calls}", "name": "run_command",
                "arguments": {"command": "python - <<'PY'\nPY" if self.calls == 1 else "python verify.py"}},))
    async def execute(request):
        if request.action_id == "call-1":
            obs = make_observation(action_id=request.action_id, attempt=1, tool_name="run_command", status="failed",
                effect_class="unknown", text="ParserError: invalid redirection", error_kind="tool_error",
                metadata={"stderr": "ParserError: invalid redirection", "exit_code": 1, "request_arguments": dict(request.arguments)})
            return ActionExecutionResult(ActionStatus.FAILED, observation=obs, error_kind="tool_error")
        return ActionExecutionResult(ActionStatus.SUCCEEDED)
    store = RunStore(tmp_path, data_root=tmp_path / "runtime")
    await store.open()
    try:
        handle = await RunCoordinator(store).submit(RunRequest("Inspect and verify", ("verified",)))
        worker = RunWorker(store, ReActKernel(Model()), worker_id="parse", action_executor=execute, permission_mode="bypass-prompts")
        await worker.execute(await worker.claim(handle.run_id))
        p = await store.load_projection(handle.run_id)
        assert p.status.value == "completed" and not p.working_state.unresolved_errors
        assert any(a.status is ActionStatus.FAILED for a in p.actions)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_product_candidate_cannot_label_a_lookup_as_a_passing_test(tmp_path):
    from types import SimpleNamespace
    from cc_harness.coordinator import RunCoordinator, RunRequest
    from cc_harness.run_kernel import ActionRequest
    from cc_harness.run_model import ActionStatus, CompletionCandidate, EvidenceKind, EvidenceRef
    from cc_harness.run_store import RunStore
    from cc_harness.worker import ActionExecutionResult, RunWorker
    async def execute(request):
        return ActionExecutionResult(ActionStatus.SUCCEEDED)
    store = RunStore(tmp_path, data_root=tmp_path / "runtime")
    await store.open()
    try:
        handle = await RunCoordinator(store).submit(RunRequest("Fix the code", ("tests pass",)))
        worker = RunWorker(store, object(), worker_id="strict", action_executor=execute,
                           capability_runtime=SimpleNamespace())
        lease = await worker.claim(handle.run_id)
        await worker._execute_action(lease, ActionRequest("read", "Read", {"path": "README.md"}))
        candidate = CompletionCandidate(("tests pass",), (EvidenceRef("fake", EvidenceKind.TEST, "sha256:passed", "claimed pytest", 1.),))
        assert not await worker._try_accept_completion(lease, candidate)
    finally:
        await store.close()


def test_arbitrary_assertion_script_is_hash_bound_and_compound_commands_are_rejected(tmp_path, monkeypatch):
    from cc_harness.local_verification import python_assertion_verifier
    script = tmp_path / "_audit.py"
    script.write_text("assert 2 + 2 == 4\n")
    first = python_assertion_verifier(tmp_path, "python _audit.py")
    assert first and first["assertions"] == 1
    assert python_assertion_verifier(tmp_path, "python _audit.py | echo ok") is None
    assert python_assertion_verifier(tmp_path, "python -O _audit.py") is None
    script.write_text("assert 2 + 3 == 5\n")
    assert python_assertion_verifier(tmp_path, "python _audit.py")["script_digest"] != first["script_digest"]
    script.write_text("print('ALL_VERIFIED')\n")
    assert python_assertion_verifier(tmp_path, "python _audit.py") is None

    monkeypatch.setenv("PYTHONOPTIMIZE", "1")
    script.write_text("assert True\n")
    assert python_assertion_verifier(tmp_path, "python _audit.py") is None


@pytest.mark.asyncio
async def test_verified_outputs_enter_completion_review_without_more_exploration(tmp_path, monkeypatch):
    import re
    from cc_harness.capability_runtime import AgentCapabilityRuntime
    from cc_harness.config import AppConfig, ContextConfig
    from cc_harness.coordinator import RunCoordinator, RunRequest
    from cc_harness.memory.config import MemoryConfig
    from cc_harness.run_kernel import ModelSegment, ReActKernel
    from cc_harness.run_model import ActionStatus
    from cc_harness.run_store import RunStore
    from cc_harness.worker import ActionExecutionResult, RunWorker
    monkeypatch.setenv("CC_HARNESS_HOME", str(tmp_path / "user"))
    class Model:
        calls = 0
        async def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 3:
                assert not tools
                review = next(m['content'] for m in messages if 'Runtime completion review:' in str(m.get('content', '')))
                proposal = json.loads(re.search(r'<cc-harness-complete>(.*?)</cc-harness-complete>', review, re.S).group(1))
                return ModelSegment(text="Verified delivery", completion_candidate=proposal)
            name, args = (("Write", {"path": "result.json", "content": "{}"}) if self.calls == 1
                          else ("run_command", {"command": "python verify.py"}))
            return ModelSegment(tool_calls=({'id': f'call-{self.calls}', 'name': name, 'arguments': args},))
    async def execute(request):
        if request.tool_name == 'Write':
            (tmp_path / 'result.json').write_text('{}')
            return ActionExecutionResult(ActionStatus.SUCCEEDED, modified_paths=('result.json',))
        return ActionExecutionResult(ActionStatus.SUCCEEDED)
    store = RunStore(tmp_path, data_root=tmp_path / 'runtime')
    await store.open()
    capability = AgentCapabilityRuntime(store, tmp_path, None,
        AppConfig(openai_api_key='test', openai_base_url='https://unused.invalid', openai_model='test', mcp_servers={}),
        ContextConfig(context_window=64000), MemoryConfig())
    try:
        handle = await RunCoordinator(store).submit(RunRequest('Create result.json and verify it', ('verified',)))
        model = Model()
        worker = RunWorker(store, ReActKernel(model), worker_id='review', action_executor=execute,
            capability_runtime=capability, permission_mode='bypass-prompts',
            available_tools=({'type': 'function', 'function': {'name': 'Write'}}, {'type': 'function', 'function': {'name': 'run_command'}}))
        await worker.execute(await worker.claim(handle.run_id))
        assert (await store.load_projection(handle.run_id)).status.value == 'completed'
        assert model.calls == 3
    finally:
        await capability.close()
        await store.close()
