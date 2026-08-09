"""Task 1: LoCoMo runner 应使用 mode='coding' 而非 'chat'。

Bug 现状: eval/locomo/runner.py:226 写死 mode='chat' →
模型走陪聊路径,事实题常编造或拒绝回答。
修复: 改 mode='coding',触发 ReAct 工具检索 + 强制思考格式。

契约级测试:直接读 runner.py 源文件,断言 mode='coding' 出现 2 次(turn
replay + QA),且 'mode="chat"' 已不存在。比 mock 整个 _run_sample
链路更可靠。
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUNNER = REPO / "eval/locomo/runner.py"


def test_runner_uses_coding_mode_for_both_replay_and_qa():
    src = RUNNER.read_text(encoding="utf-8")

    coding_count = src.count('mode="coding"')
    chat_count = src.count('mode="chat"')

    # 期望:turn replay 路径 1 次 + QA 路径 1 次 = 2 次 coding,0 次 chat
    assert coding_count >= 2, (
        f"runner.py 应至少 2 处 mode='coding'(turn replay + QA),实际 {coding_count}"
    )
    assert chat_count == 0, (
        f"runner.py 不应再出现 mode='chat',实际 {chat_count} 次"
    )
