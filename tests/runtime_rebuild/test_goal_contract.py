from cc_harness.goals import GoalContractService, GoalDecision


def test_low_risk_goal_is_auto_accepted() -> None:
    service = GoalContractService()
    goal = service.build("add a parser", ["tests pass"])
    assessment = service.assess(goal)
    assert assessment.decision is GoalDecision.AUTO_ACCEPT
    assert assessment.accepted


def test_ambiguous_and_high_risk_goal_requires_decision() -> None:
    service = GoalContractService()
    goal = service.build("deploy something to production", ["it works"])
    assessment = service.assess(goal)
    assert assessment.decision is GoalDecision.HIGH_RISK_REVIEW
    assert not assessment.accepted
    assert assessment.questions
