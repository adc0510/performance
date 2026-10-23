"""ProcessPoolExecutor.map edge cases."""

import multiprocessing
from concurrent.futures import ProcessPoolExecutor

import perf
from _common import setup
from app import workers

EXPECT = {
    "new": True,
    "summary": "ProcessPoolExecutor.map: empty input, one item, several chunks over fresh workers; arg/result pairing; spawn and forkserver",
    "checks": [
        {"status": "works", "desc": "empty input: no worker nodes, empty result",
         "file": "empty", "node": "empty", "result": [], "lacks": ["exception"]},
        {"status": "works", "desc": "...nothing grafted", "file": "empty", "count": "app.workers.*", "n": 0},
        {"status": "works", "desc": "one item: exactly one node with that item's arg/result",
         "file": "one", "node": "app.workers.square", "args": [7], "result": 49},
        {"status": "works", "desc": "several chunks over 2 fresh spawn workers: one node per item",
         "file": "many_spawn", "count": "app.workers.square", "n": 7},
        {"status": "works", "desc": "...every node pairs its own arg with its own result",
         "stdout": "*spawn pairs ok: True*"},
        {"status": "works", "desc": "same under forkserver", "file": "many_forkserver", "count": "app.workers.square", "n": 7},
        {"status": "works", "desc": "...pairs ok under forkserver", "stdout": "*forkserver pairs ok: True*"},
    ],
}


def run(method, items, chunksize=1):
    ctx = multiprocessing.get_context(method)
    with ProcessPoolExecutor(2, mp_context=ctx) as pool:
        return list(pool.map(workers.square, items, chunksize=chunksize))


@perf.trace(OUTPUT_FILE="empty")
def empty():
    return run("spawn", [])


@perf.trace(OUTPUT_FILE="one")
def one():
    return run("spawn", [7])


@perf.trace(OUTPUT_FILE="many_spawn")
def many_spawn():
    return run("spawn", range(7), chunksize=2)


@perf.trace(OUTPUT_FILE="many_forkserver")
def many_forkserver():
    return run("forkserver", range(7), chunksize=2)


def pairs_ok(name):
    import json
    import sys

    sys.path.insert(0, ".")
    from run_all import walk

    d = json.load(open(f"{perf.OUTPUT_DIR}/{name}.json"))
    nodes = [n for n in walk(d["trace"]) if n["name"] == "app.workers.square"]
    return sorted(n["args"][0] for n in nodes) == list(range(7)) and all(n["result"] == n["args"][0] ** 2 for n in nodes)


if __name__ == "__main__":
    setup(__file__)
    empty()
    one()
    many_spawn()
    print("spawn pairs ok:", pairs_ok("many_spawn"))
    many_forkserver()
    print("forkserver pairs ok:", pairs_ok("many_forkserver"))
