from __future__ import annotations

import zipfile
from io import BytesIO

import pytest
from pydantic import ValidationError

from eval.canary import CANARY_DEFINITIONS, CanaryInstruction, install_canary_contracts
from eval.canary.models import ProtectedFile
from eval.core import EvalStore, content_fingerprint


async def test_catalog_is_versioned_deterministic_and_preserves_crlf(tmp_path) -> None:
    first = EvalStore(tmp_path / "first")
    second = EvalStore(tmp_path / "second")
    await first.open()
    await second.open()
    try:
        first_contracts = await install_canary_contracts(first)
        second_contracts = await install_canary_contracts(second)

        assert len(first_contracts) == len(CANARY_DEFINITIONS) == 10
        assert tuple(map(content_fingerprint, first_contracts)) == tuple(
            map(content_fingerprint, second_contracts)
        )
        assert {item.task_version for item in first_contracts} == {"1.1.0"}
        assert {item.suite_version for item in first_contracts} == {"1.1.0"}
        assert {item.budget.wall_time_seconds for item in first_contracts} == {180}
        assert {item.budget.max_input_tokens for item in first_contracts} == {200_000}
        crlf = next(item for item in first_contracts if item.task_id.endswith("crlf"))
        archive = await first.read_artifact(crlf.initial_state_ref)
        with zipfile.ZipFile(BytesIO(archive)) as fixture:
            solution = fixture.read("solution.py")
            tests = fixture.read("test_solution.py")
        assert b"\r\n" in solution
        assert b"\n" not in solution.replace(b"\r\n", b"")
        assert b"\r\n" not in tests
    finally:
        await first.close()
        await second.close()


def test_instruction_rejects_workspace_traversal() -> None:
    with pytest.raises(ValidationError, match="traversal"):
        CanaryInstruction(
            contract_id="canary.bad",
            prompt="fix it",
            test_targets=("../test_solution.py",),
            protected_files=(
                ProtectedFile(
                    path="../test_solution.py",
                    digest=f"sha256:{'a' * 64}",
                ),
            ),
            outcome_paths=("solution.py",),
        )
