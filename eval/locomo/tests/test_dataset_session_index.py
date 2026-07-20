"""build_session_index — 把 D1:3 / D2:5 这种 evidence ref 映射回 session_name。"""
from eval.locomo import dataset as ds


SAMPLE_CONV = {
    "speaker_a": "D1",
    "speaker_b": "D2",
    "session_1_date_time": "2024-01-01T10:00:00",
    "session_1": [
        {"speaker": "D1", "dia_id": 1, "text": "hi"},
        {"speaker": "D2", "dia_id": 2, "text": "hello"},
    ],
    "session_2_date_time": "2024-01-02T10:00:00",
    "session_2": [
        {"speaker": "D1", "dia_id": 3, "text": "how are you"},
        {"speaker": "D2", "dia_id": 4, "text": "fine"},
    ],
    "session_3_date_time": "2024-01-03T10:00:00",
    "session_3": [
        {"speaker": "D1", "dia_id": 5, "text": "bye"},
    ],
}


def test_first_session_first_utterance():
    idx = ds.build_session_index(SAMPLE_CONV)
    assert idx["D1:1"] == "session_1"
    assert idx["D2:2"] == "session_1"


def test_cross_session_reference():
    idx = ds.build_session_index(SAMPLE_CONV)
    # D1:3 在 session_2 里,不在 session_1
    assert idx["D1:3"] == "session_2"
    # D2:4 在 session_2
    assert idx["D2:4"] == "session_2"


def test_last_session_last_utterance():
    idx = ds.build_session_index(SAMPLE_CONV)
    assert idx["D1:5"] == "session_3"


def test_real_locomo_conversation_runs():
    """用真实 locomo10.json 的第一个对话跑一遍,build 不抛。"""
    from pathlib import Path
    repo = Path(__file__).resolve().parents[3]
    data_file = repo / "eval/locomo/data/locomo10.json"
    if not data_file.exists():
        import pytest
        pytest.skip("locomo10.json missing; run download_dataset.py first")
    import json
    conv = json.loads(data_file.read_text(encoding="utf-8"))[0]["conversation"]
    idx = ds.build_session_index(conv)
    assert isinstance(idx, dict)
    # 至少覆盖 D1:1 / D1:2 / D1:3
    assert any(k.startswith("D1:") for k in idx.keys())
    assert any(k.startswith("D2:") for k in idx.keys())