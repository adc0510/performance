"""Several traced roots sharing one reused thread pool (copy_context workaround)."""

import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor

import perf
from _common import setup

EXPECT = {
    "new": True,
    "summary": "Reused ThreadPoolExecutor shared by concurrent traced roots (each submitting with copy_context): no cross-trace leakage",
    "checks": [
        {"status": "works", "fixes": "two traces starting at once on different threads crashed inside the traced code (RuntimeError: dictionary changed size during iteration); concurrent class patching could double-wrap", "desc": "root A's pool jobs nest under root A",
         "file": "job_a", "count": "__main__.work", "n": 3},
        {"status": "works", "fixes": "per-trace overrides (@trace(OUTPUT_FILE=...)) were global: concurrent traces wrote into each other's files", "desc": "...and carry only A's inputs", "file": "job_a", "node": "__main__.work", "select": {"args": ["a", 2]}, "result": "a2"},
        {"status": "works", "desc": "root B's pool jobs nest under root B, none of A's leak in",
         "file": "job_b", "count": "__main__.work", "n": 3},
        {"status": "works", "desc": "...and carry only B's inputs", "file": "job_b", "node": "__main__.work", "select": {"args": ["b", 2]}, "result": "b2"},
        {"status": "works", "desc": "pool threads reused across both roots don't retain either root",
         "stdout": "*leaked across traces: False*"},
        {"status": "works", "new": True, "desc": "WITHOUT copy_context: two concurrent traces sharing one pool each get exactly their own jobs",
         "file": "plain_a", "count": "__main__.work", "n": 3},
        {"status": "works", "new": True, "desc": "...and only their own inputs", "file": "plain_b", "node": "__main__.work", "select": {"args": ["b", 2]}, "result": "b2"},
        {"status": "works", "new": True, "desc": "...nothing of A in B's trace or B in A's", "stdout": "*plain leaked across traces: False*"},
        {"status": "works", "desc": "after both traces, a job on the same (reused) pool thread records nothing",
         "stdout": "*context on reused thread: None*"},
    ],
}

POOL = ThreadPoolExecutor(2)
START = threading.Barrier(2)


def work(tag, i):
    return f"{tag}{i}"


def run_plain(tag):
    return [f.result() for f in [POOL.submit(work, tag, i) for i in range(3)]]


@perf.trace(OUTPUT_FILE="plain_a")
def plain_a():
    START.wait()
    return run_plain("a")


@perf.trace(OUTPUT_FILE="plain_b")
def plain_b():
    START.wait()
    return run_plain("b")


def run_jobs(tag):
    futures = [POOL.submit(contextvars.copy_context().run, work, tag, i) for i in range(3)]
    return [f.result() for f in futures]


@perf.trace(OUTPUT_FILE="job_a")
def job_a():
    START.wait()
    return run_jobs("a")


@perf.trace(OUTPUT_FILE="job_b")
def job_b():
    START.wait()
    return run_jobs("b")


if __name__ == "__main__":
    import json

    setup(__file__)
    threads = [threading.Thread(target=job_a), threading.Thread(target=job_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    a = json.load(open(f"{perf.OUTPUT_DIR}/job_a.json"))
    b = json.load(open(f"{perf.OUTPUT_DIR}/job_b.json"))
    text_a, text_b = json.dumps(a["trace"]), json.dumps(b["trace"])
    print("leaked across traces:", '"b",' in text_a or '"a",' in text_b)
    threads = [threading.Thread(target=plain_a), threading.Thread(target=plain_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    a = json.load(open(f"{perf.OUTPUT_DIR}/plain_a.json"))
    b = json.load(open(f"{perf.OUTPUT_DIR}/plain_b.json"))
    text_a, text_b = json.dumps(a["trace"]), json.dumps(b["trace"])
    print("plain leaked across traces:", '"b",' in text_a or '"a",' in text_b)
    print("context on reused thread:", POOL.submit(perf._current_node.get).result())
    POOL.shutdown()
