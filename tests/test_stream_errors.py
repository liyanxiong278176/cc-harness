from cc_harness.stream_errors import ProviderStreamError, classify_stream_error


def test_partial_provider_transport_error_is_not_retryable():
    error = ProviderStreamError(
        RuntimeError("read timeout"), attempts=1, partial_output=True
    )
    details = classify_stream_error(error)
    assert details.code == "provider_transport_error"
    assert details.phase == "connection"
    assert details.partial_output is True
    assert details.retryable is False
    assert details.to_dict()["attempt"] == 1


def test_sse_projection_error_is_actionable_without_raw_exception_text():
    details = classify_stream_error(
        RuntimeError("snapshot digest mismatch; secret=should-not-leak"),
        phase="sse",
        attempt=3,
    )
    payload = details.to_dict()
    assert payload["phase"] == "sse"
    assert payload["code"] == "persistence_error"
    assert payload["attempt"] == 3
    assert "secret" not in str(payload)
