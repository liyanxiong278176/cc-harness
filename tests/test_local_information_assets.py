from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cc_harness.artifacts import ArtifactStore
from cc_harness.config import AppConfig, ContextConfig, load_context_config
from cc_harness.continuation import build_continuation, continuation_message, validate_continuation
from cc_harness.local_assets import LocalAssets
from cc_harness.local_skills import LocalSkills
from cc_harness.memory.config import MemoryConfig
from cc_harness.memory.pipeline import MemoryPipeline
from cc_harness.memory.recall import read_persona, read_top_scenarios
from cc_harness.memory.store import MemoryStore
from cc_harness.run_model import GoalContract
from cc_harness.run_projection import RunProjection


def skill(root: Path, *, text="first", name="workflow"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(f"---\nname: {name}\ndescription: A local workflow\n---\n{text}", encoding="utf-8")
    (root / "scripts").mkdir(exist_ok=True)
    (root / "scripts" / "verify.py").write_text(text, encoding="utf-8")


def test_skill_conflicts_require_explicit_source_and_pin_every_resource(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    user = tmp_path / "user-skills"
    skill(project / ".cc-harness" / "skills" / "demo")
    skill(user / "demo", text="user")
    objects = ArtifactStore(tmp_path / "objects")
    registry = LocalSkills(project, objects, user)
    assert all(s["conflict"] and not s["enabled"] for s in registry.discover())
    registry.configure("project:demo", True)
    original, _ = registry.snapshot("run-1")
    skill(project / ".cc-harness" / "skills" / "demo", text="changed")
    restarted = LocalSkills(project, objects, user)
    assert restarted.snapshot("run-1")[0] == original
    result = restarted.read("run-1", "project:demo", "scripts/verify.py", tool_names=set())
    assert result["content"] == "first"
    assert Path(result["resource_path"]).read_text() == "first"
    assert restarted.snapshot("run-2")[0] != original
    changed, payload = restarted.snapshot("run-1", reload=True)
    assert changed != original and payload["revision"] == 2
    registry.configure("user:demo", True)
    assert [s["id"] for s in registry.discover() if s["enabled"]] == ["user:demo"]


def test_skill_escapes_and_unavailable_dependencies_are_rejected(tmp_path):
    skill(tmp_path / ".cc-harness" / "skills" / "demo")
    root = tmp_path / ".cc-harness" / "skills" / "demo"
    (root / "SKILL.md").write_text("---\nname: workflow\nrequired_tools: [missing]\n---\nsteps", encoding="utf-8")
    registry = LocalSkills(tmp_path, ArtifactStore(tmp_path / "objects"), tmp_path / "user")
    registry.configure("project:demo", True)
    _, catalog = registry.catalog("run", set())
    assert not catalog["skills"][0]["available"]
    with pytest.raises(ValueError, match="unavailable tools"):
        registry.read("run", "project:demo", tool_names=set())
    with pytest.raises(ValueError, match="resource"):
        registry.read("run", "project:demo", "../../secret", tool_names={"missing"})


def test_context_policy_file_and_environment_precedence(tmp_path):
    policy = tmp_path / "policy.yaml"
    policy.write_text("context:\n  policy_version: local@2\n  protect_zone_tokens: 77\n  context_window: 40000\n", encoding="utf-8")
    config = load_context_config(policy, environ={"CONTEXT_PROTECT_TOKENS": "123"})
    assert config.policy_version == "local@2"
    assert config.protect_zone_tokens == 123
    assert config.context_window == 40000


def test_continuation_detects_missing_or_altered_state_and_corrupt_evidence(tmp_path):
    projection = replace(RunProjection.empty("run"), goal=GoalContract("fix", ("tests pass",), ("no release",)))
    package = build_continuation(projection)
    artifacts = ArtifactStore(tmp_path)
    message = continuation_message(package)
    assert not validate_continuation([message], package, artifacts)
    assert validate_continuation([], package, artifacts)
    assert validate_continuation([{**message, "content": "tests passed"}], package, artifacts)
    result = artifacts.put_text("original")
    package["artifact_refs"] = [result.digest]
    artifacts.object_path(result.digest).write_text("changed")
    assert "corrupt" in validate_continuation([continuation_message(package)], package, artifacts)[0]


@pytest.mark.asyncio
async def test_forget_retracts_derived_files_and_prevents_reextraction_after_restart(tmp_path):
    store = MemoryStore(tmp_path / "memory.db", 3, project_scope=str(tmp_path))
    await store.init_schema()
    content = "I prefer pytest"
    source = hashlib.sha256(f"user\x00{content}".encode()).hexdigest()
    await store.add_conversation("session", 1, "user", content, 1.0)
    memory = await store.add(content, [1., 0., 0.], "pipeline", session_id="session",
                             provenance_json=json.dumps({"source_digest": source}))
    scenario = tmp_path / "scenarios" / "session-v0001.md"
    scenario.parent.mkdir()
    scenario.write_text(f"summary: {content}\natom_ids:\n- {memory.id}", encoding="utf-8")
    (tmp_path / "persona.md").write_text(content, encoding="utf-8")
    result = await store.forget(memory.id)
    assert result["raw_history_retained"]
    assert await store.count() == 0
    assert read_persona(tmp_path / "persona.md") is None
    assert read_top_scenarios(scenario.parent, 5) == []
    assert await store.search_conversation("pytest") == []
    assert (await (await store._db.execute("SELECT COUNT(*) FROM conversation")).fetchone())[0] == 1
    assert (await store.get(memory.id)).text == ""
    await store.close()
    await store.init_schema()
    assert (await store.forget(memory.id))["memory_epoch"] == result["memory_epoch"]
    with pytest.raises(ValueError, match="forgotten"):
        await store.add("paraphrase", [1., 0., 0.], "pipeline", provenance_json=json.dumps({"source_digest": source}))
    await store.close()


@pytest.mark.asyncio
async def test_forget_fences_inflight_scenario_publication(tmp_path):
    from cc_harness.memory.scenario import cluster_scenarios
    store = MemoryStore(tmp_path / "memory.db", 3)
    await store.init_schema()
    memory = await store.add("source fact", [1., 0., 0.], "pipeline", session_id="s")
    entered, release = asyncio.Event(), asyncio.Event()

    class LLM:
        async def chat(self, *_args, **_kwargs):
            entered.set()
            await release.wait()
            yield SimpleNamespace(kind="done", content="old fact")

    task = asyncio.create_task(cluster_scenarios(store, None, "s", tmp_path / "scenarios", min_atoms=1, llm=LLM()))
    await entered.wait()
    await store.forget(memory.id)
    release.set()
    assert await task == []
    assert not list((tmp_path / "scenarios").glob("*.md"))
    await store.close()


@pytest.mark.asyncio
async def test_verified_auto_memory_rejects_assistant_inference_and_saves_source(tmp_path):
    from cc_harness.memory.service import MemoryService
    from cc_harness.memory.decider import Decision, DecisionResult
    from cc_harness.tokens import TokenCounter
    store = MemoryStore(tmp_path / "memory.db", 3)
    await store.init_schema()
    service = MemoryService(store, SimpleNamespace(embed=AsyncMock(return_value=[1., 0., 0.])),
                            SimpleNamespace(decide=AsyncMock(return_value=DecisionResult(action=Decision.ADD))))
    pipeline = MemoryPipeline(None, service, verified_sources=True)
    pipeline._extract = AsyncMock(return_value=[
        {"quote": "I prefer pytest", "source_index": 0, "kind": "preference"},
        {"quote": "maybe production is safe", "source_index": 1, "kind": "project_fact"},
        {"quote": "not in input", "source_index": 0, "kind": "decision"},
    ])
    result = await pipeline.maybe_run([{"role": "user", "content": "I prefer pytest"},
                                      {"role": "assistant", "content": "maybe production is safe"}],
                                     TokenCounter(), 10000, turn_idx=1, every_n=1, session_id="s")
    assert len(result.results) == 1
    items = await store.list_all()
    assert items[0].text == "I prefer pytest"
    assert json.loads(items[0].provenance_json)["verified_at"] > 0
    # The first user request is embedded in a runtime template. Only its actual
    # user text, rather than the surrounding execution rules, can become memory.
    from cc_harness.interaction_history import objective_messages
    goal = replace(RunProjection.empty("goal"), goal=GoalContract("I prefer ruff", ("report",)))
    pipeline._extract = AsyncMock(return_value=[
        {"quote": "I prefer ruff", "source_index": 0, "kind": "preference"},
        {"quote": "Do not claim completion without evidence.", "source_index": 0, "kind": "decision"},
    ])
    result = await pipeline.maybe_run(list(objective_messages(goal, cwd=tmp_path)), TokenCounter(),
                                     10000, turn_idx=2, every_n=1, session_id="s")
    assert len(result.results) == 1 and result.results[0].memory.text == "I prefer ruff"
    await store.close()


@pytest.mark.asyncio
async def test_management_and_gc_preserve_nested_artifacts_and_skill_snapshots(tmp_path):
    from cc_harness.durable_runtime import DurableRuntimeClient
    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / "runtime")
    try:
        service = LocalAssets(client)
        skill(tmp_path / ".cc-harness" / "skills" / "demo")
        service.skills.configure("project:demo", True)
        snapshot, _ = service.skills.snapshot("pinned")
        nested = client.store.artifacts.put_text("pinned resource")
        # Treat a nested artifact as an event-owned root via a snapshot history entry.
        manifest = client.store.artifacts.put_text(json.dumps({"artifact_ref": nested.digest}))
        with service.skills._connect() as db:
            db.execute("INSERT INTO skill_snapshot_history VALUES (?,?,?)", ("nested", 1, manifest.digest))
        orphan = client.store.artifacts.put_text("uncommitted")
        for ref in (snapshot, manifest.digest, nested.digest, orphan.digest):
            os.utime(client.store.artifacts.object_path(ref), (0, 0))
        preview = await service.cleanup_preview()
        assert [x["digest"] for x in preview["items"]] == [orphan.digest]
        await service.cleanup(preview["token"])
        assert not client.store.artifacts.exists(orphan.digest)
        assert client.store.artifacts.exists(snapshot)
        assert client.store.artifacts.exists(nested.digest)
    finally:
        await client.close()


def test_webui_and_command_management_use_shared_backend(tmp_path):
    from fastapi.testclient import TestClient
    from cc_harness.entrypoint import build_parser
    from cc_harness.webui import WebRuntimeManager, create_web_app
    manager = WebRuntimeManager(initial_cwd=tmp_path, data_root=tmp_path / "runtime")
    with TestClient(create_web_app(manager)) as web:
        assert web.post("/api/projects/select", json={"path": str(tmp_path)}).status_code == 200
        skill(tmp_path / ".cc-harness" / "skills" / "demo")
        items = web.get("/api/local-assets").json()
        assert not items["skills"][0]["enabled"]
        assert web.post("/api/local-assets", json={"action": "enable", "id": "project:demo", "project_root": str(tmp_path)}).status_code == 200
        assert web.get("/api/local-assets").json()["skills"][0]["enabled"]
        assert web.post("/api/local-assets", json={"action": "disable", "id": "project:demo", "project_root": str(tmp_path / "other")}).status_code == 400
    args = build_parser().parse_args(["--command", "assets", "--asset-action", "forget", "--asset-id", "memory-id"])
    assert args.asset_action == "forget" and args.asset_id == "memory-id"


@pytest.mark.asyncio
async def test_real_worker_pins_skills_in_events_and_restores_same_version(tmp_path):
    from cc_harness.capability_runtime import AgentCapabilityRuntime
    from cc_harness.coordinator import RunRequest
    from cc_harness.durable_runtime import DurableRuntimeClient
    from cc_harness.run_kernel import ModelSegment, ReActKernel
    from cc_harness.worker import RunWorker

    class Model:
        calls = []
        async def complete(self, messages, tools):
            self.calls.append(messages)
            return ModelSegment(text="Inspection needs further work.")

    config = AppConfig(openai_api_key="test", openai_base_url="https://unused.invalid", openai_model="test", mcp_servers={})
    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / "runtime")
    skill(tmp_path / ".cc-harness" / "skills" / "demo")
    registry = LocalSkills(tmp_path, client.store.artifacts, tmp_path / "user")
    registry.configure("project:demo", True)
    model = Model()
    try:
        handle = await client.coordinator.submit(RunRequest("Inspect locally", ("report",), constraints=("Do not release",)))
        capabilities = AgentCapabilityRuntime(client.store, tmp_path, None, config, ContextConfig(), MemoryConfig())
        worker = RunWorker(client.store, ReActKernel(model), worker_id="first", capability_runtime=capabilities)
        await worker.execute(await worker.claim(handle.run_id))
        projection = await client.store.load_projection(handle.run_id)
        assert projection.skill_snapshot
        assert any(e.event_type == "SkillSnapshotPinned" for e in (await client.store.read(handle.run_id)).events)
        assert any("Do not release" in str(m.get("content")) and m.get("_continuation_digest") for m in model.calls[-1])
        skill(tmp_path / ".cc-harness" / "skills" / "demo", text="changed")
        state_dir = tmp_path / ".cc-harness" / "context" / handle.run_id
        (state_dir / "CONTINUATION.json").write_text('{"goal":"fake"}', encoding="utf-8")
        await client.coordinator.resume(handle.run_id)
        resumed = AgentCapabilityRuntime(client.store, tmp_path, None, config, ContextConfig(), MemoryConfig())
        second = RunWorker(client.store, ReActKernel(model), worker_id="resumed", capability_runtime=resumed)
        await second.execute(await second.claim(handle.run_id))
        restored = await client.store.load_projection(handle.run_id)
        assert restored.skill_snapshot == projection.skill_snapshot
        assert registry.read(handle.run_id, "project:demo", "scripts/verify.py", tool_names=set(), pinned_digest=restored.skill_snapshot)["content"] == "first"
        assert json.loads((state_dir / "CONTINUATION.json").read_text(encoding="utf-8"))["goal"]["objective"] == "Inspect locally"
        # Explicit reload is an event, not merely a mutable registry pointer.
        result = await LocalAssets(client).reload_skills(handle.run_id)
        assert (await client.store.load_projection(handle.run_id)).skill_snapshot == result["snapshot"]
        assert result["snapshot"] != projection.skill_snapshot
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_reference_rebinding_checks_exact_historical_bytes_and_project_boundary(tmp_path):
    from cc_harness.durable_runtime import DurableRuntimeClient
    from cc_harness.memory.offload.offload import _commit_sqlite_manifest
    from cc_harness.memory.offload.read_ref import read_ref_handler
    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / "runtime")
    try:
        body = "original evidence\nsecond line"
        digest = "sha256:" + hashlib.sha256(body.encode()).hexdigest()
        _commit_sqlite_manifest(client.store.db_path, "run", {"node_id": "node1", "content_digest": digest,
            "result_ref": f"C:/old/project/refs/objects/{digest[7:]}.md", "session_id": "run", "created_at": 1.0})
        target = tmp_path / "evidence.txt"
        target.write_bytes(b"different")
        assets = LocalAssets(client)
        with pytest.raises(ValueError, match="does not match"):
            assets.bind_reference("node1", "evidence.txt", "run")
        outside = tmp_path.parent / "outside.txt"
        outside.write_bytes(body.encode())
        with pytest.raises(ValueError, match="inside"):
            assets.bind_reference("node1", str(outside), "run")
        target.write_bytes(body.encode())
        result = assets.bind_reference("node1", "evidence.txt", "run")
        refs_dir = tmp_path / ".cc-harness" / "context" / "run" / "offload" / "refs"
        read = await read_ref_handler({"node_id": "node1"}, cwd=str(tmp_path), refs_dir=refs_dir,
                                      state_db_path=client.store.db_path, history_context_id="run")
        assert result["digest"] == digest and not read.is_error
        assert body in read.llm_text
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_repeated_real_compaction_and_restart_keep_goal_and_latest_guidance(tmp_path, monkeypatch):
    from cc_harness.capability_runtime import AgentCapabilityRuntime
    from cc_harness.coordinator import RunRequest
    from cc_harness.durable_runtime import DurableRuntimeClient
    from cc_harness.execution_control import ExecutionControl
    from cc_harness.llm import StreamEvent

    class Characters:
        def count_text(self, text):
            return len(text or "")
        def categorize(self, messages, tools=None):
            return {"system_prompt": sum(self.count_text(m.get("content", "")) for m in messages), "tool_definitions": 0}

    class Summary:
        model = "offline-summary"
        calls = 0
        async def chat(self, *_args, **_kwargs):
            self.calls += 1
            yield StreamEvent(kind="done", content="Prior exploration is recorded in source references.")

    monkeypatch.setenv("CC_HARNESS_HOME", str(tmp_path / "user"))
    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / "runtime")
    config = AppConfig(openai_api_key="test", openai_base_url="https://unused.invalid", openai_model="test", mcp_servers={})
    context_config = ContextConfig(context_window=12000, output_reserve_tokens=100, protect_zone_tokens=100,
                                   evidence_token_budget=0, provider_safety_factor=1,
                                   tier1_threshold=.1, tier2_threshold=.2, tier3_threshold=.3)
    llm = Summary()
    try:
        handle = await client.coordinator.submit(RunRequest("Fix locally", ("tests pass",), constraints=("Do not release",)))
        projection = await client.store.load_projection(handle.run_id)
        source = [{"role": "system", "content": "policy", "_context_mandatory": True},
                  {"role": "assistant", "content": "historical exploration\n" * 1000},
                  {"role": "assistant", "content": "recent exact tail. " * 15},
                  {"role": "user", "content": "continue"}]
        capabilities = AgentCapabilityRuntime(client.store, tmp_path, llm, config, context_config, MemoryConfig(), token_counter=Characters())
        for _ in range(3):
            result = await capabilities.build_context(projection, source, [])
            assert result.compaction.summarized
            assert result.compaction.after_tokens < result.compaction.before_tokens
            assert not result.coverage["integrity_errors"]
            source.extend([{"role": "assistant", "content": "more exploration\n" * 1000},
                           {"role": "assistant", "content": "recent exact tail. " * 15},
                           {"role": "user", "content": "continue"}])
        await ExecutionControl(client.store).guidance(handle.run_id, "Do not edit generated files.")
        restored_projection = await client.store.load_projection(handle.run_id)
        restarted = AgentCapabilityRuntime(client.store, tmp_path, llm, config, context_config, MemoryConfig(), token_counter=Characters())
        restored = await restarted.build_context(restored_projection, source, [])
        message = next(m for m in restored.messages if m.get("_continuation_digest"))
        assert "Do not release" in message["content"]
        assert "Do not edit generated files." in message["content"]
        assert llm.calls >= 3
        assert source[1]["content"] == "historical exploration\n" * 1000
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_forget_clears_superseded_and_merged_versions(tmp_path):
    store = MemoryStore(tmp_path / "memory.db", 3)
    await store.init_schema()
    try:
        first = await store.add("old fact", [1., 0., 0.], "pipeline")
        second = await store.supersede(first.id, "updated fact", [1., 0., 0.])
        merged = await store.add("merged conclusion", [1., 0., 0.], "consolidation",
                                 provenance_json=json.dumps({"parent_ids": [second.id], "memory_epoch": 0}))
        result = await store.forget(second.id)
        assert set(result["forgotten_ids"]) == {first.id, second.id, merged.id}
        assert await store.count() == 0
        for ident in result["forgotten_ids"]:
            assert (await store.get(ident)).text == ""
        with pytest.raises(ValueError, match="generation"):
            await store.add("stale writer result", [1., 0., 0.], "pipeline", provenance_json=json.dumps({"memory_epoch": 0}))
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_unrecoverable_context_budget_waits_before_any_model_or_tool_action(tmp_path):
    from cc_harness.capability_runtime import AgentCapabilityRuntime
    from cc_harness.coordinator import RunRequest
    from cc_harness.durable_runtime import DurableRuntimeClient
    from cc_harness.run_kernel import ModelSegment, ReActKernel
    from cc_harness.worker import RunWorker
    model = SimpleNamespace(complete=AsyncMock(return_value=ModelSegment(text="must not run")))
    config = AppConfig(openai_api_key="test", openai_base_url="https://unused.invalid", openai_model="test", mcp_servers={})
    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / "runtime")
    try:
        handle = await client.coordinator.submit(RunRequest("Inspect locally", ("report",)))
        capabilities = AgentCapabilityRuntime(client.store, tmp_path, None, config,
            ContextConfig(context_window=100, output_reserve_tokens=0, protect_zone_tokens=0), MemoryConfig())
        worker = RunWorker(client.store, ReActKernel(model), worker_id="bounded", capability_runtime=capabilities)
        await worker.execute(await worker.claim(handle.run_id))
        projection = await client.store.load_projection(handle.run_id)
        assert projection.status.value == "waiting_event"
        assert projection.task.wait["kind"] == "waiting_input"
        assert not model.complete.called
        assert not projection.actions
    finally:
        await client.close()
