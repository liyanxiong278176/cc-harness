"""PID plus creation identity detects terminal loss without PID reuse mistakes."""
from __future__ import annotations

import os
from pathlib import Path


def process_identity(pid: int) -> str | None:
    if pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            creation, exit_time, system, user = (wintypes.FILETIME() for _ in range(4))
            kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            if not kernel.GetProcessTimes(handle, *map(ctypes.byref, (creation, exit_time, system, user))):
                return None
            return f"{pid}:{creation.dwHighDateTime}:{creation.dwLowDateTime}"
        finally:
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle(handle)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return f"{pid}:{stat[stat.rfind(')') + 2:].split()[19]}"
    except (OSError, IndexError):
        # macOS has no /proc. ps supplies process birth time without shell interpolation.
        import subprocess
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                                capture_output=True, text=True, check=False)
        return f"{pid}:{result.stdout.strip()}" if result.returncode == 0 and result.stdout.strip() else None
