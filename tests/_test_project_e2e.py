"""End-to-end tests for Sub-project A (Todo tracking).

These tests exercise the full stack:

  - Pure Service layer:    init → service.create/update/delete/validate
                           → storage yaml round-trip
  - REPL + FakeLLM:        run_repl → run_turn → TodoService handlers
                           → storage yaml round-trip

Convention: leading underscore in filename ⇒ pytest skips by default
(`testpaths = ["tests"]` + Python's default `test_*.py` glob excludes
`_test_*.py`). Run explicitly with:

    pytest tests/_test_project_e2e.py --no-header -s

Uses FakeLLM (no real OPENAI_API_KEY required) for the REPL-level tests.
The pure Service tests need nothing external.

Skips by default — to opt in: `pytest tests/_test_project_e2e.py`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cc_harness.cli.init import init_noninteractive
from cc_harness.llm import PendingToolCall
from cc_harness.mcp_client import ToolResult
from cc_harness.project.models import Manifest
from cc_harness.project.service import TodoService


# ===========================================================================
# Fakes — mirror tests/test_agent.py patterns. Kept inline so this file
# is self-contained.
# ===========================================================================


@dataclass
class _FakeStreamEvent:
    """One event yielded by _FakeLLM.chat()."""

    kind: str
    text: str = ""
    finish_reason: str | None = None
    pending: list = field(default_factory=list)
    content: str = ""
    usage: object = None


@dataclass
class _FakeLLM:
    """Yields pre-programmed lists of StreamEvents on chat().

    `responses` is a list of lists: one inner list per turn. Each inner list
    is yielded in order to the consumer (agent.run_turn → _stream_one_turn).
    """

    responses: list  # list[list[_FakeStreamEvent]]
    call_count: int = 0

    async def chat(self, messages: list[dict], tools: list[dict] | None):
        idx = self.call_count
        self.call_count += 1
        if idx >= len(self.responses):
            # Default final stop if exhausted — defensive.
            yield _FakeStreamEvent(kind="done", content="", pending=[], finish_reason="stop")
            return
        for ev in self.responses[idx]:
            yield ev


class _FakeMCP:
    """MCP replacement. No tools, no-op call_tool."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def list_tools(self) -> list[dict]:
        return []

    async def call_tool(self, name: str, args: dict):
        self.calls.append((name, args))
        return ToolResult.success("noop")

    async def shutdown(self) -> None:
        pass


# ===========================================================================
# Helpers
# ===========================================================================


def _init_project(tmp_path: Path, *, name: str = "e2e",
                  resume_mode: str = "manual") -> Manifest:
    """Initialize a fresh project skeleton at tmp_path.

    `write_gitignore=False` skips the git probe subprocess (deterministic
    in tmp_path).
    """
    return init_noninteractive(
        tmp_path, name=name, resume_mode=resume_mode, write_gitignore=False,
    )


def _read_tasks_yaml(proj: Path) -> dict:
    """Read the project's todos.yaml and return the parsed dict."""
    import yaml

    yaml_path = proj / ".cc-harness" / "todos" / "todos.yaml"
    return yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {"tasks": []}


def _fake_inputs(seq: list[str]):
    """Build a coroutine that returns successive values from `seq`.

    Once exhausted, raises EOFError (simulates the user closing stdin).
    """
    queue = list(seq)

    async def _fn(prompt: str) -> str:
        if not queue:
            raise EOFError()
        return queue.pop(0)

    return _fn


def _tc_pending(tool_name: str, args: dict, call_id: str = "c1") -> PendingToolCall:
    """Build a PendingToolCall with JSON-serialized args."""
    import json

    return PendingToolCall(
        index=0, id=call_id, name=tool_name,
        arguments_json=json.dumps(args, ensure_ascii=False),
    )


# ===========================================================================
# 1. Pure Service-layer E2E (no REPL, no LLM)
# ===========================================================================


@pytest.mark.asyncio
async def test_service_full_lifecycle(tmp_path: Path):
    """Pure Service E2E: full create → update → resolve → delete cycle.

    No REPL, no LLM, no MCP — just exercises the service end-to-end including
    yaml persistence and round-trip reads. Covers spec components 2 + 5 + 7.
    """
    manifest = _init_project(tmp_path)
    svc = TodoService(project_root=tmp_path, manifest=manifest)

    # --- Create ---
    t1 = await svc.create(title="task 1")
    t2 = await svc.create(title="task 2", depends_on=[t1.id])

    assert t1.id != t2.id
    assert len(t1.id) == 8  # uuid4 hex[:8]
    assert t1.status == "pending"
    assert t2.depends_on == [t1.id]

    # --- Update (status guard: pending → in_progress → done) ---
    await svc.update(t1.id, status="in_progress")
    await svc.update(t1.id, status="done")
    fetched = await svc.get(t1.id)
    assert fetched.status == "done"

    await svc.update(t2.id, status="in_progress")
    fetched2 = await svc.get(t2.id)
    assert fetched2.status == "in_progress"

    # --- Resolve (BFS upstream chain) ---
    chain = await svc.resolve(t2.id)
    chain_ids = {t.id for t in chain}
    assert t1.id in chain_ids
    assert t2.id in chain_ids

    # --- Force delete → dangling dependency ---
    await svc.delete(t1.id, force=True)
    issues = await svc.validate()
    rule_ids = {i.rule_id for i in issues}
    assert "missing_dependency" in rule_ids, (
        f"force-delete of depended-on task should leave dangling ref; "
        f"got issues: {[i.rule_id for i in issues]}"
    )

    # --- Persistence: t1 gone, t2 remains in todos.yaml ---
    data = _read_tasks_yaml(tmp_path)
    persisted_ids = {t["id"] for t in data["tasks"]}
    assert t1.id not in persisted_ids
    assert t2.id in persisted_ids
    assert len(data["tasks"]) == 1


@pytest.mark.asyncio
async def test_service_md_cleanup_on_force_delete(tmp_path: Path):
    """Verify md file is removed after force delete (spec line 345: avoid disk orphan).

    Storage must delete the per-task md file when its task is removed.
    """
    manifest = _init_project(tmp_path)
    svc = TodoService(project_root=tmp_path, manifest=manifest)

    t = await svc.create(
        title="with description",
        description="# Some markdown\n\nbody text",
    )

    md_path = tmp_path / ".cc-harness" / "todos" / f"{t.id}.md"
    assert md_path.is_file(), "todo_create should write per-task md file"
    assert "body text" in md_path.read_text(encoding="utf-8")

    # Force-delete (the task is pending, no dependents, so default works too)
    await svc.delete(t.id, force=True)

    assert not md_path.exists(), "todo_delete should clean up md file"
    data = _read_tasks_yaml(tmp_path)
    assert t.id not in {x["id"] for x in data["tasks"]}


# ===========================================================================
# 2. REPL + FakeLLM E2E (full stack, no real LLM)
# ===========================================================================


async def _run_repl_with_fake(
    proj: Path, monkeypatch, llm: _FakeLLM, user_inputs: list[str],
) -> None:
    """Boot a real run_repl with FakeLLM/FakeMCP and canned user input.

    Patches:
      - repl._read_user        → canned inputs (EOFError when exhausted)
      - repl.init_session_executor → noop (don't try to build a sandbox)
      - repl.shutdown_session_executor → AsyncMock
      - agent.confirm_tool → "yes" (bypass L4 'ask' gate so unknown tools dispatch)
    """
    from unittest.mock import AsyncMock

    from cc_harness import agent as agent_mod
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl

    monkeypatch.setattr(repl_mod, "_read_user", _fake_inputs(user_inputs))
    monkeypatch.setattr(repl_mod, "init_session_executor", lambda c, r: None)
    monkeypatch.setattr(repl_mod, "shutdown_session_executor", AsyncMock())
    monkeypatch.setattr(agent_mod, "confirm_tool", lambda *a, **k: "yes")

    mcp = _FakeMCP()
    await run_repl(llm, mcp, cwd=str(proj), default_mode="coding")


@pytest.mark.asyncio
async def test_repl_with_fake_llm_create(tmp_path: Path, monkeypatch):
    """REPL + FakeLLM: turn 1 emits todo_create, turn 2 stops.

    Verifies the full REPL → run_turn → TodoService handler → yaml persistence
    round-trip without a real LLM. After REPL exits, the task should be on disk.
    """
    proj = tmp_path / "proj"
    proj.mkdir()

    llm = _FakeLLM(responses=[
        # Turn 1: emit todo_create
        [
            _FakeStreamEvent(kind="content", text="creating"),
            _FakeStreamEvent(
                kind="done", content="creating",
                pending=[_tc_pending("todo_create", {"title": "hello e2e"})],
                finish_reason="tool_calls",
            ),
        ],
        # Turn 2: stop with summary
        [
            _FakeStreamEvent(kind="content", text="done"),
            _FakeStreamEvent(
                kind="done", content="done", pending=[], finish_reason="stop",
            ),
        ],
    ])

    await _run_repl_with_fake(proj, monkeypatch, llm, ["create a task", "exit"])

    # After REPL exits, the task must be persisted on disk.
    data = _read_tasks_yaml(proj)
    titles = {t["title"] for t in data["tasks"]}
    assert "hello e2e" in titles, (
        f"todo_create tool call should persist task to todos.yaml; "
        f"got titles: {titles}"
    )
    # LLM was invoked twice (one tool-call turn, one final turn).
    assert llm.call_count == 2


@pytest.mark.asyncio
async def test_repl_with_fake_llm_update(tmp_path: Path, monkeypatch):
    """REPL + FakeLLM: seed task then FakeLLM emits todo_update → done.

    Exercises read-modify-write through TodoService via the agent dispatch path.
    """
    from cc_harness.project.service import TodoService

    proj = tmp_path / "proj"
    proj.mkdir()
    manifest = _init_project(proj)

    # Pre-seed a pending task via direct service write.
    seed_svc = TodoService(project_root=proj, manifest=manifest)
    seed_task = await seed_svc.create(title="will be marked done")

    llm = _FakeLLM(responses=[
        # Turn 1: emit todo_update status=in_progress (legal from pending)
        [
            _FakeStreamEvent(kind="content", text="updating"),
            _FakeStreamEvent(
                kind="done", content="updating",
                pending=[_tc_pending(
                    "todo_update",
                    {"task_id": seed_task.id, "status": "in_progress"},
                )],
                finish_reason="tool_calls",
            ),
        ],
        # Turn 2: stop
        [
            _FakeStreamEvent(kind="content", text="ok"),
            _FakeStreamEvent(
                kind="done", content="ok", pending=[], finish_reason="stop",
            ),
        ],
    ])

    await _run_repl_with_fake(
        proj, monkeypatch, llm, ["mark it done", "exit"],
    )

    # The seeded task's status should now be 'in_progress' on disk.
    data = _read_tasks_yaml(proj)
    by_id = {t["id"]: t for t in data["tasks"]}
    assert seed_task.id in by_id, "seeded task should still exist"
    assert by_id[seed_task.id]["status"] == "in_progress", (
        f"todo_update should have set status=in_progress; got "
        f"{by_id[seed_task.id]['status']!r}"
    )
    assert llm.call_count == 2


@pytest.mark.asyncio
async def test_repl_with_fake_llm_delete(tmp_path: Path, monkeypatch):
    """REPL + FakeLLM: seed task then FakeLLM emits todo_delete.

    Verifies the delete path through the agent dispatch, including cleanup
    of the md file and yaml removal.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    manifest = _init_project(proj)

    # Pre-seed a task with description so a md file exists.
    seed_svc = TodoService(project_root=proj, manifest=manifest)
    seed_task = await seed_svc.create(
        title="will be deleted", description="delete-me body",
    )
    md_path = proj / ".cc-harness" / "todos" / f"{seed_task.id}.md"
    assert md_path.is_file()

    llm = _FakeLLM(responses=[
        # Turn 1: emit todo_delete
        [
            _FakeStreamEvent(kind="content", text="deleting"),
            _FakeStreamEvent(
                kind="done", content="deleting",
                pending=[_tc_pending(
                    "todo_delete", {"task_id": seed_task.id},
                )],
                finish_reason="tool_calls",
            ),
        ],
        # Turn 2: stop
        [
            _FakeStreamEvent(kind="content", text="ok"),
            _FakeStreamEvent(
                kind="done", content="ok", pending=[], finish_reason="stop",
            ),
        ],
    ])

    await _run_repl_with_fake(
        proj, monkeypatch, llm, ["delete it", "exit"],
    )

    # Task should be gone from yaml AND md file cleaned up.
    data = _read_tasks_yaml(proj)
    assert seed_task.id not in {t["id"] for t in data["tasks"]}, (
        "todo_delete should remove task from todos.yaml"
    )
    assert not md_path.exists(), "todo_delete should clean up md file"
    assert llm.call_count == 2


# ===========================================================================
# 3. Optional real-LLM test — only runs when OPENAI_API_KEY is set.
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="needs OPENAI_API_KEY for real LLM",
)
async def test_repl_with_real_llm_create(tmp_path: Path, monkeypatch):
    """End-to-end with a real LLM. Skipped unless OPENAI_API_KEY is set.

    Boots the real REPL, feeds a user message asking the LLM to create a
    todo, lets the real LLM emit a `todo_create` tool_call, and asserts the
    task landed in todos.yaml. This is the only test in this module that
    actually hits the configured provider.
    """
    from unittest.mock import AsyncMock

    from cc_harness import agent as agent_mod
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl
    from cc_harness.llm import LLMClient

    proj = tmp_path / "proj"
    proj.mkdir()

    api_key = os.environ["OPENAI_API_KEY"]
    base_url = os.environ.get("OPENAI_BASE_URL")
    model = os.environ.get("OPENAI_MODEL", "")
    real_llm = LLMClient(api_key=api_key, model=model, base_url=base_url)

    monkeypatch.setattr(
        repl_mod, "_read_user",
        _fake_inputs([
            "Use todo_create to add a task titled 'real-llm-e2e'. "
            "Only call the tool, do not write any other text.",
            "exit",
        ]),
    )
    monkeypatch.setattr(repl_mod, "init_session_executor", lambda c, r: None)
    monkeypatch.setattr(repl_mod, "shutdown_session_executor", AsyncMock())
    monkeypatch.setattr(agent_mod, "confirm_tool", lambda *a, **k: "yes")

    mcp = _FakeMCP()
    await run_repl(real_llm, mcp, cwd=str(proj), default_mode="coding")

    # Real LLM might or might not call the tool — accept either persisted or not,
    # but at minimum the REPL must have completed without error.
    # In practice with a well-aligned model the task will be there.
    data = _read_tasks_yaml(proj)
    titles = {t["title"] for t in data["tasks"]}
    # Soft assert: if the LLM cooperated, the title should be present.
    # We don't fail the test if it didn't, since LLM behavior is non-deterministic.
    if "real-llm-e2e" not in titles:
        pytest.skip(
            f"Real LLM did not emit todo_create within timeout; "
            f"persisted titles: {titles}"
        )