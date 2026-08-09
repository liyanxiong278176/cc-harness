from datetime import UTC, datetime

import pytest

from eval.core import CapabilityDomain, ResourceUsage, ResultStatus, canonical_json_bytes
from eval.launch import HarnessKind
from eval.parity import (
    ImportedHarnessResult,
    NormalizedPairBundle,
    NormalizedPairRecord,
    ParitySuite,
    analyze_imported_parity,
    load_normalized_bundle,
)


def _usage() -> ResourceUsage:
    return ResourceUsage(
        wall_time_ms=1_000,
        steps=2,
        model_calls=2,
        tool_calls=4,
        input_tokens=1_000,
        output_tokens=200,
        cost_microusd=10_000,
    )


def _bundle(*, baseline_version: str = "2.1.221") -> NormalizedPairBundle:
    return NormalizedPairBundle(
        source_id="swebench.verified",
        generated_at=datetime.now(UTC),
        candidate_id="cc-harness",
        baseline_id="claude-code",
        candidate_version="0.1.0",
        baseline_version=baseline_version,
        requested_model="deepseek-v4-flash",
        resolved_model="deepseek-v4-flash",
        environment_digest="sha256:" + "a" * 64,
        records=tuple(
            NormalizedPairRecord(
                pair_id=f"task-{index}.r1",
                task_id=f"task-{index}",
                repetition=1,
                order=(HarnessKind.CC_HARNESS, HarnessKind.CLAUDE_CODE),
                domains=(CapabilityDomain.CODING_OUTCOME,),
                candidate=ImportedHarnessResult(status=ResultStatus.PASS, usage=_usage()),
                baseline=ImportedHarnessResult(status=ResultStatus.PASS, usage=_usage()),
            )
            for index in range(10)
        ),
    )


def test_import_analysis_writes_standard_result_layout(tmp_path) -> None:
    source = tmp_path / "source" / "bundle.json"
    source.parent.mkdir()
    source.write_bytes(canonical_json_bytes(_bundle()))
    output = tmp_path / "result" / "parity-test"

    result = analyze_imported_parity((source,), output, suite=ParitySuite.DEV)

    assert result.conclusion == "inconclusive"
    assert (output / "manifest.json").is_file()
    assert (output / "schedule.json").is_file()
    assert (output / "summary.json").is_file()
    assert (output / "parity-report.md").is_file()
    assert (output / "integrity.json").is_file()
    assert len(list((output / "trials").glob("*.json"))) == 10
    assert (output / "scoring" / "swebench.verified.bundle.json").is_file()


def test_import_rejects_claude_version_drift(tmp_path) -> None:
    source = tmp_path / "bundle.json"
    source.write_bytes(canonical_json_bytes(_bundle(baseline_version="2.1.220")))

    with pytest.raises(ValueError, match="version drift"):
        load_normalized_bundle(source)


def test_import_rejects_omitted_hard_gate_veto(tmp_path) -> None:
    bundle = _bundle()
    first = bundle.records[0].model_copy(
        update={
            "candidate": ImportedHarnessResult(status=ResultStatus.FAIL, usage=_usage()),
            "baseline": ImportedHarnessResult(status=ResultStatus.PASS, usage=_usage()),
        }
    )
    source = tmp_path / "bundle.json"
    source.write_bytes(
        canonical_json_bytes(bundle.model_copy(update={"records": (first, *bundle.records[1:])}))
    )

    with pytest.raises(ValueError, match="hard-gate veto"):
        load_normalized_bundle(source)
