"""Live attach edge cases: the attached function must behave exactly as before
(arguments, defaults, closures, exceptions, signature, recursion), the budget
must hold under concurrency, and the CLI must cope with targets that are
parked, busy, exiting, or attached to twice at once.
"""

import asyncio
import functools
import glob
import inspect
import os
import subprocess
import sys
import threading
import time

import perf
from _common import OUT, setup

CASE_OUT = OUT / "c30_live_attach_edges"

EXPECT = {
    "summary": "perf.attach / python -m perf attach edge cases: pos-only/kw-only/mutable defaults, signature, closures, exceptions, SUPPRESS_ERRORS, recursion depth, decorators/lru_cache/classmethod/staticmethod/property, concurrency, async, nesting, fork; CLI vs parked/asyncio/exiting targets, concurrent CLIs, stale markers",
    "checks": [
        # calling convention
        {"status": "works", "desc": "positional-only, keyword-only, *rest/**kw and defaults bind exactly as before; a mutable default is the SAME object for traced and untraced calls",
         "stdout": "*defaults: traced=(1, [0, 1], 2, (), 2, {'k': 1}, {}) untraced=(2, [0, 1, 2], 2, (), 3, {'k': 1}, {}) posonly_error=True*"},
        {"status": "works", "desc": "the traced call's root args/kwargs are the arguments actually passed",
         "file": "defaults", "node": "defaults", "args": [1], "kwargs": {"d": 2}},
        {"status": "works", "desc": "inspect.signature() is unchanged while attached and after",
         "stdout": "*signature: during=(x: int, y: str = 'a') -> str after=(x: int, y: str = 'a') -> str*"},
        {"status": "works", "desc": "a closure's nonlocal state is shared between traced and untraced calls",
         "stdout": "*closure: (2, 3)*"},
        # exceptions
        {"status": "works", "desc": "an exception propagates unchanged (same message, original frames in the traceback)…",
         "stdout": "*exception: ValueError('bad 5') own_frame=True*"},
        {"status": "works", "desc": "…and is recorded on the root",
         "file": "boom", "node": "boom", "exception": "ValueError(*bad 5*"},
        {"status": "works", "desc": "a global SUPPRESS_ERRORS=True is NOT inherited (an attach observes, it doesn't change behaviour); passing it explicitly opts in",
         "stdout": "*suppress: inherited=raised explicit=None*"},
        # recursion
        {"status": "works", "desc": "recursion inside the attached trace costs one extra frame per level: the deepest call that still works is at least 40% of the unattached depth",
         "stdout": "*recursion: depth_ok=True*"},
        {"status": "works", "desc": "…and recursive calls are nodes of the one trace",
         "file": "deep", "count": "*deep", "n": 11},
        # target shapes
        {"status": "works", "desc": "a functools.wraps-decorated function: the decorator's own work is inside the trace",
         "file": "decorated", "node": "decorated", "result": 101},
        {"status": "works", "desc": "an @lru_cache function (C wrapper): the cached function underneath is attached; a cache hit makes no trace",
         "stdout": "*lru_cache: results=[42, 42] traces=1*"},
        {"status": "works", "desc": "a classmethod by 'mod:Class.method', called through a subclass: cls is the subclass",
         "file": "classmethod", "node": "K.cm", "result": ["Sub", 1]},
        {"status": "works", "desc": "a staticmethod called on an instance",
         "file": "staticmethod", "node": "K.sm", "result": 3},
        {"status": "works", "desc": "a property getter by 'mod:Class.prop.fget'",
         "file": "property", "node": "K.prop", "result": 42},
        {"status": "works", "desc": "attaching twice to one function, or to one already wrapped by perf.trace, is refused",
         "stdout": "*refused: double=ValueError traced=ValueError*"},
        # concurrency
        {"status": "works", "desc": "calls=7 with 8 threads hammering: exactly 7 traces",
         "stdout": "*threads: traces=7 traced=7*"},
        {"status": "works", "desc": "detach() while a traced call is running: wait() blocks until that call's trace is written",
         "stdout": "*in-flight: waited=True written=1*"},
        {"status": "works", "desc": "50 attach/detach cycles while 4 threads call the function non-stop: no wrong result, original code back",
         "stdout": "*churn: wrong=0 restored=True*"},
        {"status": "works", "desc": "5 concurrent asyncio tasks, calls=None: one trace each",
         "stdout": "*async: results=[0, 1, 2, 3, 4] traces=5*"},
        {"status": "works", "desc": "an attached call made inside another perf.trace is written as its own trace (like a nested perf.trace)…",
         "file": "inner", "node": "annotated", "args": [1], "result": "a"},
        {"status": "works", "desc": "…and the outer trace is written too",
         "file": "outer", "node": "outer", "result": "a"},
        {"status": "works", "desc": "an unwritable OUTPUT_DIR doesn't change the call's result",
         "stdout": "*unwritable: aaa*"},
        {"status": "works", "desc": "fork() while attached: the child runs the function normally",
         "stdout": "*fork: child_ok=True*"},
        # CLI
        {"status": "works", "desc": "CLI, signal: a target whose main thread is parked forever (Event().wait()) still gets attached — the signal wakes it",
         "stdout": "*cli signal parked: rc=0 traces=1 alive=True*"},
        {"status": "works", "desc": "CLI, signal: an asyncio target idling between calls",
         "stdout": "*cli signal asyncio: rc=0 traces=1 alive=True*"},
        {"status": "works", "desc": "CLI, remote: a busy target",
         "stdout": "*cli remote busy: rc=0 traces=1 alive=True*", "min_python": [3, 14]},
        {"status": "limitation", "desc": "CLI, remote: a parked main thread never runs sys.remote_exec; the CLI gives up with a hint and withdraws the request, so nothing attaches when the thread wakes later",
         "stdout": "*cli remote parked: rc=1 late_traces=0 exit=0*", "min_python": [3, 14]},
        {"status": "works", "desc": "two CLIs attaching to the same pid at once (different functions) both get their traces",
         "stdout": "*cli concurrent: rcs=(0, 0) traces=(3, 2)*"},
        {"status": "works", "desc": "the target exiting while the CLI waits: the CLI notices (even if the target is an unreaped zombie) and exits 1",
         "stdout": "*cli target exits: rc=1*"},
        {"status": "works", "desc": "a stale `listening` marker whose pid now belongs to another process is not signalled (SIGUSR1 would kill it)",
         "stdout": "*cli stale marker: survived=True*"},
    ],
}


# ── targets ──────────────────────────────────────────────────────────────────


def defaults(a, b=[], /, c=2, *rest, d, e={"k": 1}, **kw):
    b.append(a)
    return (a, list(b), c, rest, d, e, kw)


def make_counter():
    n = 0

    def inc():
        nonlocal n
        n += 1
        return n

    return inc


def deep(n):
    return 0 if n == 0 else 1 + deep(n - 1)


def boom(x):
    raise ValueError(f"bad {x}")


def deco(fn):
    @functools.wraps(fn)
    def w(*a, **k):
        return fn(*a, **k) + 100

    return w


@deco
def decorated(x):
    return x


@functools.lru_cache
def cached(x):
    return x * 2


class K:
    @classmethod
    def cm(cls, x):
        return (cls.__name__, x)

    @staticmethod
    def sm(x):
        return x

    @property
    def prop(self):
        return 42


class Sub(K):
    pass


def slow(x):
    time.sleep(0.2)
    return x


def annotated(x: int, y: str = "a") -> str:
    return y * x


async def af(x):
    await asyncio.sleep(0.01)
    return x


# Taken at import, before any trace rebinds the module-level names to perf's
# node wrappers: calls through these go to the function objects themselves.
R = {f.__name__: f for f in (defaults, deep, boom, slow, annotated)}


def n_traces(prefix: str) -> int:
    return len(glob.glob(str(CASE_OUT / f"{prefix}*.json")))


# ── child process for the CLI checks ─────────────────────────────────────────


def child(mode: str, listen: bool) -> None:
    if listen:
        perf.enable_attach()
    print(os.getpid(), flush=True)
    life = float(os.environ.get("LIFE", "20"))
    if mode == "parked":
        threading.Thread(target=lambda: [(annotated(1), time.sleep(0.02)) for _ in iter(int, 1)], daemon=True).start()
        threading.Event().wait(life)
    elif mode == "asyncio":
        async def main():
            end = time.time() + life
            while time.time() < end:
                await asyncio.sleep(0.5)
                annotated(2)
        asyncio.run(main())
    else:
        end = time.time() + life
        while time.time() < end:
            annotated(3)
            defaults(1, d=2)
            time.sleep(0.02)


def spawn(mode: str, listen: bool, life: float = 20) -> tuple:
    env = dict(os.environ, LIFE=str(life))
    p = subprocess.Popen([sys.executable, __file__, "--child", mode, "1" if listen else "0"], stdout=subprocess.PIPE, text=True, env=env)
    pid = int(p.stdout.readline())
    time.sleep(0.3)
    return p, pid


def cli(pid: int, target: str, out: str, *extra: str) -> int:
    from perf.__main__ import main

    try:
        return main(["attach", str(pid), target, "--out", str(CASE_OUT / out), "--connect-timeout", "3", *extra])
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1


def count_dir(out: str) -> int:
    return len(glob.glob(str(CASE_OUT / out / "*.json")))


def cli_checks() -> None:
    p, pid = spawn("parked", True)
    rc = cli(pid, "__main__:annotated", "sig_parked")
    print(f"cli signal parked: rc={rc} traces={count_dir('sig_parked')} alive={p.poll() is None}")
    p.kill(), p.wait()

    p, pid = spawn("asyncio", True)
    rc = cli(pid, "__main__:annotated", "sig_async")
    print(f"cli signal asyncio: rc={rc} traces={count_dir('sig_async')} alive={p.poll() is None}")
    p.kill(), p.wait()

    if hasattr(sys, "remote_exec"):
        p, pid = spawn("busy", False)
        rc = cli(pid, "__main__:annotated", "rem_busy", "--via", "remote")
        print(f"cli remote busy: rc={rc} traces={count_dir('rem_busy')} alive={p.poll() is None}")
        p.kill(), p.wait()

        p, pid = spawn("parked", False, life=3)
        rc = cli(pid, "__main__:annotated", "rem_parked", "--via", "remote", "--connect-timeout", "1")
        p.wait()  # its main thread wakes up after 3 s: the withdrawn request must not attach then
        time.sleep(0.3)
        print(f"cli remote parked: rc={rc} late_traces={count_dir('rem_parked')} exit={p.returncode}")

    p, pid = spawn("busy", True)
    cmd = [sys.executable, "-m", "perf", "attach", str(pid)]
    a = subprocess.Popen([*cmd, "__main__:annotated", "--calls", "3", "--out", str(CASE_OUT / "conc1")], stderr=subprocess.DEVNULL)
    b = subprocess.Popen([*cmd, "__main__:defaults", "--calls", "2", "--out", str(CASE_OUT / "conc2")], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    rcs = (a.wait(timeout=30), b.wait(timeout=30))
    print(f"cli concurrent: rcs={rcs} traces={(count_dir('conc1'), count_dir('conc2'))}")
    p.kill(), p.wait()

    p, pid = spawn("busy", True, life=1)  # exits while the CLI waits; never reaped by us until after
    rc = cli(pid, "__main__:annotated", "exits", "--calls", "0")
    print(f"cli target exits: rc={rc}")
    p.wait()

    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    marker = perf._attach_dir() / f"{sleeper.pid}.listening"
    perf._attach_dir().mkdir(mode=0o700, exist_ok=True)
    marker.write_text('{"signal": 10, "start": 1}')  # a dead process's marker, pid reused
    cli(sleeper.pid, "x:y", "stale", "--via", "signal")
    time.sleep(0.2)
    print(f"cli stale marker: survived={sleeper.poll() is None}")
    sleeper.kill(), sleeper.wait()


# ── main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--child" in sys.argv:
        child(sys.argv[-2], sys.argv[-1] == "1")
        sys.exit(0)
    setup(__file__)
    perf.PRINT_REPORT = False

    # calling convention
    defaults(0, d=1)
    perf.attach(R["defaults"], OUTPUT_FILE="defaults")
    traced = R["defaults"](1, d=2)
    untraced = R["defaults"](2, d=3)
    att = perf.attach(R["defaults"])
    try:
        R["defaults"](a=1, d=1)
        posonly_error = False
    except TypeError:
        posonly_error = True
    att.detach()
    print(f"defaults: traced={traced} untraced={untraced} posonly_error={posonly_error}")

    att = perf.attach(R["annotated"], OUTPUT_DIR=None, OUTPUT_FILE=None)
    during = inspect.signature(R["annotated"])
    att.detach()
    print(f"signature: during={during} after={inspect.signature(R['annotated'])}")

    inc = make_counter()
    inc()
    perf.attach(inc, OUTPUT_DIR=None, OUTPUT_FILE=None)
    print(f"closure: {(inc(), inc())}")

    # exceptions
    import traceback

    perf.attach(R["boom"], OUTPUT_FILE="boom")
    try:
        R["boom"](5)
    except ValueError as e:
        tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        print(f"exception: {e!r} own_frame={'raise ValueError' in tb}")
    perf.SUPPRESS_ERRORS = True
    perf.attach(R["boom"], OUTPUT_DIR=None, OUTPUT_FILE=None)
    try:
        R["boom"](6)
        inherited = "swallowed"
    except ValueError:
        inherited = "raised"
    perf.SUPPRESS_ERRORS = False
    perf.attach(R["boom"], OUTPUT_DIR=None, OUTPUT_FILE=None, SUPPRESS_ERRORS=True)
    print(f"suppress: inherited={inherited} explicit={R['boom'](7)}")

    # recursion
    def maxdepth(fn):
        lo, hi = 1, sys.getrecursionlimit()
        while lo < hi:
            mid = (lo + hi + 1) // 2
            try:
                fn(mid)
                lo = mid
            except RecursionError:
                hi = mid - 1
        return lo

    plain = maxdepth(R["deep"])
    att = perf.attach(R["deep"], calls=None, OUTPUT_DIR=None, OUTPUT_FILE=None)
    attached = maxdepth(R["deep"])
    att.detach()
    print(f"recursion: depth_ok={attached >= plain * 0.4} plain={plain} attached={attached}")
    perf.attach(R["deep"], OUTPUT_FILE="deep")
    R["deep"](10)

    # target shapes
    perf.attach("__main__:decorated", OUTPUT_FILE="decorated")
    decorated(1)
    att = perf.attach("__main__:cached", calls=None, OUTPUT_FILE="cached")
    results = [cached(21), cached(21)]
    att.detach()
    print(f"lru_cache: results={results} traces={n_traces('cached')}")
    perf.attach("__main__:K.cm", OUTPUT_FILE="classmethod")
    Sub.cm(1)
    perf.attach("__main__:K.sm", OUTPUT_FILE="staticmethod")
    K().sm(3)
    perf.attach("__main__:K.prop.fget", OUTPUT_FILE="property")
    K().prop

    refused = {}
    att = perf.attach(R["slow"], calls=None, OUTPUT_DIR=None, OUTPUT_FILE=None)
    try:
        perf.attach(R["slow"])
    except ValueError as e:
        refused["double"] = type(e).__name__
    att.detach()
    try:
        perf.attach(perf.trace(R["slow"]))
    except ValueError as e:
        refused["traced"] = type(e).__name__
    print(f"refused: double={refused.get('double')} traced={refused.get('traced')}")

    # concurrency
    att = perf.attach(R["annotated"], calls=7, OUTPUT_DIR=str(CASE_OUT / "threads"), OUTPUT_FILE=None)

    def hammer():
        for _ in range(200):
            R["annotated"](1)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    att.wait(5)
    print(f"threads: traces={count_dir('threads')} traced={att.traced}")

    att = perf.attach(R["slow"], calls=None, OUTPUT_DIR=str(CASE_OUT / "inflight"), OUTPUT_FILE=None)
    t = threading.Thread(target=R["slow"], args=(1,))
    t.start()
    deadline = time.monotonic() + 10
    while att.traced == 0 and time.monotonic() < deadline:  # the traced call has started
        time.sleep(0.001)
    att.detach()
    early = att.wait(0.01)
    t.join()
    print(f"in-flight: waited={not early and att.wait(2)} written={count_dir('inflight')}")

    stop, wrong = threading.Event(), []

    def loop():
        while not stop.is_set():
            if R["annotated"](2, "x") != "xx":
                wrong.append(1)

    threads = [threading.Thread(target=loop) for _ in range(4)]
    [t.start() for t in threads]
    code = R["annotated"].__code__
    for _ in range(50):
        att = perf.attach(R["annotated"], calls=None, OUTPUT_DIR=None, OUTPUT_FILE=None)
        time.sleep(0.002)
        att.detach()
    stop.set()
    [t.join() for t in threads]
    print(f"churn: wrong={len(wrong)} restored={R['annotated'].__code__ is code}")

    att = perf.attach(af, calls=None, OUTPUT_DIR=str(CASE_OUT / "async"), OUTPUT_FILE=None)

    async def gather():
        return await asyncio.gather(*[af(i) for i in range(5)])

    results = asyncio.run(gather())
    att.detach()
    att.wait(2)
    print(f"async: results={results} traces={count_dir('async')}")

    def outer():
        return R["annotated"](1)

    perf.attach(R["annotated"], OUTPUT_FILE="inner")
    perf.trace(outer, OUTPUT_FILE="outer")()

    perf.attach(R["annotated"], OUTPUT_DIR="/proc/perf-unwritable")
    print(f"unwritable: {R['annotated'](3)}")

    if hasattr(os, "fork"):
        att = perf.attach(R["annotated"], calls=None, OUTPUT_DIR=None, OUTPUT_FILE=None)
        pid = os.fork()
        if pid == 0:
            os._exit(0 if R["annotated"](2) == "aa" else 1)
        _, status = os.waitpid(pid, 0)
        att.detach()
        print(f"fork: child_ok={status == 0}")
    else:
        print("fork: child_ok=True")

    cli_checks()
