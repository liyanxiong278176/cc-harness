"""Stable capability-tool bundle selection for provider cache reuse."""
from __future__ import annotations

import hashlib
import json
from typing import Iterable


DEFAULT_BUNDLES = frozenset({"core"})


def parse_tool_bundles(raw: str | Iterable[str] | None) -> frozenset[str]:
    if raw is None:
        return DEFAULT_BUNDLES
    if isinstance(raw, str):
        values = {item.strip().lower() for item in raw.split(",") if item.strip()}
    else:
        values = {str(item).strip().lower() for item in raw if str(item).strip()}
    if not values:
        return DEFAULT_BUNDLES
    if "all" in values:
        return frozenset({"core", "mcp", "web", "domain"})
    values.add("core")
    return frozenset(values)


def _declared_bundles(spec: dict) -> set[str]:
    function = spec.get("function") or {}
    metadata = function.get("x-cc-harness-capability") or {}
    raw = metadata.get("bundles") or metadata.get("bundle") or ""
    if isinstance(raw, str):
        return {item.strip().lower() for item in raw.split(",") if item.strip()}
    if isinstance(raw, (list, tuple, set)):
        return {str(item).strip().lower() for item in raw if str(item).strip()}
    return set()


def is_mcp_bundle_enabled(spec: dict, bundles: frozenset[str]) -> bool:
    """Return whether an MCP schema belongs to an enabled optional bundle."""
    if "mcp" in bundles or "all" in bundles:
        return True
    declared = _declared_bundles(spec)
    if declared & bundles:
        return True
    name = str((spec.get("function") or {}).get("name") or "").lower()
    if "web" in bundles and any(token in name for token in ("http", "web", "browser", "search", "fetch")):
        return True
    # A project-specific domain bundle can be declared as ``domain:<name>``;
    # the schema must opt into that exact name to avoid guessing side effects.
    for bundle in bundles:
        if bundle.startswith("domain:") and bundle[7:] in declared:
            return True
    return False


def select_tool_specs(
    specs: list[dict],
    bundles: frozenset[str] | None,
    *,
    native_names: set[str] | None = None,
) -> list[dict]:
    """Keep native core tools and only explicitly enabled MCP bundles."""
    if bundles is None:
        return list(specs)
    native_names = native_names or set()
    return [
        spec for spec in specs
        if str((spec.get("function") or {}).get("name") or "") in native_names
        or is_mcp_bundle_enabled(spec, bundles)
    ]


def bundle_digest(specs: list[dict], bundles: frozenset[str] | None) -> str:
    if bundles is None:
        return "legacy-all"
    names = sorted(
        str((spec.get("function") or {}).get("name") or "")
        for spec in specs
    )
    payload = json.dumps({"bundles": sorted(bundles), "tools": names}, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "DEFAULT_BUNDLES",
    "parse_tool_bundles",
    "select_tool_specs",
    "bundle_digest",
    "is_mcp_bundle_enabled",
]
