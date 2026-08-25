"""Shared classification helpers for the non-scoring verifier smoke gate.

This module intentionally has no dependency on the ``eval`` package. Harbor
imports the agent from the host-side project path before the task container is
started, so importing the smoke classifier must not pull in cc-harness's
SQLite/runtime dependencies.
"""

from __future__ import annotations


def verifier_smoke_environment_error(output: str) -> str | None:
    """Return an environment error found in a verifier smoke log.

    A verifier is allowed to return a normal non-zero reward while the task
    workspace is still in its baseline state. Only failures that prevent the
    verifier from starting or reaching its runtime are considered a preflight
    failure here. The formal Harbor verifier remains the source of the
    official score.
    """

    normalized = output.casefold()
    if "cc_harness_verifier_smoke_timeout" in normalized:
        return "verifier smoke timed out"
    if "cc_harness_verifier_smoke_oserror" in normalized:
        return "verifier smoke could not start"
    markers = (
        "command not found",
        "not found",
        "no such file or directory",
        "permission denied",
        "modulenotfounderror",
        "no module named",
        "uvx: not found",
        "could not resolve",
        "temporary failure in name resolution",
        "ssl_error",
        "bad gateway",
        "connection reset",
        "connection refused",
        "network is unreachable",
        "apt-get",
        "e: unable to locate",
        "qemu: not found",
        "docker: not found",
        "address already in use",
    )
    for marker in markers:
        if marker in normalized:
            return f"verifier smoke reported environment marker: {marker}"
    return None
