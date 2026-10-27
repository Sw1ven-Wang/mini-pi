"""Kill-on-close ownership and read-only OS lifetime proofs for V11.1.

This is lifecycle cleanup, NOT a security boundary. The fixed worker is attached
before its stdin gate is opened; arbitrary interrupted effects are not replayed.
"""

import ctypes
from ctypes import wintypes


class BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
        ("flags", wintypes.DWORD), ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "read_operations", "write_operations", "other_operations", "read_bytes", "write_bytes", "other_bytes")]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", BasicLimits), ("io", IoCounters),
        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]


class WindowsProcessJob:
    def __init__(self, pid: int, *, name: str | None = None):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for method_name, (args, result) in signatures.items():
            method = getattr(self.api, method_name)
            method.argtypes, method.restype = args, result
        ctypes.set_last_error(0)
        self.handle = self.api.CreateJobObjectW(None, name)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "cannot create verification job")
        if name is not None and ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            self.close()
            raise OSError(183, "verification job identity already exists")
        process = None
        try:
            limits = ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise OSError(ctypes.get_last_error(), "cannot configure verification job")
            process = self.api.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
            if not process or not self.api.AssignProcessToJobObject(self.handle, process):
                raise OSError(ctypes.get_last_error(), "cannot attach verification process")
        except BaseException:
            self.close()
            raise
        finally:
            if process:
                self.api.CloseHandle(process)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def job_is_stopped(name: str) -> bool:
    """Read-only OS ownership proof. Never search for or terminate a reused PID.

    A kill-on-close job remains alive while associated processes remain. Access
    errors are unknown, not absence. Reserved (not attached) leases cannot use this.
    """
    if not name.startswith("Local\\mini-pi-"):
        return False
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    api.OpenJobObjectW.restype = wintypes.HANDLE
    api.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wintypes.DWORD, ctypes.c_void_p]
    api.QueryInformationJobObject.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    handle = api.OpenJobObjectW(4, False, name)  # JOB_OBJECT_QUERY
    if not handle:
        return ctypes.get_last_error() == 2  # ERROR_FILE_NOT_FOUND only
    class Accounting(ctypes.Structure):
        _fields_ = [("user", ctypes.c_longlong), ("kernel", ctypes.c_longlong),
                    ("period_user", ctypes.c_longlong), ("period_kernel", ctypes.c_longlong),
                    ("page_faults", wintypes.DWORD), ("total", wintypes.DWORD),
                    ("active", wintypes.DWORD), ("terminated", wintypes.DWORD)]
    try:
        value = Accounting()
        return bool(api.QueryInformationJobObject(handle, 1, ctypes.byref(value), ctypes.sizeof(value), None)
                    and value.active == 0)
    finally:
        api.CloseHandle(handle)
