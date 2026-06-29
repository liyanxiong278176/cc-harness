"""Convert promptfoo eval/redteam result JSON(s) into a readable Markdown report.

Usage:
    python tools/report_to_md.py eval-results.json [owasp-results.json ...] [-o report.md]
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

# --- Classification (mirrors spec table; single source of truth) ---
PLUGIN_CATEGORY = {
    "shell-injection": "沙箱", "sql-injection": "沙箱", "ssrf": "沙箱",
    "pii:api-db": "沙箱", "pii:direct": "沙箱", "pii:session": "沙箱", "pii:social": "沙箱",
    "bfla": "权限", "bola": "权限", "rbac": "权限", "excessive-agency": "权限",
    "prompt-extraction": "提示词注入", "overreliance": "提示词注入",
    "hallucination": "提示词注入", "ascii-smuggling": "提示词注入",
    "indirect-prompt-injection": "提示词注入",
}
STATIC_CATEGORY = {
    "shell-injection": "沙箱", "credential-exfil": "沙箱",
    "self-modification": "权限", "excessive-agency": "权限", "fs-overreach": "权限",
    "hijacking": "提示词注入", "prompt-extraction": "提示词注入", "overreliance": "提示词注入",
}


def _metadata(result: dict) -> dict:
    return (result.get("metadata") or result.get("testCase", {}).get("metadata") or {})


def classify_issue(result: dict) -> str:
    """沙箱 / 权限 / 提示词注入 / 其它. Does NOT consider infra failure
    (caller checks detect_infra_failure first). pluginId wins over category."""
    md = _metadata(result)
    plugin = md.get("pluginId")
    if plugin:
        if plugin.startswith("harmful"):
            return "其它"
        return PLUGIN_CATEGORY.get(plugin, "其它")
    cat = md.get("category")
    return STATIC_CATEGORY.get(cat, "其它") if cat else "其它"
