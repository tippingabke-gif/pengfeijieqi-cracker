"""Source-level parent-exit integration test for ``Bridge``.

The harness creates only its own Python processes.  A supervisor starts a
source ``Bridge`` runner, and a separate holder keeps the runner's stdin write
end open after the supervisor exits.  This makes ``parent_exit`` observable
without conflating it with host stdin EOF.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parent
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102


def _kernel():
    k = C.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    k.OpenProcess.restype = W.HANDLE
    k.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
    k.WaitForSingleObject.restype = W.DWORD
    k.GetExitCodeProcess.argtypes = [W.HANDLE, C.POINTER(W.DWORD)]
    k.GetExitCodeProcess.restype = W.BOOL
    k.TerminateProcess.argtypes = [W.HANDLE, W.UINT]
    k.TerminateProcess.restype = W.BOOL
    k.CloseHandle.argtypes = [W.HANDLE]
    k.CloseHandle.restype = W.BOOL
    return k


def _wait_pid(k, pid: int, timeout_ms: int = 7000) -> int:
    handle = k.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        raise C.WinError(C.get_last_error())
    try:
        result = k.WaitForSingleObject(handle, timeout_ms)
        if result == WAIT_TIMEOUT:
            raise TimeoutError(f"PID {pid} did not exit within {timeout_ms} ms")
        if result != WAIT_OBJECT_0:
            raise OSError(f"WaitForSingleObject({pid}) returned 0x{result:08x}")
        code = W.DWORD()
        if not k.GetExitCodeProcess(handle, C.byref(code)):
            raise C.WinError(C.get_last_error())
        value = int(code.value)
        return value - (1 << 32) if value & 0x80000000 else value
    finally:
        k.CloseHandle(handle)


def _terminate_pid(k, pid: int) -> None:
    handle = k.OpenProcess(
        PROCESS_TERMINATE | SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION,
        False, int(pid)
    )
    if not handle:
        return
    try:
        if k.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT:
            k.TerminateProcess(handle, 0xDEAD)
        k.WaitForSingleObject(handle, 3000)
    finally:
        k.CloseHandle(handle)


def _source_runner_code(log_path: Path) -> str:
    return """
import sys
from pathlib import Path
sys.path.insert(0, {root!r})
from bridge import Bridge, EventLog
bridge = Bridge(Path(sys.executable), isolate=False, patch_module=None,
                require_patch=False, log=EventLog(Path({log!r})))
raise SystemExit(bridge.run())
""".format(root=str(ROOT), log=str(log_path))


def _supervisor_code(runner_code: str, mode: str) -> str:
    return """
import json, os, subprocess, sys, time
runner_code = {runner_code!r}
runner = subprocess.Popen(
    [sys.executable, '-c', runner_code],
    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
)
# Keep a duplicate of runner.stdin alive after this supervisor exits.  Without
# it, stdin EOF would race ParentGuard's process-object signal.
holder = subprocess.Popen(
    [sys.executable, '-c', 'import time; time.sleep(60)'],
    stdin=runner.stdin, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
)
runner.stdin.write(b'import time; time.sleep(60)\\n')
runner.stdin.flush()
print(json.dumps({{'runner_pid': runner.pid, 'holder_pid': holder.pid}}), flush=True)
if {mode!r} == 'normal':
    sys.stdin.buffer.readline()
    os._exit(0)
while True:
    time.sleep(1)
""".format(runner_code=runner_code, mode=mode)


def _read_log(path: Path, event: str = "child_exit") -> list[dict]:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if path.is_file():
            rows = []
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
            if any(row.get("event") == event for row in rows):
                return rows
        time.sleep(0.05)
    raise TimeoutError(f"bridge {event} log timeout: {path}")


def _run_case(k, mode: str) -> dict:
    case_dir = ROOT / "parent_guard_tests" / f"{time.time_ns()}-{mode}"
    case_dir.mkdir(parents=True)
    with nullcontext(case_dir) as td:
        log_path = Path(td) / "bridge.jsonl"
        runner_code = _source_runner_code(log_path)
        supervisor = subprocess.Popen(
            [sys.executable, "-c", _supervisor_code(runner_code, mode)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        runner_pid = holder_pid = child_pid = None
        retained = {}
        result = None
        try:
            line = supervisor.stdout.readline().decode("utf-8", "replace").strip()
            if not line:
                error = supervisor.stderr.read().decode("utf-8", "replace")
                raise RuntimeError(f"supervisor did not report PIDs: {error}")
            reported = json.loads(line)
            runner_pid, holder_pid = int(reported["runner_pid"]), int(reported["holder_pid"])
            # Retain the actual test process objects before exiting the owner.
            # Later exit-code queries cannot race object destruction or PID reuse.
            access = PROCESS_TERMINATE | SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
            for pid in (supervisor.pid, runner_pid, holder_pid):
                handle = k.OpenProcess(access, False, pid)
                if not handle:
                    raise C.WinError(C.get_last_error())
                retained[pid] = handle
            ready_rows = _read_log(log_path, "bridge_ready")
            child_pid = next(int(r["pid"]) for r in ready_rows if r.get("event") == "spawn")
            retained[child_pid] = k.OpenProcess(access, False, child_pid)
            if not retained[child_pid]:
                raise C.WinError(C.get_last_error())
            guard = next(r for r in ready_rows if r.get("event") == "parent_guard_ready")
            assert guard["parent_pid"] == supervisor.pid, guard
            started = time.monotonic()
            if mode == "forced":
                _terminate_pid(k, supervisor.pid)
            else:
                supervisor.stdin.write(b"exit\n")
                supervisor.stdin.flush()
                _wait_pid(k, supervisor.pid, 5000)
            runner_code_value = _wait_pid(k, runner_pid, 8000)
            rows = _read_log(log_path)
            child_pids = [int(row["pid"]) for row in rows if row.get("event") == "spawn"]
            if len(child_pids) != 1:
                raise AssertionError(f"spawn rows: {child_pids!r}")
            child_pid = child_pids[0]
            child_code = _wait_pid(k, child_pid, 3000)
            reasons = [row.get("reason") for row in rows if row.get("event") == "shutdown_requested"]
            if "parent_exit" not in reasons:
                raise AssertionError(f"parent_exit missing: {reasons!r}; rows={rows!r}")
            assert "host_eof" not in reasons[:reasons.index("parent_exit")], reasons
            assert k.WaitForSingleObject(retained[holder_pid], 0) == WAIT_TIMEOUT
            child_exit = [row for row in rows if row.get("event") == "child_exit"][-1]
            if int(child_exit["engine_pid"]) != child_pid:
                raise AssertionError(f"child_exit PID mismatch: {child_exit!r}")
            result = {
                "mode": mode,
                "supervisor_pid": supervisor.pid,
                "runner_pid": runner_pid,
                "runner_returncode": runner_code_value,
                "holder_pid": holder_pid,
                "child_pid": child_pid,
                "child_returncode": child_code,
                "shutdown_reasons": reasons,
                "child_exit": child_exit,
                "holder_alive_during_exit": True,
                "exit_elapsed_seconds": round(time.monotonic() - started, 3),
                "log": str(log_path),
            }
            return result
        finally:
            if supervisor.poll() is None:
                _terminate_pid(k, supervisor.pid)
            if runner_pid is not None:
                _terminate_pid(k, runner_pid)
            if holder_pid is not None:
                _terminate_pid(k, holder_pid)
            if child_pid is not None:
                _terminate_pid(k, child_pid)
            if result is not None:
                result["all_created_processes_exited"] = all(
                    k.WaitForSingleObject(handle, 0) == WAIT_OBJECT_0
                    for handle in retained.values()
                )
                (case_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
            for handle in retained.values():
                if handle:
                    k.CloseHandle(handle)


def main() -> int:
    if os.name != "nt":
        print("SKIP: Windows-only source integration test")
        return 0
    k = _kernel()
    started = time.monotonic()
    results = [_run_case(k, mode) for mode in ("normal", "forced")]
    print(json.dumps({"elapsed_seconds": round(time.monotonic() - started, 3), "cases": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
