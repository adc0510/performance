"""Threads: the trace follows work onto other threads automatically."""

import contextvars
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import perf
from _common import setup

EXPECT = {
    "summary": "threading.Thread (target / subclass / Timer), ThreadPoolExecutor submit/map, copy_context workaround, library-started threads, a worker outliving the trace",
    "checks": [
        {"status": "works", "fixes": "was a limitation: calls made on another thread were silently dropped", "desc": "Thread(target=project_fn): the thread's calls nest under the code that STARTED the thread",
         "path": ["main", "__main__.start_thread", "__main__.in_thread", "__main__.leaf"]},
        {"status": "works", "new": True, "desc": "...with the thread's own args/result", "node": "__main__.in_thread", "args": [1], "result": 2},
        {"status": "works", "new": True, "desc": "a Thread subclass overriding run(): its calls are traced the same way",
         "path": ["main", "__main__.start_subclass", "Worker.run", "__main__.leaf"]},
        {"status": "works", "new": True, "desc": "threading.Timer with a project function: traced",
         "path": ["main", "__main__.start_timer", "__main__.on_timer"]},
        {"status": "works", "fixes": "was a limitation: calls made on another thread were silently dropped", "desc": "ThreadPoolExecutor.submit and .map: every job nests under the submitter",
         "path": ["main", "__main__.fan_out", "__main__.in_pool"]},
        {"status": "works", "new": True, "desc": "...one node per job (2 submit + 1 map)", "count": "__main__.in_pool", "n": 3},
        {"status": "works", "new": True, "desc": "...the submitter's own result is unchanged", "node": "__main__.fan_out", "result": [0, 2, 4]},
        {"status": "works", "new": True, "desc": "code still using the copy_context workaround gets exactly one node per job (no doubling)",
         "count": "__main__.in_pool_ctx", "n": 2},
        {"status": "works", "new": True, "desc": "a thread raising: its node records the exception (the trace and the caller are unaffected)",
         "node": "__main__.thread_fails", "exception": "ValueError('in thread')"},
        {"status": "limitation", "new": True, "desc": "a thread whose ENTRY point is library code (here Thread(target=sorted, key=project_fn)) does not inherit the trace: project calls it makes are not recorded — such threads are usually long-lived workers that must not attach unrelated work to this trace",
         "absent": "__main__.sort_key"},
        {"status": "works", "new": True, "desc": "a worker still running when the trace ends: its node is kept as unfinished (time null) and nothing it does afterwards is recorded",
         "file": "outlives", "node": "__main__.slow_worker", "raw": True, "lacks": ["result"]},
        {"status": "works", "new": True, "desc": "...its calls made after the trace ended are absent", "file": "outlives", "absent": "__main__.after_end"},
        {"status": "works", "new": True, "desc": "...and its late work didn't crash or corrupt the written trace", "stdout": "*late worker done: True*"},
        {"status": "works", "new": True, "desc": "stress: the trace closes while 8 threads are making thousands of calls — trace written, valid JSON, no errors, nodes recorded",
         "stdout": "*stress ok: True*"},
    ],
}


def leaf(x):
    return x


def in_thread(x):
    leaf(x)
    return x + 1


class Worker(threading.Thread):
    def run(self):
        leaf(5)


def on_timer():
    return "tick"


def in_pool(x):
    return x * 2


def in_pool_ctx(x):
    return x * 2


def thread_fails():
    raise ValueError("in thread")


def sort_key(x):
    return -x


def start_thread():
    t = threading.Thread(target=in_thread, args=(1,))
    t.start()
    t.join()


def start_subclass():
    w = Worker()
    w.start()
    w.join()


def start_timer():
    t = threading.Timer(0.0, on_timer)
    t.start()
    t.join()


def fan_out():
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(in_pool, i) for i in range(2)]
        mapped = list(pool.map(in_pool, [2]))
    return [f.result() for f in futures] + mapped


def fan_out_with_context():
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(contextvars.copy_context().run, in_pool_ctx, i) for i in range(2)]
    return [f.result() for f in futures]


def start_failing_thread():
    threading.excepthook = lambda args: None  # keep the expected traceback out of the output
    t = threading.Thread(target=thread_fails)
    t.start()
    t.join()


def start_library_thread():
    t = threading.Thread(target=sorted, args=([3, 1, 2],), kwargs={"key": sort_key})
    t.start()
    t.join()


@perf.trace
def main():
    start_thread()
    start_subclass()
    start_timer()
    fan_out()
    fan_out_with_context()
    start_failing_thread()
    start_library_thread()


RELEASE = threading.Event()
LATE_DONE = threading.Event()


def after_end():
    return "late"


def slow_worker():
    RELEASE.wait()
    after_end()
    LATE_DONE.set()


@perf.trace(OUTPUT_FILE="outlives")
def outlives():
    threading.Thread(target=slow_worker, daemon=True).start()
    time.sleep(0.05)
    return "trace ends while the worker still runs"


STOP = threading.Event()
STARTED = threading.Semaphore(0)


def busy(i):
    payload = {"worker": i, "data": list(range(40))}
    leaf(payload)
    STARTED.release()  # this worker is running inside the trace
    while not STOP.is_set():
        leaf(payload)


@perf.trace(OUTPUT_FILE="stress")
def stress():
    for i in range(8):
        threading.Thread(target=busy, args=(i,), daemon=True).start()
    # A worker whose function only starts after the trace ended is (correctly)
    # not part of it -- so make sure all 8 are running before the trace closes.
    for _ in range(8):
        STARTED.acquire(timeout=10)
    time.sleep(0.05)


def stress_ok():
    import io, json, contextlib

    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        stress()
    STOP.set()
    d = json.load(open(f"{perf.OUTPUT_DIR}/stress.json"))
    workers = [c for c in d["trace"]["children"] if c["name"] == "__main__.busy"]
    calls = sum(len(w["children"]) for w in workers)
    ok = len(workers) == 8 and calls > 100 and "Traceback" not in err.getvalue() and "[perf]" not in err.getvalue()
    if not ok:
        print(f"stress detail: workers={len(workers)} calls={calls} stderr={err.getvalue()[-800:]!r}")
    return ok


if __name__ == "__main__":
    setup(__file__)
    main()
    outlives()
    RELEASE.set()
    print("late worker done:", LATE_DONE.wait(5))
    print("stress ok:", stress_ok())
