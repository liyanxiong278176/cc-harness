"""Shared permission-mode vocabulary for CLI, TUI, WebUI, and Durable Runs.

The UI labels intentionally mirror the three modes exposed by the mature
coding-agent clients.  A mode controls *approval prompts*; it never weakens
the PolicyEngine hard boundaries (workspace containment, sensitive paths, and
other security denials).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .run_model import EffectClass


PERMISSION_MODES = ("default", "auto-edit", "bypass-prompts")


@dataclass(frozen=True)
class PermissionModeSpec:
    """User-facing copy and a short explanation for one permission mode."""

    mode: str
    label: str
    description: str


PERMISSION_MODE_SPECS: dict[str, PermissionModeSpec] = {
    "default": PermissionModeSpec(
        "default",
        "请求批准",
        "编辑外部文件和使用互联网时始终询问",
    ),
    "auto-edit": PermissionModeSpec(
        "auto-edit",
        "帮我批准",
        "仅对检测到的风险操作请求批准",
    ),
    "bypass-prompts": PermissionModeSpec(
        "bypass-prompts",
        "完全访问权限",
        "可不受限制地访问互联网和你电脑上的任何文件",
    ),
}


def normalize_permission_mode(value: Any, *, default: str = "default") -> str:
    """Validate and normalize a mode from config/API/CLI input."""

    fallback = str(default or "default").strip().lower()
    if fallback not in PERMISSION_MODES:
        fallback = "default"
    candidate = str(value or fallback).strip().lower()
    if candidate not in PERMISSION_MODES:
        raise ValueError(
            "permission mode must be one of: " + ", ".join(PERMISSION_MODES)
        )
    return candidate


def _effect_value(effect_class: EffectClass | str) -> str:
    if isinstance(effect_class, EffectClass):
        return effect_class.value
    return str(effect_class or EffectClass.UNKNOWN.value).strip().lower()


def requires_approval_for_mode(
    effect_class: EffectClass | str,
    mode: str,
    *,
    declared: bool = False,
) -> bool:
    """Return whether an action should pause at the durable approval gate.

    ``default`` keeps the conservative Durable Runtime behavior: every
    non-read-only action requires explicit approval. ``auto-edit`` allows
    declared workspace mutations while retaining approval for external,
    unknown, or explicitly-declared risky actions. ``bypass-prompts`` skips
    ordinary approval gates entirely. The caller still runs PolicyEngine, so
    hard-deny security decisions remain effective in every mode.
    """

    normalized = normalize_permission_mode(mode)
    effect = _effect_value(effect_class)
    if normalized == "bypass-prompts":
        return False
    if effect == EffectClass.READ_ONLY.value:
        return bool(declared)
    if normalized == "auto-edit" and effect == EffectClass.WORKSPACE_MUTATION.value:
        return False
    return True


__all__ = [
    "PERMISSION_MODES",
    "PERMISSION_MODE_SPECS",
    "PermissionModeSpec",
    "normalize_permission_mode",
    "requires_approval_for_mode",
]
