from __future__ import annotations

import subprocess
import sys
import zipfile
from io import BytesIO
from pathlib import Path

from eval.canary import ADVANCED_CANARY_DEFINITIONS, install_advanced_canary_contracts
from eval.core import EvalStore


def _pytest(workspace: Path, target: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        (sys.executable, "-m", "pytest", target, "-q"),
        cwd=workspace,
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
    )


def _apply_reference(task_id: str, workspace: Path) -> None:
    if task_id.endswith("cross-file-runtime-config"):
        (workspace / "app/config.py").write_text(
            """def merge_runtime_config(defaults, environment, cli):
    config = dict(defaults)
    config.update(environment)
    config.update(cli)
    return config
""",
            encoding="utf-8",
        )
        (workspace / "app/retry.py").write_text(
            """def retry_schedule(attempts, base, cap):
    return [min(cap, base * (2 ** attempt)) for attempt in range(attempts)]
""",
            encoding="utf-8",
        )
        return
    if task_id.endswith("checkpoint-recovery"):
        (workspace / "storage/codec.py").write_text(
            """import json


def encode_checkpoint(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def decode_checkpoint(raw):
    return json.loads(raw)
""",
            encoding="utf-8",
        )
        (workspace / "storage/checkpoint.py").write_text(
            """import os
import shutil
from pathlib import Path

from storage.codec import decode_checkpoint, encode_checkpoint


def load_checkpoint(path):
    path = Path(path)
    try:
        return decode_checkpoint(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        backup = path.with_suffix(path.suffix + ".bak")
        return decode_checkpoint(backup.read_text(encoding="utf-8"))


def save_checkpoint(path, payload):
    path = Path(path)
    backup = path.with_suffix(path.suffix + ".bak")
    temporary = path.with_suffix(path.suffix + ".tmp")
    if path.exists():
        shutil.copy2(path, backup)
    temporary.write_text(encode_checkpoint(payload), encoding="utf-8")
    os.replace(temporary, path)
""",
            encoding="utf-8",
        )
        return
    if task_id.endswith("decision-record-context"):
        (workspace / "src/retention.py").write_text(
            """_RETENTION_DAYS = {"audit": 365, "session": 30, "ephemeral": 1}


def retention_days(record_type):
    try:
        return _RETENTION_DAYS[record_type]
    except KeyError as exc:
        raise ValueError(f"unknown record type: {record_type}") from exc


def should_purge(record_type, age_days):
    return age_days >= retention_days(record_type)
""",
            encoding="utf-8",
        )
        return
    raise AssertionError(f"missing reference solution for {task_id}")


async def test_advanced_tasks_start_failing_and_reference_solutions_pass(tmp_path) -> None:
    store = EvalStore(tmp_path / "evidence")
    await store.open()
    try:
        contracts = await install_advanced_canary_contracts(store)
        assert len(contracts) == len(ADVANCED_CANARY_DEFINITIONS) == 3
        assert {item.task_version for item in contracts} == {"1.1.0"}
        assert {item.suite_version for item in contracts} == {"1.1.0"}
        checkpoint = next(item for item in contracts if item.task_id.endswith("checkpoint-recovery"))
        assert checkpoint.budget.wall_time_seconds == 300
        for contract in contracts:
            workspace = tmp_path / contract.task_id.replace(".", "-")
            workspace.mkdir()
            archive = await store.read_artifact(contract.initial_state_ref)
            with zipfile.ZipFile(BytesIO(archive)) as fixture:
                fixture.extractall(workspace)
            target = next(
                definition.test_targets[0]
                for definition in ADVANCED_CANARY_DEFINITIONS
                if definition.task_id == contract.task_id
            )

            initial = _pytest(workspace, target)
            assert initial.returncode != 0, initial.stdout.decode("utf-8", errors="replace")

            _apply_reference(contract.task_id, workspace)
            repaired = _pytest(workspace, target)
            assert repaired.returncode == 0, (repaired.stdout + repaired.stderr).decode(
                "utf-8", errors="replace"
            )
    finally:
        await store.close()
