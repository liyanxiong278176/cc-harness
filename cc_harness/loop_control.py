"""Deterministic control plane for the model-driven agent loop.

The model still chooses actions.  This module owns the state and invariants
that should not depend on the model remembering them: completion checks,
failure classification, progress detection, conservative scheduling, and an
append-only action journal.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class ToolErrorKind(str, Enum):
    TRANSIENT = "transient"
    INVALID_ARGUMENTS = "invalid_arguments"
    PERMISSION = "permission"
    NOT_FOUND = "not_found"
    TEST_FAILURE = "test_failure"
    EXECUTION = "execution"
    UNKNOWN = "unknown"


_TRANSIENT_RE = re.compile(
    r"(?:timeout|timed out|temporar|connection(?: reset| error)?|rate.?limit|"
    r"429|502|503|504|service unavailable|try again)",
    re.IGNORECASE,
)
_ARGUMENT_RE = re.compile(
    r"(?:json parse|schema|validation|invalid (?:argument|parameter)|must be|required)",
    re.IGNORECASE,
)
_PERMISSION_RE = re.compile(
    r"(?:permission denied|access denied|hard.?den|policy.*(?:deny|reject)|user.*(?:deny|reject))",
    re.IGNORECASE,
)
_NOT_FOUND_RE = re.compile(
    r"(?:not found|no such file|does not exist|unknown tool)", re.IGNORECASE,
)
_TEST_FAILURE_RE = re.compile(
    r"(?:test(?:s|ing)? failed|failed[, ]+\d+|assertionerror|pytest.*error)",
    re.IGNORECASE,
)
_TEST_COMMAND_RE = re.compile(
    r"(?:^|[;&|\s])(?:pytest|python\s+-m\s+pytest|npm\s+(?:run\s+)?test|"
    r"pnpm\s+(?:run\s+)?test|yarn\s+test|cargo\s+test|go\s+test|"
    r"dotnet\s+test|mvn(?:w)?\s+test|gradle(?:w)?\s+test)(?:\s|$)",
    re.IGNORECASE,
)
_SERVICE_HEALTH_COMMAND_RE = re.compile(
    r"(?:curl|wget)\b[^\n]*(?:localhost|127\.0\.0\.1|0\.0\.0\.0)|"
    r"(?:grpcurl|nc\s+-z|netcat\s+-z|systemctl\s+is-active|service\s+\S+\s+status)",
    re.IGNORECASE,
)
_EXPLICIT_ARTIFACT_RE = re.compile(r"(?<![\w.-])(/app/[A-Za-z0-9_./-]+)")
_ARTIFACT_DIRECTIVE_RE = re.compile(
    r"\b(?:create|write|save|store|produce|generate|output|place|put|"
    r"创建|写入|保存|生成|输出|放到|存储)\b",
    re.IGNORECASE,
)
_SERVICE_REQUEST_RE = re.compile(
    r"\b(?:server|service|daemon|listen(?:ing)?|endpoint|grpc|smtp|"
    r"webserver|web server|服务器|服务|监听|端口)\b|"
    r"https?://(?:localhost|127\.0\.0\.1)",
    re.IGNORECASE,
)
_SHELL_MUTATION_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:cp|copy|mv|move|rm|del|mkdir|rmdir|touch|"
    r"git\s+(?:apply|checkout|restore|reset|clean)|"
    r"(?:python|node|perl|ruby)\b.*(?:write|replace))\b",
    re.IGNORECASE,
)
_CODE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cs", ".css", ".go", ".h", ".hpp", ".html",
    ".java", ".js", ".jsx", ".kt", ".php", ".py", ".rb", ".rs", ".sh",
    ".sql", ".swift", ".ts", ".tsx", ".vue",
}
_MUTATING_TOOL_NAMES = {
    "Edit", "Write", "edit_file", "write_file", "create_file", "delete_file",
    "move_file", "copy_file", "apply_patch",
}
_READ_ONLY_NATIVE_TOOLS = {"Read", "Glob", "Grep"}
_SENSITIVE_ARG_KEY_RE = re.compile(
    r"(?:api.?key|token|secret|password|credential|authorization|cookie)", re.IGNORECASE,
)


def classify_tool_error(text: str, *, exception: BaseException | None = None) -> ToolErrorKind:
    """Classify a tool failure into a recovery category."""
    value = f"{type(exception).__name__}: {exception}\n{text}" if exception else text
    if _PERMISSION_RE.search(value):
        return ToolErrorKind.PERMISSION
    if _ARGUMENT_RE.search(value):
        return ToolErrorKind.INVALID_ARGUMENTS
    if _NOT_FOUND_RE.search(value):
        return ToolErrorKind.NOT_FOUND
    if _TEST_FAILURE_RE.search(value):
        return ToolErrorKind.TEST_FAILURE
    if _TRANSIENT_RE.search(value):
        return ToolErrorKind.TRANSIENT
    if value.strip():
        return ToolErrorKind.EXECUTION
    return ToolErrorKind.UNKNOWN


@dataclass(frozen=True)
class RecoveryDecision:
    kind: ToolErrorKind
    retry: bool
    terminal: bool
    instruction: str


@dataclass(frozen=True)
class RecoveryPolicy:
    max_transient_retries: int = 2
    retry_delay_seconds: float = 0.25

    def decide(self, text: str, *, attempt: int, exception: BaseException | None = None) -> RecoveryDecision:
        kind = classify_tool_error(text, exception=exception)
        if kind is ToolErrorKind.TRANSIENT and attempt <= self.max_transient_retries:
            return RecoveryDecision(kind, True, False, "Retry the same call after a short delay.")
        instructions = {
            ToolErrorKind.TRANSIENT: "Transient retries are exhausted; choose another source or report the outage.",
            ToolErrorKind.INVALID_ARGUMENTS: "Repair the arguments from the tool schema before trying again.",
            ToolErrorKind.PERMISSION: "This path is terminal; do not attempt a bypass.",
            ToolErrorKind.NOT_FOUND: "Refresh the path or tool inventory before choosing an alternative.",
            ToolErrorKind.TEST_FAILURE: "Use the failing test evidence to revise the implementation.",
            ToolErrorKind.EXECUTION: "Inspect the error and change the hypothesis or action.",
            ToolErrorKind.UNKNOWN: "Re-plan from the available evidence.",
        }
        return RecoveryDecision(
            kind,
            False,
            kind is ToolErrorKind.PERMISSION,
            instructions[kind],
        )


@dataclass(frozen=True)
class CompletionContract:
    required_paths: tuple[str, ...] = ()
    require_verification_after_code_changes: bool = True
    require_session_todos_complete: bool = True
    require_service_health_check: bool = False
    max_rechecks: int = 2


def completion_contract_from_instruction(instruction: str) -> CompletionContract:
    """Derive only explicit, user-visible completion obligations.

    The extractor deliberately ignores arbitrary paths mentioned as inputs. A
    path becomes required only when its line also contains a create/output
    directive. This keeps the contract useful for benchmark and normal coding
    tasks without consulting hidden tests or verifier files.
    """

    required: list[str] = []
    for line in instruction.splitlines():
        if not _ARTIFACT_DIRECTIVE_RE.search(line):
            continue
        for match in _EXPLICIT_ARTIFACT_RE.finditer(line):
            path = match.group(1).rstrip(".,:;)]}'\"")
            if path not in required:
                required.append(path)
    return CompletionContract(
        required_paths=tuple(required),
        require_service_health_check=bool(_SERVICE_REQUEST_RE.search(instruction)),
    )


@dataclass
class WorkingState:
    project_root: Path
    logical_cwd: Path
    sequence: int = 0
    modified_paths: set[str] = field(default_factory=set)
    read_paths: set[str] = field(default_factory=set)
    last_mutation_sequence: int = 0
    last_verification_sequence: int = 0
    last_verification_ok: bool | None = None
    last_service_health_sequence: int = 0
    last_service_health_ok: bool | None = None
    last_tool_name: str = ""
    last_error_kind: str | None = None
    unresolved_errors: list[dict[str, Any]] = field(default_factory=list)
    result_fingerprints: list[str] = field(default_factory=list)

    @classmethod
    def new(cls, project_root: Path) -> WorkingState:
        root = Path(project_root).resolve()
        return cls(project_root=root, logical_cwd=root)

    def observe(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        is_error: bool,
        result_text: str,
        error_kind: ToolErrorKind | None = None,
    ) -> str:
        self.sequence += 1
        self.last_tool_name = tool_name
        path = _tool_path(args)
        if _is_mutating_call(tool_name, args):
            self.last_mutation_sequence = self.sequence
            if path:
                self.modified_paths.add(path)
        elif path:
            self.read_paths.add(path)

        if _is_verification_call(tool_name, args):
            self.last_verification_sequence = self.sequence
            self.last_verification_ok = not is_error
        if _is_service_health_call(tool_name, args):
            self.last_service_health_sequence = self.sequence
            self.last_service_health_ok = not is_error

        kind = error_kind or (classify_tool_error(result_text) if is_error else None)
        self.last_error_kind = kind.value if kind else None
        if is_error:
            self.unresolved_errors.append({
                "sequence": self.sequence,
                "tool": tool_name,
                "kind": self.last_error_kind,
                "result_hash": _text_digest(result_text),
            })

        fingerprint = action_fingerprint(tool_name, args, is_error=is_error, result_text=result_text)
        self.result_fingerprints.append(fingerprint)
        self.result_fingerprints = self.result_fingerprints[-20:]
        return fingerprint

    @property
    def code_was_modified(self) -> bool:
        return any(Path(path).suffix.lower() in _CODE_SUFFIXES for path in self.modified_paths)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_root": str(self.project_root),
            "logical_cwd": str(self.logical_cwd),
            "sequence": self.sequence,
            "modified_paths": sorted(self.modified_paths),
            "read_paths": sorted(self.read_paths),
            "last_mutation_sequence": self.last_mutation_sequence,
            "last_verification_sequence": self.last_verification_sequence,
            "last_verification_ok": self.last_verification_ok,
            "last_service_health_sequence": self.last_service_health_sequence,
            "last_service_health_ok": self.last_service_health_ok,
            "last_tool_name": self.last_tool_name,
            "last_error_kind": self.last_error_kind,
            "unresolved_errors": list(self.unresolved_errors),
            "result_fingerprints": list(self.result_fingerprints),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], project_root: Path) -> WorkingState:
        root = Path(project_root).resolve()
        logical = Path(data.get("logical_cwd") or root)
        try:
            logical.resolve().relative_to(root)
        except (OSError, ValueError):
            logical = root
        return cls(
            project_root=root,
            logical_cwd=logical,
            sequence=int(data.get("sequence", 0)),
            modified_paths=set(data.get("modified_paths") or []),
            read_paths=set(data.get("read_paths") or []),
            last_mutation_sequence=int(data.get("last_mutation_sequence", 0)),
            last_verification_sequence=int(data.get("last_verification_sequence", 0)),
            last_verification_ok=data.get("last_verification_ok"),
            last_service_health_sequence=int(data.get("last_service_health_sequence", 0)),
            last_service_health_ok=data.get("last_service_health_ok"),
            last_tool_name=str(data.get("last_tool_name") or ""),
            last_error_kind=data.get("last_error_kind"),
            unresolved_errors=list(data.get("unresolved_errors") or []),
            result_fingerprints=list(data.get("result_fingerprints") or [])[-20:],
        )


@dataclass(frozen=True)
class CompletionReport:
    passed: bool
    issues: tuple[str, ...] = ()

    def feedback(self) -> str:
        lines = "\n".join(f"- {issue}" for issue in self.issues)
        return (
            "<completion_verification status=\"rejected\">\n"
            "The candidate final answer was not accepted. Resolve every item, then verify again:\n"
            f"{lines}\n</completion_verification>"
        )


class CompletionVerifier:
    def __init__(self, contract: CompletionContract | None = None) -> None:
        self.contract = contract or CompletionContract()

    async def verify(
        self,
        state: WorkingState,
        *,
        todo_service: Any = None,
        session_id: str = "",
    ) -> CompletionReport:
        issues: list[str] = []
        for raw_path in self.contract.required_paths:
            target = _resolve_under_root(state.project_root, raw_path)
            if target is None or not target.exists():
                issues.append(f"required path is missing: {raw_path}")

        if (
            self.contract.require_verification_after_code_changes
            and state.code_was_modified
            and (
                state.last_verification_sequence < state.last_mutation_sequence
                or state.last_verification_ok is not True
            )
        ):
            issues.append("code changed after the last successful test or verification command")

        if self.contract.require_session_todos_complete and todo_service is not None and session_id:
            try:
                tasks = await todo_service.list(include_done=True)
            except Exception:  # noqa: BLE001 - optional TODO integration is fail-soft
                tasks = []
            incomplete = [
                task.id
                for task in tasks
                if session_id in getattr(task, "active_sessions", [])
                and getattr(task, "status", None) not in {"done", "cancelled"}
            ]
            if incomplete:
                issues.append("session TODOs are incomplete: " + ", ".join(sorted(incomplete)))
        if self.contract.require_service_health_check and state.last_service_health_ok is not True:
            issues.append(
                "service task has no successful local health check; probe its requested endpoint "
                "or process status before finishing"
            )
        return CompletionReport(not issues, tuple(issues))


@dataclass(frozen=True)
class StallDecision:
    stalled: bool
    repeated: int = 0
    instruction: str = ""


@dataclass
class StallController:
    repeat_threshold: int = 3
    _last_fingerprint: str = ""
    _repeat_count: int = 0
    _replans: int = 0
    _blocked_action: str = ""

    def should_block(self, action_signature: str) -> bool:
        return bool(self._blocked_action and action_signature == self._blocked_action)

    def observe(self, fingerprint: str, *, action_signature: str = "") -> StallDecision:
        if fingerprint == self._last_fingerprint:
            self._repeat_count += 1
        else:
            self._last_fingerprint = fingerprint
            self._repeat_count = 1
        if self._repeat_count < self.repeat_threshold:
            return StallDecision(False, self._repeat_count)
        self._replans += 1
        self._blocked_action = action_signature
        return StallDecision(
            True,
            self._repeat_count,
            "No progress detected from repeated identical action and observation. "
            "Do not repeat it; state a new hypothesis and choose a different action.",
        )


@dataclass(frozen=True)
class ScheduledCall:
    index: int
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ExecutionBatch:
    calls: tuple[ScheduledCall, ...]
    parallel: bool


class ToolScheduler:
    """Conservative scheduler: only proven read-only native tools fan out."""

    def plan(self, calls: Sequence[ScheduledCall]) -> tuple[ExecutionBatch, ...]:
        batches: list[ExecutionBatch] = []
        read_batch: list[ScheduledCall] = []
        for call in calls:
            if call.name in _READ_ONLY_NATIVE_TOOLS:
                read_batch.append(call)
                continue
            if read_batch:
                batches.append(ExecutionBatch(tuple(read_batch), parallel=len(read_batch) > 1))
                read_batch = []
            batches.append(ExecutionBatch((call,), parallel=False))
        if read_batch:
            batches.append(ExecutionBatch(tuple(read_batch), parallel=len(read_batch) > 1))
        return tuple(batches)


@dataclass(frozen=True)
class JournalEvent:
    sequence: int
    timestamp: float
    session_id: str
    kind: str
    action_id: str
    tool: str
    args: dict[str, Any]
    outcome: dict[str, Any]
    state: dict[str, Any]


class ActionJournal:
    """Append-only JSONL journal used to reconstruct loop state after a crash."""

    def __init__(self, path: Path, *, session_id: str) -> None:
        self.path = Path(path)
        self.session_id = session_id or "default"
        self._sequence = self._read_last_sequence()

    def append(
        self,
        *,
        kind: str,
        action_id: str,
        tool: str,
        args: dict[str, Any],
        outcome: dict[str, Any],
        state: WorkingState,
    ) -> JournalEvent:
        self._sequence += 1
        event = JournalEvent(
            sequence=self._sequence,
            timestamp=time.time(),
            session_id=self.session_id,
            kind=kind,
            action_id=action_id,
            tool=tool,
            args=_journal_safe_args(args),
            outcome=_json_safe(outcome),
            state=state.to_dict(),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(asdict(event), ensure_ascii=False, sort_keys=True) + "\n"
        fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        return event

    def recover_state(self, project_root: Path) -> WorkingState:
        latest: dict[str, Any] | None = None
        for event in self.events():
            if event.get("session_id") == self.session_id and isinstance(event.get("state"), dict):
                latest = event["state"]
        return WorkingState.from_dict(latest or {}, project_root)

    def incomplete_actions(self) -> tuple[str, ...]:
        started: set[str] = set()
        finished: set[str] = set()
        for event in self.events():
            if event.get("session_id") != self.session_id:
                continue
            action_id = str(event.get("action_id") or "")
            if event.get("kind") == "tool_started":
                started.add(action_id)
            elif event.get("kind") == "tool_finished":
                finished.add(action_id)
        return tuple(sorted(started - finished))

    def events(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        events: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    def _read_last_sequence(self) -> int:
        sequences = [int(event.get("sequence", 0)) for event in self.events()]
        return max(sequences, default=0)


@dataclass(frozen=True)
class LoopControlConfig:
    enabled: bool = True
    completion_verification: bool = True
    error_recovery: bool = True
    stall_detection: bool = True
    parallel_read_tools: bool = True
    action_journal: bool = True
    completion_contract: CompletionContract = field(default_factory=CompletionContract)
    recovery_policy: RecoveryPolicy = field(default_factory=RecoveryPolicy)
    stall_repeat_threshold: int = 3


def action_fingerprint(
    tool_name: str,
    args: dict[str, Any],
    *,
    is_error: bool,
    result_text: str,
) -> str:
    payload = {
        "tool": tool_name,
        "args": _json_safe(args),
        "is_error": is_error,
        "result_hash": hashlib.sha256(result_text.encode("utf-8", errors="replace")).hexdigest(),
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def action_signature(tool_name: str, args: dict[str, Any]) -> str:
    payload = {"tool": tool_name, "args": _json_safe(args)}
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _is_mutating_call(tool_name: str, args: dict[str, Any]) -> bool:
    short_name = tool_name.rsplit("__", 1)[-1]
    if tool_name in _MUTATING_TOOL_NAMES or short_name in _MUTATING_TOOL_NAMES:
        return True
    command = args.get("command")
    return tool_name == "run_command" and isinstance(command, str) and bool(_SHELL_MUTATION_RE.search(command))


def _is_verification_call(tool_name: str, args: dict[str, Any]) -> bool:
    command = args.get("command")
    return tool_name == "run_command" and isinstance(command, str) and bool(_TEST_COMMAND_RE.search(command))


def _is_service_health_call(tool_name: str, args: dict[str, Any]) -> bool:
    command = args.get("command")
    return (
        tool_name == "run_command"
        and isinstance(command, str)
        and bool(_SERVICE_HEALTH_COMMAND_RE.search(command))
    )


def _tool_path(args: dict[str, Any]) -> str:
    for key in ("path", "file_path", "target", "destination"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.replace("\\", "/")
    return ""


def _resolve_under_root(root: Path, raw_path: str) -> Path | None:
    target = Path(raw_path)
    if not target.is_absolute():
        target = root / target
    try:
        resolved = target.resolve(strict=False)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return resolved


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        if isinstance(value, dict):
            return {str(key): _json_safe(item) for key, item in value.items()}
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray)):
            return [_json_safe(item) for item in value]
        return repr(value)


def _journal_safe_args(args: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in args.items():
        if _SENSITIVE_ARG_KEY_RE.search(str(key)):
            safe[str(key)] = "<redacted>"
        elif key in {"content", "command", "patch", "stdin"} and isinstance(value, str):
            safe[str(key)] = {"sha256": _text_digest(value), "length": len(value)}
        elif isinstance(value, dict):
            safe[str(key)] = _journal_safe_args(value)
        elif isinstance(value, list):
            safe[str(key)] = [
                _journal_safe_args(item) if isinstance(item, dict) else _json_safe(item)
                for item in value
            ]
        else:
            safe[str(key)] = _json_safe(value)
    return safe


def _text_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
