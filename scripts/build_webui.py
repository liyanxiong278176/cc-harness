"""Build the React WebUI and stage its static files in the Python package.

This keeps the runtime dependency-free from Node.js: package builders run this
script once, while installed users only need the Python wheel and its embedded
``cc_harness/web_assets`` files.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
DIST = WEB / "dist"
TARGET = ROOT / "cc_harness" / "web_assets"


def main() -> int:
    npm = "npm.cmd" if os.name == "nt" else "npm"
    subprocess.run([npm, "run", "build"], cwd=WEB, check=True)
    if not (DIST / "index.html").is_file():
        raise SystemExit("WebUI build did not produce web/dist/index.html")
    TARGET.mkdir(parents=True, exist_ok=True)
    # Remove only generated files from the package asset directory.  Keeping
    # old Vite hash bundles would make the wheel grow on every release.
    for stale in TARGET.iterdir():
        if stale.is_dir():
            shutil.rmtree(stale)
        else:
            stale.unlink()
    for item in DIST.iterdir():
        destination = TARGET / item.name
        if item.is_dir():
            shutil.copytree(item, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(item, destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
