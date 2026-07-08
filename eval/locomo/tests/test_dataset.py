from eval.locomo.dataset import parse_sample, iter_turns, iter_qa, Turn, QA

def test_parse_sample_basic():
    raw = {
        "sample_id": "s1",
        "conversation": {
            "session_1": [
                {"speaker": "A", "dia_id": "d1", "text": "hello"},
                {"speaker": "B", "dia_id": "d2", "text": "hi"},
            ],
            "session_2": [
                {"speaker": "A", "dia_id": "d3", "text": "what's up?"},
            ],
        },
        "qa": [{"question": "q1", "answer": "a1", "category": "test", "evidence": ["d1"]}],
    }
    sample = parse_sample(raw)
    assert sample.sample_id == "s1"
    turns = list(iter_turns(sample))
    assert len(turns) == 3
    assert isinstance(turns[0], Turn)
    assert turns[0].text == "hello"
    assert turns[0].session == "session_1"
    assert turns[2].session == "session_2"
    qa = list(iter_qa(sample))
    assert len(qa) == 1
    assert isinstance(qa[0], QA)
    assert qa[0].question == "q1"

def test_iter_turns_handles_missing_sessions():
    raw = {"sample_id": "x", "conversation": {}, "qa": [{"question": "q", "answer": "a", "category": "c", "evidence": []}]}
    sample = parse_sample(raw)
    assert list(iter_turns(sample)) == []

def test_iter_turns_skips_malformed_entries():
    raw = {
        "sample_id": "x",
        "conversation": {
            "session_1": [
                {"speaker": "A", "dia_id": "d1", "text": "ok"},
                {"bad": "entry"},
            ],
        },
        "qa": [{"question": "q", "answer": "a", "category": "c", "evidence": []}],
    }
    sample = parse_sample(raw)
    turns = list(iter_turns(sample))
    assert len(turns) == 1
    assert turns[0].text == "ok"

def test_iter_qa_returns_empty_for_no_qa():
    raw = {"sample_id": "x", "conversation": {}, "qa": []}
    sample = parse_sample(raw)
    assert list(iter_qa(sample)) == []

def test_iter_qa_skips_non_dict():
    raw = {
        "sample_id": "x",
        "conversation": {},
        "qa": ["not a dict", {"question": "q", "answer": "a", "category": "c", "evidence": []}],
    }
    sample = parse_sample(raw)
    qa = list(iter_qa(sample))
    assert len(qa) == 1
