"""Live attach: tracing the next calls of a function in a process that is
already running, without restarting it or editing its source.

In-process: perf.attach(target, calls=..., seconds=..., **config). Across
processes: `python -m perf attach <pid> <target>`, delivered by a signal
(the target called perf.enable_attach()) or by sys.remote_exec (3.14+,
nothing prepared in the target).
"""

import asyncio
import inspect
import json
import subprocess
import sys
import time

import perf
from _common import OUT, setup
from app import registry, util
from app.repo_sql import SqlRepo
from app.util import normalize as norm_alias  # a reference taken long before the attach

EXPECT = {
    "summary": "perf.attach on a function every caller already holds (registry dict, `from x import`), methods using super(), recursion, async, call/time budgets, bad config, and `python -m perf attach <pid>` against a live child process",
    "checks": [
        {"status": "works", "desc": "a function only ever called through a dict filled at import time is traced: the function object itself is switched over, not a name",
         "file": "registry", "node": "handle_upper", "args": ["abc"], "result": "ABC"},
        {"status": "works", "desc": "a caller holding `from app.util import normalize` (taken before the attach) is traced too",
         "file": "alias", "node": "normalize", "args": ["A  B"], "result": "a b"},
        {"status": "works", "desc": "the attached call's callees nest under it, as with @perf.trace",
         "file": "callees", "path": ["slugify", "app.util.normalize"]},
        {"status": "works", "desc": "a method using zero-arg super() (a __class__ closure cell) attached by 'module:Class.method' string",
         "file": "method", "path": ["SqlRepo.get", "Repo.get"]},
        {"status": "works", "desc": "recursion: one trace per top-level call; the recursive calls are nodes inside it, not traces of their own",
         "file": "recursion", "count": "*fact", "n": 5},
        {"status": "works", "desc": "an async function: the trace covers the awaited body",
         "file": "async", "node": "fetch", "args": [3], "result": 6},
        {"status": "works", "desc": "TRACE_LOCALS=True is short for the attached function's own locals",
         "file": "locals", "node": "fact", "has": ["locals"], "min_python": [3, 12]},
        {"status": "works", "desc": "calls=2 traces exactly two calls, then the original code (and signature/defaults) is back",
         "stdout": "*budget: traced=2 reason=calls restored=True signature=True defaults=(3,)*"},
        {"status": "works", "desc": "seconds=… stops after the window; detach() stops at once",
         "stdout": "*window: reason=seconds*detach: reason=detached restored=True*"},
        {"status": "works", "desc": "a bad config value is rejected by attach() itself, before the program's next call could fail on it",
         "stdout": "*bad config: TypeError perf.attach: TRACE_LOCALS must be a string or a list of strings, got 7; untouched=True*"},
        {"status": "works", "desc": "attaching to a module the process never imported, or a generator function, is refused",
         "stdout": "*refused: LookupError TypeError*"},
        {"status": "works", "desc": "`python -m perf attach <pid>` (signal: target called perf.enable_attach()) traces the live child's next call into --out",
         "file": "live", "node": "handle_lower", "args": ["LIVE"], "result": "live"},
        {"status": "works", "desc": "…and the child kept running unaffected, then exited normally",
         "stdout": "*signal child: rc=0 exit=0 wrong=0*"},
        {"status": "works", "desc": "`python -m perf attach <pid> --via remote` (sys.remote_exec, nothing prepared in the target) traces the live child",
         "file": "remote", "node": "handle_lower", "args": ["LIVE"], "min_python": [3, 14]},
        {"status": "works", "desc": "…and that child kept running unaffected too",
         "stdout": "*remote child: rc=0 exit=0 wrong=0*", "min_python": [3, 14]},
    ],
}


def fact(n):
    return 1 if n <= 1 else n * fact(n - 1)


async def fetch(x):
    await asyncio.sleep(0)
    return x * 2


def scaled(x, scale=3):
    return x * scale


# Taken at import: by the time it's checked, earlier traces have rebound the
# module-level name `scaled` to perf's node wrapper (dict values stay put).
HOLDER = {"f": scaled}


def gen():
    yield 1


def child(listen: bool) -> None:
    """A long-running 'program': calls handle_lower through the registry dict
    until stdin closes; counts wrong results."""
    import threading

    if listen:
        perf.enable_attach()
    stop = threading.Event()
    threading.Thread(target=lambda: (sys.stdin.read(), stop.set()), daemon=True).start()
    print("ready", flush=True)
    wrong = 0
    while not stop.is_set():
        wrong += registry.dispatch("lower", "LIVE") != "live"
        time.sleep(0.01)
    print(f"wrong={wrong}", flush=True)


def run_child(via: str, out_file: str) -> str:
    from perf.__main__ import main

    proc = subprocess.Popen([sys.executable, __file__, "--child", via], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "ready"
    rc = main(["attach", str(proc.pid), "app.registry:handle_lower", "--via", via,
               "--out", str(OUT / "c29_live_attach"), "--set", f"OUTPUT_FILE={out_file}"])
    time.sleep(0.05)
    out, _ = proc.communicate("", timeout=30)
    return f"rc={rc} exit={proc.returncode} {out.strip()}"


if __name__ == "__main__":
    if "--child" in sys.argv:
        child(listen=sys.argv[-1] == "signal")
        sys.exit(0)
    setup(__file__)
    perf.PRINT_REPORT = False

    perf.attach("app.registry:handle_upper", OUTPUT_FILE="registry")
    registry.dispatch("upper", "abc")

    perf.attach(util.normalize, OUTPUT_FILE="alias")
    norm_alias("A  B")

    perf.attach("app.util.slugify", OUTPUT_FILE="callees")
    util.slugify("Hello World")

    perf.attach("app.repo_sql:SqlRepo.get", OUTPUT_FILE="method")
    SqlRepo({"k": "v"}).get("k")

    perf.attach(fact, OUTPUT_FILE="recursion")
    fact(5)

    perf.attach(fetch, OUTPUT_FILE="async")
    asyncio.run(fetch(3))

    perf.attach(fact, OUTPUT_FILE="locals", TRACE_LOCALS=True)
    fact(2)

    holder = HOLDER
    sig, code = inspect.signature(holder["f"]), holder["f"].__code__
    a = perf.attach(holder["f"], calls=2, OUTPUT_FILE="budget")
    results = [holder["f"](1), holder["f"](2), holder["f"](3, scale=1)]
    a.wait(5)
    f = holder["f"]
    print(f"budget: traced={a.traced} reason={a.reason} restored={f.__code__ is code} signature={inspect.signature(f) == sig} defaults={f.__defaults__} results={results}")

    w = perf.attach(scaled, calls=None, seconds=0.05, OUTPUT_FILE="window")
    w.wait(5)
    d = perf.attach(scaled, calls=None, OUTPUT_FILE="window")
    d.detach()
    print(f"window: reason={w.reason} detach: reason={d.reason} restored={holder['f'].__code__ is code}")

    try:
        perf.attach(scaled, TRACE_LOCALS=7)
    except TypeError as e:
        print(f"bad config: TypeError {e}; untouched={holder['f'].__code__ is code}")

    refused = []
    for target in ("not_imported_anywhere:fn", gen):
        try:
            perf.attach(target)
        except (LookupError, TypeError) as e:
            refused.append(type(e).__name__)
    print("refused:", " ".join(refused))

    print("signal child:", run_child("signal", "live"))
    if hasattr(sys, "remote_exec"):
        print("remote child:", run_child("remote", "remote"))
    json.dumps(perf.attachments() == [])
