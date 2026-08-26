"""Build the Python desktop bridge in the filename Tauri expects.

The script is intentionally small and platform-local: PyInstaller must run on
the target OS so native Python extensions (for example sqlite-vec) are built
for the same platform as the installer. GitHub Actions invokes it once per
Windows/macOS runner before ``tauri build``.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_NAME = "cc-harness-desktop-bridge"
EXCLUDED_OPTIONAL_MODULES = (
    # These packages belong to benchmark/evaluation or optional developer
    # tooling. They are not imported by the desktop runtime and otherwise
    # make PyInstaller scan multi-gigabyte scientific/ML trees.
    "agentdojo",
    "deepeval",
    "langfuse",
    "matplotlib",
    "pandas",
    "pytest",
    "scipy",
    "tensorboard",
    "tensorflow",
    "torch",
)


def rust_target() -> str:
    try:
        output = subprocess.check_output(
            ["rustc", "-vV"], cwd=ROOT, text=True, stderr=subprocess.STDOUT
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit("rustc is required to determine the Tauri target triple") from exc
    for line in output.splitlines():
        if line.startswith("host:"):
            return line.split(":", 1)[1].strip()
    raise SystemExit("rustc -vV did not report a host target")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", help="Rust target triple; defaults to rustc host")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "desktop" / "src-tauri" / "binaries",
        help="directory where the Tauri sidecar is written",
    )
    args = parser.parse_args(argv)
    target = args.target or rust_target()
    suffix = ".exe" if "windows" in target else ""

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--onefile",
        "--name",
        BRIDGE_NAME,
        "--paths",
        str(ROOT),
        str(ROOT / "scripts" / "desktop_bridge_entry.py"),
    ]
    for module in EXCLUDED_OPTIONAL_MODULES:
        command.extend(["--exclude-module", module])
    subprocess.run(
        command,
        cwd=ROOT,
        check=True,
    )
    source = ROOT / "dist" / f"{BRIDGE_NAME}{suffix}"
    if not source.is_file():
        raise SystemExit(f"PyInstaller did not produce {source}")
    destination_dir = (ROOT / args.output).resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{BRIDGE_NAME}-{target}{suffix}"
    shutil.copy2(source, destination)
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
