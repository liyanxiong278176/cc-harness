import hashlib
import json
import os

import pytest

from cc_harness.agent import NATIVE_TOOLS
from cc_harness.native_tools import edit_tool, glob_tool, grep_tool, read_tool, write_tool


def _payload(result):
    assert result.is_error is False, result.llm_text
    return json.loads(result.llm_text)


@pytest.mark.asyncio
async def test_read_returns_hash_encoding_newline_and_cursor(tmp_path):
    target = tmp_path / "sample.txt"
    raw = b"one\r\ntwo\r\nthree\r\n"
    target.write_bytes(raw)

    result = _payload(
        await read_tool(
            {"path": "sample.txt", "offset": 2, "limit": 1},
            cwd=str(tmp_path),
        )
    )

    assert result["content"] == "two\r\n"
    assert result["encoding"] == "utf-8"
    assert result["newline"] == "crlf"
    assert result["content_hash"] == f"sha256:{hashlib.sha256(raw).hexdigest()}"
    assert result["truncated"] is True
    assert result["next_offset"] == 3


@pytest.mark.asyncio
async def test_read_rejects_workspace_escape_and_binary(tmp_path):
    outside = tmp_path.parent / "outside-native-tool.txt"
    outside.write_text("secret", encoding="utf-8")
    escaped = await read_tool({"path": str(outside)}, cwd=str(tmp_path))
    assert escaped.is_error
    assert "outside the workspace" in escaped.llm_text

    (tmp_path / "binary.bin").write_bytes(b"a\x00b")
    binary = await read_tool({"path": "binary.bin"}, cwd=str(tmp_path))
    assert binary.is_error
    assert "binary" in binary.llm_text


@pytest.mark.asyncio
async def test_container_workspace_alias_maps_to_project_root(tmp_path):
    target = tmp_path / "observability" / "audit.json"
    target.parent.mkdir()
    target.write_text('{"ok": true}\n', encoding="utf-8")

    result = _payload(await read_tool({"path": "/workspace/observability/audit.json"}, cwd=str(tmp_path)))
    assert result["content"].replace("\r\n", "\n") == '{"ok": true}\n'

    written = _payload(
        await write_tool(
            {
                "path": "/workspace/observability/new.json",
                "content": "{}\n",
                "mode": "create_only",
            },
            cwd=str(tmp_path),
        )
    )
    assert written["path"] == "observability/new.json"
    assert (tmp_path / "observability" / "new.json").read_text() == "{}\n"


@pytest.mark.asyncio
async def test_edit_requires_current_hash_and_one_exact_match(tmp_path):
    target = tmp_path / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    current = _payload(await read_tool({"path": "module.py"}, cwd=str(tmp_path)))

    changed = _payload(
        await edit_tool(
            {
                "path": "module.py",
                "old_text": "value = 1",
                "new_text": "value = 2",
                "expected_hash": current["content_hash"],
            },
            cwd=str(tmp_path),
        )
    )

    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert changed["before_hash"] == current["content_hash"]
    assert "-value = 1" in changed["diff"]
    assert "+value = 2" in changed["diff"]

    stale = await edit_tool(
        {
            "path": "module.py",
            "old_text": "value = 2",
            "new_text": "value = 3",
            "expected_hash": current["content_hash"],
        },
        cwd=str(tmp_path),
    )
    assert stale.is_error
    assert "stale content hash" in stale.llm_text
    assert target.read_text(encoding="utf-8") == "value = 2\n"


@pytest.mark.asyncio
async def test_edit_normalizes_lf_arguments_to_crlf_file(tmp_path):
    target = tmp_path / "module.py"
    target.write_bytes(b"def value():\r\n    return 1\r\n")
    current = _payload(await read_tool({"path": "module.py"}, cwd=str(tmp_path)))

    changed = _payload(
        await edit_tool(
            {
                "path": "module.py",
                "old_text": "def value():\n    return 1\n",
                "new_text": "def value():\n    return 2\n",
                "expected_hash": current["content_hash"],
            },
            cwd=str(tmp_path),
        )
    )

    assert target.read_bytes() == b"def value():\r\n    return 2\r\n"
    assert changed["before_hash"] == current["content_hash"]


@pytest.mark.asyncio
async def test_edit_rejects_ambiguous_match_without_writing(tmp_path):
    target = tmp_path / "duplicates.txt"
    target.write_text("same\nsame\n", encoding="utf-8")
    current = _payload(await read_tool({"path": target.name}, cwd=str(tmp_path)))

    result = await edit_tool(
        {
            "path": target.name,
            "old_text": "same",
            "new_text": "changed",
            "expected_hash": current["content_hash"],
        },
        cwd=str(tmp_path),
    )

    assert result.is_error
    assert "found 2 matches" in result.llm_text
    assert target.read_text(encoding="utf-8") == "same\nsame\n"


@pytest.mark.asyncio
async def test_write_modes_never_silently_overwrite(tmp_path):
    created = _payload(
        await write_tool(
            {
                "path": "created.txt",
                "content": "first\n",
                "mode": "create_only",
            },
            cwd=str(tmp_path),
        )
    )
    assert created["before_hash"] is None

    duplicate = await write_tool(
        {
            "path": "created.txt",
            "content": "second\n",
            "mode": "create_only",
        },
        cwd=str(tmp_path),
    )
    assert duplicate.is_error
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "first\n"

    replaced = _payload(
        await write_tool(
            {
                "path": "created.txt",
                "content": "second\n",
                "mode": "replace_existing",
                "expected_hash": created["content_hash"],
            },
            cwd=str(tmp_path),
        )
    )
    assert replaced["before_hash"] == created["content_hash"]
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "second\n"


@pytest.mark.asyncio
async def test_write_create_only_creates_safe_parent_directories_by_default(tmp_path):
    created = _payload(
        await write_tool(
            {
                "path": "nested/deep/result.json",
                "content": "{}\n",
                "mode": "create_only",
            },
            cwd=str(tmp_path),
        )
    )

    assert created["path"] == "nested/deep/result.json"
    assert (tmp_path / "nested" / "deep" / "result.json").read_text() == "{}\n"


@pytest.mark.asyncio
async def test_write_can_require_an_existing_parent_directory(tmp_path):
    result = await write_tool(
        {
            "path": "missing/result.json",
            "content": "{}\n",
            "mode": "create_only",
            "create_parents": False,
        },
        cwd=str(tmp_path),
    )

    assert result.is_error
    assert "parent directory does not exist" in result.llm_text
    assert not (tmp_path / "missing").exists()


@pytest.mark.asyncio
async def test_write_rejects_symlink_parent_traversal(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    try:
        os.symlink(real, linked, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    result = await write_tool(
        {
            "path": "linked/result.txt",
            "content": "unsafe",
            "mode": "create_only",
        },
        cwd=str(tmp_path),
    )

    assert result.is_error
    assert "symbolic link or junction" in result.llm_text
    assert not (real / "result.txt").exists()


@pytest.mark.asyncio
async def test_write_rejects_detected_link_parent_without_os_privileges(tmp_path, monkeypatch):
    from cc_harness import native_tools

    linked = tmp_path / "linked"
    linked.mkdir()
    original = native_tools._is_link_like
    monkeypatch.setattr(
        native_tools,
        "_is_link_like",
        lambda path: path == linked or original(path),
    )

    result = await write_tool(
        {
            "path": "linked/result.txt",
            "content": "unsafe",
            "mode": "create_only",
        },
        cwd=str(tmp_path),
    )

    assert result.is_error
    assert "symbolic link or junction" in result.llm_text
    assert not (linked / "result.txt").exists()


@pytest.mark.asyncio
async def test_write_removes_new_empty_parents_after_atomic_failure(tmp_path, monkeypatch):
    from cc_harness.native_tools import MutationEngine

    def fail_create(_self, _target, _raw):
        raise OSError("simulated create failure")

    monkeypatch.setattr(MutationEngine, "_atomic_create", fail_create)
    result = await write_tool(
        {
            "path": "new/deep/result.txt",
            "content": "content",
            "mode": "create_only",
        },
        cwd=str(tmp_path),
    )

    assert result.is_error
    assert not (tmp_path / "new").exists()


@pytest.mark.asyncio
async def test_glob_is_stable_and_resumable(tmp_path):
    for name in ("c.py", "a.py", "b.txt"):
        (tmp_path / name).write_text(name, encoding="utf-8")

    first = _payload(
        await glob_tool(
            {"pattern": "*.py", "limit": 1},
            cwd=str(tmp_path),
        )
    )
    second = _payload(
        await glob_tool(
            {
                "pattern": "*.py",
                "limit": 1,
                "cursor": first["next_cursor"],
            },
            cwd=str(tmp_path),
        )
    )

    assert [item["path"] for item in first["matches"]] == ["a.py"]
    assert [item["path"] for item in second["matches"]] == ["c.py"]
    assert second["next_cursor"] is None


@pytest.mark.asyncio
async def test_globstar_matches_root_and_nested_files(tmp_path):
    (tmp_path / "root.txt").write_text("root", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "child.txt").write_text("child", encoding="utf-8")

    result = _payload(
        await glob_tool(
            {"pattern": "**/*", "limit": 10},
            cwd=str(tmp_path),
        )
    )

    assert [item["path"] for item in result["matches"]] == [
        "nested/child.txt",
        "root.txt",
    ]


@pytest.mark.asyncio
async def test_glob_excludes_internal_harness_logs(tmp_path):
    (tmp_path / "visible.txt").write_text("visible", encoding="utf-8")
    audit_dir = tmp_path / ".cc-harness" / "logs"
    audit_dir.mkdir(parents=True)
    (audit_dir / "policy.jsonl").write_text("internal", encoding="utf-8")

    result = _payload(
        await glob_tool(
            {"pattern": "**/*", "limit": 10},
            cwd=str(tmp_path),
        )
    )

    assert [item["path"] for item in result["matches"]] == ["visible.txt"]


@pytest.mark.asyncio
async def test_grep_returns_locations_and_skips_binary(tmp_path):
    (tmp_path / "a.py").write_text("Needle here\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("nothing\nneedle again\n", encoding="utf-8")
    (tmp_path / "data.bin").write_bytes(b"needle\x00hidden")

    result = _payload(
        await grep_tool(
            {
                "pattern": "needle",
                "case_sensitive": False,
                "include": ["*.py"],
            },
            cwd=str(tmp_path),
        )
    )

    assert [(item["path"], item["line"]) for item in result["matches"]] == [
        ("a.py", 1),
        ("b.py", 2),
    ]
    assert result["truncated"] is False


def test_agent_registers_first_party_file_tools():
    assert {"Read", "Edit", "Write", "Glob", "Grep"}.issubset(NATIVE_TOOLS)
