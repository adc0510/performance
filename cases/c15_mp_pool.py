"""multiprocessing.Pool and multiprocessing.pool.ThreadPool are traced like ProcessPoolExecutor."""

import multiprocessing
from multiprocessing.pool import ThreadPool

import perf
from _common import setup
from app import workers

EXPECT = {
    "new": True,
    "summary": "multiprocessing.Pool (spawn + fork): apply, apply_async + callback, map (chunked), starmap, imap, imap_unordered, map_async failure; ThreadPool",
    "checks": [
        {"status": "works", "fixes": "was a limitation: multiprocessing.Pool workers were not traced at all",
         "desc": "Pool.apply: the worker-side subtree is grafted under the submitting call",
         "file": "spawn", "path": ["spawn", "__main__.run_all", "app.workers.square", "app.workers.helper"]},
        {"status": "works", "desc": "Pool.map with chunks: one node per item, each pairing its own arg and result",
         "stdout": "*spawn map pairs ok: True*"},
        {"status": "works", "desc": "every entry point recorded (apply 1 + apply_async 1 + map 6 + imap 3 + imap_unordered 3 = 14 squares; starmap 2 scales)",
         "file": "spawn", "count": "app.workers.square", "n": 14},
        {"status": "works", "desc": "starmap (several args per item)", "file": "spawn", "count": "Worker.scale", "n": 2},
        {"status": "works", "desc": "the program still gets plain results (never perf's internal wrapper) — including in apply_async callbacks",
         "stdout": "*spawn results ok: True*"},
        {"status": "works", "desc": "a failing task: the worker node records the exception, the caller gets the original exception",
         "file": "spawn", "node": "app.workers.explode", "exception": "ValueError('bad input 3')"},
        {"status": "works", "desc": "...caller side", "stdout": "*spawn caller got: ValueError bad input 3*"},
        {"status": "works", "desc": "same under the fork start method", "file": "fork", "count": "app.workers.square", "n": 14},
        {"status": "works", "desc": "...results ok under fork", "stdout": "*fork results ok: True*"},
        {"status": "works", "desc": "multiprocessing.pool.ThreadPool (same API, threads): traced the same way",
         "file": "threadpool", "count": "app.workers.square", "n": 14},
        {"status": "works", "desc": "outside a trace the pool behaves exactly as without perf", "stdout": "*untraced results ok: True*"},
    ],
}

EXPECTED = {
    "apply": 4, "apply_async": 9, "map": [0, 1, 4, 9, 16, 25], "starmap": [2, 6],
    "imap": [0, 1, 4], "imap_unordered": [0, 1, 4], "callback": [9],
}


def run_all(pool):
    got = {"callback": []}
    got["apply"] = pool.apply(workers.square, (2,))
    got["apply_async"] = pool.apply_async(workers.square, (3,), callback=got["callback"].append).get()
    got["map"] = pool.map(workers.square, range(6), chunksize=2)
    got["starmap"] = pool.starmap(call_scale, [(1, 2), (3, 2)])
    got["imap"] = list(pool.imap(workers.square, range(3)))
    got["imap_unordered"] = sorted(pool.imap_unordered(workers.square, range(3)))
    try:
        pool.map_async(workers.explode, [3]).get()
    except ValueError as exc:
        got["error"] = f"{type(exc).__name__} {exc}"
    return got


def call_scale(x, factor):
    return workers.Worker(factor).scale(x)


def check(label, got):
    error = got.pop("error", None)
    print(f"{label} results ok:", got == EXPECTED)
    if error:
        print(f"{label} caller got:", error)


@perf.trace(OUTPUT_FILE="spawn")
def spawn():
    with multiprocessing.get_context("spawn").Pool(2) as pool:
        return run_all(pool)


@perf.trace(OUTPUT_FILE="fork")
def fork():
    with multiprocessing.get_context("fork").Pool(2) as pool:
        return run_all(pool)


@perf.trace(OUTPUT_FILE="threadpool")
def threadpool():
    with ThreadPool(2) as pool:
        return run_all(pool)


def map_pairs_ok():
    import json

    from run_all import walk

    d = json.load(open(f"{perf.OUTPUT_DIR}/spawn.json"))
    run = next(n for n in walk(d["trace"]) if n["name"] == "__main__.run_all")
    squares = [n for n in run["children"] if n["name"] == "app.workers.square"]
    return all(n["result"] == n["args"][0] ** 2 for n in squares)


if __name__ == "__main__":
    setup(__file__)
    check("spawn", spawn())
    print("spawn map pairs ok:", map_pairs_ok())
    check("fork", fork())
    threadpool()
    with multiprocessing.get_context("spawn").Pool(2) as pool:
        check("untraced", run_all(pool))
