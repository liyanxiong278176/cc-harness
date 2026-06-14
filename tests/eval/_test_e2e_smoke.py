"""End-to-end smoke for eval pipeline.

NOT collected by pytest default (underscore prefix). Run manually:

    .venv/Scripts/python.exe -m pytest tests/eval/_test_e2e_smoke.py -v -s

Costs real LLM tokens (~50K, ~1分钱 on DeepSeek). Requires HF login.
"""
import asyncio
import pytest
from pathlib import Path
from eval.run import main, Args


@pytest.mark.asyncio
async def test_e2e_3_task_smoke(tmp_path):
    args = Args(
        level="1", limit=3, seed=42, include_attachments=False,
        branches=["context-compaction"],  # single branch to halve cost
        worktree_dir=tmp_path / "wt", keep_worktrees=True,
        mcp_config=Path("mcp.json"),
        context_window=None, tier_overrides=None, max_iter=10,
        parallel=False, on_error="continue", checkpoint_every=2,
        abort_after_overflows=3, dry_run=False,
        output_dir=tmp_path / "out", report_format=["markdown", "json"],
        no_report=False,
    )
    # Bypass the >=30 limit guard for smoke (Args was built directly, not via parse_args)
    rc = await main(args)
    assert rc == 0
    assert (tmp_path / "out" / "context-compaction" / "session_metrics.json").exists()
    assert (tmp_path / "out" / "context-compaction" / "trace.jsonl").exists()
