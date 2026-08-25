from __future__ import annotations

from pathlib import Path

from scripts import run_cc_only_benchmark


def test_selected_terminal_run_reuses_its_immutable_wheel(tmp_path: Path) -> None:
    output = tmp_path / "full-selected-holdout"
    (output / "frozen-inputs").mkdir(parents=True)
    (output / "manifest.json").write_text("{}", encoding="utf-8")
    wheel = output / "frozen-inputs" / "cc_harness-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"frozen")

    assert (
        run_cc_only_benchmark._existing_terminal_frozen_wheel(
            output, benchmark="terminal-bench-2.1"
        )
        == wheel
    )


def test_new_terminal_run_has_no_existing_immutable_wheel(tmp_path: Path) -> None:
    output = tmp_path / "full-selected-new"

    assert (
        run_cc_only_benchmark._existing_terminal_frozen_wheel(
            output, benchmark="terminal-bench-2.1"
        )
        is None
    )


def test_terminal_check_never_reuses_wheel_from_directory_it_may_archive(
    tmp_path: Path,
) -> None:
    output = tmp_path / "check"
    (output / "frozen-inputs").mkdir(parents=True)
    (output / "manifest.json").write_text("{}", encoding="utf-8")
    (output / "frozen-inputs" / "cc_harness-0.1.0-py3-none-any.whl").write_bytes(
        b"stale-check-wheel"
    )

    assert (
        run_cc_only_benchmark._terminal_frozen_wheel_for_invocation(
            output,
            benchmark="terminal-bench-2.1",
            check_only=True,
        )
        is None
    )
