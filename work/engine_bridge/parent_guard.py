"""Detect loss of the process that launched this bridge.

The guard keeps one native process handle for the direct parent observed at
startup.  It never enumerates processes and never opens a PID after startup;
the wait is therefore tied to the same process object even if the numeric PID
is later reused.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import os
import threading


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
PROCESS_BASIC_INFORMATION_CLASS = 0
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF


class _ProcessBasicInformation(C.Structure):
    _fields_ = [
        ("ExitStatus", W.LONG),
        ("PebBaseAddress", C.c_void_p),
        ("AffinityMask", C.c_size_t),
        ("BasePriority", W.LONG),
        ("UniqueProcessId", C.c_size_t),
        ("InheritedFromUniqueProcessId", C.c_size_t),
    ]


class _FileTime(C.Structure):
    _fields_ = [("low", W.DWORD), ("high", W.DWORD)]

    @property
    def value(self) -> int:
        return (int(self.high) << 32) | int(self.low)


class ParentGuard:
    """Hold and monitor the bridge's direct parent process.

    ``parent_exited()`` is intended for a short-interval bridge main loop.  It
    returns ``True`` only after the held process object is signaled.  If the
    process identity check or wait API fails, a ``ParentGuardError`` is raised
    so the caller can stop instead of continuing without a verified owner.
    """

    def __init__(self, *, parent_pid: int | None = None):
        if os.name != "nt":
            raise OSError("ParentGuard requires Windows")
        self._lock = threading.Lock()
        self._closed = False
        self._kernel = C.WinDLL("kernel32", use_last_error=True)
        self._ntdll = C.WinDLL("ntdll", use_last_error=True)

        self._kernel.GetCurrentProcess.argtypes = []
        self._kernel.GetCurrentProcess.restype = W.HANDLE
        self._kernel.GetCurrentProcessId.argtypes = []
        self._kernel.GetCurrentProcessId.restype = W.DWORD
        self._kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
        self._kernel.OpenProcess.restype = W.HANDLE
        self._kernel.GetProcessId.argtypes = [W.HANDLE]
        self._kernel.GetProcessId.restype = W.DWORD
        self._kernel.GetProcessTimes.argtypes = [
            W.HANDLE, C.POINTER(_FileTime), C.POINTER(_FileTime),
            C.POINTER(_FileTime), C.POINTER(_FileTime),
        ]
        self._kernel.GetProcessTimes.restype = W.BOOL
        self._kernel.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
        self._kernel.WaitForSingleObject.restype = W.DWORD
        self._kernel.CloseHandle.argtypes = [W.HANDLE]
        self._kernel.CloseHandle.restype = W.BOOL

        query = self._ntdll.NtQueryInformationProcess
        query.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.ULONG, C.POINTER(W.ULONG)]
        query.restype = W.LONG
        self._query = query

        current_pid = int(self._kernel.GetCurrentProcessId())
        observed_parent = self._get_parent_pid()
        if parent_pid is None:
            parent_pid = observed_parent
        parent_pid = int(parent_pid)
        if parent_pid <= 0 or parent_pid == current_pid:
            raise ParentGuardError("invalid direct parent PID")
        if observed_parent and parent_pid != observed_parent:
            raise ParentGuardError(
                f"parent PID changed during startup: {observed_parent} -> {parent_pid}"
            )

        access = SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
        handle = self._kernel.OpenProcess(access, False, parent_pid)
        if not handle:
            raise C.WinError(C.get_last_error())
        self._handle = handle
        self.pid = parent_pid
        try:
            self.process_id = int(self._kernel.GetProcessId(handle))
            if self.process_id != self.pid:
                raise ParentGuardError("parent handle PID mismatch")
            self.creation_time = self._creation_time(handle)
            self.current_creation_time = self._creation_time(
                self._kernel.GetCurrentProcess()
            )
            if self.creation_time > self.current_creation_time:
                raise ParentGuardError(
                    "parent creation time is newer than this bridge process"
                )
            # The inherited PID stays fixed after parent exit.  This is a
            # consistency check only; the creation-time comparison above
            # rejects a replacement process if that PID was already reused.
            if self._get_parent_pid() != self.pid:
                raise ParentGuardError("direct parent changed during startup")
        except BaseException:
            self.close()
            raise

    def _get_parent_pid(self) -> int:
        info = _ProcessBasicInformation()
        returned = W.ULONG()
        status = self._query(
            self._kernel.GetCurrentProcess(), PROCESS_BASIC_INFORMATION_CLASS,
            C.byref(info), C.sizeof(info), C.byref(returned)
        )
        if status < 0:
            raise ParentGuardError(
                f"NtQueryInformationProcess failed: 0x{status & 0xffffffff:08x}"
            )
        if returned.value < C.sizeof(info):
            raise ParentGuardError("short PROCESS_BASIC_INFORMATION")
        return int(info.InheritedFromUniqueProcessId)

    def _creation_time(self, handle: W.HANDLE) -> int:
        creation = _FileTime()
        exit_time = _FileTime()
        kernel_time = _FileTime()
        user_time = _FileTime()
        if not self._kernel.GetProcessTimes(
            handle, C.byref(creation), C.byref(exit_time),
            C.byref(kernel_time), C.byref(user_time)
        ):
            raise C.WinError(C.get_last_error())
        return creation.value

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def parent_exited(self) -> bool:
        """Return whether the startup parent process has terminated."""
        with self._lock:
            if self._closed:
                raise ParentGuardError("parent guard is closed")
            result = self._kernel.WaitForSingleObject(self._handle, 0)
        if result == WAIT_OBJECT_0:
            return True
        if result == WAIT_TIMEOUT:
            return False
        if result == WAIT_FAILED:
            raise C.WinError(C.get_last_error())
        raise ParentGuardError(f"WaitForSingleObject returned 0x{result:08x}")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            handle = self._handle
            self._handle = None
        if handle and not self._kernel.CloseHandle(handle):
            raise C.WinError(C.get_last_error())

    def __enter__(self) -> "ParentGuard":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class ParentGuardError(RuntimeError):
    """The bridge no longer has a verified direct parent to keep serving."""

