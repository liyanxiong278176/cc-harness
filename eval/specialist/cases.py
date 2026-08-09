"""Materialize concrete deterministic cases from the frozen specialist catalog."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .context_fixture import materialize_context_fixture
from .models import SpecialistSuite, SpecialistTaskDefinition

SessionMode = Literal["new", "continue", "fresh"]


@dataclass(frozen=True)
class CasePhase:
    name: str
    prompt: str
    session_mode: SessionMode
    final: bool = False


@dataclass(frozen=True)
class SpecialistCase:
    task: SpecialistTaskDefinition
    files: dict[str, bytes]
    phases: tuple[CasePhase, ...]
    expected: Any
    grader_kind: str
    required_capabilities: tuple[str, ...]
    uses_mcp: bool = False


def build_case(
    task: SpecialistTaskDefinition,
    fixture_root: Path,
    *,
    context_window_tokens: int,
    locomo_path: Path,
) -> SpecialistCase:
    if task.suite is SpecialistSuite.AGENT_LOOP:
        return _agent_loop_case(task)
    if task.suite is SpecialistSuite.CONTEXT:
        return _context_case(task, fixture_root, context_window_tokens)
    if task.suite is SpecialistSuite.MEMORY:
        return _memory_case(task, locomo_path)
    return _tools_case(task)


def _agent_loop_case(task: SpecialistTaskDefinition) -> SpecialistCase:
    token = _token(task)
    answer_path = ".specialist/answer.json"
    if task.scenario == "transient-tool-failure":
        key = f"key-{task.variant}-{token[:6]}"
        prompt = (
            f"Call the specialist MCP flaky_read tool with key {key!r}. It is expected to fail "
            "transiently; recover without changing the key until it succeeds. "
            + _answer_instruction(task.task_id, f"value:{key}", answer_path)
        )
        return _single(task, prompt, f"value:{key}", ("mcp",), uses_mcp=True)
    if task.scenario == "permanent-tool-failure":
        expected = f"fallback-{task.variant}-{token[:8]}"
        files = {"fallback.txt": f"authoritative={expected}\n".encode()}
        prompt = (
            "Try specialist MCP permanent_failure once. It will remain unavailable. Recover by "
            "finding the authoritative local fallback instead of retrying forever. "
            + _answer_instruction(task.task_id, expected, answer_path)
        )
        return _single(task, prompt, expected, ("mcp", "read"), files=files, uses_mcp=True)
    if task.scenario == "failed-test-recovery":
        expected = task.variant * 7 + 3
        files = {
            "solution.py": b"def transform(value):\n    return value - 1\n",
            "verify.py": (
                "from pathlib import Path\n"
                "state = Path('.probe-count')\n"
                "count = int(state.read_text() or '0') + 1 if state.exists() else 1\n"
                "state.write_text(str(count))\n"
                "if count == 1:\n"
                "    raise SystemExit('transient verifier setup failure')\n"
                "from solution import transform\n"
                f"assert transform({task.variant}) == {expected}\n"
            ).encode(),
        }
        prompt = (
            "Repair solution.py. Run `python verify.py`; the verifier has one transient setup "
            "failure, so use its later deterministic result to finish the repair. Do not edit "
            f"verify.py. Set `answer` to the integer returned by `transform({task.variant})` "
            "after the repair, not a status code or the number of verifier runs. "
            + _answer_instruction(task.task_id, expected, answer_path)
        )
        return _single(
            task,
            prompt,
            expected,
            ("shell",),
            files=files,
            grader_kind="python-transform",
        )
    if task.scenario == "wrong-hypothesis-recovery":
        expected = f"accepted-{task.variant}-{token[:8]}"
        files = {
            "decisions/old.md": f"status: superseded\nvalue: stale-{token[:8]}\n".encode(),
            "decisions/current.md": f"status: accepted\nvalue: {expected}\n".encode(),
            "decisions/draft.md": f"status: draft\nvalue: draft-{token[8:16]}\n".encode(),
        }
        prompt = (
            "Review all decision records and use only the accepted record. "
            + _answer_instruction(task.task_id, expected, answer_path)
        )
        return _single(task, prompt, expected, ("glob", "read"), files=files)
    if task.scenario == "no-progress-stop":
        expected = f"local-evidence-{task.variant}-{token[:8]}"
        files = {"evidence/authoritative.txt": f"value={expected}\n".encode()}
        prompt = (
            "Call the specialist MCP no_progress source exactly once. It never yields new "
            "evidence, so do not repeat it; continue with the local authoritative evidence. "
            + _answer_instruction(task.task_id, expected, answer_path)
        )
        return _single(task, prompt, expected, ("mcp", "read"), files=files, uses_mcp=True)

    key = f"resume-{task.variant}-{token[:8]}"
    expected = f"effect-for-{key}"
    first = CasePhase(
        name="checkpoint",
        session_mode="new",
        prompt=(
            "Call specialist MCP mutate_once with idempotency_key "
            f"{key!r} and value {expected!r}. Record that the operation completed, then stop."
        ),
    )
    second = CasePhase(
        name="resume",
        session_mode="continue",
        final=True,
        prompt=(
            "The process has restarted. Verify the prior side effect by calling mutate_once with "
            "the same idempotency key and value; do not create a second effect. Set `answer` to "
            "the retained value returned by the tool, not its status. "
            + _answer_instruction(task.task_id, expected, answer_path)
        ),
    )
    return SpecialistCase(
        task=task,
        files={},
        phases=(first, second),
        expected=expected,
        grader_kind="idempotent-effect",
        required_capabilities=("mcp",),
        uses_mcp=True,
    )


def _context_case(
    task: SpecialistTaskDefinition,
    fixture_root: Path,
    context_window_tokens: int,
) -> SpecialistCase:
    assert task.context_profile is not None
    seed = int(_token(task)[:8], 16)
    fixture = materialize_context_fixture(
        fixture_root,
        task.context_profile,
        context_window_tokens=context_window_tokens,
        seed=seed,
    )
    corpus = fixture.document_path.read_text(encoding="utf-8")
    facts = [
        f"FACT-{seed}-{index:02d}=accepted-{index:02d}"
        for index in range(task.context_profile.required_fact_count)
    ]
    expected = {"facts": facts, "source_id": f"authoritative-{seed}"}
    prompt = (
        "The following frozen corpus contains distractors and one AUTHORITATIVE-RECORD. Preserve "
        "every accepted fact and its source_id. Ignore superseded records.\n\n"
        f"{corpus}\n\n" + _answer_instruction(task.task_id, expected, ".specialist/answer.json")
    )
    phases = [CasePhase(name="pressure", prompt=prompt, session_mode="new", final=True)]
    if task.context_profile.require_resume:
        phases[0] = CasePhase(name="pressure", prompt=prompt, session_mode="new")
        phases.append(
            CasePhase(
                name="resume",
                session_mode="continue",
                final=True,
                prompt=(
                    "Continue from the prior context after process restart. Write the accepted "
                    "facts and source without rereading or inventing values. "
                    + _answer_instruction(task.task_id, expected, ".specialist/answer.json")
                ),
            )
        )
    return SpecialistCase(
        task=task,
        files={},
        phases=tuple(phases),
        expected=expected,
        grader_kind="exact-json",
        required_capabilities=(),
    )


def _memory_case(task: SpecialistTaskDefinition, locomo_path: Path) -> SpecialistCase:
    if task.scenario == "locomo":
        samples = json.loads(locomo_path.read_text(encoding="utf-8"))
        sample = next(item for item in samples if item["sample_id"] == task.dataset_sample_id)
        conversation = json.dumps(sample["conversation"], ensure_ascii=False, sort_keys=True)
        qas = sample["qa"]
        questions = [item.get("question", "") for item in qas]
        expected = [item.get("answer", "") for item in qas]
        phases = (
            CasePhase(
                name="acquire",
                session_mode="new",
                prompt=(
                    "Store the following conversation as durable project memory with source and "
                    f"time information preserved.\n\n{conversation}"
                ),
            ),
            CasePhase(
                name="query",
                session_mode="fresh",
                final=True,
                prompt=(
                    "In this fresh session, answer every question from project memory in the same "
                    "order. Do not use outside knowledge. Questions:\n"
                    + json.dumps(questions, ensure_ascii=False)
                    + "\n"
                    + _answer_instruction(task.task_id, expected, ".specialist/answer.json")
                ),
            ),
        )
        return SpecialistCase(
            task=task,
            files={},
            phases=phases,
            expected=expected,
            grader_kind="locomo-f1",
            required_capabilities=(),
        )

    token = _token(task)
    original = f"project-fact-{task.variant}-{token[:10]}"
    updated = f"updated-fact-{task.variant}-{token[10:20]}"
    acquire = CasePhase(
        name="acquire",
        session_mode="new",
        prompt=(
            f"Remember this durable fact for this project: value={original}. Source=brief-v1. "
            "Confirm only after it is persisted."
        ),
    )
    phases: list[CasePhase] = [acquire]
    expected: Any = original
    grader_kind = "exact-json"
    if task.scenario == "conflict-update":
        phases.append(
            CasePhase(
                name="update",
                session_mode="fresh",
                prompt=(
                    f"Update the project fact: value={updated} supersedes {original}. "
                    "Source=brief-v2. Persist the update."
                ),
            )
        )
        expected = updated
    elif task.scenario == "forget":
        phases.append(
            CasePhase(
                name="forget",
                session_mode="fresh",
                prompt=f"Forget the durable project fact value={original} and confirm its removal.",
            )
        )
        expected = "unknown"
    elif task.scenario == "project-isolation":
        expected = "unknown"
        grader_kind = "project-isolation"
    elif task.scenario == "source-attribution":
        expected = {"value": original, "source": "brief-v1"}
    elif task.scenario == "persistence-confirmation":
        phases[0] = CasePhase(
            name="acquire",
            session_mode="new",
            prompt=(
                "Persist this durable project fact using the native memory mechanism and verify "
                f"that persistence was confirmed: value={original}."
            ),
        )

    phases.append(
        CasePhase(
            name="query",
            session_mode="fresh",
            final=True,
            prompt=(
                "In this fresh session, report the current durable project fact. Use `unknown` if "
                "it was forgotten or does not belong to this project. "
                + _answer_instruction(task.task_id, expected, ".specialist/answer.json")
            ),
        )
    )
    return SpecialistCase(
        task=task,
        files={},
        phases=tuple(phases),
        expected=expected,
        grader_kind=grader_kind,
        required_capabilities=(),
    )


def _tools_case(task: SpecialistTaskDefinition) -> SpecialistCase:
    token = _token(task)
    scenario = task.scenario
    answer_path = ".specialist/answer.json"
    files: dict[str, bytes] = {}
    grader_kind = "exact-json"
    uses_mcp = False
    if scenario == "read":
        expected = f"read-{task.variant}-{token[:10]}"
        files[f"data/level-{task.variant}/record.txt"] = (
            f"noise=ignore\nauthoritative={expected}\n"
        ).encode()
        prompt = "Read the nested authoritative record. " + _answer_instruction(
            task.task_id, expected, answer_path
        )
    elif scenario == "glob":
        expected = sorted(
            [
                f"packages/p{task.variant}/item-{index}.target"
                for index in range(1, task.variant + 3)
            ]
        )
        for path in expected:
            files[path] = b"selected\n"
        files[f"packages/p{task.variant}/ignore.tmp"] = b"ignore\n"
        prompt = (
            "Find every *.target file and return sorted relative paths. "
            + _answer_instruction(task.task_id, expected, answer_path)
        )
    elif scenario == "grep":
        expected = [f"marker-{task.variant}-{index}" for index in range(1, 4)]
        for index, marker in enumerate(expected, start=1):
            files[f"logs/part-{index}.log"] = f"noise\nNEEDLE={marker}\n".encode()
        prompt = (
            "Search all logs for NEEDLE values and preserve file order. "
            + _answer_instruction(task.task_id, expected, answer_path)
        )
    elif scenario == "edit":
        expected = task.variant * 11
        files["module.py"] = b"def calculate(value):\n    return value - 1\n"
        prompt = (
            f"Fix calculate so calculate({task.variant}) returns {expected}. Keep the change focused. "
            + _answer_instruction(task.task_id, expected, answer_path)
        )
        grader_kind = "python-calculate"
    elif scenario == "write":
        expected = {
            "enabled": True,
            "retries": task.variant,
            "token": token[:12],
        }
        prompt = (
            "Create config/generated.json containing exactly this object: "
            + json.dumps(expected, ensure_ascii=False, sort_keys=True)
            + ". "
            + _answer_instruction(task.task_id, expected, answer_path)
        )
        grader_kind = "written-config"
    elif scenario == "shell":
        expected = f"shell-{task.variant}-{token[:10]}"
        files["probe.py"] = f"print({expected!r})\n".encode()
        prompt = "Run `python probe.py` and capture its exact output. " + _answer_instruction(
            task.task_id, expected, answer_path
        )
    elif scenario == "composed":
        expected = task.variant * 13
        files = {
            "src/config.py": f"MULTIPLIER = {task.variant}\n".encode(),
            "src/service.py": (
                b"from src.config import MULTIPLIER\n\n"
                b"def compute(value):\n"
                b"    return value - MULTIPLIER\n"
            ),
            "src/__init__.py": b"",
        }
        prompt = (
            f"Inspect the src package and fix compute so compute(13) returns {expected}. "
            + _answer_instruction(task.task_id, expected, answer_path)
        )
        grader_kind = "python-composed"
    else:
        uses_mcp = True
        if task.variant == 1:
            expected = {"accepted": True, "record_id": "record-01", "enabled": True}
            prompt = (
                "Call specialist MCP schema_guard with record_id `record-01` and enabled true. "
                + _answer_instruction(task.task_id, expected, answer_path)
            )
        elif task.variant == 2:
            expected = [f"record-{index:02d}" for index in range(1, 13)]
            prompt = (
                "Use specialist MCP paged_lookup with limit 5 and follow every next_cursor. Return "
                "all record ids in order. "
                + _answer_instruction(task.task_id, expected, answer_path)
            )
        elif task.variant == 3:
            key = f"tool-key-{token[:8]}"
            expected = f"value:{key}"
            prompt = (
                f"Recover from specialist MCP flaky_read failures for key {key!r}. "
                + _answer_instruction(task.task_id, expected, answer_path)
            )
        else:
            expected = f"mutation-{token[:8]}"
            prompt = (
                "Call specialist MCP mutate_once twice with idempotency_key `tools-v4` and value "
                f"{expected!r}; report the retained value. "
                + _answer_instruction(task.task_id, expected, answer_path)
            )
    return _single(
        task,
        prompt,
        expected,
        task.required_capabilities,
        files=files,
        grader_kind=grader_kind,
        uses_mcp=uses_mcp,
    )


def _single(
    task: SpecialistTaskDefinition,
    prompt: str,
    expected: Any,
    capabilities: tuple[str, ...],
    *,
    files: dict[str, bytes] | None = None,
    grader_kind: str = "exact-json",
    uses_mcp: bool = False,
) -> SpecialistCase:
    return SpecialistCase(
        task=task,
        files=files or {},
        phases=(CasePhase(name="task", prompt=prompt, session_mode="new", final=True),),
        expected=expected,
        grader_kind=grader_kind,
        required_capabilities=capabilities,
        uses_mcp=uses_mcp,
    )


def _answer_instruction(task_id: str, expected_shape: Any, path: str) -> str:
    schema = json.dumps(_json_schema(expected_shape), ensure_ascii=False, sort_keys=True)
    return (
        f"Write {path} as UTF-8 JSON with exactly two keys: `task_id` equal to {task_id!r} and "
        f"`answer`. The answer must conform to this value-free JSON schema: {schema}. Do not include "
        "explanatory text inside the answer value."
    )


def _json_schema(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        item_schemas = {_schema_key(_json_schema(item)): _json_schema(item) for item in value}
        if not item_schemas:
            items: dict[str, Any] = {}
        elif len(item_schemas) == 1:
            items = next(iter(item_schemas.values()))
        else:
            items = {"anyOf": list(item_schemas.values())}
        return {"type": "array", "items": items}
    if isinstance(value, dict):
        properties = {str(key): _json_schema(item) for key, item in value.items()}
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }
    raise TypeError(f"unsupported expected answer type: {type(value).__name__}")


def _schema_key(schema: dict[str, Any]) -> str:
    return json.dumps(schema, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _token(task: SpecialistTaskDefinition) -> str:
    return hashlib.sha256(task.task_id.encode("utf-8")).hexdigest()
