"""Layered display settings for the classic inline terminal shell."""
from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class TerminalUISettings:
    tui: str = "fullscreen"
    # Leave terminal mouse reporting off so the host terminal can perform
    # native drag-to-select/copy.  Enable explicitly when wheel scrolling is
    # preferred over native selection.
    capture_mouse: bool = False
    custom_line: str = "🛩️  冲鸭"
    show_project: bool = True
    show_git: bool = True
    show_duration: bool = True
    show_session_name: bool = True
    startup_blank_rows: int = 3


def load_terminal_settings(project_root: Path, *, user_root: Path | None = None) -> TerminalUISettings:
    values: dict = {}
    user_root = Path(user_root or (Path.home() / ".cc-harness"))
    for path in (user_root / "settings.json", Path(project_root) / ".cc-harness" / "settings.json"):
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        ui = raw.get("ui", raw)
        if isinstance(ui, dict):
            values.update(ui)
    allowed = {item.name for item in fields(TerminalUISettings)}
    return TerminalUISettings(**{key: value for key, value in values.items() if key in allowed})


def save_project_terminal_setting(project_root: Path, key: str, value) -> Path:
    """Update one project-local UI setting without discarding unrelated keys."""
    allowed = {item.name for item in fields(TerminalUISettings)}
    if key not in allowed:
        raise KeyError(key)
    path = Path(project_root) / ".cc-harness" / "settings.json"
    data: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
    ui = data.get("ui")
    if not isinstance(ui, dict):
        ui = {}
        data["ui"] = ui
    ui[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
