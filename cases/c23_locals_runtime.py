"""TRACE_LOCALS at runtime: async, generators, failures, TIMEOUT, threads, process workers, patterns (Python 3.12+)."""

import asyncio
import multiprocessing
import threading
import time

import perf
from _common import locals_entries, setup, trace_nodes
from app import workers

EXPECT = {
    "new": True,
    "summary": "TRACE_LOCALS at runtime: concurrent async calls, generator send/close, raising + SUPPRESS_ERRORS, TIMEOUT in a traced loop, untraced threads, process-pool workers, pattern forms, the cap with globals",
    "checks": [
        {"status": "works", "min_python": [3, 12], "desc": "two concurrent calls of the same async function each keep their own locals (frames are tracked separately across awaits)",
         "stdout": "*async: [[(1, 'a = tag', {'a': 'x'}), (3, 'b = a + \"!\"', {'b': 'x!'})], [(1, 'a = tag', {'a': 'y'}), (3, 'b = a + \"!\"', {'b': 'y!'})]]*"},
        {"status": "works", "min_python": [3, 12], "desc": "generator receiving values by send(): the received value is recorded at the yield line",
         "stdout": "*gen_send: [(1, 'received = yield 1', {'received': 5}), (2, 'got = received * 2', {'got': 10})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "generator closed early: entries up to the close are kept",
         "stdout": "*gen_closed: [(1, 'i = 0', {'i': 0}), (3, 'i += 1', {'i': 1}), (3, 'i += 1', {'i': 2})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "SUPPRESS_ERRORS: the suppressed call still has its locals and its exception",
         "file": "suppressed", "node": "__main__.suppressed", "exception": "RuntimeError('s')", "result": 0, "has": ["locals"]},
        {"status": "works", "fixes": "TIMEOUT could be swallowed by perf's own defensive error handling when the alarm interrupted perf's code (line tracking, value capture), letting a timed-out program run on forever",
         "min_python": [3, 12], "desc": "TIMEOUT inside a function whose locals are tracked: it fires on time (every one of 10 runs)",
         "stdout": "*timeout runs on time: 10/10*"},
        {"status": "works", "min_python": [3, 12], "desc": "...and the timed-out call keeps its locals (capped) and records the TimeoutError",
         "file": "timeout", "node": "__main__.spin", "exception": "TraceTimeout(*", "has": ["locals", "locals_dropped"]},
        {"status": "works", "min_python": [3, 12], "desc": "the same function running at the same time in an UNTRACED thread is not tracked and nothing breaks",
         "stdout": "*untraced thread calls: * errors: 0*"},
        {"status": "works", "fixes": "TRACE_LOCALS was not applied inside process-pool workers, and a spawned worker named the main script's functions `__mp_main__.f` instead of `__main__.f`",
         "min_python": [3, 12], "desc": "process-pool workers: matched functions record their locals in the worker, named as in the parent",
         "file": "workers", "node": "__main__.in_worker", "has": ["locals"], "result": 14},
        {"status": "works", "new": True, "min_python": [3, 12], "desc": "...and the worker's source lines are merged into the parent trace's `sources` (under an id of its own, so workers can't collide)",
         "stdout": "*worker source: merged=True file=True prefixed=True*"},
        {"status": "works", "min_python": [3, 12], "desc": "...entries", "stdout": "*worker: [(1, 'w = x * 2', {'w': 14})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "pattern forms: `Class.*` matches its methods, `*<lambda>` matches lambdas, unmatched functions get nothing",
         "stdout": "*patterns: method=True lambda=True unmatched=False*"},
        {"status": "works", "min_python": [3, 12], "desc": "MAX_LOCALS_EVENTS caps entries, global changes included (5 iterations × [loop variable, COUNTER] = 10 entries, cap 2 → 8 counted as dropped)",
         "file": "capped", "node": "__main__.bump_global", "locals_dropped": 8},
    ],
}

COUNTER = 0


async def tagged(tag, delay):
    a = tag
    await asyncio.sleep(delay)
    b = a + "!"
    return b


async def both():
    return await asyncio.gather(tagged("x", 0.02), tagged("y", 0.01))


def gen_send():
    received = yield 1
    got = received * 2
    yield got


def gen_closed():
    i = 0
    while True:
        i += 1
        yield i


def suppressed() -> int:
    v = 1
    raise RuntimeError("s")


def spin():
    n = 0
    while True:
        n += 1


def shared(x):
    y = x + 1
    return y


def in_worker(x):
    w = x * 2
    return w


class Box:
    def put(self, v):
        self.v = v


def unmatched():
    u = 1
    return u


def bump_global():
    global COUNTER
    for _ in range(5):
        COUNTER += 1


@perf.trace(TRACE_LOCALS=["__main__.tagged", "__main__.gen_*", "Box.*", "*<lambda>"])
def main():
    asyncio.run(both())
    g = gen_send()
    next(g)
    g.send(5)
    g2 = gen_closed()
    next(g2), next(g2)
    g2.close()
    Box().put(3)
    (lambda q: q + 1)(1)
    unmatched()


@perf.trace(TRACE_LOCALS=["*"], SUPPRESS_ERRORS=True, OUTPUT_FILE="suppressed")
def run_suppressed():
    return suppressed()


@perf.trace(TRACE_LOCALS=["*"], TIMEOUT=0.1, MAX_LOCALS_EVENTS=50, OUTPUT_FILE="timeout")
def run_timeout():
    spin()


@perf.trace(TRACE_LOCALS=["__main__.in_worker"], OUTPUT_FILE="workers")
def run_workers():
    with multiprocessing.get_context("spawn").Pool(1) as pool:
        return pool.apply(in_worker, (7,))


@perf.trace(TRACE_LOCALS=["__main__.shared"], OUTPUT_FILE="threads")
def run_threads(stop):
    for i in range(200):
        shared(i)
    stop.wait(0.05)


@perf.trace(TRACE_LOCALS=["__main__.bump_global"], MAX_LOCALS_EVENTS=2, OUTPUT_FILE="capped")
def capped():
    bump_global()


def untraced(stop, counter, errors):
    while not stop.is_set():
        try:
            shared(1)
            counter.append(1)
        except Exception:
            errors.append(1)


if __name__ == "__main__":
    import json

    from run_all import walk

    setup(__file__)
    main()
    nodes = trace_nodes(f"{perf.OUTPUT_DIR}/main.json")
    print("async:", [locals_entries(n, tagged) for n in nodes if n["name"] == "__main__.tagged"])
    print("gen_send:", locals_entries(next(n for n in nodes if n["name"] == "__main__.gen_send"), gen_send))
    print("gen_closed:", locals_entries(next(n for n in nodes if n["name"] == "__main__.gen_closed"), gen_closed))
    method = next((n for n in nodes if n["name"] == "Box.put"), {})
    lam = next((n for n in nodes if n["name"].endswith("<lambda>")), {})
    unm = next((n for n in nodes if n["name"] == "__main__.unmatched"), {})
    print(f"patterns: method={bool(method.get('locals'))} lambda={'locals' in lam} unmatched={'locals' in unm}")

    run_suppressed()

    on_time = 0
    for _ in range(10):
        t = time.perf_counter()
        try:
            run_timeout()
        except TimeoutError:
            pass
        on_time += time.perf_counter() - t < 1.0
    print(f"timeout runs on time: {on_time}/10")

    run_workers()
    worker = next(n for n in trace_nodes(f"{perf.OUTPUT_DIR}/workers.json") if n["name"] == "__main__.in_worker")
    print("worker:", locals_entries(worker, in_worker))
    raw = json.load(open(f"{perf.OUTPUT_DIR}/workers.json"))
    sid = next(n for n in walk(raw["trace"]) if n["name"] == "__main__.in_worker").get("source", "")
    src = raw.get("sources", {}).get(sid, {})
    print(f"worker source: merged={bool(src.get('lines'))} file={src.get('file') == __file__} prefixed={'_' in sid}")

    stop, counter, errors = threading.Event(), [], []
    t = threading.Thread(target=untraced, args=(stop, counter, errors))
    t.start()
    run_threads(stop)
    stop.set()
    t.join()
    print(f"untraced thread calls: {len(counter)} errors: {len(errors)}")

    capped()
