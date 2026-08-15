from __future__ import annotations

import json

from cc_harness.memory.temporal import extract_temporal_metadata, temporal_relevance


def test_temporal_metadata_normalizes_named_and_month_year_dates() -> None:
    metadata = extract_temporal_metadata(
        "The appointment was on 7 May 2023 and the project started in April 2023."
    )
    assert "2023-05-07" in metadata["anchors"]
    assert "2023-04" in metadata["anchors"]
    assert "7 May 2023" in metadata["expressions"]
    assert "April 2023" in metadata["expressions"]


def test_temporal_metadata_preserves_relations_and_session_anchor() -> None:
    metadata = extract_temporal_metadata(
        "A few days before 2024-03-20, the review happened.",
        session_timestamp="2024-03-20",
    )
    assert "2024-03-20" in metadata["anchors"]
    assert "few days before" in metadata["relations"]
    assert metadata["session_anchor"] == "2024-03-20"


def test_temporal_relevance_uses_stored_provenance() -> None:
    provenance = json.dumps(
        {
            "temporal": {
                "anchors": ["2024-03-19"],
                "relations": [],
                "expressions": ["March 19, 2024"],
            }
        }
    )
    assert temporal_relevance("What happened before 2024-03-20?", "review", provenance) > 0
