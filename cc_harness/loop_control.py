"""Deterministic control plane for the model-driven agent loop.

The model still chooses actions.  This module owns the state and invariants
that should not depend on the model remembering them: completion checks,
failure classification, progress detection, conservative scheduling, and an
append-only action journal.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from .run_model import GoalContract


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
_EXPLICIT_ARTIFACT_RE = re.compile(
    r"(?<![\w.-])(/(?:app|tmp|workspace)/[A-Za-z0-9_./-]+)"
)
_ARTIFACT_DIRECTIVE_RE = re.compile(
    r"\b(?:create|write|save|store|produce|generate|output|place|put|"
    r"required|must\s+(?:exist|contain|be)|deliver(?:able|ed)|artifact|"
    r"创建|写入|保存|生成|输出|放到|存储|必须(?:存在|包含)|产物|交付)\b",
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
class ArtifactRequirement:
    """A concrete deliverable required by a task contract.

    ``path`` is always resolved under the run workspace.  ``kind`` and
    ``min_bytes`` let the runtime distinguish a real artifact from an empty
    placeholder, while the optional digest gives callers a deterministic
    integrity check when a task supplies one.
    """

    path: str
    kind: str = "file"
    min_bytes: int = 1
    sha256: str | None = None
    format: str | None = None

    def __post_init__(self) -> None:
        if not str(self.path).strip():
            raise ValueError("artifact requirement path is required")
        if self.kind not in {"file", "directory", "any"}:
            raise ValueError("artifact requirement kind must be file, directory, or any")
        if self.min_bytes < 0:
            raise ValueError("artifact requirement min_bytes must be non-negative")
        if self.sha256 is not None:
            value = str(self.sha256)
            if not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", value):
                raise ValueError("artifact requirement sha256 must be a 64-character hex digest")
        if self.format is not None and self.format not in {"json", "text", "binary"}:
            raise ValueError("artifact requirement format must be json, text, or binary")

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "min_bytes": self.min_bytes,
            "sha256": self.sha256,
            "format": self.format,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArtifactRequirement":
        if not isinstance(value, Mapping):
            raise ValueError("artifact requirement must be an object")
        return cls(
            path=str(value.get("path") or ""),
            kind=str(value.get("kind") or "file"),
            min_bytes=max(0, int(value.get("min_bytes", 1))),
            sha256=(str(value["sha256"]) if value.get("sha256") else None),
            format=(str(value["format"]) if value.get("format") else None),
        )


@dataclass(frozen=True)
class CompletionContract:
    required_paths: tuple[str, ...] = ()
    required_artifacts: tuple[ArtifactRequirement, ...] = ()
    verification_commands: tuple[str, ...] = ()
    service_health_commands: tuple[str, ...] = ()
    require_verification_after_code_changes: bool = True
    require_session_todos_complete: bool = True
    require_service_health_check: bool = False
    max_rechecks: int = 2
    environment_retry_limit: int = 10
    deadline_seconds: float | None = None
    # Empty for ordinary user sessions.  Trusted task adapters may explicitly
    # allow task-image roots such as /tmp for deliverables that live outside
    # the mounted workspace.
    allowed_external_roots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.max_rechecks < 0:
            raise ValueError("max_rechecks must be non-negative")
        if self.environment_retry_limit < 0:
            raise ValueError("environment_retry_limit must be non-negative")
        if self.deadline_seconds is not None and (
            not math.isfinite(float(self.deadline_seconds)) or self.deadline_seconds <= 0
        ):
            raise ValueError("deadline_seconds must be a positive finite number")
        for raw_root in self.allowed_external_roots:
            normalized = str(raw_root).replace("\\", "/").rstrip("/")
            if not normalized.startswith("/") or normalized in {"", "/", "/app"}:
                raise ValueError("allowed_external_roots must contain safe absolute POSIX roots")
        paths = set(self.required_paths)
        artifact_paths = {item.path for item in self.required_artifacts}
        if not artifact_paths.issubset(paths):
            raise ValueError("required_artifacts must be included in required_paths")


@dataclass(frozen=True)
class TaskContract:
    """Structured task contract shared by planning, execution and completion.

    GoalContract remains the durable source of truth.  This value object is a
    runtime view that adds executable obligations (artifacts, verification and
    service probes) without relying on the model to restate them in prose.
    """

    objective: str
    acceptance_criteria: tuple[str, ...]
    constraints: tuple[str, ...] = ()
    allowed_scope: tuple[str, ...] = ()
    excluded_scope: tuple[str, ...] = ()
    completion: CompletionContract = field(default_factory=CompletionContract)

    @classmethod
    def from_goal(
        cls,
        goal: GoalContract,
        *,
        completion: CompletionContract | None = None,
    ) -> "TaskContract":
        if not isinstance(goal, GoalContract):
            raise TypeError("task contract requires a GoalContract")
        selected = completion or CompletionContract()
        return cls(
            objective=goal.objective,
            acceptance_criteria=tuple(goal.acceptance_criteria),
            constraints=tuple(goal.constraints),
            allowed_scope=tuple(goal.allowed_scope),
            excluded_scope=tuple(goal.excluded_scope),
            completion=selected,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "acceptance_criteria": list(self.acceptance_criteria),
            "constraints": list(self.constraints),
            "allowed_scope": list(self.allowed_scope),
            "excluded_scope": list(self.excluded_scope),
            "completion": {
                "required_paths": list(self.completion.required_paths),
                "required_artifacts": [
                    item.to_dict() for item in self.completion.required_artifacts
                ],
                "verification_commands": list(self.completion.verification_commands),
                "service_health_commands": list(self.completion.service_health_commands),
                "require_verification_after_code_changes": (
                    self.completion.require_verification_after_code_changes
                ),
                "require_session_todos_complete": self.completion.require_session_todos_complete,
                "require_service_health_check": self.completion.require_service_health_check,
                "max_rechecks": self.completion.max_rechecks,
                "environment_retry_limit": self.completion.environment_retry_limit,
                "deadline_seconds": self.completion.deadline_seconds,
                "allowed_external_roots": list(self.completion.allowed_external_roots),
            },
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TaskContract":
        if not isinstance(value, Mapping):
            raise ValueError("task contract must be an object")
        raw_completion = value.get("completion")
        completion_data = raw_completion if isinstance(raw_completion, Mapping) else {}
        required_paths = tuple(str(item) for item in completion_data.get("required_paths") or ())
        artifacts = tuple(
            ArtifactRequirement.from_dict(item)
            for item in completion_data.get("required_artifacts") or ()
        )
        completion = CompletionContract(
            required_paths=required_paths,
            required_artifacts=artifacts,
            verification_commands=tuple(
                str(item) for item in completion_data.get("verification_commands") or ()
            ),
            service_health_commands=tuple(
                str(item) for item in completion_data.get("service_health_commands") or ()
            ),
            require_verification_after_code_changes=bool(
                completion_data.get("require_verification_after_code_changes", True)
            ),
            require_session_todos_complete=bool(
                completion_data.get("require_session_todos_complete", True)
            ),
            require_service_health_check=bool(
                completion_data.get("require_service_health_check", False)
            ),
            max_rechecks=max(0, int(completion_data.get("max_rechecks", 2))),
            environment_retry_limit=max(0, int(completion_data.get("environment_retry_limit", 10))),
            deadline_seconds=(
                float(completion_data["deadline_seconds"])
                if completion_data.get("deadline_seconds") is not None
                else None
            ),
            allowed_external_roots=tuple(
                str(item).replace("\\", "/").rstrip("/")
                for item in completion_data.get("allowed_external_roots") or ()
            ),
        )
        return cls(
            objective=str(value.get("objective") or ""),
            acceptance_criteria=tuple(str(item) for item in value.get("acceptance_criteria") or ()),
            constraints=tuple(str(item) for item in value.get("constraints") or ()),
            allowed_scope=tuple(str(item) for item in value.get("allowed_scope") or ()),
            excluded_scope=tuple(str(item) for item in value.get("excluded_scope") or ()),
            completion=completion,
        )


def artifact_validation_issues(
    project_root: Path,
    requirements: Iterable[ArtifactRequirement],
    *,
    allowed_external_roots: Iterable[str] = (),
) -> tuple[str, ...]:
    """Validate required deliverables without consulting model output.

    The function is intentionally synchronous and side-effect free so the
    worker, interactive loop, and post-run auditors share exactly the same
    artifact contract.  ``/app`` is the canonical task-image root and maps to
    the durable project root; other absolute paths are rejected as out of
    scope unless an explicit trusted root was supplied.
    """

    root = Path(project_root).resolve()
    external_roots = tuple(allowed_external_roots)
    issues: list[str] = []
    for requirement in requirements:
        target = _resolve_under_root(
            root,
            requirement.path,
            allowed_external_roots=external_roots,
        )
        if target is None:
            issues.append(f"required artifact is outside workspace: {requirement.path}")
            continue
        if not target.exists():
            issues.append(f"required artifact is missing: {requirement.path}")
            continue
        if requirement.kind == "file" and not target.is_file():
            issues.append(f"required artifact is not a file: {requirement.path}")
            continue
        if requirement.kind == "directory" and not target.is_dir():
            issues.append(f"required artifact is not a directory: {requirement.path}")
            continue
        if target.is_file():
            try:
                if target.stat().st_size < requirement.min_bytes:
                    issues.append(
                        f"required artifact is empty or too small: {requirement.path}"
                    )
                if requirement.sha256:
                    digest = hashlib.sha256()
                    with target.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                    actual = "sha256:" + digest.hexdigest()
                    expected = requirement.sha256
                    if not expected.startswith("sha256:"):
                        expected = "sha256:" + expected
                    if actual != expected:
                        issues.append(f"required artifact digest mismatch: {requirement.path}")
                if requirement.format == "json":
                    if target.stat().st_size > 4 * 1024 * 1024:
                        issues.append(f"required JSON artifact is too large to validate: {requirement.path}")
                    else:
                        try:
                            json.loads(target.read_text(encoding="utf-8"))
                        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                            issues.append(f"required artifact is not valid JSON: {requirement.path}")
            except OSError:
                issues.append(f"required artifact cannot be read: {requirement.path}")
    return tuple(issues)


def _normalise_command(value: str) -> str:
    """Normalize a command before hashing it into durable loop state."""

    text = str(value or "").strip().strip("`")
    text = re.sub(r"^[-*>`\s]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _instruction_commands(instruction: str, pattern: re.Pattern[str]) -> tuple[str, ...]:
    """Extract executable snippets, preferring commands in backticks."""

    found: list[str] = []
    candidates = re.findall(r"`([^`\n]+)`", instruction) + instruction.splitlines()
    command_start = re.compile(
        r"\b(?:python(?:3)?\s+-m\s+pytest|pytest|npm\s+(?:run\s+)?test|"
        r"pnpm\s+(?:run\s+)?test|yarn\s+test|cargo\s+test|go\s+test|"
        r"dotnet\s+test|mvn(?:w)?\s+test|gradle(?:w?)\s+test|"
        r"curl|wget|grpcurl|(?:nc|netcat)\s+-z|systemctl\s+is-active|"
        r"service\s+\S+\s+status)\b",
        re.IGNORECASE,
    )
    service_start = re.compile(
        r"\b(?:curl|wget|grpcurl|(?:nc|netcat)\s+-z|systemctl\s+is-active|"
        r"service\s+\S+\s+status)\b",
        re.IGNORECASE,
    )
    for raw in candidates:
        line = _normalise_command(raw)
        start = (service_start if pattern is _SERVICE_HEALTH_COMMAND_RE else command_start).search(line)
        command = line[start.start():] if start else line
        # Prose often lists a command followed by another instruction on the
        # same line.  Keep the executable prefix only; fenced snippets remain
        # untouched except for terminal punctuation.
        for separator in (
            " and then ", " then ", " and probe ", " and verify ",
            " and check ", "；", "。",
        ):
            if separator in command.casefold():
                index = command.casefold().index(separator)
                command = command[:index]
                break
        command = command.strip("`'\".,;:)]}")
        if not command or not pattern.search(command):
            continue
        if command not in found:
            found.append(command)
    return tuple(found)


def completion_contract_from_instruction(
    instruction: str,
    *,
    trusted_public_instruction: bool = False,
) -> CompletionContract:
    """Derive only explicit, user-visible completion obligations.

    The extractor deliberately ignores arbitrary paths mentioned as inputs. A
    path becomes required only when its line also contains a create/output
    directive. This keeps the contract useful for benchmark and normal coding
    tasks without consulting hidden tests or verifier files.
    """

    required: list[str] = []
    artifact_formats: dict[str, str | None] = {}
    artifact_digests: dict[str, str | None] = {}
    for line in instruction.splitlines():
        if not _ARTIFACT_DIRECTIVE_RE.search(line):
            continue
        for match in _EXPLICIT_ARTIFACT_RE.finditer(line):
            path = match.group(1).rstrip(".,:;)]}'\"")
            if path not in required:
                required.append(path)
            lowered = line.casefold()
            artifact_formats.setdefault(
                path,
                "json" if path.casefold().endswith(".json") and "json" in lowered else None,
            )
            digest_match = re.search(r"\bsha256:[0-9a-f]{64}\b", line, re.IGNORECASE)
            if digest_match:
                artifact_digests[path] = digest_match.group(0).lower()
    verification_commands = _instruction_commands(instruction, _TEST_COMMAND_RE)
    service_health_commands = _instruction_commands(instruction, _SERVICE_HEALTH_COMMAND_RE)
    # Only the official adapter may authorize paths outside the mounted
    # workspace.  Interactive user text remains workspace-only by default.
    allowed_external_roots = ("/tmp", "/workspace") if trusted_public_instruction else ()
    return CompletionContract(
        required_paths=tuple(required),
        required_artifacts=tuple(
            ArtifactRequirement(
                path=path,
                sha256=artifact_digests.get(path),
                format=artifact_formats.get(path),
            )
            for path in required
        ),
        verification_commands=verification_commands,
        service_health_commands=service_health_commands,
        require_service_health_check=bool(_SERVICE_REQUEST_RE.search(instruction)),
        allowed_external_roots=allowed_external_roots,
    )


@dataclass
class WorkingState:
    project_root: Path
    logical_cwd: Path
    sequence: int = 0
    modified_paths: set[str] = field(default_factory=set)
    read_paths: set[str] = field(default_factory=set)
    last_mutation_sequence: int = 0
    # A final sentence is not evidence.  Keep durable counters for the last
    # successful observation so the interactive verifier can reject a
    # model-only completion even when the task did not spell out a file or
    # test command.
    last_successful_sequence: int = 0
    last_decisive_sequence: int = 0
    last_verification_sequence: int = 0
    last_verification_ok: bool | None = None
    last_service_health_sequence: int = 0
    last_service_health_ok: bool | None = None
    last_tool_name: str = ""
    last_error_kind: str | None = None
    unresolved_errors: list[dict[str, Any]] = field(default_factory=list)
    result_fingerprints: list[str] = field(default_factory=list)
    # Histories are bounded and digest-only so completion survives a restart
    # without persisting command contents or other sensitive arguments.
    verification_history: list[dict[str, Any]] = field(default_factory=list)
    service_health_history: list[dict[str, Any]] = field(default_factory=list)

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
        result_metadata: Mapping[str, Any] | None = None,
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

        if not is_error:
            self.last_successful_sequence = self.sequence
            if (
                _is_mutating_call(tool_name, args)
                or _is_verification_call(tool_name, args)
                or _is_service_health_call(tool_name, args)
            ):
                self.last_decisive_sequence = self.sequence

        metadata = dict(result_metadata or {})
        if _is_verification_call(tool_name, args):
            self.last_verification_sequence = self.sequence
            self.last_verification_ok = not is_error
            self.verification_history.append(
                {
                    "sequence": self.sequence,
                    "tool": tool_name,
                    "command_digest": _text_digest(
                        _normalise_command(str(args.get("command") or ""))
                    ),
                    "ok": not is_error,
                    "result_hash": _text_digest(result_text),
                }
            )
            self.verification_history = self.verification_history[-64:]
        if _is_service_health_call(tool_name, args):
            self.last_service_health_sequence = self.sequence
            readiness = metadata.get("readiness")
            health_status = str(metadata.get("health_status") or "")
            if isinstance(readiness, Mapping) and readiness.get("requested"):
                health_ok = readiness.get("status") == "ready"
            elif health_status:
                health_ok = health_status == "healthy"
            else:
                health_ok = not is_error
            self.last_service_health_ok = bool(health_ok)
            self.service_health_history.append(
                {
                    "sequence": self.sequence,
                    "tool": tool_name,
                    "command_digest": _text_digest(
                        _normalise_command(str(args.get("command") or ""))
                    ),
                    "ok": bool(health_ok),
                    "result_hash": _text_digest(result_text),
                    "readiness_status": (
                        readiness.get("status") if isinstance(readiness, Mapping) else None
                    ),
                    "health_status": health_status or None,
                }
            )
            self.service_health_history = self.service_health_history[-64:]

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
            "last_successful_sequence": self.last_successful_sequence,
            "last_decisive_sequence": self.last_decisive_sequence,
            "last_verification_sequence": self.last_verification_sequence,
            "last_verification_ok": self.last_verification_ok,
            "last_service_health_sequence": self.last_service_health_sequence,
            "last_service_health_ok": self.last_service_health_ok,
            "last_tool_name": self.last_tool_name,
            "last_error_kind": self.last_error_kind,
            "unresolved_errors": list(self.unresolved_errors),
            "result_fingerprints": list(self.result_fingerprints),
            "verification_history": list(self.verification_history),
            "service_health_history": list(self.service_health_history),
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
            last_successful_sequence=int(data.get("last_successful_sequence", 0)),
            last_decisive_sequence=int(data.get("last_decisive_sequence", 0)),
            last_verification_sequence=int(data.get("last_verification_sequence", 0)),
            last_verification_ok=data.get("last_verification_ok"),
            last_service_health_sequence=int(data.get("last_service_health_sequence", 0)),
            last_service_health_ok=data.get("last_service_health_ok"),
            last_tool_name=str(data.get("last_tool_name") or ""),
            last_error_kind=data.get("last_error_kind"),
            unresolved_errors=list(data.get("unresolved_errors") or []),
            result_fingerprints=list(data.get("result_fingerprints") or [])[-20:],
            verification_history=list(data.get("verification_history") or [])[-64:],
            service_health_history=list(data.get("service_health_history") or [])[-64:],
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
        if state.last_successful_sequence <= 0:
            issues.append(
                "completion requires at least one successful tool observation; "
                "a model final message alone is not evidence"
            )
        artifact_paths = {item.path for item in self.contract.required_artifacts}
        for raw_path in self.contract.required_paths:
            if raw_path in artifact_paths:
                continue
            target = _resolve_under_root(
                state.project_root,
                raw_path,
                allowed_external_roots=self.contract.allowed_external_roots,
            )
            if target is None or not target.exists():
                issues.append(f"required path is missing: {raw_path}")
        issues.extend(
            artifact_validation_issues(
                state.project_root,
                self.contract.required_artifacts,
                allowed_external_roots=self.contract.allowed_external_roots,
            )
        )

        if self.contract.verification_commands:
            verified_digests = {
                str(item.get("command_digest"))
                for item in state.verification_history
                if bool(item.get("ok"))
            }
            for command in self.contract.verification_commands:
                digest = _text_digest(_normalise_command(command))
                if digest not in verified_digests:
                    issues.append(
                        "required verification command was not successfully executed: "
                        + command
                    )

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
        if self.contract.service_health_commands:
            healthy_digests = {
                str(item.get("command_digest"))
                for item in state.service_health_history
                if bool(item.get("ok"))
            }
            for command in self.contract.service_health_commands:
                digest = _text_digest(_normalise_command(command))
                if digest not in healthy_digests and not any(
                    bool(item.get("ok")) and item.get("tool") == "service_status"
                    for item in state.service_health_history
                ):
                    issues.append(
                        "required service health command was not successful: " + command
                    )
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
    replan_required: bool = False
    replan_id: str | None = None
    blocked_action: str = ""


@dataclass
class StallController:
    repeat_threshold: int = 3
    _last_fingerprint: str = ""
    _repeat_count: int = 0
    _replans: int = 0
    _blocked_action: str = ""

    def should_block(self, action_signature: str) -> bool:
        if not self._blocked_action:
            return False
        if action_signature == self._blocked_action:
            return True
        # A genuinely different action acknowledges the replan directive and
        # opens a fresh trajectory.  Without this reset the old controller
        # permanently blocked future calls after the first stall.
        self.reset_after_replan()
        return False

    @property
    def replan_count(self) -> int:
        return self._replans

    @property
    def blocked_action(self) -> str:
        return self._blocked_action

    def reset_after_replan(self) -> None:
        """Clear the blocked signature while retaining the audit counter."""

        self._blocked_action = ""
        self._repeat_count = 0
        self._last_fingerprint = ""

    def observe(self, fingerprint: str, *, action_signature: str = "") -> StallDecision:
        if self._blocked_action and action_signature != self._blocked_action:
            self.reset_after_replan()
        if fingerprint == self._last_fingerprint:
            self._repeat_count += 1
        else:
            self._last_fingerprint = fingerprint
            self._repeat_count = 1
        if self._repeat_count < self.repeat_threshold:
            return StallDecision(False, self._repeat_count)
        # Emit one durable replan directive per blocked signature.  Repeated
        # calls are then rejected by ``should_block`` until the model chooses
        # a different action, preventing a hot loop and making the recovery
        # boundary visible to the caller.
        if self._blocked_action == action_signature and action_signature:
            return StallDecision(
                True,
                self._repeat_count,
                "No progress detected from repeated identical action and observation. "
                "Do not repeat it; state a new hypothesis and choose a different action.",
                replan_required=False,
                replan_id=f"replan-{self._replans}",
                blocked_action=action_signature,
            )
        self._replans += 1
        self._blocked_action = action_signature
        return StallDecision(
            True,
            self._repeat_count,
            "No progress detected from repeated identical action and observation. "
            "Do not repeat it; state a new hypothesis and choose a different action.",
            replan_required=True,
            replan_id=f"replan-{self._replans}",
            blocked_action=action_signature,
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
    if tool_name == "service_status":
        return True
    command = args.get("command")
    return (
        tool_name == "run_command"
        and isinstance(command, str)
        and bool(
            _SERVICE_HEALTH_COMMAND_RE.search(command)
            or str(args.get("readiness_command") or "").strip()
            or str(args.get("health_command") or "").strip()
        )
    )


def _tool_path(args: dict[str, Any]) -> str:
    for key in ("path", "file_path", "target", "destination"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.replace("\\", "/")
    return ""


def _resolve_under_root(
    root: Path,
    raw_path: str,
    *,
    allowed_external_roots: Iterable[str] = (),
) -> Path | None:
    target = Path(raw_path)
    # Harbor and other task runners expose the workspace as ``/app`` even
    # when the durable worker is running from a host checkout.  Keep that
    # mapping explicit; arbitrary absolute paths remain outside the contract.
    normalized = str(raw_path).replace("\\", "/")
    if normalized == "/app" or normalized.startswith("/app/"):
        target = root / normalized.removeprefix("/app/").removeprefix("/app")
    elif target.is_absolute():
        allowed = tuple(
            str(item).replace("\\", "/").rstrip("/")
            for item in allowed_external_roots
            if str(item).strip()
        )
        matched_root = next(
            (
                candidate
                for candidate in allowed
                if normalized == candidate or normalized.startswith(candidate + "/")
            ),
            None,
        )
        if matched_root is None:
            return None
        target = Path(normalized)
        try:
            resolved_external = target.resolve(strict=False)
            resolved_external.relative_to(Path(matched_root).resolve(strict=False))
        except (OSError, ValueError):
            return None
        return resolved_external
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
