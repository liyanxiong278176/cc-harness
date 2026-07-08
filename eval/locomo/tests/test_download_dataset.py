import json
from pathlib import Path
import pytest
from eval.locomo.download_dataset import verify_dataset

def test_verify_dataset_accepts_list(tmp_path):
    fake = tmp_path / "locomo10.json"
    fake.write_text(json.dumps([
        {"sample_id": "s1", "conversation": {"session_1": [{"speaker": "A", "dia_id": "d1", "text": "hi"}]},
         "qa": [{"question": "q?", "answer": "a", "category": "test", "evidence": ["d1"]}]}
    ]))
    samples = verify_dataset(fake)
    assert len(samples) == 1
    assert samples[0]["sample_id"] == "s1"

def test_verify_dataset_accepts_single_dict(tmp_path):
    fake = tmp_path / "locomo10.json"
    fake.write_text(json.dumps(
        {"sample_id": "x", "conversation": {}, "qa": [{"question": "q", "answer": "a", "category": "c", "evidence": []}]}
    ))
    samples = verify_dataset(fake)
    assert len(samples) == 1

def test_verify_dataset_rejects_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="locomo data not found"):
        verify_dataset(tmp_path / "nonexistent.json")

def test_verify_dataset_rejects_no_qa(tmp_path):
    fake = tmp_path / "locomo10.json"
    fake.write_text(json.dumps([{"sample_id": "x", "conversation": {}, "qa": []}]))
    with pytest.raises(ValueError, match="no QA pairs"):
        verify_dataset(fake)
