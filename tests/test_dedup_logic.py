"""Tests for dedup logic in curate_attacks.py"""
import numpy as np
import pytest
from unittest.mock import patch, MagicMock

from eval.promptfoo.tools import curate_attacks
from eval.promptfoo.tools.curate_attacks import AttackCandidate


def _fake_embed_response(vectors: list[list[float]]):
    """Build a mock requests response with SiliconFlow embeddings shape."""
    mock = MagicMock()
    mock.json.return_value = {
        "data": [{"embedding": v} for v in vectors]
    }
    mock.raise_for_status = MagicMock()
    return mock


def test_embed_calls_siliconflow(monkeypatch):
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "test-key")
    monkeypatch.setenv("EMBEDDING_MODEL", "BAAI/bge-m3")

    fake_vectors = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    mock_response = _fake_embed_response(fake_vectors)

    with patch("eval.promptfoo.tools.curate_attacks.requests.post",
               return_value=mock_response) as mock_post:
        result = curate_attacks.embed(["text1", "text2"])

    assert result.shape == (2, 3)
    assert mock_post.called
    call_kwargs = mock_post.call_args.kwargs
    assert call_kwargs["json"]["input"] == ["text1", "text2"]
    assert call_kwargs["json"]["model"] == "BAAI/bge-m3"
    assert "Bearer test-key" in call_kwargs["headers"]["Authorization"]


def test_compute_similarities_returns_max_per_candidate():
    """Each candidate gets the max cosine sim vs any static attack."""
    candidates = [
        AttackCandidate("c1", "p1", 0.25, "", "x", 0.0),
    ]
    static = [
        {"vars": {"prompt": "s1"}},
        {"vars": {"prompt": "s2"}},
    ]
    # Manually set embeddings to make similarity deterministic
    static_embs = np.array([[1.0, 0.0], [0.0, 1.0]])
    cand_embs = np.array([[0.7, 0.7]])  # sim to first = 0.7/sqrt(0.98) ≈ 0.707

    with patch.object(curate_attacks, "embed") as mock_embed:
        mock_embed.side_effect = [static_embs, cand_embs]
        sims = curate_attacks.compute_similarities(candidates, static)

    assert len(sims) == 1
    assert 0.7 < sims[0] < 0.71


def test_compute_similarities_aborts_on_embed_error():
    candidates = [AttackCandidate("c1", "p1", 0.25, "", "x")]
    static = [{"vars": {"prompt": "s1"}}]

    with patch.object(curate_attacks, "embed",
                      side_effect=RuntimeError("API down")):
        with pytest.raises(RuntimeError, match="API down"):
            curate_attacks.compute_similarities(candidates, static)


def test_compute_similarities_raises_on_shape_mismatch():
    """If embed() returns mismatched dims (e.g. model changed between calls),
    raise a clear ValueError instead of producing garbage similarities."""
    candidates = [AttackCandidate("c1", "p1", 0.25, "", "x")]
    static = [{"vars": {"prompt": "s1"}}]

    # 3-dim vs 5-dim — np.dot would silently produce wrong values
    static_embs = np.array([[1.0, 0.0, 0.0]])
    cand_embs = np.array([[1.0, 0.0, 0.0, 0.0, 0.0]])

    with patch.object(curate_attacks, "embed") as mock_embed:
        mock_embed.side_effect = [static_embs, cand_embs]
        with pytest.raises(ValueError, match="dimension"):
            curate_attacks.compute_similarities(candidates, static)