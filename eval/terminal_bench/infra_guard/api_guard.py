"""Provider endpoint/key presence guard without secret disclosure."""

from __future__ import annotations

import os
import urllib.parse
from dotenv import dotenv_values

from .base import GuardContext, GuardResult, RecoveryResult


class ApiGuard:
    name = "api"

    def pre_check(self, context: GuardContext) -> GuardResult:
        values = {
            key: value
            for key, value in dotenv_values(context.project_root / ".env").items()
            if value is not None
        }
        base = os.environ.get("OPENAI_BASE_URL") or values.get("OPENAI_BASE_URL")
        key = os.environ.get("OPENAI_API_KEY") or values.get("OPENAI_API_KEY")
        parsed = urllib.parse.urlparse(base or "")
        ready = bool(base and key and parsed.scheme in {"http", "https"} and parsed.hostname)
        return GuardResult(self.name, "pre_check", ready=ready, blocking=True, details={"base_url_present": bool(base), "api_key_present": bool(key), "scheme": parsed.scheme or None, "host": parsed.hostname}, error=None if ready else "OPENAI_BASE_URL/API key is not configured")

    def prevent(self, context: GuardContext) -> GuardResult:
        del context
        return GuardResult(self.name, "prevent", details={"secrets_logged": False, "retry_policy": "provider-controlled"})

    def recover(self, context: GuardContext, error_text: str) -> RecoveryResult:
        del context
        text = str(error_text or "").casefold()
        if any(marker in text for marker in ("402", "insufficient balance", "invalid api key", "401")):
            return RecoveryResult(self.name, recovered=False, details={"pause_for_user": True})
        return RecoveryResult(self.name, recovered=True, details={"bounded_transport_retry": any(marker in text for marker in ("429", "500", "502", "503", "504", "timeout"))})

    def post_cleanup(self, context: GuardContext) -> dict[str, object]:
        del context
        return {"ok": True, "secrets_logged": False}
