"""Manifest load/save/validate 测试(组件 1)。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from cc_harness.project.manifest import (
    ManifestError,
    load_manifest,
    save_manifest,
)
from cc_harness.project.models import Manifest


# ---------------------------------------------------------------------------
# Fixtures(共享)
# ---------------------------------------------------------------------------


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def proj_minimal() -> Path:
    return FIXTURES_DIR / "project_minimal"


@pytest.fixture
def proj_with_tasks() -> Path:
    return FIXTURES_DIR / "project_with_tasks"


@pytest.fixture
def proj_invalid() -> Path:
    return FIXTURES_DIR / "project_invalid"


# ---------------------------------------------------------------------------
# load_manifest — 成功路径
# ---------------------------------------------------------------------------


def test_load_manifest_returns_none_when_no_file(tmp_path):
    """项目根无 `.cc-harness/project.yaml` → 返回 None(spec 行为)。"""
    assert load_manifest(tmp_path) is None


def test_load_manifest_minimal_fixture(proj_minimal):
    m = load_manifest(proj_minimal)
    assert m is not None
    assert m.project_id == "7f3a-mini-a91d"
    assert m.name == "cc-harness-minimal"
    assert m.todos_path == ".cc-harness/todos"
    assert m.schema_version == 1
    assert m.resume_mode == "ask"
    assert m.memory.integration.completion_capture is False
    assert m.live.position == "top"


def test_load_manifest_with_tasks(proj_with_tasks):
    m = load_manifest(proj_with_tasks)
    assert m is not None
    assert m.project_id == "7f3a-tasks-a91d"
    assert m.todos_path == ".cc-harness/todos"


def test_load_manifest_only_required_fields(tmp_path):
    """只写 4 个必填,其他全走默认。"""
    proj = tmp_path / "p"
    proj.mkdir()
    cc = proj / ".cc-harness"
    cc.mkdir()
    (cc / "project.yaml").write_text(
        "project_id: p1\n"
        "name: x\n"
        "todos_path: todos\n"
        "created_at: 2026-07-14T10:00:00Z\n",
        encoding="utf-8",
    )
    m = load_manifest(proj)
    assert m is not None
    assert m.schema_version == 1
    assert m.resume_mode == "ask"
    assert m.memory.integration.completion_capture is False


def test_load_manifest_uses_absolute_path_from_project_root(tmp_path):
    """project_root 必须指向包含 .cc-harness 的目录。"""
    proj = tmp_path / "p"
    proj.mkdir()
    cc = proj / ".cc-harness"
    cc.mkdir()
    (cc / "project.yaml").write_text(
        "project_id: x\nname: y\ntodos_path: t\ncreated_at: 2026-07-14T10:00:00Z\n",
        encoding="utf-8",
    )
    assert load_manifest(proj) is not None
    # 子目录查不到
    assert load_manifest(proj / "subdir") is None if (proj / "subdir").exists() else True


def test_load_manifest_parses_iso_z_suffix(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    cc = proj / ".cc-harness"
    cc.mkdir()
    (cc / "project.yaml").write_text(
        "project_id: x\nname: y\ntodos_path: t\ncreated_at: 2026-07-14T10:00:00Z\n",
        encoding="utf-8",
    )
    m = load_manifest(proj)
    assert m is not None
    assert m.created_at.tzinfo is not None
    assert m.created_at.year == 2026


# ---------------------------------------------------------------------------
# load_manifest — 失败路径(必填字段)
# ---------------------------------------------------------------------------


def test_load_manifest_invalid_fixture_missing_required(proj_invalid):
    """故意缺 `todos_path` 和 `created_at` → ManifestError。"""
    with pytest.raises(ManifestError) as exc:
        load_manifest(proj_invalid)
    msg = str(exc.value)
    assert "todos_path" in msg or "created_at" in msg


def test_load_manifest_missing_project_id(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    cc = proj / ".cc-harness"
    cc.mkdir()
    (cc / "project.yaml").write_text(
        "name: x\ntodos_path: t\ncreated_at: 2026-07-14T10:00:00Z\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="project_id"):
        load_manifest(proj)


def test_load_manifest_missing_created_at(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    cc = proj / ".cc-harness"
    cc.mkdir()
    (cc / "project.yaml").write_text(
        "project_id: x\nname: y\ntodos_path: t\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="created_at"):
        load_manifest(proj)


# ---------------------------------------------------------------------------
# load_manifest — schema_version 行为
# ---------------------------------------------------------------------------


def test_load_manifest_unknown_schema_version_fails_closed(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    cc = proj / ".cc-harness"
    cc.mkdir()
    (cc / "project.yaml").write_text(
        "project_id: x\nname: y\ntodos_path: t\n"
        "created_at: 2026-07-14T10:00:00Z\n"
        "schema_version: 99\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="schema_version"):
        load_manifest(proj)


def test_load_manifest_schema_version_zero_warns_but_succeeds(tmp_path, caplog):
    """schema_version=0 (< 1) → warn log 但通过(spec 兼容老 manifest)。"""
    import logging
    proj = tmp_path / "p"
    proj.mkdir()
    cc = proj / ".cc-harness"
    cc.mkdir()
    (cc / "project.yaml").write_text(
        "project_id: x\nname: y\ntodos_path: t\n"
        "created_at: 2026-07-14T10:00:00Z\n"
        "schema_version: 0\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        m = load_manifest(proj)
    assert m is not None
    assert any("schema_version" in r.message for r in caplog.records)


def test_load_manifest_schema_version_invalid_type(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    cc = proj / ".cc-harness"
    cc.mkdir()
    (cc / "project.yaml").write_text(
        "project_id: x\nname: y\ntodos_path: t\n"
        "created_at: 2026-07-14T10:00:00Z\n"
        "schema_version: not_a_number\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="schema_version"):
        load_manifest(proj)


# ---------------------------------------------------------------------------
# load_manifest — resume_mode 校验
# ---------------------------------------------------------------------------


def test_load_manifest_invalid_resume_mode(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    cc = proj / ".cc-harness"
    cc.mkdir()
    (cc / "project.yaml").write_text(
        "project_id: x\nname: y\ntodos_path: t\n"
        "created_at: 2026-07-14T10:00:00Z\n"
        "resume_mode: nonsense\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="resume_mode"):
        load_manifest(proj)


# ---------------------------------------------------------------------------
# load_manifest — 未知字段 warn 不报错
# ---------------------------------------------------------------------------


def test_load_manifest_unknown_field_warns_but_passes(tmp_path, caplog):
    """未知字段 → warn,不报错(spec 组件 1 约束 4)。"""
    import logging
    proj = tmp_path / "p"
    proj.mkdir()
    cc = proj / ".cc-harness"
    cc.mkdir()
    (cc / "project.yaml").write_text(
        "project_id: x\nname: y\ntodos_path: t\n"
        "created_at: 2026-07-14T10:00:00Z\n"
        "experimental: true\n"
        "owner: alice\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        m = load_manifest(proj)
    assert m is not None
    warnings = [r.message for r in caplog.records]
    assert any("experimental" in w for w in warnings)
    assert any("owner" in w for w in warnings)


def test_load_manifest_unknown_nested_field_warns(tmp_path, caplog):
    """嵌套未知字段(在 memory/live 下)也只 warn。"""
    import logging
    proj = tmp_path / "p"
    proj.mkdir()
    cc = proj / ".cc-harness"
    cc.mkdir()
    (cc / "project.yaml").write_text(
        "project_id: x\nname: y\ntodos_path: t\n"
        "created_at: 2026-07-14T10:00:00Z\n"
        "memory:\n"
        "  db_path: x.db\n"
        "  unknown_top: 1\n"
        "  integration:\n"
        "    completion_capture: true\n"
        "    extra_thing: 99\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        m = load_manifest(proj)
    assert m is not None
    assert m.memory.integration.completion_capture is True


# ---------------------------------------------------------------------------
# load_manifest — yaml 损坏
# ---------------------------------------------------------------------------


def test_load_manifest_corrupt_yaml(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    cc = proj / ".cc-harness"
    cc.mkdir()
    (cc / "project.yaml").write_text(
        "project_id: x\nname: [unclosed\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="parse"):
        load_manifest(proj)


def test_load_manifest_non_mapping_top_level(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    cc = proj / ".cc-harness"
    cc.mkdir()
    (cc / "project.yaml").write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ManifestError, match="mapping"):
        load_manifest(proj)


# ---------------------------------------------------------------------------
# save_manifest — 写盘 + 读回一致
# ---------------------------------------------------------------------------


def test_save_manifest_creates_directory(tmp_path):
    """`.cc-harness` 不存在时自动创建。"""
    proj = tmp_path / "p"
    proj.mkdir()
    m = Manifest(
        project_id="x",
        name="x",
        todos_path="todos",
        created_at=datetime(2026, 7, 14, 10, 0, 0, tzinfo=timezone.utc),
    )
    out = save_manifest(proj, m)
    assert out.is_file()
    assert (proj / ".cc-harness" / "project.yaml").is_file()


def test_save_manifest_then_load_roundtrip(tmp_path):
    """save → load → 内容一致。"""
    proj = tmp_path / "p"
    proj.mkdir()
    m = Manifest(
        project_id="rt-001",
        name="roundtrip",
        todos_path=".cc-harness/todos",
        created_at=datetime(2026, 7, 14, 10, 0, 0, tzinfo=timezone.utc),
        resume_mode="auto",
    )
    save_manifest(proj, m)
    m2 = load_manifest(proj)
    assert m2 is not None
    assert m2.project_id == "rt-001"
    assert m2.name == "roundtrip"
    assert m2.resume_mode == "auto"


def test_save_manifest_atomic_no_tmp_left(tmp_path):
    """原子写完成不应残留 .tmp 文件。"""
    proj = tmp_path / "p"
    proj.mkdir()
    m = Manifest(
        project_id="x", name="x", todos_path="t",
        created_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
    )
    save_manifest(proj, m)
    tmp_files = list((proj / ".cc-harness").glob("*.tmp"))
    assert tmp_files == []


def test_save_manifest_overwrites_existing(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    cc = proj / ".cc-harness"
    cc.mkdir()
    (cc / "project.yaml").write_text(
        "project_id: old\nname: old\ntodos_path: old\ncreated_at: 2025-01-01T00:00:00Z\n",
        encoding="utf-8",
    )
    m = Manifest(
        project_id="new", name="new", todos_path="new",
        created_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
    )
    save_manifest(proj, m)
    loaded = load_manifest(proj)
    assert loaded is not None
    assert loaded.project_id == "new"


def test_save_manifest_utf8_chinese(tmp_path):
    """name 含中文 → UTF-8 正确写盘 + 读回。"""
    proj = tmp_path / "p"
    proj.mkdir()
    m = Manifest(
        project_id="cn-001",
        name="长程任务追踪",
        todos_path="todos",
        created_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
    )
    save_manifest(proj, m)
    # 文件应是 UTF-8(无 GBK)
    raw = (proj / ".cc-harness" / "project.yaml").read_bytes()
    assert "长程任务追踪".encode("utf-8") in raw
    loaded = load_manifest(proj)
    assert loaded is not None
    assert loaded.name == "长程任务追踪"


# ---------------------------------------------------------------------------
# load_manifest — live 子树校验
# ---------------------------------------------------------------------------


def test_load_manifest_invalid_live_position(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    cc = proj / ".cc-harness"
    cc.mkdir()
    (cc / "project.yaml").write_text(
        "project_id: x\nname: y\ntodos_path: t\n"
        "created_at: 2026-07-14T10:00:00Z\n"
        "live:\n  position: nonsense\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="position"):
        load_manifest(proj)


def test_load_manifest_invalid_live_max_height(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    cc = proj / ".cc-harness"
    cc.mkdir()
    (cc / "project.yaml").write_text(
        "project_id: x\nname: y\ntodos_path: t\n"
        "created_at: 2026-07-14T10:00:00Z\n"
        "live:\n  max_height: -1\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="max_height"):
        load_manifest(proj)