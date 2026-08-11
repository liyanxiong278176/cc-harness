import json
from pathlib import Path

from eval.context_memory.adapters.longmemeval import (
    _iter_json_array_records,
    _read_record,
    _record_index,
)


def test_longmemeval_json_array_is_indexed_without_retaining_records(tmp_path: Path) -> None:
    path = tmp_path / "longmemeval.json"
    records = [
        {
            "question_id": "q-1",
            "question_type": "multi-hop",
            "haystack_sessions": [[{"role": "user", "content": "brace } [ text"}]],
        },
        {
            "question_id": "q-2",
            "question_type": "temporal",
            "haystack_sessions": [[{"role": "assistant", "content": "中文\\n\\\"quoted\\\""}]],
        },
    ]
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")

    references = _record_index(path)
    assert [(item.question_id, item.question_type) for item in references] == [
        ("q-1", "multi-hop"),
        ("q-2", "temporal"),
    ]
    streamed = list(_iter_json_array_records(path))
    assert [item[2]["question_id"] for item in streamed] == ["q-1", "q-2"]
    assert _read_record(path, references[1])["question_id"] == "q-2"
