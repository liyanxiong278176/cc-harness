"""PyInstaller entrypoint for the packaged desktop sidecar."""

from cc_harness.desktop_bridge import main


if __name__ == "__main__":
    raise SystemExit(main())
