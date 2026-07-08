"""Test that _classify routes memory tool calls to the right policy tier.

Memory tools (memory_save / memory_recall) are injected via Task 5's
extra_native_specs and must be classified as fs_write / fs_read respectively so
the L4 policy engine routes them correctly:
  - memory_save  → fs_write → ASK  (write operation)
  - memory_recall → fs_read  → ALLOW (workdir-relative read)
"""
from cc_harness.policy import _classify


def test_memory_save_classified_as_fs_write():
    assert _classify("memory_save") == "fs_write"


def test_memory_recall_classified_as_fs_read():
    assert _classify("memory_recall") == "fs_read"


def test_existing_classifications_unchanged():
    """New cases must not break the existing 5 classifications."""
    assert _classify("run_command") == "shell"
    assert _classify("mcp__filesystem__read_file") == "fs_read"
    assert _classify("mcp__filesystem__write_file") == "fs_write"
    assert _classify("mcp__git__status") == "git_read"
    assert _classify("mcp__git__commit") == "git_write"
    assert _classify("mcp__context7__get_docs") == "docs"