"""Task 3: locomo dataset 应能从 sample 抽出 event_date(锚定日期)。

Bug 现状: locomo 评测中 type=2 (时间) 题的 gold 是绝对日期(如 7 May 2023)
但对话里只写 "yesterday" / "last week" 这类相对时间。模型无锚定日期不可推算。

locomo 原始数据中 conversation.session_N 的每条消息有 timestamp 字段
(ISO 字符串,2023-05-08T00:00:00 这种)。取 session_1 第一条 timestamp
作为锚定日期。
"""
from pathlib import Path
import json

REPO = Path(__file__).resolve().parents[1]


def test_infer_event_date_returns_iso_date_for_real_sample():
    """conv-26 的 session_1 第一条 timestamp 应返回 ISO 字符串。"""
    from eval.locomo.dataset import infer_event_date

    data = json.loads(
        (REPO / "eval/locomo/data/locomo10.json").read_text(encoding="utf-8")
    )
    conv26 = next(s for s in data if s.get("sample_id") == "conv-26")

    d = infer_event_date(conv26)

    assert d is not None, "infer_event_date 应返非 None"
    # 真实 conv-26 session_1 第一条 timestamp 是 2023-05-08
    assert "2023-05-08" in d, (
        f"conv-26 锚定日期应含 2023-05-08,实际 {d!r}"
    )


def test_infer_event_date_handles_missing_session1():
    """空 sample → 返 None,不抛。"""
    from eval.locomo.dataset import infer_event_date

    assert infer_event_date({}) is None
    assert infer_event_date({"conversation": {}}) is None
    assert infer_event_date({"conversation": {"session_1": []}}) is None
