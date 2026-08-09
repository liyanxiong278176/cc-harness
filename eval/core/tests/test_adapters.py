from __future__ import annotations

from eval.core import AdapterIdentity, EvidenceAdapter, TrialExecutionContext, TrialResult


class ExampleAdapter:
    identity = AdapterIdentity(adapter_id="example", adapter_version="1.0.0")

    async def run_trial(self, context: TrialExecutionContext) -> TrialResult:
        raise NotImplementedError


class MissingRunMethod:
    identity = AdapterIdentity(adapter_id="broken", adapter_version="1.0.0")


def test_adapter_protocol_is_runtime_checkable() -> None:
    assert isinstance(ExampleAdapter(), EvidenceAdapter)
    assert not isinstance(MissingRunMethod(), EvidenceAdapter)
