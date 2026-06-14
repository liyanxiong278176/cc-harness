"""Tests for eval.run CLI (Phase 6)."""
import asyncio
import subprocess as sp
import pytest


def test_defaults():
    from eval.run import parse_args
    a = parse_args([])
    assert a.level == "1"
    assert a.limit == 30
    assert a.seed == 42
    assert a.include_attachments is True
    assert a.branches == ["master", "context-compaction"]
    assert a.checkpoint_every == 5
    assert a.abort_after_overflows == 3
    assert a.on_error == "continue"
    assert a.parallel is False
    assert a.dry_run is False
    assert a.report_format == ["markdown", "csv", "json"]
    assert a.context_window is None


def test_overrides():
    from eval.run import parse_args
    a = parse_args([
        "--level", "all", "--limit", "100", "--seed", "7",
        "--branches", "master", "--worktree-dir", "/tmp/wt",
        "--context-window", "32000", "--parallel", "--dry-run",
    ])
    assert a.level == "all"
    assert a.limit == 100
    assert a.branches == ["master"]
    assert a.context_window == 32000
    assert a.parallel is True
    assert a.dry_run is True


def test_limit_below_30_rejected():
    from eval.run import parse_args
    with pytest.raises(SystemExit):
        parse_args(["--limit", "10"])


def test_preflight_collects_issues(monkeypatch, tmp_path):
    from eval.run import preflight, Args
    args = Args(
        level="1", limit=30, seed=42, include_attachments=True,
        branches=["master", "context-compaction"],
        worktree_dir=tmp_path / "wt", keep_worktrees=False,
        mcp_config=tmp_path / "nonexistent.json",  # bad
        context_window=None, tier_overrides=None, max_iter=20,
        parallel=False, on_error="continue", checkpoint_every=5,
        abort_after_overflows=3, dry_run=False, output_dir=None,
        report_format=["markdown"], no_report=False,
    )
    monkeypatch.setattr("eval.run._git_status_clean", lambda: True)
    monkeypatch.setattr("eval.run._branch_exists", lambda b: True)
    monkeypatch.setattr("eval.run._hf_logged_in", lambda: True)
    issues = preflight(args)
    assert any("mcp" in i.lower() for i in issues)


def test_worktree_add_and_remove(tmp_path, monkeypatch):
    from eval.run import worktree_add, worktree_remove
    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    (repo / "x.txt").write_text("hi")
    sp.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    # Create a SECOND branch so the worktree can check it out
    # (the main repo already occupies 'main')
    sp.run(["git", "branch", "feature"], cwd=repo, check=True, capture_output=True)

    wt = repo / "wt"
    monkeypatch.chdir(repo)
    worktree_add(wt, "feature")
    assert wt.exists()
    assert (wt / "x.txt").read_text() == "hi"
    worktree_remove(wt)
    assert not wt.exists()


def test_dry_run_does_not_call_llm(monkeypatch, tmp_path, capsys):
    from eval.run import main, Args
    from eval.datasets.gaia_loader import GaiaTask

    fake_tasks = [GaiaTask(f"t{i}", "q", 1, "a", None) for i in range(50)]
    monkeypatch.setattr("eval.run._load_gaia_validation", lambda: fake_tasks)
    monkeypatch.setattr("eval.run._git_status_clean", lambda: True)
    monkeypatch.setattr("eval.run._branch_exists", lambda b: True)
    monkeypatch.setattr("eval.run._hf_logged_in", lambda: True)
    args = Args(
        level="1", limit=30, seed=42, include_attachments=True,
        branches=["master"], worktree_dir=tmp_path / "wt",
        keep_worktrees=True, mcp_config=tmp_path,  # any existing path
        context_window=None, tier_overrides=None, max_iter=20,
        parallel=False, on_error="continue", checkpoint_every=5,
        abort_after_overflows=3, dry_run=True,
        output_dir=tmp_path / "out", report_format=["markdown"],
        no_report=False,
    )
    rc = asyncio.run(main(args))
    assert rc == 0
    out = capsys.readouterr().out
    assert "30" in out  # mentions task count
