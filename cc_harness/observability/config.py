"""Configuration for the local observation plane; secrets never enter evidence."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class ObservationConfig:
    root: Path
    enabled_at: str
    base_url: str
    public_key: str
    secret_key: str
    ordinary_sample: float = 0.10
    complex_sample: float = 0.50
    poll_seconds: float = 2.0
    segment_chars: int = 48_000

    @classmethod
    def load(cls, environ: Mapping[str, str] | None = None) -> ObservationConfig | None:
        env = os.environ if environ is None else environ
        if "PYTEST_CURRENT_TEST" in env and "CC_HARNESS_OBSERVABILITY_CONFIG" not in env:
            return None
        if env.get("CC_HARNESS_OBSERVABILITY_DISABLED", "").lower() in {"1", "true"}:
            return None
        default = "D:/agent_data/cc-harness-observability/config.json" if os.name == "nt" else "~/.local/share/cc-harness-observability/config.json"
        path = Path(env.get("CC_HARNESS_OBSERVABILITY_CONFIG", default)).expanduser()
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        config = cls(
            root=Path(raw["root"]), enabled_at=str(raw["enabled_at"]),
            base_url=str(raw["base_url"]), public_key=str(raw["public_key"]), secret_key=str(raw["secret_key"]),
            ordinary_sample=float(raw.get("ordinary_sample", .10)), complex_sample=float(raw.get("complex_sample", .50)),
            poll_seconds=float(raw.get("poll_seconds", 2)), segment_chars=int(raw.get("segment_chars", 48_000)),
        )
        if not 0 <= config.ordinary_sample <= 1 or not 0 <= config.complex_sample <= 1:
            raise ValueError("sampling probabilities must be between zero and one")
        if config.poll_seconds <= 0 or config.segment_chars < 1024:
            raise ValueError("invalid observation polling or segment size")
        if os.name == "nt" and config.root.resolve().drive.casefold() != "d:":
            raise ValueError("observation persistence must be on D:")
        return config
