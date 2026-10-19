"""Bridge-level replay of a captured GUI FEN without starting the GUI.

This deliberately uses ``isolate=False``: the source bridge, runtime patch,
child job and protocol adapter are exercised, while WFP setup is omitted so a
developer test does not mutate host firewall state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import queue
import threading
import time
from pathlib import Path

from bridge import Bridge, EventLog, ENGINE_SHA256


GUI_FEN = (
    "position fen "
    "xxxxkxxxx/9/1x5x1/x1x1x1x1x/9/9/X1X1X1X1X/1X5X1/9/XXXXKXXXX "
    "w A2B2N2R2C2P5a2b2n2r2c2p5 - - 0 1"
)


class InputFeed:
    def __init__(self) -> None:
        self.items: queue.Queue[bytes] = queue.Queue()

    def feed(self, line: str | bytes) -> None:
        self.items.put(line.encode() if isinstance(line, str) else line)

    def readline(self) -> bytes:
        return self.items.get()


class Capture:
    def __init__(self) -> None:
        self.data = bytearray()
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)

    def write(self, data: bytes) -> int:
        with self.changed:
            self.data.extend(data)
            self.changed.notify_all()
        return len(data)

    def flush(self) -> None:
        return None

    def lines(self) -> list[str]:
        with self.lock:
            return self.data.decode("utf-8", "replace").splitlines()

    def wait_for(self, predicate, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self.changed:
            while True:
                if predicate(self.data.decode("utf-8", "replace").splitlines()):
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.changed.wait(remaining)


def run(out_path: Path, log_path: Path) -> dict:
    root = Path(__file__).resolve().parents[2]
    engine = root / "work/jieqi/private/test_copy/App/Engine/ACE_JieQi_64.exe"
    runtime = root / "work/engine_reverse/runtime_patch.py"
    digest = hashlib.sha256(engine.read_bytes()).hexdigest()
    if digest != ENGINE_SHA256:
        raise RuntimeError(f"engine hash mismatch: {digest}")
    source = InputFeed()
    output = Capture()
    error = Capture()
    bridge = Bridge(
        engine, isolate=False, patch_module=runtime, require_patch=True,
        log=EventLog(log_path),
    )
    result: dict = {
        "engine": str(engine),
        "engine_sha256": digest,
        "isolate": False,
        "gui_fen": GUI_FEN,
    }
    run_result: list[int] = []

    def supervise() -> None:
        try:
            run_result.append(bridge.run(
                input_stream=source, output_stream=output, error_stream=error,
            ))
        except BaseException as exc:  # retain diagnostics in the JSON artifact
            result["bridge_exception"] = repr(exc)

    thread = threading.Thread(target=supervise, daemon=True)
    thread.start()
    try:
        source.feed("uci\n")
        if not output.wait_for(lambda xs: "uciok pf" in xs, 15):
            raise TimeoutError("bridge did not forward uciok pf")
        source.feed("isready\n")
        if not output.wait_for(lambda xs: "readyok" in xs, 15):
            raise TimeoutError("bridge did not forward readyok")
        source.feed("setoption name Threads value 1\n")
        source.feed(GUI_FEN + "\n")
        source.feed("go movetime 1000\n")
        if not output.wait_for(lambda xs: any(x.startswith("bestmove ") for x in xs), 15):
            raise TimeoutError("bridge did not produce bestmove")
        source.feed("quit\n")
        thread.join(12)
        if thread.is_alive():
            raise TimeoutError("bridge did not exit after quit")
    finally:
        if thread.is_alive():
            bridge._request_stop("replay_cleanup", expected=True)
            thread.join(5)
    result.update({
        "bridge_returncode": run_result[0] if run_result else None,
        "stdout": output.lines(),
        "stderr": error.lines(),
        "bestmoves": [x for x in output.lines() if x.startswith("bestmove ")],
        "log": str(log_path),
    })
    rows = [json.loads(x) for x in log_path.read_text(encoding="utf-8").splitlines()]
    result["events"] = rows
    adapted = [x for x in rows if x.get("event") == "fen_adapted"]
    result["fen_adapted"] = adapted
    result["search_results"] = [x for x in rows if x.get("event") == "search_result"]
    if not result["bestmoves"] or not adapted or not result["search_results"]:
        raise AssertionError("bridge replay did not prove adaptation and search")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.out, args.log), ensure_ascii=False, indent=2))
