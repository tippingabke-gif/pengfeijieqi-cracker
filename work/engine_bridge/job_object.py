"""Lifetime binding for a caller-owned Windows ``subprocess.Popen`` child.

Create ``ChildJob(proc)`` immediately after Popen and retain it until cleanup.
``close()`` releases the last non-inheritable job handle and terminates any
remaining job processes.  It does not close Popen's process handle or wait for
exit; callers must wait for the child before releasing other resources.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import os
import subprocess
import threading


JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


class _BasicLimitInformation(C.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", C.c_longlong),
        ("PerJobUserTimeLimit", C.c_longlong),
        ("LimitFlags", W.DWORD),
        ("MinimumWorkingSetSize", C.c_size_t),
        ("MaximumWorkingSetSize", C.c_size_t),
        ("ActiveProcessLimit", W.DWORD),
        ("Affinity", C.c_size_t),
        ("PriorityClass", W.DWORD),
        ("SchedulingClass", W.DWORD),
    ]


class _IoCounters(C.Structure):
    _fields_ = [(name, C.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _ExtendedLimitInformation(C.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", C.c_size_t),
        ("JobMemoryLimit", C.c_size_t),
        ("PeakProcessMemoryUsed", C.c_size_t),
        ("PeakJobMemoryUsed", C.c_size_t),
    ]


class _ProcessBasicInformation(C.Structure):
    _fields_ = [
        ("ExitStatus", W.LONG),
        ("PebBaseAddress", C.c_void_p),
        ("AffinityMask", C.c_size_t),
        ("BasePriority", W.LONG),
        ("UniqueProcessId", C.c_size_t),
        ("InheritedFromUniqueProcessId", C.c_size_t),
    ]


class ChildJob:
    """Bind exactly one direct Popen child to a kill-on-close Windows job.

    Windows 10 nested jobs are accepted by the native API.  Assignment failures
    are surfaced to the caller; there is no uncontained fallback.
    """

    def __init__(self, proc: subprocess.Popen):
        if os.name != "nt":
            raise OSError("ChildJob requires Windows")
        if not isinstance(proc, subprocess.Popen):
            raise TypeError("Expected a caller-owned subprocess.Popen child")
        if proc.poll() is not None:
            raise RuntimeError("Caller-owned child has already exited")
        native = getattr(proc, "_handle", None)
        if native is None or not int(native):
            raise ValueError("Popen has no live Windows process handle")

        self.proc = proc  # Keep Popen and its native handle alive.
        self._process_handle = W.HANDLE(int(native))
        self._handle = None
        self._lock = threading.Lock()
        self._kernel = C.WinDLL("kernel32", use_last_error=True)
        self._ntdll = C.WinDLL("ntdll", use_last_error=True)
        specs = {
            "CreateJobObjectW": ([C.c_void_p, W.LPCWSTR], W.HANDLE),
            "SetInformationJobObject": ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
            "AssignProcessToJobObject": ([W.HANDLE, W.HANDLE], W.BOOL),
            "GetProcessId": ([W.HANDLE], W.DWORD),
            "CloseHandle": ([W.HANDLE], W.BOOL),
        }
        for name, (args, result) in specs.items():
            fn = getattr(self._kernel, name)
            fn.argtypes, fn.restype = args, result
        query = self._ntdll.NtQueryInformationProcess
        query.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.ULONG, C.POINTER(W.ULONG)]
        query.restype = W.LONG
        self._check_child()

        # NULL security attributes create a non-inheritable handle.  An unnamed
        # job cannot acquire other owners by an accidental name collision.
        handle = self._kernel.CreateJobObjectW(None, None)
        if not handle:
            raise C.WinError(C.get_last_error())
        self._handle = handle
        try:
            info = _ExtendedLimitInformation()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self._kernel.SetInformationJobObject(
                handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, C.byref(info), C.sizeof(info)
            ):
                raise C.WinError(C.get_last_error())
            self._check_child()
            if not self._kernel.AssignProcessToJobObject(handle, self._process_handle):
                raise C.WinError(C.get_last_error())
        except BaseException:
            self.close()
            raise

    def _check_child(self) -> None:
        # Query the actual retained Popen handle; never open an arbitrary PID.
        if self.proc.poll() is not None:
            raise RuntimeError("Caller-owned child has already exited")
        pid = self._kernel.GetProcessId(self._process_handle)
        if not pid:
            raise C.WinError(C.get_last_error())
        if pid != self.proc.pid:
            raise ValueError("Popen PID differs from its retained process handle")
        info = _ProcessBasicInformation()
        returned = W.ULONG()
        status = self._ntdll.NtQueryInformationProcess(
            self._process_handle, 0, C.byref(info), C.sizeof(info), C.byref(returned)
        )
        if status < 0:
            raise OSError(f"NtQueryInformationProcess: 0x{status & 0xffffffff:08x}")
        if returned.value != C.sizeof(info):
            raise RuntimeError("Unexpected PROCESS_BASIC_INFORMATION size")
        if info.UniqueProcessId != pid or info.InheritedFromUniqueProcessId != os.getpid():
            raise ValueError("Target is not a direct child of this caller")

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._handle is None

    def close(self) -> None:
        """Close the job once; remaining child processes are terminated by Windows."""
        with self._lock:
            if self._handle is None:
                return
            if not self._kernel.CloseHandle(self._handle):
                raise C.WinError(C.get_last_error())
            self._handle = None

    def __enter__(self) -> "ChildJob":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
