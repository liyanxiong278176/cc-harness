"""--emit-events flag: run_turn 的事件 dict 落盘成 JSONL。"""
import json

import pytest

from cc_harness.web.events import ThoughtEvent


@pytest.mark.asyncio
async def test_jsonl_writer_emitter_appends_lines(tmp_path):
    """构造的 emitter 收 dict → 文件追加 JSONL 行。"""
    from cc_harness.repl import make_jsonl_emitter

    out = tmp_path / "traj.jsonl"
    emitter = make_jsonl_emitter(out)
    await emitter(ThoughtEvent(text="hello", iteration=0).model_dump())
    await emitter(ThoughtEvent(text="world", iteration=1).model_dump())

    lines = out.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["text"] == "hello"
    assert json.loads(lines[1])["iteration"] == 1


def test_jsonl_writer_emitter_creates_parent_directories(tmp_path):
    """构造 emitter 时创建目标文件的父目录。"""
    from cc_harness.repl import make_jsonl_emitter

    out = tmp_path / "nested" / "traj.jsonl"
    make_jsonl_emitter(out)

    assert out.parent.is_dir()


def test_main_argparse_has_emit_events():
    """main.py argparse 接受 --emit-events。"""
    import main

    parser = main._build_arg_parser()
    ns = parser.parse_args(["--emit-events", "/tmp/x.jsonl"])
    assert ns.emit_events == "/tmp/x.jsonl"
