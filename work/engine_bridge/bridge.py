"""Small UCI_PF process bridge for the bundled ACE JieQi engine.

The bridge keeps the engine's native ``message_key`` and ``@@`` records on the
wire, while supplying the host-side event which the stock GUI waits for after
``uciok pf``.  It never edits the engine on disk.  A runtime patch
module is loaded before any GUI command is forwarded; the module is expected to
patch only the child process and return an object with ``close()``.

Example::

    python bridge.py --engine C:\\...\\App\\Engine\\ACE_JieQi_64.exe --patch-module C:\\...\\runtime_patch.py

The process is deliberately boring: stdin/stdout are the protocol, stderr is
diagnostics, and only the child created by this process is stopped at exit.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Callable

try:
    from job_object import ChildJob
except ImportError:  # importlib-loaded source tests
    ChildJob = None
try:
    from parent_guard import ParentGuard
except ImportError:  # importlib-loaded source tests
    ParentGuard = None
try:
    from fen_compat import FenCompatibilityError, adapt_position_line
except ImportError:  # importlib-loaded source tests
    _fen_spec = importlib.util.spec_from_file_location(
        "pengfei_bridge_fen_compat", Path(__file__).with_name("fen_compat.py"))
    if not _fen_spec or not _fen_spec.loader:
        raise
    _fen_module = importlib.util.module_from_spec(_fen_spec)
    _fen_spec.loader.exec_module(_fen_module)
    FenCompatibilityError = getattr(_fen_module, "FenCompatibilityError", ValueError)
    adapt_position_line = getattr(_fen_module, "adapt_position_line")


ENGINE_NAME = "ACE_JieQi_64.exe"
ENGINE_SHA256 = "065497ebe421e2337a2c5e83a948964c78e5e99b7058789843ac39d5d77edaea"
EVENT_LINE = "verifyTieziByDsa 1"
LOG_DIRECTORY_ENV = "PF_ENGINE_BRIDGE_LOG_DIR"
VERSION = "1.2.1"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for block in iter(lambda: fp.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def validate_engine(value: str | os.PathLike[str]) -> Path:
    path = Path(value).expanduser().resolve()
    if path.name.casefold() != ENGINE_NAME.casefold():
        raise ValueError(f"engine must be {ENGINE_NAME}: {path}")
    if not path.is_file():
        raise FileNotFoundError(path)
    if getattr(sys, "frozen", False) and path.parent != Path(sys.executable).resolve().parent:
        raise ValueError("engine must be beside the bridge executable")
    digest = _sha256(path)
    if digest.casefold() != ENGINE_SHA256:
        raise ValueError(f"engine hash mismatch: {digest}")
    return path


class EventLog:
    def __init__(self, path: Path | None):
        self.path = path
        self.lock = threading.Lock()
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields: Any) -> None:
        if not self.path:
            return
        row = ({"time": time.time(), **event, **fields} if isinstance(event, dict)
               else {"time": time.time(), "event": event, **fields})
        with self.lock, self.path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(row, ensure_ascii=False) + "\n")

    def set_default(self, engine: Path, pid: int) -> None:
        if self.path:
            return
        override = os.environ.get(LOG_DIRECTORY_ENV)
        if override:
            directory = Path(override).expanduser().resolve()
        else:
            # A packaged bridge lives in App/Engine. Keep its diagnostics next
            # to the app's existing log directory without touching the target.
            directory = engine.parent.parent / "log" / "engine_bridge"
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"bridge-{pid}-{time.time_ns()}.jsonl"


def _load_module(path: Path):
    name = "pengfei_bridge_" + path.stem
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise ImportError(f"cannot load patch module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _apply_runtime_patch(module: Any, proc: subprocess.Popen[bytes], engine: Path,
                         emit: Callable[..., None]):
    """The provider owns only the instrumentation attached to this child."""
    handle = module.apply_patch(proc, engine, emit)
    try:
        wait_ready = getattr(handle, "wait_ready", None)
        if wait_ready and wait_ready(timeout=15) is False:
            raise TimeoutError("engine runtime patch not ready")
        return handle
    except BaseException:
        if handle is not None:
            handle.close()
        raise


class Bridge:
    def __init__(self, engine: Path, *, isolate: bool, patch_module: Path | None,
                 require_patch: bool, log: EventLog):
        self.engine = engine
        self.isolate = isolate
        self.patch_module = patch_module
        self.require_patch = require_patch
        self.log = log
        self.proc: subprocess.Popen[bytes] | None = None
        self.patch_handle: Any = None
        self.job: Any = None
        self.parent_guard: Any = None
        self.wfp: Any = None
        self.stop = threading.Event()
        self.input_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.io_error: Exception | None = None
        self.stop_reason: str | None = None
        self.requested_shutdown: str | None = None
        self.forced_exit = False
        self.exit_logged = False
        self.native_uci_seen = False
        self.protocol_ready_logged = False

    def _mark_protocol_ready(self, method: str, *, search_validated: bool = False) -> None:
        assert self.proc is not None
        with self.state_lock:
            if self.protocol_ready_logged:
                return
            self.protocol_ready_logged = True
        self.log.write("protocol_ready", bridge_pid=os.getpid(),
                       engine_pid=self.proc.pid, engine_sha256=ENGINE_SHA256,
                       method=method, search_validated=search_validated)

    def _request_stop(self, reason: str, *, expected: bool = False) -> None:
        with self.state_lock:
            # A host closing stdin after the engine died must not turn a crash
            # already in progress into a clean quit in the diagnostic record.
            if expected and self.proc is not None and self.proc.poll() is None:
                self.requested_shutdown = self.requested_shutdown or reason
            self.stop_reason = self.stop_reason or reason
        self.log.write("shutdown_requested", reason=reason,
                       requested_shutdown=self.requested_shutdown)
        self.stop.set()

    def _load_isolation(self):
        if not self.isolate:
            return None
        try:
            import wfp_isolation as module
        except ImportError:
            source = Path(__file__).resolve().parents[1] / "jieqi" / "wfp_isolation.py"
            if not source.is_file():
                raise FileNotFoundError(source)
            module = _load_module(source)
        isolation = module.Isolation()
        try:
            isolation.add(self.engine)
            if len(isolation.ids) != 4:
                raise RuntimeError("engine WFP isolation incomplete")
            self.log.write("wfp_ready", filters=len(isolation.ids))
            return isolation
        except BaseException:
            isolation.close()
            raise

    def _emit(self, stream: Any, text: str) -> None:
        stream.write((text + "\n").encode("utf-8", "replace"))
        stream.flush()

    @staticmethod
    def _is_gui_plaintext(line: str) -> bool:
        token = line.strip().split(None, 1)[0].casefold() if line.strip() else ""
        return token in {"request", "hardware"}

    def _stdout_loop(self, out: Any) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for raw in iter(self.proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            self.log.write("engine_stdout", line=line)
            if line.strip().casefold() == "uciok pf":
                self.native_uci_seen = True
                # Release the host's verification event before it handles the
                # UCI completion. The native key and encrypted records stay
                # unchanged and retain their ordering.
                self._emit(out, EVENT_LINE)
                self.log.write("synthetic_event", line=EVENT_LINE)
                self.log.write("synthetic_before_uciok", line=EVENT_LINE)
            out.write(raw)
            out.flush()
            if line.strip().casefold() == "readyok":
                self._mark_protocol_ready("native_readyok")
            elif line.strip().casefold().startswith("bestmove "):
                self._mark_protocol_ready("native_bestmove", search_validated=True)
                self.log.write("search_result", bridge_pid=os.getpid(),
                               engine_pid=self.proc.pid, engine_sha256=ENGINE_SHA256,
                               method="native_bestmove", line=line, search_validated=True)
        self._request_stop("engine_stdout_eof")

    def _stderr_loop(self, err: Any) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        for raw in iter(self.proc.stderr.readline, b""):
            err.write(raw)
            err.flush()
            self.log.write("engine_stderr", line=raw.decode("utf-8", "replace").rstrip())

    def _write_engine(self, raw: bytes) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        with self.input_lock:
            self.proc.stdin.write(raw)
            self.proc.stdin.flush()

    def _stdin_loop(self, input_stream: Any) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        for raw in iter(input_stream.readline, b""):
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if self._is_gui_plaintext(line):
                self.log.write("filtered_host_line", line=line)
                continue
            try:
                forwarded_line = adapt_position_line(line)
            except FenCompatibilityError as exc:
                self.log.write("fen_compat_rejected", line=line, error=str(exc))
                self._request_stop("fen_compat_rejected")
                return
            if forwarded_line == line:
                # Preserve opaque protocol bytes exactly when this command is
                # not a GUI FEN that needs translation.
                forwarded_raw = raw
            else:
                forwarded_raw = forwarded_line.encode("utf-8", "replace")
                if raw.endswith(b"\r\n"):
                    forwarded_raw += b"\r\n"
                elif raw.endswith(b"\n"):
                    forwarded_raw += b"\n"
            if forwarded_line != line:
                self.log.write("fen_adapted", original=line, forwarded=forwarded_line)
            is_quit = line.strip().casefold() == "quit"
            if is_quit:
                self._request_stop("host_quit", expected=True)
            if self.patch_handle is not None and not is_quit:
                prepare = getattr(self.patch_handle, "prepare_search", None)
                if prepare:
                    # UCI_PF ciphertext has no stable textual command token.
                    # Refresh the local quota for every forwarded command.
                    prepare(threads=256)
            self._write_engine(forwarded_raw)
            self.log.write("host_stdin", line=line)
            token = line.strip().split(None, 1)[0].casefold() if line.strip() else ""
            if self.native_uci_seen and token not in {"", "uci", "quit", "stop", "request", "hardware"}:
                # In UCI_PF the host only sends later commands after its UCI
                # and verification gates open. Ciphertext proves that progress;
                # it does not prove an engine search completed.
                self._mark_protocol_ready("gui_post_handshake_command")
            if is_quit:
                return
        self._request_stop("host_eof", expected=True)

    def _guard(self, target: Callable[..., None], *args: Any) -> None:
        try:
            target(*args)
        except Exception as exc:
            self.io_error = exc
            self.log.write("io_error", operation=target.__name__, error=repr(exc))
            self._request_stop("io_error:" + target.__name__)

    def _quit_engine(self) -> None:
        try:
            self._write_engine(b"quit\n")
        except (BrokenPipeError, OSError, AttributeError):
            pass

    @staticmethod
    def _cancel_reader(thread: threading.Thread) -> None:
        """Unblock our own Windows stdin reader when the engine exits first."""
        if os.name != "nt" or not thread.is_alive() or thread.native_id is None:
            return
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenThread.restype = wintypes.HANDLE
        kernel.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
        kernel.CancelSynchronousIo.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenThread(1, False, thread.native_id)
        if handle:
            try:
                kernel.CancelSynchronousIo(handle)
            finally:
                kernel.CloseHandle(handle)

    def run(self, *, input_stream: Any = None, output_stream: Any = None,
            error_stream: Any = None) -> int:
        try:
            return self._run(input_stream or sys.stdin.buffer,
                             output_stream or sys.stdout.buffer,
                             error_stream or sys.stderr.buffer)
        except KeyboardInterrupt:
            self._request_stop("interrupted", expected=True)
            raise
        except BaseException:
            self._request_stop("startup_or_runtime_error")
            raise
        finally:
            self.close()

    def _run(self, input_stream: Any, output_stream: Any, error_stream: Any) -> int:
        guard_type = ParentGuard
        if guard_type is None:
            guard_type = _load_module(Path(__file__).with_name("parent_guard.py")).ParentGuard
        self.parent_guard = guard_type()
        self.log.write("parent_guard_ready", parent_pid=self.parent_guard.pid,
                       parent_creation_time=self.parent_guard.creation_time)
        if self.parent_guard.parent_exited():
            self._request_stop("parent_exit", expected=True)
            return 0
        self.wfp = self._load_isolation()
        self.proc = subprocess.Popen(
            [str(self.engine)], cwd=str(self.engine.parent), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            bufsize=0,
        )
        try:
            job_type = ChildJob
            if job_type is None:
                module = _load_module(Path(__file__).with_name("job_object.py"))
                job_type = module.ChildJob
            self.job = job_type(self.proc)
        except BaseException:
            if self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait(timeout=3)
            raise
        self.log.write("spawn", pid=self.proc.pid, engine=str(self.engine))

        # Attach/patch before forwarding any protocol command from the GUI.
        if self.patch_module or self.require_patch:
            try:
                if self.patch_module:
                    mod = _load_module(self.patch_module)
                else:
                    import runtime_patch as mod
                self.patch_handle = _apply_runtime_patch(mod, self.proc, self.engine,
                                                         self.log.write)
                self.log.write("runtime_patch_ready", module=str(self.patch_module))
            except Exception as exc:
                self.log.write("runtime_patch_error", error=repr(exc))
                if self.require_patch:
                    raise
                print(f"runtime patch skipped: {exc!r}", file=sys.stderr, flush=True)
        self.log.write("bridge_ready", bridge_pid=os.getpid(),
                       engine_pid=self.proc.pid, engine_sha256=ENGINE_SHA256)
        threads = [
            threading.Thread(target=self._guard, args=(self._stdout_loop, output_stream), daemon=True),
            threading.Thread(target=self._guard, args=(self._stderr_loop, error_stream), daemon=True),
            threading.Thread(target=self._guard, args=(self._stdin_loop, input_stream), daemon=True),
        ]
        for t in threads:
            t.start()
        try:
            while self.proc.poll() is None and not self.stop.wait(0.1):
                if self.parent_guard.parent_exited():
                    self._request_stop("parent_exit", expected=True)
                    break
            if self.proc.poll() is not None and self.stop_reason is None:
                self.stop_reason = "engine_exit"
            if self.proc.poll() is None:
                # A full engine stdin pipe must not hold the supervising thread
                # behind input_lock. The wait deadline always remains active.
                threading.Thread(target=self._quit_engine, daemon=True).start()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.forced_exit = True
                    self.log.write("child_terminate", reason=self.stop_reason)
                    self.proc.kill()
                    self.proc.wait(timeout=3)
            expected = self.requested_shutdown is not None
            rc = int(self.proc.returncode or 0)
            # The bundled engine currently reports 0xC0000409 on quit. Keep
            # that abnormal status visible even when quit/EOF was requested;
            # only a real zero exit is a clean bounded shutdown.
            if rc != 0:
                self.stop_reason = ("engine_crash_after_" + self.requested_shutdown
                                     if expected else "engine_crash")
                return 3
            if self.io_error and not expected:
                return 2
            # A spontaneous zero exit while the GUI is still connected is an
            # engine failure; host quit/EOF is the only expected zero exit.
            return 0 if expected else 3
        finally:
            self._cancel_reader(threads[2])
            for t in threads:
                t.join(timeout=1)

    def close(self) -> None:
        if self.job is not None:
            try:
                if self.proc is not None and self.proc.poll() is None:
                    self.forced_exit = True
                self.job.close()
                if self.proc is not None and self.proc.poll() is None:
                    self.proc.wait(timeout=3)
            except Exception as exc:
                self.log.write("job_close_error", error=repr(exc))
            self.job = None
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.forced_exit = True
                self.log.write("child_terminate", reason=self.stop_reason or "cleanup")
                self.proc.kill()
                self.proc.wait(timeout=3)
            except Exception as exc:
                self.log.write("engine_close_error", error=repr(exc))
        if self.proc is not None and not self.exit_logged:
            code = self.proc.poll()
            self.log.write("child_exit", engine_pid=self.proc.pid, returncode=code,
                           returncode_hex=hex(code & 0xffffffff) if code is not None else None,
                           reason=self.stop_reason or "cleanup",
                           expected=self.requested_shutdown is not None,
                           requested_shutdown=self.requested_shutdown, forced=self.forced_exit)
            self.exit_logged = True
        if self.patch_handle is not None:
            try:
                close = getattr(self.patch_handle, "close", None)
                if close:
                    close()
            except Exception as exc:
                self.log.write("runtime_patch_close_error", error=repr(exc))
            self.patch_handle = None
        if self.wfp is not None:
            try:
                self.wfp.close()
            except Exception as exc:
                self.log.write("wfp_close_error", error=repr(exc))
            self.wfp = None
        if self.parent_guard is not None:
            try:
                self.parent_guard.close()
            except Exception as exc:
                self.log.write("parent_guard_close_error", error=repr(exc))
            self.parent_guard = None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ACE JieQi UCI_PF process bridge")
    home = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
    ap.add_argument("--engine", type=Path, default=home / ENGINE_NAME)
    ap.add_argument("--patch-module", type=Path)
    ap.add_argument("--require-patch", action="store_true", default=True)
    ap.add_argument("--log", type=Path)
    ap.add_argument("--check-resources", action="store_true")
    args = ap.parse_args(argv)
    log = None
    bridge = None
    code = 2
    try:
        patch = args.patch_module.expanduser().resolve() if args.patch_module else None
        if patch is None and not getattr(sys, "frozen", False):
            auto = Path(__file__).resolve().parents[1] / "engine_reverse" / "runtime_patch.py"
            if auto.is_file():
                patch = auto
        if patch and not patch.is_file():
            raise FileNotFoundError(patch)
        if args.check_resources:
            module = _load_module(patch) if patch else __import__("runtime_patch")
            import frida
            try:
                import wfp_isolation
            except ImportError:
                wfp_isolation = _load_module(Path(__file__).resolve().parents[1] / "jieqi" / "wfp_isolation.py")
            print(json.dumps({"version": VERSION, "frozen": bool(getattr(sys, "frozen", False)),
                              "runtime_patch": bool(module.apply_patch), "early_patch": bool(module.impl.apply_patch),
                              "wfp": bool(wfp_isolation.Isolation), "parent_guard": bool(ParentGuard),
                              "frida": frida.__version__}))
            return 0
        log = EventLog(args.log.expanduser().resolve() if args.log else None)
        log.set_default(args.engine.expanduser().resolve(), os.getpid())
        log.write("bridge_start", bridge_version=VERSION, bridge_pid=os.getpid(),
                  expected_engine_sha256=ENGINE_SHA256, log=str(log.path))
        engine = validate_engine(args.engine)
        bridge = Bridge(engine, isolate=True, patch_module=patch,
                        require_patch=args.require_patch, log=log)
        code = bridge.run()
    except KeyboardInterrupt:
        code = 130
    except Exception as exc:
        if log:
            log.write("bridge_error", error=repr(exc))
        print(f"bridge error: {exc}", file=sys.stderr, flush=True)
        code = 2
    finally:
        if log:
            log.write("bridge_exit", bridge_pid=os.getpid(), returncode=code,
                      reason=bridge.stop_reason if bridge else "startup_error")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
