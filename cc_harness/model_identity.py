"""Canonical model identities for OpenAI-compatible provider responses.

Providers sometimes return a deployment alias in the response ``model``
field even though the request used a pinned public model id.  The alias is a
provider observation and must remain visible in bounded provider metadata,
but the durable run identity needs one deterministic value for protocol
parity checks.  Keep this mapping deliberately small: unknown model changes
must still fail closed instead of being guessed away.
"""

from __future__ import annotations


# This is the only alias observed for the pinned Terminal-Bench DeepSeek
# contract.  Do not broaden this table without a provider-side confirmation;
# accepting arbitrary prefixes would hide a real model/configuration drift.
_KNOWN_MODEL_ALIASES: dict[tuple[str, str], str] = {
    ("deepseek-flash", "deepseek-v4-flash"): "deepseek-v4-flash",
}


def canonical_model_identity(
    reported_model: str | None,
    requested_model: str | None,
) -> str | None:
    """Return the parity identity for a provider-reported model id.

    ``reported_model`` is returned unchanged for unknown values so callers
    can continue to reject unexpected drift.  Only the exact, documented
    DeepSeek deployment alias above is accepted as equivalent to the pinned
    request identity.
    """

    if not isinstance(reported_model, str) or not reported_model.strip():
        return None
    reported = reported_model.strip()
    requested = str(requested_model or "").strip()
    if not requested or reported == requested:
        return reported
    return _KNOWN_MODEL_ALIASES.get((reported, requested), reported)


__all__ = ["canonical_model_identity"]
