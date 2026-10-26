"""Read only LOCALAPPDATA from live Docker backend processes on Windows."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import subprocess


def verified_backend_pids(expected: Path) -> list[int]:
    raw = subprocess.run(["powershell", "-NoProfile", "-Command",
        "ConvertTo-Json -Compress -InputObject @(Get-Process -Name com.docker.backend -ErrorAction SilentlyContinue | ForEach-Object {$_.Id})"],
        capture_output=True, text=True, check=True).stdout
    pids = json.loads(raw or "[]")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    native = ctypes.WinDLL("ntdll")
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    class BasicInformation(ctypes.Structure):
        _fields_ = [("reserved", ctypes.c_void_p), ("peb", ctypes.c_void_p),
                    ("reserved2", ctypes.c_void_p * 2), ("pid", ctypes.c_void_p), ("reserved3", ctypes.c_void_p)]

    def read(handle, address, size):
        buffer = ctypes.create_string_buffer(size)
        count = ctypes.c_size_t()
        kernel.ReadProcessMemory(handle, address, buffer, size, ctypes.byref(count))
        return buffer.raw[:count.value]

    verified = []
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        raise RuntimeError("Docker backend storage verification requires 64-bit Python")
    for pid in pids:
        handle = kernel.OpenProcess(0x0400 | 0x0010, False, pid)
        if not handle:
            continue  # A service helper may belong to SYSTEM, not this user.
        try:
            info = BasicInformation()
            result = native.NtQueryInformationProcess(ctypes.c_void_p(handle), 0, ctypes.byref(info), ctypes.sizeof(info), None)
            if result or not info.peb:
                continue
            parameters = int.from_bytes(read(handle, info.peb + 0x20, 8), "little")
            if not parameters:
                continue
            environment = int.from_bytes(read(handle, parameters + 0x80, 8), "little")
            if not environment:
                continue
            # The complete block stays in memory. Never log, return or persist
            # other variables: they may contain model or application secrets.
            block = read(handle, environment, 65536).decode("utf-16-le", errors="ignore").split("\x00\x00")[0]
            value = next((entry.split("=", 1)[1] for entry in block.split("\x00") if entry.upper().startswith("LOCALAPPDATA=")), None)
            if value:
                if Path(value).resolve() != expected.resolve():
                    raise RuntimeError("A live Docker backend uses a different data root; restart with start_docker_on_d.ps1")
                verified.append(pid)
        finally:
            kernel.CloseHandle(handle)
    if not verified:
        raise RuntimeError("Cannot verify a live D-drive Docker backend; start it with start_docker_on_d.ps1")
    return verified
