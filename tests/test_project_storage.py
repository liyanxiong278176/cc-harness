"""TodoStorage 测试(组件 5)。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from cc_harness.project.models import Manifest, TodoTask
from cc_harness.project.storage import StorageError, TodoStorage, _parse_frontmatter, _render_md


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def proj_with_tasks() -> Path:
    return FIXTURES_DIR / "project_with_tasks"


def _make_manifest(project_root: Path) -> Manifest:
    return Manifest(
        project_id="test",
        name="test",
        todos_path=".cc-harness/todos",
        created_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
    )


def _make_storage(tmp_path: Path) -> TodoStorage:
    proj = tmp_path / "proj"
    proj.mkdir()
    cc = proj / ".cc-harness" / "todos"
    cc.mkdir(parents=True)
    (cc / "todos.yaml").write_text("tasks: []\n", encoding="utf-8")
    return TodoStorage(proj, _make_manifest(proj))


def _make_task(
    task_id: str = "abc12345",
    title: str = "test",
    status: str = "pending",
    description: str = "",
    active_sessions: list[str] | None = None,
    depends_on: list[str] | None = None,
) -> TodoTask:
    now = datetime.now(timezone.utc)
    return TodoTask(
        id=task_id,
        title=title,
        status=status,
        description=description,
        depends_on=list(depends_on or []),
        parent_task=None,
        assigned_to=None,
        priority=None,
        labels=[],
        due_date=None,
        effort_estimate=None,
        acceptance_criteria=[],
        created_at=now,
        updated_at=now,
        active_sessions=list(active_sessions or []),
    )


# ---------------------------------------------------------------------------
# load_all — 基础
# ---------------------------------------------------------------------------


def test_load_all_empty(tmp_path):
    s = _make_storage(tmp_path)
    assert s.load_all() == []


def test_load_all_no_yaml_file(tmp_path):
    """todos.yaml 不存在 → 空列表(不报错)。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    cc = proj / ".cc-harness" / "todos"
    cc.mkdir(parents=True)
    # 注意:不写 todos.yaml
    s = TodoStorage(proj, _make_manifest(proj))
    assert s.load_all() == []


def test_save_and_load_roundtrip(tmp_path):
    s = _make_storage(tmp_path)
    task = _make_task(description="hello world")
    s.save_all([task])
    loaded = s.load_all()
    assert len(loaded) == 1
    assert loaded[0].id == "abc12345"
    assert loaded[0].title == "test"
    assert loaded[0].status == "pending"
    assert loaded[0].description == "hello world"


def test_save_multiple_tasks(tmp_path):
    s = _make_storage(tmp_path)
    tasks = [
        _make_task(task_id="aaa11111", title="a"),
        _make_task(task_id="bbb22222", title="b"),
        _make_task(task_id="ccc33333", title="c"),
    ]
    s.save_all(tasks)
    loaded = s.load_all()
    assert [t.id for t in loaded] == ["aaa11111", "bbb22222", "ccc33333"]


# ---------------------------------------------------------------------------
# md 双文件 — save 写 frontmatter + body
# ---------------------------------------------------------------------------


def test_save_creates_md_file(tmp_path):
    s = _make_storage(tmp_path)
    task = _make_task(description="md body")
    s.save_all([task])
    md_path = tmp_path / "proj" / ".cc-harness" / "todos" / "abc12345.md"
    assert md_path.is_file()
    content = md_path.read_text(encoding="utf-8")
    assert content.startswith("---\n")
    assert "id: abc12345" in content
    assert "md body" in content


def test_save_md_frontmatter_has_all_fields(tmp_path):
    """save 时 md frontmatter = T15 全字段。"""
    s = _make_storage(tmp_path)
    now = datetime(2026, 7, 14, 10, 0, 0, tzinfo=timezone.utc)
    task = TodoTask(
        id="full0001",
        title="full",
        status="in_progress",
        description="body content",
        depends_on=["dep1"],
        parent_task="p",
        assigned_to="user",
        priority="high",
        labels=["x", "y"],
        due_date=datetime(2026, 8, 1, tzinfo=timezone.utc),
        effort_estimate=2.5,
        acceptance_criteria=["AC1"],
        created_at=now,
        updated_at=now,
        active_sessions=["s1"],
    )
    s.save_all([task])
    md_text = s.load_task_md("full0001")
    assert "id: full0001" in md_text
    assert "title: full" in md_text
    assert "status: in_progress" in md_text
    assert "depends_on:" in md_text
    assert "parent_task: p" in md_text
    assert "assigned_to: user" in md_text
    assert "priority: high" in md_text
    assert "active_sessions:" in md_text


def test_save_yaml_has_all_fields(tmp_path):
    """save 时 yaml = T15 全字段(主索引)。"""
    s = _make_storage(tmp_path)
    task = _make_task(description="x")
    s.save_all([task])
    raw = (s.yaml_path).read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    assert "tasks" in data
    assert len(data["tasks"]) == 1
    entry = data["tasks"][0]
    assert entry["id"] == "abc12345"
    assert entry["title"] == "test"


# ---------------------------------------------------------------------------
# 合并策略 — md description 优先(关键 spec 决策)
# ---------------------------------------------------------------------------


def test_storage_md_description_overrides_yaml(tmp_path):
    """md 文件的 description 覆盖 yaml 的 description(spec 关键决策)。"""
    s = _make_storage(tmp_path)
    # 1. save task with description="yaml version"
    task = _make_task(description="yaml version")
    s.save_all([task])

    # 2. 用户外部编辑 md,改 description → "md version"
    md_path = tmp_path / "proj" / ".cc-harness" / "todos" / "abc12345.md"
    content = md_path.read_text(encoding="utf-8")
    content = content.replace("yaml version", "md version")
    md_path.write_text(content, encoding="utf-8")

    # 3. reload — description 应是 md 版本
    loaded = s.load_all()
    assert len(loaded) == 1
    assert loaded[0].description == "md version"


def test_storage_md_missing_falls_back_to_yaml(tmp_path, caplog):
    """yaml 引用 task 但 md 文件不存在 → warn + 用 yaml.description(不报错)。"""
    import logging
    s = _make_storage(tmp_path)
    task = _make_task(description="from yaml")
    s.save_all([task])

    # 删除 md
    md_path = tmp_path / "proj" / ".cc-harness" / "todos" / "abc12345.md"
    md_path.unlink()

    with caplog.at_level(logging.WARNING):
        loaded = s.load_all()
    assert len(loaded) == 1
    assert loaded[0].description == "from yaml"
    # warn 应包含 "md file missing"
    assert any("md file missing" in r.message for r in caplog.records)


def test_storage_orphan_md_warns_no_prune(tmp_path, caplog):
    """md 文件存在但 yaml 不引用 → warn log,绝不静默 prune。"""
    import logging
    s = _make_storage(tmp_path)
    # 写一个 yaml 不引用的 md
    md_path = tmp_path / "proj" / ".cc-harness" / "todos" / "orphan111.md"
    md_path.write_text("---\nid: orphan111\n---\n\norphan body\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        loaded = s.load_all()

    # orphan 应仍存在,未被删
    assert md_path.is_file()
    # loaded 应空(yaml 没引用)
    assert loaded == []
    assert any("orphan md" in r.message for r in caplog.records)


def test_storage_md_field_conflict_yaml_wins(tmp_path, caplog):
    """md frontmatter 的非 description 字段与 yaml 冲突 → warn + yaml 胜。"""
    import logging
    s = _make_storage(tmp_path)
    task = _make_task(description="x", status="pending")
    s.save_all([task])

    # 用户外部编辑 md,把 status 改成 done(其他字段保留)
    md_path = tmp_path / "proj" / ".cc-harness" / "todos" / "abc12345.md"
    content = md_path.read_text(encoding="utf-8")
    content = content.replace("status: pending", "status: done")
    md_path.write_text(content, encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        loaded = s.load_all()
    assert loaded[0].status == "pending"  # yaml 胜
    assert any("status" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 原子写
# ---------------------------------------------------------------------------


def test_save_atomic_no_tmp_files_left(tmp_path):
    """save_all 后不应残留 .tmp 文件。"""
    s = _make_storage(tmp_path)
    task = _make_task()
    s.save_all([task])

    tmp_files = list(s.todos_dir.glob("*.tmp"))
    assert tmp_files == []


def test_save_atomic_yaml_replace(tmp_path):
    """yaml 写用 .tmp + os.replace(防中途读到半成品)。"""
    s = _make_storage(tmp_path)
    task = _make_task(description="v1")
    s.save_all([task])
    assert yaml.safe_load(s.yaml_path.read_text(encoding="utf-8"))["tasks"][0]["description"] == "v1"

    task2 = _make_task(description="v2")
    s.save_all([task2])
    assert yaml.safe_load(s.yaml_path.read_text(encoding="utf-8"))["tasks"][0]["description"] == "v2"


def test_save_atomic_md_replace(tmp_path):
    """md 写也走原子。"""
    s = _make_storage(tmp_path)
    task = _make_task(description="v1")
    s.save_all([task])

    md_path = tmp_path / "proj" / ".cc-harness" / "todos" / "abc12345.md"
    assert "v1" in md_path.read_text(encoding="utf-8")

    task2 = _make_task(description="v2")
    s.save_all([task2])
    assert "v2" in md_path.read_text(encoding="utf-8")
    assert not list(s.todos_dir.glob("*.md.tmp"))


# ---------------------------------------------------------------------------
# active_sessions 自动 prune
# ---------------------------------------------------------------------------


def test_active_sessions_prune_at_50(tmp_path, caplog):
    """active_sessions > 50 → 截断到最近 50 + warn。"""
    import logging
    s = _make_storage(tmp_path)
    sessions = [f"session-{i:03d}" for i in range(60)]
    task = _make_task(active_sessions=sessions)

    with caplog.at_level(logging.WARNING):
        s.save_all([task])

    loaded = s.load_all()
    assert len(loaded[0].active_sessions) == 50
    # 应保留最近 50 条(= session-010 ~ session-059)
    assert loaded[0].active_sessions[0] == "session-010"
    assert loaded[0].active_sessions[-1] == "session-059"
    assert any("truncated" in r.message for r in caplog.records)


def test_active_sessions_under_50_unchanged(tmp_path):
    s = _make_storage(tmp_path)
    sessions = [f"session-{i:03d}" for i in range(30)]
    task = _make_task(active_sessions=sessions)
    s.save_all([task])
    loaded = s.load_all()
    assert loaded[0].active_sessions == sessions


# ---------------------------------------------------------------------------
# fixture 加载(project_with_tasks)
# ---------------------------------------------------------------------------


def test_load_project_with_tasks_fixture(proj_with_tasks):
    """加载 6 任务 fixture,验证 yaml 主索引内容。"""
    s = TodoStorage(proj_with_tasks, _make_manifest(proj_with_tasks))
    tasks = s.load_all()
    assert len(tasks) == 6

    # status 分布
    by_status = {t.status: 0 for t in tasks}
    for t in tasks:
        by_status[t.status] += 1
    assert by_status["done"] == 2
    assert by_status["in_progress"] == 1
    assert by_status["pending"] == 3

    # in_progress task 是 jkl01234
    in_progress = [t for t in tasks if t.status == "in_progress"]
    assert in_progress[0].id == "jkl01234"


def test_load_project_with_tasks_fixture_md_overrides_description(proj_with_tasks):
    """fixture:yaml 和 md 的 description 应该一致,但 md 优先(spec 决策)。"""
    s = TodoStorage(proj_with_tasks, _make_manifest(proj_with_tasks))
    tasks = s.load_all()
    assert len(tasks) == 6

    # 每个 task 都从对应 md 读 description
    by_id = {t.id: t for t in tasks}
    # 在 fixture 中,md body == yaml.description(我们故意设计一致)
    assert by_id["abc12345"].description == "实现一个简单的 hello world 输出脚本。"
    assert by_id["jkl01234"].description == "落 yaml + md 双文件格式。"
    assert by_id["stu22222"].description == "README + Quickstart 文档。"


# ---------------------------------------------------------------------------
# 错误恢复
# ---------------------------------------------------------------------------


def test_load_corrupt_yaml_raises_storage_error(tmp_path):
    s = _make_storage(tmp_path)
    s.yaml_path.write_text("tasks: [unclosed\n", encoding="utf-8")
    with pytest.raises(StorageError, match="parse"):
        s.load_all()


def test_load_non_mapping_top_level_raises(tmp_path):
    s = _make_storage(tmp_path)
    s.yaml_path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(StorageError, match="mapping"):
        s.load_all()


def test_load_tasks_field_not_list_raises(tmp_path):
    s = _make_storage(tmp_path)
    s.yaml_path.write_text("tasks: not_a_list\n", encoding="utf-8")
    with pytest.raises(StorageError, match="list"):
        s.load_all()


# ---------------------------------------------------------------------------
# description 超长截断
# ---------------------------------------------------------------------------


def test_save_truncates_oversized_description(tmp_path, caplog):
    """description > 50KB → warn + 截断。"""
    import logging
    s = _make_storage(tmp_path)
    huge = "a" * (60 * 1024)  # 60KB
    task = _make_task(description=huge)

    with caplog.at_level(logging.WARNING):
        s.save_all([task])

    md_text = s.load_task_md("abc12345")
    # 截断后的 md body 长度应 < 60KB
    body_start = md_text.find("---\n\n") + 4
    body_len = len(md_text[body_start:].encode("utf-8"))
    assert body_len <= 50 * 1024
    assert any("exceeds" in r.message or "truncating" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# async 接口
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_load_save_roundtrip(tmp_path):
    s = _make_storage(tmp_path)
    task = _make_task(description="async roundtrip")
    await s.asave_all([task])
    loaded = await s.aload_all()
    assert len(loaded) == 1
    assert loaded[0].description == "async roundtrip"


# ---------------------------------------------------------------------------
# 私有 helpers
# ---------------------------------------------------------------------------


def test_parse_frontmatter_with_fm():
    fm, body = _parse_frontmatter("---\nid: x\ntitle: t\n---\n\nbody here\n")
    assert fm == {"id": "x", "title": "t"}
    assert body == "body here"


def test_parse_frontmatter_no_fm():
    fm, body = _parse_frontmatter("just text\nno frontmatter\n")
    assert fm is None
    assert "just text" in body


def test_parse_frontmatter_unclosed():
    """未闭合的 frontmatter → 视为 body 兜底。"""
    fm, body = _parse_frontmatter("---\nid: x\nno closer\n")
    assert fm is None


def test_render_md_includes_body():
    t = _make_task(description="hello")
    out = _render_md(t)
    assert out.startswith("---\n")
    assert "hello\n" in out


def test_render_md_chinese_description():
    t = _make_task(description="中文描述 测试")
    out = _render_md(t)
    assert "中文描述 测试" in out