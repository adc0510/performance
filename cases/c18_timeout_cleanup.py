"""TIMEOUT cleanup: finally blocks, signal/alarm state, traces after a timeout."""

import asyncio
import signal
import time

import perf
from _common import setup

EXPECT = {
    "new": True,
    "summary": "TIMEOUT cleanup: nested partial tree, finally blocks run, previous SIGALRM handler and pending alarm restored, later traces clean, same event loop reusable",
    "checks": [
        {"status": "works", "desc": "timeout inside nested calls: every open node on the stack records the TimeoutError",
         "file": "nested", "path": ["nested", "__main__.outer", "__main__.inner_hang"]},
        {"status": "works", "desc": "...the innermost open call records it", "file": "nested",
         "node": "__main__.inner_hang", "exception": "TimeoutError(*exceeded TIMEOUT*"},
        {"status": "works", "desc": "...calls completed before the timeout are kept", "file": "nested",
         "node": "__main__.step", "result": 1, "lacks": ["exception"]},
        {"status": "works", "desc": "the timed-out code's `finally` blocks run (and calls made in them are traced)",
         "file": "nested", "node": "__main__.cleanup", "result": "cleaned"},
        {"status": "works", "desc": "the caller's own SIGALRM handler is restored afterwards",
         "stdout": "*handler restored: True*"},
        {"status": "works", "fixes": "TIMEOUT cancelled the caller's pending alarm()/setitimer", "desc": "a caller's alarm that was pending BEFORE the trace is preserved (remaining time minus elapsed), not cancelled",
         "stdout": "*pending alarm kept: True*"},
        {"status": "works", "desc": "...and it still fires", "stdout": "*user alarm fired: True*"},
        {"status": "works", "desc": "a trace after a timed-out trace is clean: no leftover nodes, no spurious timeout",
         "file": "after", "node": "after", "result": 3, "lacks": ["exception"]},
        {"status": "works", "desc": "...its tree has only its own calls", "file": "after", "count": "*", "n": 2},
        {"status": "works", "desc": "async: after a timed-out trace, another traced coroutine runs fine on the same event loop",
         "file": "async_after", "node": "async_after", "result": "fine", "lacks": ["exception"]},
    ],
}


def step(x):
    return x


def cleanup():
    return "cleaned"


def inner_hang():
    try:
        time.sleep(5)
    finally:
        cleanup()


def outer():
    step(1)
    inner_hang()


def add(a, b):
    return a + b


@perf.trace(TIMEOUT=0.2, OUTPUT_FILE="nested")
def nested():
    outer()


@perf.trace(TIMEOUT=5, OUTPUT_FILE="after")
def after():
    return add(1, 2)


async def asleep():
    await asyncio.sleep(5)


@perf.trace(TIMEOUT=0.1, OUTPUT_FILE="async_timeout")
async def async_timeout():
    await asleep()


@perf.trace(TIMEOUT=5, OUTPUT_FILE="async_after")
async def async_after():
    await asyncio.sleep(0.01)
    return "fine"


async def both():
    try:
        await async_timeout()
    except TimeoutError:
        pass
    await async_after()


if __name__ == "__main__":
    setup(__file__)
    fired = []

    def _user_handler(signum, frame):
        fired.append(signum)

    # Keep the exact object registered inside a container: perf rebinds every
    # module-level NAME bound to a project function to its wrapper while
    # tracing (see c20), so a plain global would no longer be `is`-identical
    # to what signal holds.
    registered = [_user_handler]
    signal.signal(signal.SIGALRM, registered[0])
    signal.setitimer(signal.ITIMER_REAL, 1.0)  # the caller's own alarm, pending before tracing
    try:
        nested()
    except TimeoutError:
        pass
    remaining, _ = signal.getitimer(signal.ITIMER_REAL)
    print("handler restored:", signal.getsignal(signal.SIGALRM) is registered[0])
    print("pending alarm kept:", 0.5 < remaining < 0.85, round(remaining, 2))
    after()
    time.sleep(1.0)
    print("user alarm fired:", fired == [signal.SIGALRM])
    asyncio.run(both())
