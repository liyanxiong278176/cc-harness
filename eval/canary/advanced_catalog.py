"""Higher-discrimination multi-step harness canaries."""

from __future__ import annotations

from eval.core import CapabilityDomain, EvalStore, RiskLevel, StateProfile, TaskContract

from .catalog import CanaryDefinition, CanaryFile, install_canary_contracts

ADVANCED_SUITE_ID = "harness-canary-advanced"
ADVANCED_SUITE_VERSION = "1.1.0"


def _text(path: str, content: str, *, crlf: bool = False) -> CanaryFile:
    encoded = content.encode("utf-8")
    if crlf:
        encoded = encoded.replace(b"\n", b"\r\n")
    return CanaryFile(path, encoded)


_CROSS_FILE_FILES = (
    _text("app/__init__.py", ""),
    _text(
        "app/config.py",
        """def merge_runtime_config(defaults, environment, cli):
    config = dict(defaults)
    config.update(cli)
    config.update(environment)
    return config
""",
    ),
    _text(
        "app/retry.py",
        """def retry_schedule(attempts, base, cap):
    return [min(cap, base ** attempt) for attempt in range(attempts)]
""",
        crlf=True,
    ),
    _text(
        "app/runtime.py",
        """from app.config import merge_runtime_config
from app.retry import retry_schedule


def build_runtime(defaults, environment, cli):
    config = merge_runtime_config(defaults, environment, cli)
    config["retry_delays"] = retry_schedule(
        config["attempts"], config["retry_base"], config["retry_cap"]
    )
    return config
""",
    ),
    _text(
        "tests/test_runtime.py",
        """from app.runtime import build_runtime


def test_runtime_precedence_and_retry_schedule():
    defaults = {"attempts": 4, "retry_base": 0.5, "retry_cap": 10, "region": "default"}
    environment = {"retry_cap": 2, "region": "environment"}
    cli = {"region": "cli"}

    result = build_runtime(defaults, environment, cli)

    assert result["region"] == "cli"
    assert result["retry_delays"] == [0.5, 1.0, 2.0, 2]
    assert defaults == {
        "attempts": 4,
        "retry_base": 0.5,
        "retry_cap": 10,
        "region": "default",
    }
""",
    ),
)

_CHECKPOINT_FILES = (
    _text("storage/__init__.py", ""),
    _text(
        "storage/codec.py",
        """import json


def encode_checkpoint(payload):
    return json.dumps(payload, ensure_ascii=True)


def decode_checkpoint(raw):
    return json.loads(raw)
""",
    ),
    _text(
        "storage/checkpoint.py",
        """from pathlib import Path

from storage.codec import decode_checkpoint, encode_checkpoint


def load_checkpoint(path):
    path = Path(path)
    return decode_checkpoint(path.read_text(encoding="utf-8"))


def save_checkpoint(path, payload):
    path = Path(path)
    path.write_text(encode_checkpoint(payload), encoding="utf-8")
""",
    ),
    _text(
        "tests/test_checkpoint.py",
        """import json

from storage.checkpoint import load_checkpoint, save_checkpoint


def test_load_falls_back_to_last_good_backup(tmp_path):
    target = tmp_path / "checkpoint.json"
    target.write_text("{broken", encoding="utf-8")
    target.with_suffix(".json.bak").write_text(
        json.dumps({"version": 2, "label": "上次"}, ensure_ascii=False),
        encoding="utf-8",
    )

    assert load_checkpoint(target) == {"version": 2, "label": "上次"}


def test_save_is_atomic_unicode_and_preserves_previous_value(tmp_path):
    target = tmp_path / "checkpoint.json"
    target.write_text('{"version": 1}', encoding="utf-8")

    save_checkpoint(target, {"version": 2, "label": "恢复"})

    assert load_checkpoint(target) == {"version": 2, "label": "恢复"}
    assert json.loads(target.with_suffix(".json.bak").read_text(encoding="utf-8")) == {
        "version": 1
    }
    assert "恢复" in target.read_text(encoding="utf-8")
    assert not target.with_suffix(".json.tmp").exists()
""",
    ),
)


def _decision_records() -> tuple[CanaryFile, ...]:
    files: list[CanaryFile] = []
    for index in range(1, 25):
        status = "accepted" if index == 17 else ("superseded" if index < 17 else "draft")
        if index == 17:
            policy = "audit=365, session=30, ephemeral=1; purge when age_days >= retention"
        else:
            policy = (
                f"audit={30 + index}, session={7 + index % 10}, ephemeral={index % 3 + 1}; "
                "purge semantics remain under discussion"
            )
        filler = " ".join(
            f"migration-{index:02d}-{part:02d} preserves historical compatibility notes."
            for part in range(24)
        )
        files.append(
            _text(
                f"decisions/retention-{index:02d}.md",
                f"# Retention decision {index:02d}\n\nstatus: {status}\n\n{policy}\n\n{filler}\n",
            )
        )
    return tuple(files)


_CONTEXT_FILES = (
    _text("src/__init__.py", ""),
    _text(
        "src/retention.py",
        """_RETENTION_DAYS = {"audit": 90, "session": 14, "ephemeral": 7}


def retention_days(record_type):
    return _RETENTION_DAYS.get(record_type, 0)


def should_purge(record_type, age_days):
    return age_days > retention_days(record_type)
""",
    ),
    *_decision_records(),
    _text(
        "tests/test_retention.py",
        """import pytest

from src.retention import retention_days, should_purge


def test_accepted_retention_decision_is_implemented():
    assert retention_days("audit") == 365
    assert retention_days("session") == 30
    assert retention_days("ephemeral") == 1
    with pytest.raises(ValueError):
        retention_days("unknown")


def test_purge_boundary_is_inclusive():
    assert should_purge("session", 29) is False
    assert should_purge("session", 30) is True
""",
    ),
)


ADVANCED_CANARY_DEFINITIONS = (
    CanaryDefinition(
        task_id="canary.advanced.cross-file-runtime-config",
        title="Repair runtime precedence and retry behavior across modules",
        risk=RiskLevel.HIGH,
        domains=(
            CapabilityDomain.CODING_OUTCOME,
            CapabilityDomain.AGENT_LOOP,
            CapabilityDomain.TOOLS_AND_PROTOCOLS,
        ),
        files=_CROSS_FILE_FILES,
        wall_time_seconds=180,
        prompt=(
            "Repair the runtime configuration behavior. The implementation spans multiple app "
            "modules and one source file uses CRLF. Preserve input mappings, do not modify tests, "
            "and run the focused tests before finishing."
        ),
        test_targets=("tests/test_runtime.py",),
        protected_paths=("tests/test_runtime.py",),
        outcome_paths=("app/config.py", "app/retry.py", "app/runtime.py"),
        max_model_calls=50,
        max_input_tokens=200_000,
        max_output_tokens=50_000,
    ),
    CanaryDefinition(
        task_id="canary.advanced.checkpoint-recovery",
        title="Recover checkpoints and preserve atomic Unicode state",
        risk=RiskLevel.CRITICAL,
        domains=(
            CapabilityDomain.CODING_OUTCOME,
            CapabilityDomain.RELIABILITY_AND_RECOVERY,
            CapabilityDomain.TOOLS_AND_PROTOCOLS,
        ),
        files=_CHECKPOINT_FILES,
        state_profile=StateProfile.RECOVERY,
        wall_time_seconds=300,
        prompt=(
            "Make checkpoint persistence robust to a corrupt primary file and interrupted writes. "
            "Keep the last good value as the .bak sibling, write through a .tmp sibling followed "
            "by atomic replacement, preserve readable UTF-8 text, and do not modify tests."
        ),
        test_targets=("tests/test_checkpoint.py",),
        protected_paths=("tests/test_checkpoint.py",),
        outcome_paths=("storage/codec.py", "storage/checkpoint.py"),
        max_model_calls=50,
        max_input_tokens=200_000,
        max_output_tokens=50_000,
    ),
    CanaryDefinition(
        task_id="canary.advanced.decision-record-context",
        title="Find and implement the authoritative retention decision",
        risk=RiskLevel.HIGH,
        domains=(
            CapabilityDomain.CODING_OUTCOME,
            CapabilityDomain.CONTEXT_MANAGEMENT,
            CapabilityDomain.AGENT_LOOP,
        ),
        files=_CONTEXT_FILES,
        state_profile=StateProfile.CONTEXT,
        wall_time_seconds=180,
        prompt=(
            "Review the retention decision records. They intentionally conflict; only the record "
            "whose status is accepted is authoritative. Implement that contract in src/retention.py, "
            "including unknown-type and purge-boundary behavior. Do not modify tests."
        ),
        test_targets=("tests/test_retention.py",),
        protected_paths=("tests/test_retention.py",),
        outcome_paths=("src/retention.py",),
        max_model_calls=50,
        max_input_tokens=200_000,
        max_output_tokens=50_000,
    ),
)


async def install_advanced_canary_contracts(store: EvalStore) -> tuple[TaskContract, ...]:
    return await install_canary_contracts(
        store,
        definitions=ADVANCED_CANARY_DEFINITIONS,
        suite_id=ADVANCED_SUITE_ID,
        suite_version=ADVANCED_SUITE_VERSION,
    )
