from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from eval.core import CapabilityDomain, EvalStore, GraderType, content_fingerprint
from eval.core.workspace import EMPTY_WORKSPACE_MEDIA_TYPE
from eval.native import NativePytestSpec, install_native_regression_contracts


async def test_catalog_installs_one_frozen_contract_per_domain(tmp_path) -> None:
    store = EvalStore(tmp_path / "evidence")
    await store.open()
    try:
        initial = await store.put_artifact(b"", EMPTY_WORKSPACE_MEDIA_TYPE)
        contracts = await install_native_regression_contracts(store, initial)
        assert len(contracts) == len(CapabilityDomain) == 9
        assert {contract.domains[0] for contract in contracts} == set(CapabilityDomain)
        assert len({content_fingerprint(contract) for contract in contracts}) == 9
        assert all(
            grader.grader_type is GraderType.DETERMINISTIC
            for contract in contracts
            for grader in contract.graders
        )

        repo = Path(__file__).resolve().parents[3]
        for contract in contracts:
            raw = await store.read_artifact(contract.instruction_ref)
            spec = NativePytestSpec.model_validate_json(raw)
            assert spec.contract_id == contract.task_id
            for target in spec.test_targets:
                assert (repo / target.split("::", 1)[0]).is_file(), target
    finally:
        await store.close()


@pytest.mark.parametrize(
    "target",
    ("-k expression", "../tests/test_agent.py", "/tests/test_agent.py", "C:/secret.py"),
)
def test_native_spec_rejects_unsafe_pytest_targets(target: str) -> None:
    with pytest.raises(ValidationError, match="unsafe pytest target"):
        NativePytestSpec(contract_id="bad", test_targets=(target,))


def test_native_spec_rejects_secret_environment_names() -> None:
    with pytest.raises(ValidationError, match="unsafe environment names"):
        NativePytestSpec(
            contract_id="bad",
            test_targets=("tests/test_agent.py",),
            environment=("OPENAI_API_KEY",),
        )
