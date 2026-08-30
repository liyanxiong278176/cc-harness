"""Pure domain model for the durable agent runtime.

This module deliberately has no database, filesystem, model-provider, or
terminal dependencies.  It is the small vocabulary shared by the event
codec, projection builder, store, supervisor, and client seams.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, ClassVar, Mapping


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_json(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()}"


def _as_tuple(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    return tuple(value)


class DomainValidationError(ValueError):
    """Raised when a domain value violates a runtime invariant."""


class InvalidRunTransition(DomainValidationError):
    """Raised when an event attempts an illegal lifecycle transition."""


class CompletionEvidenceError(DomainValidationError):
    """Raised when a completion candidate lacks sufficient evidence."""


class RunStatus(str, Enum):
    DRAFT = "draft"
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    WAITING_ON_PREDECESSOR = "waiting_on_predecessor"
    STALLED = "stalled"
    BLOCKED = "blocked"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    FAILED_RECOVERABLE = "failed_recoverable"
    FAILED_TERMINAL = "failed_terminal"
    COMPLETED = "completed"


class ActionStatus(str, Enum):
    PLANNED = "planned"
    PREPARED = "prepared"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ChildRunStatus(str, Enum):
    PLANNED = "planned"
    QUEUED = "queued"
    RUNNING = "running"
    CANDIDATE_SUBMITTED = "candidate_submitted"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    BLOCKED = "blocked"


class ApprovalStatus(str, Enum):
    REQUESTED = "requested"
    GRANTED = "granted"
    REJECTED = "rejected"
    EXPIRED = "expired"


class PredecessorGateStatus(str, Enum):
    READY = "ready"
    WAITING = "waiting"
    BYPASSED = "bypassed"
    INCOMPLETE = "incomplete"


class EvidenceKind(str, Enum):
    ACCEPTANCE = "acceptance"
    TEST = "test"
    BUILD = "build"
    REVIEW = "review"
    ARTIFACT = "artifact"
    ACTION_RESULT = "action_result"
    CHILD_CANDIDATE = "child_candidate"
    RECONCILIATION = "reconciliation"


class ProgressKind(str, Enum):
    ACCEPTANCE_MET = "acceptance_met"
    ERROR_REDUCED = "error_reduced"
    EFFECTIVE_CHANGE = "effective_change"
    VERIFICATION_PASSED = "verification_passed"
    CHILD_ACCEPTED = "child_accepted"
    BLOCKER_REMOVED = "blocker_removed"
    EVIDENCE_ACQUIRED = "evidence_acquired"
    FAILED_PATH_ELIMINATED = "failed_path_eliminated"


class EffectClass(str, Enum):
    READ_ONLY = "read_only"
    WORKSPACE_MUTATION = "workspace_mutation"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class EvidenceRef:
    """A verifiable reference; the content itself may live in object storage."""

    evidence_id: str
    kind: EvidenceKind | str
    digest: str
    source: str
    recorded_at: float
    project_scope: str = ""
    confidence: float = 1.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.evidence_id or not self.digest or not self.source:
            raise DomainValidationError("evidence_id, digest, and source are required")
        if not 0.0 <= self.confidence <= 1.0:
            raise DomainValidationError("evidence confidence must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind.value if isinstance(self.kind, Enum) else self.kind,
            "digest": self.digest,
            "source": self.source,
            "recorded_at": self.recorded_at,
            "project_scope": self.project_scope,
            "confidence": self.confidence,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvidenceRef":
        return cls(
            evidence_id=str(data["evidence_id"]),
            kind=str(data["kind"]),
            digest=str(data["digest"]),
            source=str(data["source"]),
            recorded_at=float(data["recorded_at"]),
            project_scope=str(data.get("project_scope") or ""),
            confidence=float(data.get("confidence", 1.0)),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True)
class GoalContract:
    objective: str
    acceptance_criteria: tuple[str, ...]
    constraints: tuple[str, ...] = ()
    allowed_scope: tuple[str, ...] = ()
    excluded_scope: tuple[str, ...] = ()
    required_evidence: tuple[str, ...] = ()
    human_review: tuple[str, ...] = ()
    contract_version: int = 1

    def __post_init__(self) -> None:
        if not self.objective.strip():
            raise DomainValidationError("goal objective cannot be empty")
        if not self.acceptance_criteria or any(not item.strip() for item in self.acceptance_criteria):
            raise DomainValidationError("goal must contain non-empty acceptance criteria")
        if self.contract_version < 1:
            raise DomainValidationError("goal contract version must be positive")

    @classmethod
    def create(
        cls,
        objective: str,
        acceptance_criteria: list[str] | tuple[str, ...],
        **kwargs: Any,
    ) -> "GoalContract":
        return cls(objective=objective, acceptance_criteria=tuple(acceptance_criteria), **kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "acceptance_criteria": list(self.acceptance_criteria),
            "constraints": list(self.constraints),
            "allowed_scope": list(self.allowed_scope),
            "excluded_scope": list(self.excluded_scope),
            "required_evidence": list(self.required_evidence),
            "human_review": list(self.human_review),
            "contract_version": self.contract_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GoalContract":
        return cls(
            objective=str(data["objective"]),
            acceptance_criteria=tuple(str(item) for item in data["acceptance_criteria"]),
            constraints=tuple(str(item) for item in data.get("constraints") or ()),
            allowed_scope=tuple(str(item) for item in data.get("allowed_scope") or ()),
            excluded_scope=tuple(str(item) for item in data.get("excluded_scope") or ()),
            required_evidence=tuple(str(item) for item in data.get("required_evidence") or ()),
            human_review=tuple(str(item) for item in data.get("human_review") or ()),
            contract_version=int(data.get("contract_version", 1)),
        )

    @property
    def digest(self) -> str:
        return digest_json(self.to_dict())


@dataclass(frozen=True)
class PlanNode:
    node_id: str
    kind: str
    depth: int = 0
    depends_on: tuple[str, ...] = ()
    owned_paths: tuple[str, ...] = ()
    child_run_id: str | None = None
    worktree_id: str | None = None
    # Child-task contract metadata is persisted with the DAG so recovery and
    # completion gates do not depend on the latest model message.
    required: bool = True
    effect_class: str = EffectClass.READ_ONLY.value
    acceptance_criteria: tuple[str, ...] = ()
    worktree_base_commit: str | None = None
    budget: Mapping[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 240
    max_retries: int = 1
    output_schema: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.node_id or not self.kind:
            raise DomainValidationError("plan node id and kind are required")
        if self.depth < 0:
            raise DomainValidationError("plan node depth cannot be negative")
        if self.timeout_seconds < 1 or self.timeout_seconds > 3600:
            raise DomainValidationError("plan node timeout_seconds must be in [1, 3600]")
        if self.max_retries < 0 or self.max_retries > 2:
            raise DomainValidationError("plan node max_retries must be in [0, 2]")

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "depth": self.depth,
            "depends_on": list(self.depends_on),
            "owned_paths": list(self.owned_paths),
            "child_run_id": self.child_run_id,
            "worktree_id": self.worktree_id,
            "required": self.required,
            "effect_class": self.effect_class,
            "acceptance_criteria": list(self.acceptance_criteria),
            "worktree_base_commit": self.worktree_base_commit,
            "budget": dict(self.budget),
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "output_schema": dict(self.output_schema),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PlanNode":
        return cls(
            node_id=str(data["node_id"]),
            kind=str(data["kind"]),
            depth=int(data.get("depth", 0)),
            depends_on=tuple(str(item) for item in data.get("depends_on") or ()),
            owned_paths=tuple(str(item) for item in data.get("owned_paths") or ()),
            child_run_id=data.get("child_run_id"),
            worktree_id=(str(data["worktree_id"]) if data.get("worktree_id") else None),
            required=bool(data.get("required", True)),
            effect_class=str(data.get("effect_class", EffectClass.READ_ONLY.value)),
            acceptance_criteria=tuple(str(item) for item in data.get("acceptance_criteria") or ()),
            worktree_base_commit=(
                str(data["worktree_base_commit"])
                if data.get("worktree_base_commit")
                else None
            ),
            budget=dict(data.get("budget") or {}),
            timeout_seconds=int(data.get("timeout_seconds", 240)),
            max_retries=int(data.get("max_retries", 1)),
            output_schema=dict(data.get("output_schema") or {}),
        )


def _normalise_path(path: str) -> str:
    value = path.replace("\\", "/").strip().rstrip("/")
    return value or "."


def _paths_overlap(left: str, right: str) -> bool:
    a = _normalise_path(left)
    b = _normalise_path(right)
    return a == b or a.startswith(f"{b}/") or b.startswith(f"{a}/")


@dataclass(frozen=True)
class PlanGraph:
    nodes: tuple[PlanNode, ...] = ()
    revision: int = 0
    max_concurrent_children: int = 3
    max_child_depth: int = 2

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise DomainValidationError("plan revision cannot be negative")
        if self.max_concurrent_children < 1 or self.max_child_depth < 0:
            raise DomainValidationError("plan concurrency and depth limits must be valid")
        self.validate()

    @property
    def by_id(self) -> dict[str, PlanNode]:
        return {node.node_id: node for node in self.nodes}

    @property
    def child_nodes(self) -> tuple[PlanNode, ...]:
        return tuple(node for node in self.nodes if node.kind == "child")

    def validate(self) -> None:
        nodes = self.by_id
        if len(nodes) != len(self.nodes):
            raise DomainValidationError("plan node ids must be unique")
        for node in self.nodes:
            if node.depth > self.max_child_depth and node.kind == "child":
                raise DomainValidationError(
                    f"child node {node.node_id} exceeds depth {self.max_child_depth}"
                )
            missing = [dependency for dependency in node.depends_on if dependency not in nodes]
            if missing:
                raise DomainValidationError(
                    f"plan node {node.node_id} depends on missing nodes: {missing}"
                )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise DomainValidationError("plan graph cannot contain dependency cycles")
            if node_id in visited:
                return
            visiting.add(node_id)
            for dependency in nodes[node_id].depends_on:
                visit(dependency)
            visiting.remove(node_id)
            visited.add(node_id)

        for node in self.nodes:
            visit(node.node_id)

    def add_node(self, node: PlanNode) -> "PlanGraph":
        if node.node_id in self.by_id:
            raise DomainValidationError(f"plan node already exists: {node.node_id}")
        return replace(self, nodes=(*self.nodes, node), revision=self.revision + 1)

    def can_run_parallel(self, node_ids: list[str] | tuple[str, ...]) -> bool:
        # A plan batch is a set of distinct nodes.  Treating a repeated id as
        # parallel work would bypass dependency/ownership checks and can lead
        # to duplicate child scheduling.
        if len(set(node_ids)) != len(node_ids):
            return False
        selected = [self.by_id[node_id] for node_id in node_ids]
        children = [node for node in selected if node.kind == "child"]
        if len(children) > self.max_concurrent_children:
            return False
        if len(children) > 1:
            # Read-only children can safely share the project snapshot. Any
            # mutating child must have a distinct isolated worktree.
            mutating = [node for node in children if node.effect_class != EffectClass.READ_ONLY.value]
            if mutating:
                worktrees = [node.worktree_id for node in mutating]
                if any(not worktree for worktree in worktrees):
                    return False
                if len(set(worktrees)) != len(worktrees):
                    return False
        for index, left in enumerate(selected):
            for right in selected[index + 1 :]:
                if any(
                    _paths_overlap(left_path, right_path)
                    for left_path in left.owned_paths
                    for right_path in right.owned_paths
                ):
                    return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [node.to_dict() for node in self.nodes],
            "revision": self.revision,
            "max_concurrent_children": self.max_concurrent_children,
            "max_child_depth": self.max_child_depth,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PlanGraph":
        return cls(
            nodes=tuple(PlanNode.from_dict(node) for node in data.get("nodes") or ()),
            revision=int(data.get("revision", 0)),
            max_concurrent_children=int(data.get("max_concurrent_children", 3)),
            max_child_depth=int(data.get("max_child_depth", 2)),
        )

    @property
    def digest(self) -> str:
        return digest_json(self.to_dict())


@dataclass(frozen=True)
class CandidateChangeSet:
    child_run_id: str
    base_commit: str
    candidate_commit: str
    diff_digest: str
    modified_paths: tuple[str, ...]
    verification_evidence: tuple[EvidenceRef, ...] = ()
    submitted_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.child_run_id or not self.base_commit or not self.candidate_commit:
            raise DomainValidationError("candidate change set needs child, base, and candidate commits")
        if not self.diff_digest:
            raise DomainValidationError("candidate change set needs a diff digest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "child_run_id": self.child_run_id,
            "base_commit": self.base_commit,
            "candidate_commit": self.candidate_commit,
            "diff_digest": self.diff_digest,
            "modified_paths": list(self.modified_paths),
            "verification_evidence": [item.to_dict() for item in self.verification_evidence],
            "submitted_at": self.submitted_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CandidateChangeSet":
        return cls(
            child_run_id=str(data["child_run_id"]),
            base_commit=str(data["base_commit"]),
            candidate_commit=str(data["candidate_commit"]),
            diff_digest=str(data["diff_digest"]),
            modified_paths=tuple(str(item) for item in data.get("modified_paths") or ()),
            verification_evidence=tuple(
                EvidenceRef.from_dict(item) for item in data.get("verification_evidence") or ()
            ),
            submitted_at=float(data.get("submitted_at", time.time())),
        )


@dataclass(frozen=True)
class RuntimeContract:
    runtime_version: str
    event_schema_version: int
    tool_contract_digest: str
    model_config_digest: str
    policy_digest: str
    capability_profile_digest: str

    def __post_init__(self) -> None:
        if not self.runtime_version:
            raise DomainValidationError("runtime version is required")
        if self.event_schema_version < 1:
            raise DomainValidationError("event schema version must be positive")
        for name in (
            "tool_contract_digest",
            "model_config_digest",
            "policy_digest",
            "capability_profile_digest",
        ):
            if not getattr(self, name):
                raise DomainValidationError(f"{name} is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_version": self.runtime_version,
            "event_schema_version": self.event_schema_version,
            "tool_contract_digest": self.tool_contract_digest,
            "model_config_digest": self.model_config_digest,
            "policy_digest": self.policy_digest,
            "capability_profile_digest": self.capability_profile_digest,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RuntimeContract":
        return cls(
            runtime_version=str(data["runtime_version"]),
            event_schema_version=int(data["event_schema_version"]),
            tool_contract_digest=str(data["tool_contract_digest"]),
            model_config_digest=str(data["model_config_digest"]),
            policy_digest=str(data["policy_digest"]),
            capability_profile_digest=str(data["capability_profile_digest"]),
        )

    @property
    def digest(self) -> str:
        return digest_json(self.to_dict())


@dataclass(frozen=True)
class RunProgress:
    kind: ProgressKind | str
    description: str
    evidence: tuple[EvidenceRef, ...] = ()
    segment: int = 0
    recorded_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.description.strip():
            raise DomainValidationError("progress description cannot be empty")
        if self.segment < 0:
            raise DomainValidationError("progress segment cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value if isinstance(self.kind, Enum) else self.kind,
            "description": self.description,
            "evidence": [item.to_dict() for item in self.evidence],
            "segment": self.segment,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RunProgress":
        return cls(
            kind=str(data["kind"]),
            description=str(data["description"]),
            evidence=tuple(EvidenceRef.from_dict(item) for item in data.get("evidence") or ()),
            segment=int(data.get("segment", 0)),
            recorded_at=float(data.get("recorded_at", time.time())),
        )


@dataclass(frozen=True)
class ActionAttempt:
    action_id: str
    run_id: str
    tool_name: str
    normalized_args_digest: str
    effect_class: EffectClass | str
    contract_digest: str
    actor_kind: str
    actor_id: str
    worker_id: str
    attempt: int = 1
    status: ActionStatus = ActionStatus.PLANNED
    lease_epoch: int = 0
    started_at: float | None = None
    arguments_artifact: str | None = None
    result_artifact: str | None = None
    error_kind: str | None = None

    def __post_init__(self) -> None:
        if not self.action_id or not self.run_id or not self.tool_name:
            raise DomainValidationError("action id, run id, and tool name are required")
        if self.attempt < 1:
            raise DomainValidationError("action attempt number must be positive")
        if self.lease_epoch < 0:
            raise DomainValidationError("lease epoch cannot be negative")

    def advance(self, status: ActionStatus, **updates: Any) -> "ActionAttempt":
        allowed = {
            ActionStatus.PLANNED: {ActionStatus.PREPARED, ActionStatus.CANCELLED},
            ActionStatus.PREPARED: {ActionStatus.STARTED, ActionStatus.CANCELLED},
            ActionStatus.STARTED: {
                ActionStatus.SUCCEEDED,
                ActionStatus.FAILED,
                ActionStatus.CANCELLED,
                ActionStatus.OUTCOME_UNKNOWN,
            },
            ActionStatus.OUTCOME_UNKNOWN: {ActionStatus.SUCCEEDED, ActionStatus.FAILED},
            ActionStatus.SUCCEEDED: set(),
            ActionStatus.FAILED: set(),
            ActionStatus.CANCELLED: set(),
        }
        if status not in allowed[self.status]:
            raise DomainValidationError(f"invalid action transition: {self.status.value} -> {status.value}")
        if status is ActionStatus.STARTED and "started_at" not in updates:
            updates["started_at"] = time.time()
        return replace(self, status=status, **updates)


@dataclass(frozen=True)
class ApprovalRequest:
    approval_id: str
    run_id: str
    action_id: str
    action_args_digest: str
    capability_scope: tuple[str, ...]
    requested_at: float = field(default_factory=time.time)
    status: ApprovalStatus = ApprovalStatus.REQUESTED
    decided_by: str | None = None
    decided_at: float | None = None

    def decide(self, status: ApprovalStatus, actor_id: str) -> "ApprovalRequest":
        if self.status is not ApprovalStatus.REQUESTED:
            raise DomainValidationError("approval has already been decided")
        if status not in {ApprovalStatus.GRANTED, ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED}:
            raise DomainValidationError("approval decision must be terminal")
        return replace(self, status=status, decided_by=actor_id, decided_at=time.time())


@dataclass(frozen=True)
class Lease:
    run_id: str
    worker_id: str
    epoch: int
    acquired_at: float
    expires_at: float

    def __post_init__(self) -> None:
        if self.epoch < 1:
            raise DomainValidationError("lease epoch must be positive")
        if self.expires_at <= self.acquired_at:
            raise DomainValidationError("lease expiry must be after acquisition")

    def is_expired(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) >= self.expires_at


@dataclass(frozen=True)
class CompletionCandidate:
    acceptance_criteria: tuple[str, ...]
    evidence: tuple[EvidenceRef, ...]
    unresolved_errors: tuple[str, ...] = ()
    outcome_unknown_actions: tuple[str, ...] = ()
    unaccepted_children: tuple[str, ...] = ()
    pending_approvals: tuple[str, ...] = ()
    modified_paths: tuple[str, ...] = ()
    excluded_scope_violations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "acceptance_criteria": list(self.acceptance_criteria),
            "evidence": [item.to_dict() for item in self.evidence],
            "unresolved_errors": list(self.unresolved_errors),
            "outcome_unknown_actions": list(self.outcome_unknown_actions),
            "unaccepted_children": list(self.unaccepted_children),
            "pending_approvals": list(self.pending_approvals),
            "modified_paths": list(self.modified_paths),
            "excluded_scope_violations": list(self.excluded_scope_violations),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CompletionCandidate":
        return cls(
            acceptance_criteria=tuple(str(item) for item in data.get("acceptance_criteria") or ()),
            evidence=tuple(EvidenceRef.from_dict(item) for item in data.get("evidence") or ()),
            unresolved_errors=tuple(str(item) for item in data.get("unresolved_errors") or ()),
            outcome_unknown_actions=tuple(
                str(item) for item in data.get("outcome_unknown_actions") or ()
            ),
            unaccepted_children=tuple(str(item) for item in data.get("unaccepted_children") or ()),
            pending_approvals=tuple(str(item) for item in data.get("pending_approvals") or ()),
            modified_paths=tuple(str(item) for item in data.get("modified_paths") or ()),
            excluded_scope_violations=tuple(
                str(item) for item in data.get("excluded_scope_violations") or ()
            ),
        )

    def validate(self, goal: GoalContract) -> None:
        expected = set(goal.acceptance_criteria)
        supplied = set(self.acceptance_criteria)
        missing_criteria = expected - supplied
        if missing_criteria:
            raise CompletionEvidenceError(
                f"missing acceptance criteria: {sorted(missing_criteria)}"
            )
        if not self.evidence:
            raise CompletionEvidenceError("completion requires verification evidence")
        if self.unresolved_errors:
            raise CompletionEvidenceError(f"unresolved errors: {sorted(self.unresolved_errors)}")
        if self.outcome_unknown_actions:
            raise CompletionEvidenceError(
                f"outcome-unknown actions: {sorted(self.outcome_unknown_actions)}"
            )
        if self.unaccepted_children:
            raise CompletionEvidenceError(
                f"unaccepted children: {sorted(self.unaccepted_children)}"
            )
        if self.pending_approvals:
            raise CompletionEvidenceError(f"pending approvals: {sorted(self.pending_approvals)}")
        if self.excluded_scope_violations:
            raise CompletionEvidenceError(
                f"excluded scope changed: {sorted(self.excluded_scope_violations)}"
            )


@dataclass(frozen=True)
class Run:
    run_id: str
    goal: GoalContract
    runtime_contract: RuntimeContract
    status: RunStatus = RunStatus.DRAFT
    parent_run_id: str | None = None
    predecessor_run_id: str | None = None
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.run_id:
            raise DomainValidationError("run id is required")


class RunStateMachine:
    """Pure whitelist for lifecycle transitions.

    Same-state events are explicitly listed because a durable event can record
    a segment, heartbeat, or approval decision without changing lifecycle
    status.  Completion is intentionally guarded by CompletionCandidate.
    """

    _TARGETS: ClassVar[dict[str, dict[RunStatus, set[RunStatus]]]] = {
        "RunQueued": {RunStatus.DRAFT: {RunStatus.QUEUED}},
        "RunClaimed": {RunStatus.QUEUED: {RunStatus.RUNNING}},
        "RunYielded": {RunStatus.RUNNING: {RunStatus.QUEUED}},
        "WorkerLeaseExpired": {RunStatus.RUNNING: {RunStatus.QUEUED}},
        "RunResumed": {
            RunStatus.STALLED: {RunStatus.QUEUED},
            RunStatus.BLOCKED: {RunStatus.QUEUED},
            RunStatus.FAILED_RECOVERABLE: {RunStatus.QUEUED},
            RunStatus.WAITING_ON_PREDECESSOR: {RunStatus.QUEUED},
            RunStatus.CANCELLED: {RunStatus.QUEUED},
        },
        "RunSegmentStarted": {RunStatus.RUNNING: {RunStatus.RUNNING}},
        "RunSegmentFinished": {RunStatus.RUNNING: {RunStatus.RUNNING}},
        "WorkerHeartbeat": {
            RunStatus.QUEUED: {RunStatus.QUEUED},
            RunStatus.RUNNING: {RunStatus.RUNNING},
        },
        "ApprovalRequested": {RunStatus.RUNNING: {RunStatus.AWAITING_APPROVAL}},
        "ApprovalGranted": {RunStatus.AWAITING_APPROVAL: {RunStatus.QUEUED}},
        "ApprovalRejected": {
            RunStatus.AWAITING_APPROVAL: {RunStatus.BLOCKED},
        },
        "InterruptRequested": {
            RunStatus.RUNNING: {RunStatus.CANCEL_REQUESTED},
            RunStatus.QUEUED: {RunStatus.CANCEL_REQUESTED},
        },
        "RunBlocked": {
            RunStatus.DRAFT: {RunStatus.BLOCKED},
            RunStatus.QUEUED: {RunStatus.BLOCKED},
            RunStatus.RUNNING: {RunStatus.BLOCKED},
            RunStatus.AWAITING_APPROVAL: {RunStatus.BLOCKED},
            RunStatus.WAITING_ON_PREDECESSOR: {RunStatus.BLOCKED},
            RunStatus.STALLED: {RunStatus.BLOCKED},
            RunStatus.FAILED_RECOVERABLE: {RunStatus.BLOCKED},
            RunStatus.CANCEL_REQUESTED: {RunStatus.BLOCKED},
        },
        "RunStalled": {RunStatus.RUNNING: {RunStatus.STALLED}},
        "RunCancelled": {
            RunStatus.DRAFT: {RunStatus.CANCELLED},
            RunStatus.QUEUED: {RunStatus.CANCELLED},
            RunStatus.AWAITING_APPROVAL: {RunStatus.CANCELLED},
            RunStatus.WAITING_ON_PREDECESSOR: {RunStatus.CANCELLED},
            RunStatus.STALLED: {RunStatus.CANCELLED},
            RunStatus.BLOCKED: {RunStatus.CANCELLED},
            RunStatus.CANCEL_REQUESTED: {RunStatus.CANCELLED},
            RunStatus.FAILED_RECOVERABLE: {RunStatus.CANCELLED},
        },
        "RunFailed": {
            RunStatus.RUNNING: {RunStatus.FAILED_RECOVERABLE, RunStatus.FAILED_TERMINAL},
            RunStatus.QUEUED: {RunStatus.FAILED_RECOVERABLE, RunStatus.FAILED_TERMINAL},
            RunStatus.BLOCKED: {RunStatus.FAILED_TERMINAL},
        },
    }

    _SAME_STATE_EVENTS: ClassVar[set[str]] = {
        "GoalContractAccepted",
        "GoalContractRevised",
        "PlanCreated",
        "PlanRevised",
        "ActionPlanned",
        "ActionPrepared",
        "ActionStarted",
        "ActionProgressRecorded",
        "ActionSucceeded",
        "ActionFailed",
        "ActionCancelled",
        "ActionOutcomeUnknown",
        "PlanDiscoveryStarted",
        "PlanDiscoveryCompleted",
        "PlanNodeStarted",
        "PlanNodeCompleted",
        "PlanNodeBlocked",
        "ModelInvocationStarted",
        "ModelInvocationFinished",
        "AssistantMessageCommitted",
        "AssistantMessageInterrupted",
        "ToolObservationChunkCommitted",
        "ToolObservationCommitted",
        "ContextProjectionBuilt",
        "ContextCompacted",
        "MemoryCandidateRecorded",
        "MemoryCheckpointCommitted",
        "PredecessorHandoffCommitted",
        "ChildDelegationCommitted",
        "ReconciliationStarted",
        "ReconciliationResolved",
        "ProgressRecorded",
        "TodoCreated",
        "TodoUpdated",
        "TodoCompleted",
        "StallDiagnosisRecorded",
        "VerificationRecorded",
        "CompletionCandidateSubmitted",
        "RunRuntimeMigrated",
        "RunSnapshotCreated",
        "FollowUpQueued",
        "FollowUpStarted",
        "PredecessorBypassed",
        "LegacyRunImported",
        "ChildRunCreated",
        "ChildRunClaimed",
        "ChildRunCompleted",
        "ChildRunCancelled",
        "ChildRunFailed",
        "ChildCandidateSubmitted",
        "ChildCandidateAccepted",
        "ChildCandidateRejected",
        "IntegrationConflictRaised",
        "RunOutcomeRecorded",
    }

    _SAME_STATE_ALLOWED: ClassVar[dict[str, set[RunStatus]]] = {
        "GoalContractAccepted": {RunStatus.DRAFT, RunStatus.QUEUED, RunStatus.RUNNING},
        "GoalContractRevised": {RunStatus.DRAFT, RunStatus.QUEUED, RunStatus.RUNNING},
        "PlanCreated": {RunStatus.DRAFT, RunStatus.QUEUED, RunStatus.RUNNING},
        "PlanRevised": {RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.BLOCKED},
        "ActionPlanned": {RunStatus.QUEUED, RunStatus.RUNNING},
        "ActionPrepared": {RunStatus.QUEUED, RunStatus.RUNNING},
        "ActionStarted": {RunStatus.RUNNING},
        "ActionProgressRecorded": {RunStatus.RUNNING},
        "ActionSucceeded": {RunStatus.RUNNING},
        "ActionFailed": {RunStatus.RUNNING, RunStatus.BLOCKED, RunStatus.FAILED_RECOVERABLE},
        "ActionCancelled": {RunStatus.RUNNING, RunStatus.CANCEL_REQUESTED},
        # A cancellation request can arrive while a tool is in flight.  The
        # owning worker must still be able to persist its observation and mark
        # the effect unknown before acknowledging RunCancelled.
        "ActionOutcomeUnknown": {
            RunStatus.RUNNING,
            RunStatus.BLOCKED,
            RunStatus.CANCEL_REQUESTED,
            RunStatus.CANCELLED,
        },
        "PlanDiscoveryStarted": {RunStatus.DRAFT, RunStatus.QUEUED, RunStatus.RUNNING},
        "PlanDiscoveryCompleted": {RunStatus.DRAFT, RunStatus.QUEUED, RunStatus.RUNNING},
        "PlanNodeStarted": {RunStatus.QUEUED, RunStatus.RUNNING},
        "PlanNodeCompleted": {RunStatus.QUEUED, RunStatus.RUNNING},
        "PlanNodeBlocked": {RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.BLOCKED},
        "ModelInvocationStarted": {RunStatus.RUNNING},
        "ModelInvocationFinished": {
            RunStatus.RUNNING,
            RunStatus.CANCEL_REQUESTED,
            RunStatus.CANCELLED,
            RunStatus.BLOCKED,
            RunStatus.STALLED,
            RunStatus.FAILED_RECOVERABLE,
            RunStatus.FAILED_TERMINAL,
            RunStatus.COMPLETED,
        },
        "AssistantMessageCommitted": {RunStatus.RUNNING},
        "AssistantMessageInterrupted": {
            RunStatus.RUNNING,
            RunStatus.CANCEL_REQUESTED,
            RunStatus.CANCELLED,
        },
        "ToolObservationChunkCommitted": {
            RunStatus.RUNNING,
            RunStatus.CANCEL_REQUESTED,
            RunStatus.CANCELLED,
        },
        "ToolObservationCommitted": {
            RunStatus.RUNNING,
            RunStatus.BLOCKED,
            RunStatus.CANCEL_REQUESTED,
            RunStatus.CANCELLED,
        },
        "ContextProjectionBuilt": {RunStatus.RUNNING},
        "ContextCompacted": {RunStatus.RUNNING},
        "MemoryCandidateRecorded": set(RunStatus),
        "MemoryCheckpointCommitted": set(RunStatus),
        "PredecessorHandoffCommitted": set(RunStatus),
        "ChildDelegationCommitted": {RunStatus.RUNNING, RunStatus.QUEUED},
        "ReconciliationStarted": {RunStatus.RUNNING, RunStatus.BLOCKED},
        "ReconciliationResolved": {RunStatus.RUNNING, RunStatus.BLOCKED},
        "ProgressRecorded": {RunStatus.RUNNING},
        "TodoCreated": {RunStatus.DRAFT, RunStatus.QUEUED, RunStatus.RUNNING},
        "TodoUpdated": {RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.BLOCKED},
        "TodoCompleted": {RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.BLOCKED},
        "VerificationRecorded": {RunStatus.RUNNING},
        "CompletionCandidateSubmitted": {RunStatus.RUNNING},
        "StallDiagnosisRecorded": {RunStatus.RUNNING, RunStatus.STALLED},
        "ChildRunCreated": {RunStatus.RUNNING, RunStatus.QUEUED},
        "ChildRunClaimed": {RunStatus.RUNNING, RunStatus.QUEUED},
        "ChildRunCompleted": set(RunStatus),
        "ChildRunCancelled": set(RunStatus),
        "ChildRunFailed": set(RunStatus),
        "ChildCandidateSubmitted": {RunStatus.RUNNING, RunStatus.QUEUED},
        "ChildCandidateAccepted": {RunStatus.RUNNING, RunStatus.BLOCKED},
        "ChildCandidateRejected": {RunStatus.RUNNING, RunStatus.BLOCKED},
        "IntegrationConflictRaised": {RunStatus.RUNNING, RunStatus.BLOCKED},
        "RunOutcomeRecorded": set(RunStatus),
        "FollowUpQueued": set(RunStatus),
        "FollowUpStarted": set(RunStatus),
        "LegacyRunImported": set(RunStatus),
        # A stalled predecessor is still a valid recovery boundary.  The
        # follow-up must be able to start from its durable handoff instead of
        # requiring a mutable/implicit reset of the predecessor first.
        "PredecessorBypassed": {
            RunStatus.QUEUED,
            RunStatus.WAITING_ON_PREDECESSOR,
            RunStatus.STALLED,
        },
        "RunRuntimeMigrated": set(RunStatus),
        "RunSnapshotCreated": set(RunStatus),
    }

    def transition(
        self,
        current: RunStatus,
        event_type: str,
        *,
        target: RunStatus | None = None,
        completion: CompletionCandidate | None = None,
        goal: GoalContract | None = None,
    ) -> RunStatus:
        if event_type == "RunCreated":
            if current is not RunStatus.DRAFT:
                raise InvalidRunTransition("RunCreated is only valid for a draft run")
            return RunStatus.DRAFT
        if event_type == "CompletionAccepted":
            if current is not RunStatus.RUNNING:
                raise InvalidRunTransition(
                    f"CompletionAccepted is invalid from {current.value}"
                )
            if completion is None or goal is None:
                raise CompletionEvidenceError("CompletionAccepted requires goal and evidence")
            completion.validate(goal)
            return RunStatus.COMPLETED
        if event_type == "RunCancelled" and current is RunStatus.RUNNING:
            raise InvalidRunTransition("running run must receive InterruptRequested before cancellation")
        if event_type == "RunRuntimeMigrated" and target is not None and target is not current:
            raise InvalidRunTransition("runtime migration cannot change lifecycle state")

        if event_type in self._SAME_STATE_EVENTS:
            allowed_states = self._SAME_STATE_ALLOWED.get(event_type, set(RunStatus))
            if current not in allowed_states:
                raise InvalidRunTransition(f"event {event_type} is invalid from {current.value}")
            return current
        targets = self._TARGETS.get(event_type, {}).get(current)
        if not targets:
            raise InvalidRunTransition(f"event {event_type} is invalid from {current.value}")
        if target is None:
            if len(targets) != 1:
                raise InvalidRunTransition(f"event {event_type} requires an explicit target")
            return next(iter(targets))
        if target not in targets:
            raise InvalidRunTransition(
                f"event {event_type} cannot transition {current.value} to {target.value}"
            )
        return target


def predecessor_gate(predecessor: RunStatus, *, bypassed: bool = False) -> PredecessorGateStatus:
    if bypassed:
        return PredecessorGateStatus.BYPASSED
    if predecessor is RunStatus.COMPLETED:
        return PredecessorGateStatus.READY
    if predecessor is RunStatus.CANCELLED:
        return PredecessorGateStatus.INCOMPLETE
    return PredecessorGateStatus.WAITING


__all__ = [
    "ActionAttempt",
    "ActionStatus",
    "ApprovalRequest",
    "ApprovalStatus",
    "CandidateChangeSet",
    "ChildRunStatus",
    "CompletionCandidate",
    "CompletionEvidenceError",
    "DomainValidationError",
    "EffectClass",
    "EvidenceKind",
    "EvidenceRef",
    "GoalContract",
    "InvalidRunTransition",
    "Lease",
    "PlanGraph",
    "PlanNode",
    "PredecessorGateStatus",
    "ProgressKind",
    "Run",
    "RunProgress",
    "RunStateMachine",
    "RunStatus",
    "RuntimeContract",
    "digest_json",
    "predecessor_gate",
]
