"""Copy only the platform files needed by the installer and Tauri updater.

Tauri writes platform-specific names into its bundle directory. Release jobs
run once per target, so this script normalizes those names before the artifacts
are combined into one GitHub Release.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def find_one(root: Path, suffix: str) -> Path:
    matches = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.name.endswith(suffix)
    )
    if not matches:
        raise SystemExit(f"missing Tauri release asset with suffix {suffix!r}")
    return matches[0]


def copy_asset(source: Path, output: Path, name: str) -> None:
    output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, output / name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--platform-key", required=True)
    args = parser.parse_args()

    key = args.platform_key
    if key == "windows-x86_64":
        installer = find_one(args.bundle_root, ".msi")
        signature = find_one(args.bundle_root, ".msi.sig")
        copy_asset(installer, args.output, "cc-harness-windows-x86_64.msi")
        copy_asset(signature, args.output, "cc-harness-windows-x86_64.msi.sig")
    elif key in {"darwin-x86_64", "darwin-aarch64"}:
        suffix = ".app.tar.gz"
        archive = find_one(args.bundle_root, suffix)
        signature = find_one(args.bundle_root, suffix + ".sig")
        copy_asset(archive, args.output, f"cc-harness-{key}.app.tar.gz")
        copy_asset(signature, args.output, f"cc-harness-{key}.app.tar.gz.sig")
        # Keep the normal DMG download beside the updater archive.
        dmg = find_one(args.bundle_root, ".dmg")
        copy_asset(dmg, args.output, f"cc-harness-{key}.dmg")
    else:
        raise SystemExit(f"unsupported platform key: {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
