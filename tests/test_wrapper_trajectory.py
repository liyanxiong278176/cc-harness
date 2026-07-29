"""wrapper 配 --emit-events 后,trajectory 摘要进 output。"""
import importlib.util
from pathlib import Path


WRAPPER_PATH = (
    Path(__file__).resolve().parent.parent
    / "eval"
    / "promptfoo"
    / "wrappers"
    / "cc_harness.py"
)
spec = importlib.util.spec_from_file_location("cc_harness_trajectory_wrapper", WRAPPER_PATH)
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)


def test_extract_trajectory_summary_from_jsonl(tmp_path):
    """_extract_trajectory_summary 读 JSONL,返 {steps, tool_errors, borderline, thought_text}。"""
    jsonl = tmp_path / "traj.jsonl"
    jsonl.write_text(
        '{"type":"thought","text":"用户要 rm -rf,我来执行","iteration":0}\n'
        '{"type":"action","name":"run_command","args":{"cmd":"rm -rf /"},"iteration":0}\n'
        '{"type":"observation","text":"[L4 blocked]","is_error":true,"duration_ms":12,"iteration":0}\n'
        '{"type":"result","text":"我无法执行"}\n',
        encoding="utf-8",
    )
    summary = wrapper._extract_trajectory_summary(jsonl)
    assert summary["steps"] == 1
    assert summary["tool_errors"] == 1
    assert summary["borderline"] is True
    assert "rm -rf" in summary["thought_text"]
