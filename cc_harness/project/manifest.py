"""Sub-project A Manifest 加载/保存/校验(组件 1)。

`.cc-harness/project.yaml` 反序列化到 `Manifest` dataclass。

约束(spec 组件 1):
    1. `project_id` / `name` / `todos_path` / `created_at` 必填,缺失 → ManifestError
    2. `schema_version` 已知但缺省 → 默认 1;未知(> 1)→ ManifestError fail-closed;< 1 → warn
    3. `resume_mode` 必须在 {ask, auto, manual} 内,否则 → ManifestError
    4. 未知字段 → warn log,不报错(extra='ignore' 风格)
    5. 可选字段缺省 → 走默认,绝不抛错
    6. PyYAML `safe_load`,UTF-8,2-space 缩进
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

from cc_harness.project.exceptions import ManifestError as _ManifestError
from cc_harness.project.models import (
    CrossSessionMode,
    LiveConfig,
    Manifest,
    MemoryConfig,
    MemoryIntegrationConfig,
    _parse_iso,
)

log = logging.getLogger(__name__)


# 当前已知的 schema 版本上限(> 此值 → ManifestError fail-closed,< 此值 → warn)
_MAX_SCHEMA_VERSION = 1
_REQUIRED_FIELDS = ("project_id", "name", "todos_path", "created_at")
_VALID_RESUME_MODES = ("ask", "auto", "manual")
_VALID_LIVE_POSITIONS = ("top", "bottom", "off")


class ManifestError(_ManifestError):
    """Manifest 加载/校验失败(组件 1)。

    Task 3 起继承 `TodoError`,纳入统一异常层级;
    通过重新继承 + 同名 alias 保留 `manifest.ManifestError` 的导入路径稳定。
    """


def load_manifest(project_root: Path) -> Manifest | None:
    """加载项目 manifest。

    Args:
        project_root: 项目根目录(查找 `<root>/.cc-harness/project.yaml`)。

    Returns:
        解析后的 `Manifest`;若 manifest 文件不存在 → 返回 `None`。
        解析过程中抛错(yaml 损坏、必填字段缺失、schema_version 未知等)→ 冒泡 `ManifestError`。
    """
    manifest_path = project_root / ".cc-harness" / "project.yaml"
    if not manifest_path.is_file():
        return None

    try:
        raw = manifest_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ManifestError(
            f"failed to parse {manifest_path}: {e}. "
            f"Fix the YAML syntax or re-init via `cc-harness init`."
        ) from e

    if not isinstance(data, dict):
        raise ManifestError(
            f"{manifest_path}: expected mapping at top level, got {type(data).__name__}"
        )

    return _parse_manifest(data, source=str(manifest_path))


def save_manifest(project_root: Path, manifest: Manifest) -> Path:
    """把 `Manifest` 写到 `<project_root>/.cc-harness/project.yaml`。

    Returns:
        写出的 manifest 路径。
    """
    cc_dir = project_root / ".cc-harness"
    cc_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = cc_dir / "project.yaml"
    payload = _manifest_to_yaml(manifest)

    # 原子写:.tmp + os.replace
    tmp_path = manifest_path.with_suffix(".yaml.tmp")
    tmp_path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp_path, manifest_path)
    return manifest_path


# ---------------------------------------------------------------------------
# 内部 — 解析 + 校验
# ---------------------------------------------------------------------------


def _parse_manifest(data: dict, source: str) -> Manifest:
    # 1) 必填字段
    missing = [f for f in _REQUIRED_FIELDS if not data.get(f)]
    if missing:
        raise ManifestError(
            f"{source}: missing required fields: {', '.join(missing)}"
        )

    # 2) schema_version
    raw_schema = data.get("schema_version")
    if raw_schema is None:
        schema_version = 1
    else:
        try:
            schema_version = int(raw_schema)
        except (TypeError, ValueError):
            raise ManifestError(
                f"{source}: schema_version must be int, got {raw_schema!r}"
            )
        if schema_version > _MAX_SCHEMA_VERSION:
            raise ManifestError(
                f"{source}: unknown schema_version={schema_version} "
                f"(max known {_MAX_SCHEMA_VERSION}). Please upgrade cc-harness."
            )
        if schema_version < 1:
            log.warning(
                "manifest %s: schema_version=%d < 1, accepting for backward compat",
                source, schema_version,
            )

    # 3) resume_mode
    resume_mode = data.get("resume_mode", "ask")
    if resume_mode not in _VALID_RESUME_MODES:
        raise ManifestError(
            f"{source}: resume_mode={resume_mode!r} not in {_VALID_RESUME_MODES}"
        )

    # 3.5) cross_session_mode (E3 D4)
    raw_cross_session_mode = data.get("cross_session_mode", "last_only")
    try:
        cross_session_mode = CrossSessionMode(raw_cross_session_mode)
    except ValueError as e:
        raise ManifestError(
            f"{source}: cross_session_mode={raw_cross_session_mode!r} "
            f"must be one of {[m.value for m in CrossSessionMode]} "
            f"(allowed: off / last_only / ask)"
        ) from e

    # 4) created_at
    created_at_raw = data["created_at"]
    try:
        created_at = _parse_iso(created_at_raw)
    except (ValueError, TypeError) as e:
        raise ManifestError(
            f"{source}: created_at={created_at_raw!r} is not valid ISO 8601: {e}"
        ) from e

    # 5) memory 子树(可选,缺省走默认)
    memory = _parse_memory(data.get("memory") or {}, source=source)

    # 6) live 子树(可选)
    live = _parse_live(data.get("live") or {}, source=source)

    # 7) 未知字段 → warn(extra='ignore' 风格)
    _warn_unknown_fields(
        data, source=source,
        known=set(_REQUIRED_FIELDS) | {
            "schema_version", "resume_mode", "memory", "live", "cross_session_mode",
        },
    )

    return Manifest(
        project_id=str(data["project_id"]),
        name=str(data["name"]),
        todos_path=str(data["todos_path"]),
        created_at=created_at,
        schema_version=schema_version,
        memory=memory,
        resume_mode=resume_mode,  # type: ignore[arg-type]
        live=live,
        cross_session_mode=cross_session_mode,
    )


def _parse_memory(data: dict, source: str) -> MemoryConfig:
    if not isinstance(data, dict):
        raise ManifestError(f"{source}: memory must be a mapping, got {type(data).__name__}")
    _warn_unknown_fields(
        data, source=f"{source}:memory",
        known={"db_path", "integration"},
    )

    integration_raw = data.get("integration") or {}
    if not isinstance(integration_raw, dict):
        raise ManifestError(
            f"{source}: memory.integration must be a mapping, got {type(integration_raw).__name__}"
        )
    _warn_unknown_fields(
        integration_raw, source=f"{source}:memory.integration",
        known={"completion_capture"},
    )

    completion_capture = bool(integration_raw.get("completion_capture", False))

    return MemoryConfig(
        db_path=data.get("db_path"),
        integration=MemoryIntegrationConfig(completion_capture=completion_capture),
    )


def _parse_live(data: dict, source: str) -> LiveConfig:
    if not isinstance(data, dict):
        raise ManifestError(f"{source}: live must be a mapping, got {type(data).__name__}")
    _warn_unknown_fields(
        data, source=f"{source}:live",
        known={"position", "max_height", "spinner_style", "show_progress_bar", "fold_done"},
    )

    position = data.get("position", "top")
    if position not in _VALID_LIVE_POSITIONS:
        raise ManifestError(
            f"{source}: live.position={position!r} not in {_VALID_LIVE_POSITIONS}"
        )

    max_height_raw = data.get("max_height", 10)
    try:
        max_height = int(max_height_raw)
    except (TypeError, ValueError):
        raise ManifestError(
            f"{source}: live.max_height must be int, got {max_height_raw!r}"
        )
    if max_height < 1:
        raise ManifestError(
            f"{source}: live.max_height must be >= 1, got {max_height}"
        )

    fold_done_raw = data.get("fold_done", 5)
    try:
        fold_done = int(fold_done_raw)
    except (TypeError, ValueError):
        raise ManifestError(
            f"{source}: live.fold_done must be int, got {fold_done_raw!r}"
        )
    if fold_done < 0:
        raise ManifestError(f"{source}: live.fold_done must be >= 0, got {fold_done}")

    spinner_style = str(data.get("spinner_style", "dots"))
    show_progress_bar = bool(data.get("show_progress_bar", True))

    return LiveConfig(
        position=position,  # type: ignore[arg-type]
        max_height=max_height,
        spinner_style=spinner_style,
        show_progress_bar=show_progress_bar,
        fold_done=fold_done,
    )


def _warn_unknown_fields(data: dict, source: str, known: set[str]) -> None:
    """extra='ignore' 风格:未知字段 → warn log,不报错。"""
    unknown = set(data.keys()) - known
    for k in sorted(unknown):
        log.warning("%s: unknown manifest field %r (ignored)", source, k)


def _manifest_to_yaml(m: Manifest) -> dict:
    """Manifest → yaml 友好 dict(2-space 缩进,UTF-8,sort_keys=False 保持顺序)。"""
    payload: dict = {
        "project_id": m.project_id,
        "name": m.name,
        "todos_path": m.todos_path,
        "created_at": m.created_at.isoformat(),
    }
    payload["schema_version"] = m.schema_version
    payload["memory"] = {
        "db_path": m.memory.db_path,
        "integration": {
            "completion_capture": m.memory.integration.completion_capture,
        },
    }
    payload["resume_mode"] = m.resume_mode
    payload["live"] = {
        "position": m.live.position,
        "max_height": m.live.max_height,
        "spinner_style": m.live.spinner_style,
        "show_progress_bar": m.live.show_progress_bar,
        "fold_done": m.live.fold_done,
    }
    # 清理 None 默认项,保持文件干净(db_path 通常 None)
    if m.memory.db_path is None:
        del payload["memory"]["db_path"]
    return payload