from pathlib import Path

import pytest

from cc_harness.sandbox_workspace import WorkspaceMaskPlan, discover_mask_targets


def test_discover_masks_workspace_credentials_but_not_templates(tmp_path):
    (tmp_path / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (tmp_path / ".env.example").write_text("TOKEN=example", encoding="utf-8")
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".cc-harness").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("credential=secret", encoding="utf-8")

    targets = discover_mask_targets(tmp_path)
    paths = {target.relative_path.as_posix() for target in targets}

    assert ".env" in paths
    assert ".ssh" in paths
    assert ".cc-harness" in paths
    assert ".git/config" in paths
    assert ".env.example" not in paths


def test_sensitive_ancestor_does_not_mask_entire_workspace(tmp_path):
    root = tmp_path / ".cc-harness" / "eval" / "workspaces" / "trial"
    (root / "app").mkdir(parents=True)
    (root / "app" / "config.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")

    targets = discover_mask_targets(root)

    assert [(item.relative_path.as_posix(), item.is_dir) for item in targets] == [
        (".env", False),
    ]


def test_mask_plan_contains_only_empty_overlays(tmp_path):
    secret = "sk-do-not-copy"
    (tmp_path / ".env").write_text(secret, encoding="utf-8")
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_rsa").write_text(secret, encoding="utf-8")

    plan = WorkspaceMaskPlan.create(discover_mask_targets(tmp_path))
    try:
        env_mask = plan.root / ".env"
        ssh_mask = plan.root / ".ssh"
        assert env_mask.read_bytes() == b""
        assert ssh_mask.is_dir()
        assert list(ssh_mask.iterdir()) == []
        assert secret not in str(plan.root)
    finally:
        root = plan.root
        plan.cleanup()
    assert not root.exists()


def test_mask_plan_can_rebuild_at_an_allowed_stable_root(tmp_path):
    (tmp_path / ".env").write_text("TOKEN=secret", encoding="utf-8")
    stable_root = tmp_path.parent / f"{tmp_path.name}-stable-mask"

    first = WorkspaceMaskPlan.create(discover_mask_targets(tmp_path), root=stable_root)
    first.cleanup()
    rebuilt = WorkspaceMaskPlan.create(discover_mask_targets(tmp_path), root=stable_root)
    try:
        assert rebuilt.root == stable_root
        assert (stable_root / ".env").read_bytes() == b""
    finally:
        rebuilt.cleanup()


def test_directory_symlink_is_masked_without_following_it(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "visible.txt").write_text("outside", encoding="utf-8")
    link = tmp_path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    targets = discover_mask_targets(tmp_path)

    assert [(target.relative_path, target.is_dir) for target in targets] == [
        (Path("linked"), True),
    ]
