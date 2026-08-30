"""Goal-contract assessment and acceptance policy.

The runtime keeps the durable goal in the RunCreated/GoalContract events.  This
module is deliberately side-effect free so a client can explain why a request
is accepted or blocked before it is submitted to a worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from .run_model import GoalContract


class GoalDecision(str, Enum):
    AUTO_ACCEPT = "auto_accept"
    NEEDS_CLARIFICATION = "needs_clarification"
    HIGH_RISK_REVIEW = "high_risk_review"


class GoalBlockedError(ValueError):
    """Raised when a goal cannot be accepted without an explicit decision."""


@dataclass(frozen=True)
class GoalAssessment:
    goal: GoalContract
    decision: GoalDecision
    reasons: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.decision is GoalDecision.AUTO_ACCEPT


_AMBIGUOUS_MARKERS = (
    "something",
    "anything",
    "whatever",
    "etc",
    "as needed",
    "maybe",
    "适当",
    "大概",
    "之类",
    "等等",
    "随便",
)
_HIGH_RISK_MARKERS = (
    "delete",
    "drop ",
    "destroy",
    "wipe",
    "production",
    "deploy",
    "publish",
    "push ",
    "send ",
    "payment",
    "credential",
    "password",
    "token",
    "密钥",
    "生产",
    "删除",
    "部署",
    "发布",
    "付款",
)


def _contains_marker(text: str, markers: Iterable[str]) -> tuple[str, ...]:
    lowered = text.casefold()
    return tuple(marker for marker in markers if marker.casefold() in lowered)


class GoalContractService:
    """Build and assess a GoalContract without changing durable state."""

    def build(
        self,
        objective: str,
        acceptance_criteria: Iterable[str],
        *,
        constraints: Iterable[str] = (),
        allowed_scope: Iterable[str] = (),
        excluded_scope: Iterable[str] = (),
        required_evidence: Iterable[str] = (),
        human_review: Iterable[str] = (),
    ) -> GoalContract:
        return GoalContract(
            objective=objective,
            acceptance_criteria=tuple(acceptance_criteria),
            constraints=tuple(constraints),
            allowed_scope=tuple(allowed_scope),
            excluded_scope=tuple(excluded_scope),
            required_evidence=tuple(required_evidence),
            human_review=tuple(human_review),
        )

    def assess(
        self,
        goal: GoalContract,
        *,
        goal_provenance: str = "user",
    ) -> GoalAssessment:
        """Assess a goal, with an explicit provenance escape hatch for sandboxes.

        Official benchmark task statements may legitimately contain words such
        as ``push`` or ``password`` (and filesystem paths such as ``/etc``)
        because they describe the task's fixture. Those words must not silently
        weaken the normal user-facing gate. The caller has to provide the
        explicit ``official_benchmark`` provenance; only the goal-level
        clarification/confirmation gate is bypassed. Tool policy, approvals,
        command sandboxing, and output guards remain active.
        """

        # ``user_confirmed`` is only set by the explicit CLI confirmation
        # path.  Keep it distinct from the benchmark provenance so the event
        # log records whether a live user or an isolated benchmark authorized
        # the high-risk goal.
        trusted_provenance = goal_provenance in {"official_benchmark", "user_confirmed"}
        text = " ".join((goal.objective, *goal.acceptance_criteria, *goal.constraints))
        # A frozen benchmark statement is an externally-owned task contract,
        # not an underspecified live user request. Do not stop the benchmark
        # before its model call because a fixture path (for example ``/etc``)
        # or a task phrase happens to match a generic goal marker. All
        # action-level controls remain enforced after this goal gate.
        ambiguous = () if trusted_provenance else _contains_marker(text, _AMBIGUOUS_MARKERS)
        high_risk = () if trusted_provenance else _contains_marker(text, _HIGH_RISK_MARKERS)
        reasons: list[str] = []
        questions: list[str] = []
        if ambiguous:
            reasons.append("goal contains ambiguous language")
            questions.append("请明确目标范围、预期结果和完成标准。")
        if high_risk:
            reasons.append("goal contains a high-risk or externally visible operation")
            questions.append("请确认该高风险操作的目标、范围和人工批准边界。")
        if goal.human_review:
            reasons.append("goal explicitly requires human review")
            questions.extend(goal.human_review)
        if high_risk:
            decision = GoalDecision.HIGH_RISK_REVIEW
        elif ambiguous:
            decision = GoalDecision.NEEDS_CLARIFICATION
        else:
            decision = GoalDecision.AUTO_ACCEPT
        return GoalAssessment(goal, decision, tuple(dict.fromkeys(reasons)), tuple(dict.fromkeys(questions)))

    def require_acceptance(self, goal: GoalContract) -> GoalAssessment:
        assessment = self.assess(goal)
        if not assessment.accepted:
            detail = "; ".join(assessment.questions or assessment.reasons)
            raise GoalBlockedError(detail or "goal requires an explicit decision")
        return assessment


def assess_goal(goal: GoalContract) -> GoalAssessment:
    """Small functional seam for callers that do not need a service object."""

    return GoalContractService().assess(goal)


__all__ = [
    "GoalAssessment",
    "GoalBlockedError",
    "GoalContractService",
    "GoalDecision",
    "assess_goal",
]
