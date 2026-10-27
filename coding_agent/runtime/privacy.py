"""Local-user recovery data permissions; these are not a host security sandbox."""

from __future__ import annotations

import csv
import os
from pathlib import Path
import subprocess
import stat

from coding_agent.runtime.models import RuntimePersistenceError


def is_link(path: Path) -> bool:
    """Detect reparse points on Python 3.11 too (Path.is_junction is newer)."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def private_directory(path: Path) -> Path:
    path = path.absolute()
    # Reject reparse/symlink components, including an already-existing state root.
    for item in (path, *path.parents):
        if is_link(item):
            raise RuntimePersistenceError("runtime state directory contains a link")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                                  capture_output=True, text=True, check=True, timeout=10)
        sid = next(csv.reader([identity.stdout.strip()]))[-1]
        if not sid.startswith("S-1-"):
            raise RuntimePersistenceError("cannot identify runtime owner")
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
        convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
        convert.restype = wintypes.BOOL
        secure = advapi.SetFileSecurityW
        secure.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
        secure.restype = wintypes.BOOL
        descriptor = ctypes.c_void_p()
        if not convert(f"D:P(A;OICI;FA;;;{sid})", 1, ctypes.byref(descriptor), None):
            raise RuntimePersistenceError("cannot build runtime permissions")
        try:
            if not secure(str(path), 0x80000004, descriptor):
                raise RuntimePersistenceError("cannot protect runtime state directory")
        finally:
            free = ctypes.WinDLL("kernel32").LocalFree
            free.argtypes = [ctypes.c_void_p]
            free.restype = ctypes.c_void_p
            free(descriptor)
    else:
        path.chmod(0o700)
    return path
