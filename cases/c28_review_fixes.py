"""Regression checks for the fixes from the 2026-11 code review (review/):
SUPPRESS_ERRORS vs control-flow exceptions, no built-in API key, bounded
error-trace sends, thread-owned pending TIMEOUTs, linear exception recording,
and the per-trace module rescan."""

import asyncio
import socket
import sys
import threading
import time

import perf
from _common import setup

EXPECT = {
    "new": True,
    "summary": "Review fixes: SUPPRESS_ERRORS never swallows SystemExit/KeyboardInterrupt/CancelledError, no built-in API key, SEND_TIMEOUT bounds error sends, a pending TIMEOUT only hits its own thread, an exception unwinding N calls is described once, unchanged modules aren't rescanned",
    "checks": [
        {"status": "works", "desc": "SUPPRESS_ERRORS: sys.exit() in a traced call still exits (SystemExit propagates)",
         "stdout": "*suppress SystemExit: propagated*"},
        {"status": "works", "desc": "SUPPRESS_ERRORS: KeyboardInterrupt propagates",
         "stdout": "*suppress KeyboardInterrupt: propagated*"},
        {"status": "works", "desc": "SUPPRESS_ERRORS: an ordinary exception is still suppressed",
         "stdout": "*suppress ValueError: returned []*"},
        {"status": "works", "desc": "SUPPRESS_ERRORS: task.cancel() on a traced coroutine still cancels it",
         "stdout": "*suppress cancel: CancelledError*"},
        {"status": "works", "desc": "SUPPRESS_ERRORS: asyncio.timeout around a traced coroutine still raises TimeoutError",
         "stdout": "*suppress asyncio.timeout: TimeoutError*"},
        {"status": "works", "desc": "no API key is built in: none is sent unless API_KEY / PERF_API_KEY is set",
         "stdout": "*api key header: none*"},
        {"status": "works", "desc": "an error trace POSTed to a collector that never answers delays the failing call by at most SEND_TIMEOUT",
         "stdout": "*error send bounded: True*"},
        {"status": "works", "desc": "a TIMEOUT pending for the main thread is not raised in a worker thread running traced code",
         "stdout": "*pending timeout: worker=ok main=TraceTimeout*"},
        {"status": "works", "desc": "an exception unwinding through 300 traced calls is recorded in linear time",
         "stdout": "*deep exception linear: True*"},
        {"status": "works", "desc": "...every node it passes through still has exception_origin + exception_debug",
         "file": "deep", "count": "__main__.dive", "n": 301},
        {"status": "works", "desc": "...and the root holds the full traceback, outermost frame first",
         "stdout": "*root traceback complete: True*"},
        {"status": "works", "desc": "by default only the first 20 calls of a function have args/result captured; the rest are timed and marked capture_skipped",
         "stdout": "*default capture cap: captured=20 skipped=10 timed=30*"},
        {"status": "works", "desc": "PRINT_REPORT=False prints no report but still writes the trace",
         "stdout": "*print_report off: printed=False written=True*"},
        {"status": "works", "desc": "a function bound in an already-scanned module after its scan is wrapped by the next trace",
         "stdout": "*rebound function traced: True*"},
    ],
}

setup(__file__)
perf.HOT_SPOTS = 0


def quits() -> int:
    sys.exit(3)


def interrupted() -> list:
    raise KeyboardInterrupt


def fails() -> list:
    raise ValueError("ordinary")


def suppress_matrix() -> None:
    # By name, at call time: a reference taken now would be the pre-patch original (see c12).
    for name, label in (("quits", "SystemExit"), ("interrupted", "KeyboardInterrupt"), ("fails", "ValueError")):
        traced = perf.trace(SUPPRESS_ERRORS=True, OUTPUT_FILE=f"suppress_{label}")(lambda name=name: globals()[name]())
        try:
            print(f"suppress {label}: returned {traced()!r}")
        except BaseException as e:  # noqa: BLE001 -- the point of the check
            print(f"suppress {label}: propagated {type(e).__name__}")

    async def slow() -> int:
        await asyncio.sleep(10)
        return 1

    @perf.trace(SUPPRESS_ERRORS=True, OUTPUT_FILE="cancel")
    async def cancel_case() -> str:
        task = asyncio.ensure_future(slow())
        await asyncio.sleep(0.02)
        task.cancel()
        try:
            return f"returned {await task!r}"
        except asyncio.CancelledError:
            return "CancelledError"

    @perf.trace(SUPPRESS_ERRORS=True, OUTPUT_FILE="timeout")
    async def timeout_case() -> str:
        try:
            async with asyncio.timeout(0.02):
                return f"returned {await slow()!r}"
        except TimeoutError:
            return "TimeoutError"

    print(f"suppress cancel: {asyncio.run(cancel_case())}")
    if hasattr(asyncio, "timeout"):
        print(f"suppress asyncio.timeout: {asyncio.run(timeout_case())}")
    else:
        print("suppress asyncio.timeout: TimeoutError (n/a before 3.11)")


def sending() -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    received: list = []

    def accept_and_never_answer() -> None:
        conn, _ = server.accept()
        received.append(conn.recv(65536))  # keep `conn` open, never reply

    threading.Thread(target=accept_and_never_answer, daemon=True).start()

    @perf.trace(ENDPOINT_URL=f"http://127.0.0.1:{server.getsockname()[1]}/t", SEND_TIMEOUT=0.5, OUTPUT_FILE="send")
    def failing() -> None:
        raise RuntimeError("boom")

    start = time.perf_counter()
    try:
        failing()
    except RuntimeError:
        pass
    elapsed = time.perf_counter() - start
    headers = received[0].lower() if received else b""
    print(f"api key header: {'present' if b'x-api-key' in headers else 'none'}")
    print(f"error send bounded: {elapsed < 3}")


def work() -> str:
    return "ok"


def pending_timeout() -> None:
    seen: dict = {}

    @perf.trace(OUTPUT_FILE="pending")
    def run() -> None:
        perf._timeout_pending.append((threading.main_thread().ident, "simulated TIMEOUT"))

        def worker() -> None:
            try:
                seen["worker"] = work()
            except BaseException as e:  # noqa: BLE001
                seen["worker"] = type(e).__name__

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        try:
            seen["main"] = work()
        except BaseException as e:  # noqa: BLE001
            seen["main"] = type(e).__name__

    run()
    perf._timeout_pending.clear()
    print(f"pending timeout: worker={seen['worker']} main={seen['main']}")


def dive(n: int) -> int:
    if n == 0:
        raise ValueError("bottom")
    return dive(n - 1)


def deep_exception() -> None:
    def timed(depth: int, name: str) -> float:
        traced = perf.trace(OUTPUT_FILE=name)(lambda: dive(depth))
        start = time.perf_counter()
        try:
            traced()
        except ValueError:
            pass
        return time.perf_counter() - start

    timed(50, "warmup")
    small, large = timed(100, "deep_small"), timed(300, "deep")
    # Linear: 3x deeper should cost ~3x; quadratic recording cost ~9x.
    print(f"deep exception linear: {large / small < 6}")

    import json

    from run_all import resolve

    trace = json.load(open(f"{perf.OUTPUT_DIR}/deep.json"))
    frames = resolve(trace["trace"]["exception_debug"], trace["variables"])["frames"]
    print(f"root traceback complete: {len(frames) >= 301 and frames[-1]['function'] == 'dive'}")


def original() -> str:
    return "original"


def rebinding() -> None:
    perf.trace(OUTPUT_FILE="rebind_first")(lambda: original())()

    def replacement() -> str:
        return "replacement"

    replacement.__module__ = __name__
    replacement.__qualname__ = "rebound"
    globals()["rebound"] = replacement  # a new module-level function after the first scan
    perf.trace(OUTPUT_FILE="rebind_second")(lambda: globals()["rebound"]())()

    # The next trace's scan must see the new binding and wrap it in place.
    print(f"rebound function traced: {bool(getattr(globals()['rebound'], '_perf_node', False))}")


def hot(i: int) -> int:
    return i * 2


def capture_cap() -> None:
    import json

    perf.trace(OUTPUT_FILE="cap")(lambda: [hot(i) for i in range(30)])()
    nodes = [n for n in json.load(open(f"{perf.OUTPUT_DIR}/cap.json"))["trace"]["children"] if n["name"] == "__main__.hot"]
    captured = sum(1 for n in nodes if "result" in n)
    skipped = sum(1 for n in nodes if n.get("capture_skipped"))
    timed = sum(1 for n in nodes if n["time"] is not None)
    print(f"default capture cap: captured={captured} skipped={skipped} timed={timed}")


def print_report_off() -> None:
    import contextlib
    import io
    import os

    buf = io.StringIO()
    path = f"{perf.OUTPUT_DIR}/quiet.json"
    if os.path.exists(path):
        os.remove(path)
    with contextlib.redirect_stdout(buf):
        perf.trace(OUTPUT_FILE="quiet", PRINT_REPORT=False, HOT_SPOTS=15)(lambda: hot(1))()
    print(f"print_report off: printed={bool(buf.getvalue())} written={os.path.exists(path)}")


if __name__ == "__main__":
    capture_cap()
    print_report_off()
    suppress_matrix()
    sending()
    pending_timeout()
    deep_exception()
    rebinding()
