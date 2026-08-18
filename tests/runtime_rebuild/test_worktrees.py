import subprocess

from cc_harness.worktrees import WorktreeManager


def _git(root, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def test_non_git_write_child_is_explicitly_serial(tmp_path) -> None:
    project = tmp_path / "plain"
    project.mkdir()
    child = WorktreeManager(project).create_child("parent", "child")
    assert not child.isolated
    assert child.reason == "isolation_unavailable"


def test_git_child_candidate_can_be_integrated(tmp_path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    _git(project, "init")
    _git(project, "config", "user.email", "test@example.com")
    _git(project, "config", "user.name", "Runtime Test")
    (project / "README.md").write_text("base\n", encoding="utf-8")
    _git(project, "add", "README.md")
    _git(project, "commit", "-m", "base")

    manager = WorktreeManager(project, state_root=tmp_path / "state")
    child = manager.create_child("parent", "child")
    (child.path / "README.md").write_text("base\nchild\n", encoding="utf-8")
    candidate = manager.commit_candidate(child, message="child change", owned_paths=("README.md",))
    assert candidate.base_commit
    assert candidate.candidate_commit != candidate.base_commit
    assert candidate.modified_paths == ("README.md",)
    result = manager.integrate_candidate(candidate)
    assert result.accepted
    assert "child" in (project / "README.md").read_text(encoding="utf-8")
    manager.remove(child)
