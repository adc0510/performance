"""Exceptions raised on worker threads vs. re-raised when the caller collects them."""

import contextvars
from concurrent.futures import ThreadPoolExecutor

import perf
from _common import setup

EXPECT = {
    "new": True,
    "summary": "Thread worker exceptions: where the worker raised vs. where future.result() re-raised",
    "checks": [
        {"status": "works", "desc": "the worker's node records the exception where it was raised (on the pool thread)",
         "node": "__main__.worker_fails", "exception": "ValueError('worker')"},
        {"status": "works", "desc": "...and its exception_origin points at the worker's own raise line",
         "node": "__main__.worker_fails", "has": ["exception_origin"]},
        {"status": "works", "desc": "the caller that re-raises via future.result() records the same exception separately",
         "node": "__main__.collect", "exception": "ValueError('worker')"},
        {"status": "works", "desc": "a caller that handles it records a normal result",
         "node": "__main__.collect_safely", "result": "handled: worker", "lacks": ["exception"]},
        {"status": "works", "fixes": "was a limitation: calls made on another thread were silently dropped", "desc": "without copy_context the failing worker is recorded too",
         "node": "__main__.worker_fails_untracked", "exception": "ValueError('untracked')"},
    ],
}

POOL = ThreadPoolExecutor(1)


def worker_fails():
    raise ValueError("worker")


def worker_fails_untracked():
    raise ValueError("untracked")


def collect():
    return POOL.submit(contextvars.copy_context().run, worker_fails).result()


def collect_safely():
    try:
        return POOL.submit(contextvars.copy_context().run, worker_fails).result()
    except ValueError as exc:
        return f"handled: {exc}"


@perf.trace
def main():
    try:
        collect()
    except ValueError:
        pass
    collect_safely()
    try:
        POOL.submit(worker_fails_untracked).result()
    except ValueError:
        pass


if __name__ == "__main__":
    setup(__file__)
    main()
    POOL.shutdown()
