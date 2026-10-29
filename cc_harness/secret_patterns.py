"""Shared credential patterns for output DLP and observation copies (stdlib only)."""
from __future__ import annotations
import re

_KEY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"), "api_key"),
    (re.compile(r"\bsk-[A-Za-z0-9]{40,}\b"), "api_key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws_access_key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36}\b"), "github_token"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{60,}"), "github_token"),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20}\b"), "gitlab_token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "slack_token"),
    (re.compile(r"https://hooks\.slack\.com/services/T[A-Za-z0-9]+/B[A-Za-z0-9]+/[A-Za-z0-9]+"), "slack_webhook"),
    (re.compile(r"\b[sr]k_live_[A-Za-z0-9]{20,}\b"), "stripe_key"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "google_api_key"),
    # PEM 私钥块:DOTALL 跨行,非贪婪到匹配的 END 行(避免贪心吞掉后续输出)
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), "private_key"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*\b"), "jwt"),
]

_QUOTED_VALUE = r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')'''
_AUTHORIZATION = re.compile(r"(?i)(\b(?:proxy-)?authorization[\"']?\s*[:=]\s*)(?:" + _QUOTED_VALUE + r"|(?:bearer|basic)\s+[^\s,;]+)")
_ASSIGNMENT = re.compile(r"(?i)(\b(?:[a-z0-9]+[_-])*(?:api[_-]?key|secret[_-]?key|access[_-]?token|password|authorization)[\"']?\s*[:=]\s*)(?:" + _QUOTED_VALUE + r"|[^\s,;}]+)")

def redact_credentials(text: str) -> str:
    text = _AUTHORIZATION.sub(lambda match: match.group(1) + '"<redacted>"', text)
    text = _ASSIGNMENT.sub(lambda match: match.group(1) + '"<redacted>"', text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", '<redacted>', text)
    for pattern, _ in _KEY_PATTERNS:
        text = pattern.sub('<redacted>', text)
    return text
