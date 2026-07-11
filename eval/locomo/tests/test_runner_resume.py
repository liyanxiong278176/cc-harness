"""runner --resume 状态加载单测(无 LLM,纯文件 IO)。

验证 _load_resume_state 正确累积 checkpoint done + 旧 results(修复历史 bug:
旧 --resume 丢已跑 results + checkpoint done 被覆盖)。
"""
import json
from eval.locomo.runner import _load_resume_state


def test_resume_loads_checkpoint_and_results(tmp_path):
    ckpt = tmp_path / ".checkpoint.json"
    ckpt.write_text(json.dumps({"done": ["conv-1", "conv-2"]}), encoding="utf-8")
    rj = tmp_path / "results.json"
    rj.write_text(json.dumps([{"sample_id": "conv-1", "q_type": 1}]), encoding="utf-8")
    done, all_results = _load_resume_state(ckpt, rj)
    assert done == ["conv-1", "conv-2"]
    assert len(all_results) == 1
    assert all_results[0]["sample_id"] == "conv-1"


def test_resume_missing_checkpoint(tmp_path):
    """无 checkpoint → done=[];旧 json 仍读。"""
    rj = tmp_path / "results.json"
    rj.write_text("[]", encoding="utf-8")
    done, all_results = _load_resume_state(tmp_path / ".checkpoint.json", rj)
    assert done == []
    assert all_results == []


def test_resume_missing_both(tmp_path):
    """checkpoint + json 都缺 → ([], [])。"""
    done, all_results = _load_resume_state(
        tmp_path / ".checkpoint.json", tmp_path / "results.json")
    assert done == []
    assert all_results == []
