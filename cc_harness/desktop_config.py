"""Workspace model configuration used by the desktop sidecar.

The desktop UI may edit the same ``.env`` file used by the CLI, but it must
never receive the raw API key back over the bridge.  Keeping this small helper
separate makes that boundary easy to test and audit.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from .config import ConfigError


_KEY_NAMES = ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL")
_ENV_ASSIGNMENT = re.compile(
    r"^(?P<prefix>\s*(?:export\s+)?)(?P<key>[A-Z][A-Z0-9_]*)\s*=.*?(?P<newline>\r?\n)?$"
)


def _encode_env_value(value: str) -> str:
    """Quote only values that would be ambiguous to python-dotenv."""

    if not re.search(r"[\s#\"']", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _values(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        parsed = dotenv_values(path)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"invalid workspace .env at {path}: {exc}") from exc
    return {key: str(value) for key, value in parsed.items() if value is not None}


def _mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 4:
        return "****"
    # Keep the mask ASCII because the sidecar may inherit the Windows GBK
    # console encoding when stdout is attached to a packaged Tauri process.
    return f"****{value[-4:]}"


def read_workspace_config(project_root: Path) -> dict[str, Any]:
    """Return safe, displayable configuration metadata for one workspace."""

    root = Path(project_root).expanduser().resolve()
    env_path = root / ".env"
    values = _values(env_path)

    # Process environment is only a fallback.  A desktop user can therefore
    # configure a project-local .env without exposing credentials in the UI.
    base_url = values.get("OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL", "")
    model = values.get("OPENAI_MODEL") or os.getenv("OPENAI_MODEL", "")
    api_key = values.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY", "")
    return {
        "workspace": str(root),
        "env_path": str(env_path),
        "base_url": base_url,
        "model": model,
        "has_api_key": bool(api_key),
        "api_key_masked": _mask_secret(api_key),
        "configured": bool(base_url and model and api_key),
    }


def _update_env_text(existing: str, updates: dict[str, str]) -> str:
    """Update known keys while preserving comments and unrelated settings."""

    lines = existing.splitlines(keepends=True)
    found: set[str] = set()
    output: list[str] = []
    for line in lines:
        match = _ENV_ASSIGNMENT.match(line)
        key = match.group("key") if match else None
        if key not in updates:
            output.append(line)
            continue
        newline = match.group("newline") or ("\r\n" if "\r\n" in existing else "\n")
        output.append(f"{match.group('prefix')}{key}={_encode_env_value(updates[key])}{newline}")
        found.add(key)

    if output and not output[-1].endswith(("\n", "\r")):
        output.append("\n")
    for key, value in updates.items():
        if key not in found:
            output.append(f"{key}={_encode_env_value(value)}\n")
    return "".join(output)


def save_workspace_config(
    project_root: Path,
    *,
    base_url: str,
    model: str,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Persist model settings to ``<workspace>/.env`` and return safe metadata.

    An empty API key deliberately keeps an existing key, which lets the UI show
    a password field without ever requiring the user to paste it again.
    """

    root = Path(project_root).expanduser().resolve()
    if not base_url.strip():
        raise ConfigError("base_url cannot be empty")
    if not model.strip():
        raise ConfigError("model cannot be empty")
    env_path = root / ".env"
    root.mkdir(parents=True, exist_ok=True)
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    current = _values(env_path)
    secret = api_key.strip() if isinstance(api_key, str) else ""
    if not secret:
        secret = current.get("OPENAI_API_KEY", "")
    if not secret:
        raise ConfigError("api_key cannot be empty (or provide an existing .env key)")

    text = _update_env_text(
        existing,
        {
            "OPENAI_BASE_URL": base_url.strip(),
            "OPENAI_API_KEY": secret,
            "OPENAI_MODEL": model.strip(),
        },
    )
    # Replace atomically so a running desktop process never observes a partial
    # credential file.  The file is intentionally not returned or logged.
    fd, temporary = tempfile.mkstemp(prefix=".cc-harness-env-", dir=str(root))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, env_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    try:
        env_path.chmod(0o600)
    except OSError:
        # Windows ACLs are managed by the user; chmod is best effort there.
        pass
    return read_workspace_config(root)


__all__ = ["read_workspace_config", "save_workspace_config"]
