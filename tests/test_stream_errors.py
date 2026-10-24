import httpx
import pytest
from openai import APIStatusError

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


@pytest.mark.parametrize(
    "status,body",
    [
        (402, {"error": {"type": "insufficient_balance_error"}}),
        (429, {"code": "insufficient_quota"}),
        (429, {"error": {"type": "billing_hard_limit_reached"}}),
    ],
)
def test_billing_failure_preserves_progress_without_inviting_automatic_retry(status, body):
    response = httpx.Response(
        status,
        request=httpx.Request("POST", "https://provider.invalid/chat"),
        headers={"x-request-id": "billing-request"},
    )
    cause = APIStatusError("secret=must-not-leak", response=response, body=body)
    details = classify_stream_error(
        ProviderStreamError(cause, attempts=2, partial_output=True)
    )
    assert details.code == "provider_billing_error"
    assert details.retryable is False
    assert details.retry_after is None
    assert details.partial_output is True
    assert details.attempt == 2
    assert details.request_id == "billing-request"
    assert "检查点" in details.next_action
    assert "secret" not in str(details.to_dict())


def test_transient_rate_limit_keeps_retry_contract():
    cause = APIStatusError(
        "rate limited",
        response=httpx.Response(429, request=httpx.Request("POST", "https://provider.invalid/chat")),
        body={"error": {"code": "rate_limit_exceeded"}},
    )
    details = classify_stream_error(cause)
    assert details.code == "provider_transport_error"
    assert details.retryable is True
