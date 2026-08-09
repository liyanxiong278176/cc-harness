from __future__ import annotations

from eval.core import BudgetEnforcement
from eval.core.tests.test_models import task_contract
from eval.parity.live import _observational_contracts


def test_observational_contracts_are_versioned_and_separate_from_bounded_evidence() -> None:
    bounded = task_contract()

    (observed,) = _observational_contracts((bounded,), 3600)

    assert observed.task_version == "1.2.0-observe"
    assert observed.suite_version == "1.2.0-observe"
    assert observed.budget.enforcement is BudgetEnforcement.OBSERVE
    assert observed.budget.emergency_watchdog_seconds == 3600
    assert "observational-unbounded" in observed.tags
    assert observed != bounded
