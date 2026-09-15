"""Goal-contract assessment and acceptance policy.

The runtime keeps the durable goal in the RunCreated/GoalContract events.  This
module is deliberately side-effect free so a client can explain why a request
is accepted or blocked before it is submitted to a worker.
"""

from __future__ import annotations

import re
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
def _contains_marker(text: str, markers: Iterable[str]) -> tuple[str, ...]:
    lowered = text.casefold()
    return tuple(marker for marker in markers if marker.casefold() in lowered)


# A goal often *describes* a production-shaped feature, credentials, payments,
# or destructive business states without asking the Runtime to perform an
# external side effect.  The old substring list treated every noun as a live
# operation, so a request such as "build a production campus food-delivery
# app" was blocked before the first model call.  Keep this gate conservative:
# only explicit imperative operations (or an un-simulated production payment
# service) require a decision.  Tool-level policy and approvals remain the
# enforcement point once the model starts working.
_HIGH_RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "deployment",
        re.compile(
            r"\b(?:deploy|publish|push)\b[^.!?\n]{0,120}\b"
            r"(?:production|prod|live|remote|server|webserver|origin|public)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "destructive_operation",
        re.compile(
            r"\b(?:delete|drop|destroy|wipe|erase)\b[^.!?\n]{0,80}\b"
            r"(?:all|the|files?|directories?|database|data|repo(?:sitory)?|"
            r"project|account|production|server)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "secret_exfiltration",
        re.compile(
            r"\b(?:reveal|expose|exfiltrate|print|log|share|send|upload)\b"
            r"[^.!?\n]{0,80}\b(?:api[\s_-]?key|password|token|credential|secret)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "production_payment_service",
        re.compile(
            r"\bproduction\b[^.!?\n]{0,60}\b"
            r"(?:payment|payments|billing|banking|charge)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "deployment_cn",
        re.compile(
            r"(?:部署|发布|推送|上线)\s*(?:到|至)?\s*"
            r"(?:生产|线上|远程|服务器|公网|仓库|webserver)",
            re.IGNORECASE,
        ),
    ),
    (
        "destructive_operation_cn",
        re.compile(
            r"(?:删除|销毁|清空|覆盖)\s*(?:全部|所有|生产|线上|数据库|"
            r"数据|目录|文件|仓库|项目|账号)",
            re.IGNORECASE,
        ),
    ),
    (
        "external_payment_cn",
        re.compile(
            r"(?:真实支付|线上支付|立即付款|转账给|向[^\n]{0,20}转账|"
            r"发送[^\n]{0,20}(?:密钥|密码|令牌)|泄露[^\n]{0,20}(?:密钥|密码|令牌))",
            re.IGNORECASE,
        ),
    ),
)

_NEGATION_PATTERN = re.compile(
    r"(?:\b(?:not|don't|do not|never|without|no)\b|不要|禁止|不得|不应|无需|不需要)"
    r"\s*$",
    re.IGNORECASE,
)
_SIMULATION_PATTERN = re.compile(
    r"(?:mock|fake|simulation|simulated|stub|test[- ]only|replaceable\s+adapter|"
    r"模拟|仿真|虚拟|不需要真实|无需真实|不真实)",
    re.IGNORECASE,
)


def _match_is_negated(text: str, match: re.Match[str]) -> bool:
    """Avoid turning an explicit safety constraint into a risky request."""

    prefix = text[max(0, match.start() - 24) : match.start()]
    return bool(_NEGATION_PATTERN.search(prefix))


def _match_is_simulated(text: str, match: re.Match[str]) -> bool:
    """Treat mock/simulated integrations as implementation requirements."""

    window = text[max(0, match.start() - 100) : min(len(text), match.end() + 100)]
    return bool(_SIMULATION_PATTERN.search(window))


def _contains_high_risk_operation(text: str) -> tuple[str, ...]:
    hits: list[str] = []
    for name, pattern in _HIGH_RISK_PATTERNS:
        match = next(pattern.finditer(text), None)
        if match is None or _match_is_negated(text, match):
            continue
        # A payment/secret phrase accompanied by mock/simulation language is
        # a local implementation concern, not authorization to move money or
        # disclose credentials.  The regular action/secret patterns still
        # catch an explicit non-simulated operation elsewhere in the goal.
        if name in {"production_payment_service", "external_payment_cn", "secret_exfiltration"} and _match_is_simulated(text, match):
            continue
        hits.append(name)
    return tuple(hits)


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
        interaction_mode: str = "coding",
    ) -> GoalContract:
        return GoalContract(
            objective=objective,
            acceptance_criteria=tuple(acceptance_criteria),
            constraints=tuple(constraints),
            allowed_scope=tuple(allowed_scope),
            excluded_scope=tuple(excluded_scope),
            required_evidence=tuple(required_evidence),
            human_review=tuple(human_review),
            interaction_mode=interaction_mode,
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
        high_risk = () if trusted_provenance else _contains_high_risk_operation(text)
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
