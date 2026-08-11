from __future__ import annotations

from pathlib import Path

from eval.cc_only.storage import read_json
from eval.context_memory.progress import ContextMemoryProgress


def test_progress_persists_task_phase_usage_and_eta(tmp_path: Path) -> None:
    lines: list[str] = []
    output = tmp_path / "result"
    tracker = ContextMemoryProgress(
        output,
        benchmark="fixture",
        profile="portfolio",
        model="deepseek-v4-flash",
        total_tasks=2,
        state={"trials": {"fixture/one": {"treatment": {"status": "pending"}}}},
        emit=lines.append,
    )

    tracker.task_start(1, "fixture/one", "treatment")
    tracker.task_shape(3, 2)
    tracker.item_started("event", 1, 3, "event-1")
    tracker.phase_started("ingest-0001")
    tracker.phase_completed("ingest-0001", {"model_calls": 2, "tool_calls": 3})
    tracker.item_completed("event", 1, 3)
    tracker.item_started("question", 1, 2, "question-1")
    tracker.phase_started("answer-0001")
    tracker.phase_completed("answer-0001", {"model_calls": 1})
    tracker.item_completed("question", 1, 2, score=0.5)
    tracker.task_complete("fixture/one", "complete")
    tracker.finish("incomplete")

    payload = read_json(output / "progress.json")
    assert payload["status"] == "incomplete"
    assert payload["completed_tasks"] == 1
    assert payload["current"]["question_index"] == 1
    assert payload["usage"]["model_calls"] == 3
    assert payload["usage"]["tool_calls"] == 3
    assert "Tasks: `1/2`" in (output / "progress.md").read_text(encoding="utf-8")
    assert lines and "1/2" in lines[-1]
    assert (output / "progress.log").read_text(encoding="utf-8").count("finished") == 1


def test_progress_resume_does_not_double_count_terminal_tasks(tmp_path: Path) -> None:
    state = {
        "trials": {
            "fixture/one": {"treatment": {"status": "complete"}},
            "fixture/two": {"treatment": {"status": "pending"}},
        }
    }
    output = tmp_path / "result"
    first = ContextMemoryProgress(
        output,
        benchmark="fixture",
        profile="portfolio",
        model="deepseek-v4-flash",
        total_tasks=2,
        state=state,
        emit=lambda _line: None,
    )
    first.finish("running")

    resumed = ContextMemoryProgress(
        output,
        benchmark="fixture",
        profile="portfolio",
        model="deepseek-v4-flash",
        total_tasks=2,
        state=state,
        emit=lambda _line: None,
    )
    resumed.task_skipped(1, "fixture/one", "complete")
    assert read_json(output / "progress.json")["completed_tasks"] == 1
