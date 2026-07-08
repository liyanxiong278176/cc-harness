"""Download snap-research/locomo dataset to eval/locomo/data/.

Source: https://github.com/snap-research/locomo
File: data/locomo10.json (10 long conversations, with QA pairs).
License: see upstream LICENSE.txt — local eval only, NOT committed to this repo.
"""
import json
import urllib.request
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"
DEFAULT_FILE = DATA_DIR / "locomo10.json"
SOURCE_URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"


def verify_dataset(path: Path) -> list[dict[str, Any]]:
    """Load and sanity-check locomo JSON. Returns list of samples.
    Raises FileNotFoundError if file missing.
    Raises ValueError if file empty/no QA.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"locomo data not found at {path}; run download_dataset() first"
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "sample_id" in raw:
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError(f"locomo JSON must be list or sample dict, got {type(raw).__name__}")
    for i, s in enumerate(raw):
        if not isinstance(s, dict):
            raise ValueError(f"sample #{i} is not a dict")
        if not s.get("qa"):
            raise ValueError(f"sample #{i} ({s.get('sample_id','?')}) has no QA pairs")
    return raw


def download_dataset(target: Path = DEFAULT_FILE, url: str = SOURCE_URL) -> Path:
    """Download locomo10.json from upstream. Returns target path."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[download] {url} -> {target}")
    with urllib.request.urlopen(url, timeout=60) as resp:
        body = resp.read()
    target.write_bytes(body)
    samples = verify_dataset(target)
    print(f"[download] OK: {len(samples)} samples, {target.stat().st_size // 1024} KB")
    return target


if __name__ == "__main__":
    download_dataset()
