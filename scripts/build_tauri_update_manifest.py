"""Build the static latest.json consumed by Tauri's updater plugin."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


PLATFORMS = {
    "windows-x86_64": "cc-harness-windows-x86_64.msi",
    "darwin-x86_64": "cc-harness-darwin-x86_64.app.tar.gz",
    "darwin-aarch64": "cc-harness-darwin-aarch64.app.tar.gz",
}


def asset_file(asset_dir: Path, name: str) -> Path:
    matches = sorted(path for path in asset_dir.rglob(name) if path.is_file())
    if not matches:
        raise SystemExit(f"missing release asset: {name}")
    return matches[0]


def signature_for(asset_dir: Path, archive: str) -> str:
    signature_path = asset_file(asset_dir, f"{archive}.sig")
    return signature_path.read_text(encoding="utf-8").strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    base = f"https://github.com/liyanxiong278176/cc-harness/releases/download/{args.tag}"
    platforms = {}
    for platform, archive in PLATFORMS.items():
        platforms[platform] = {
            "url": f"{base}/{archive}",
            "signature": signature_for(args.assets, archive),
        }

    manifest = {
        "version": args.version,
        "notes": "cc-harness 桌面端更新",
        "pub_date": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "platforms": platforms,
    }
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
