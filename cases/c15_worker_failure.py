"""A process-pool worker that dies abruptly (os._exit), not via an exception."""

import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

import perf
from _common import setup
from app import crasher

EXPECT = {
    "new": True,
    "summary": "Process worker killed abruptly: BrokenProcessPool in the parent, earlier completed work kept, trace still written",
    "checks": [
        {"status": "works", "desc": "work that completed before the crash keeps its worker-side node",
         "node": "app.crasher.fine", "args": [1], "result": 2},
        {"status": "works", "desc": "the submitting call records BrokenProcessPool",
         "node": "__main__.submit_crash", "exception": "BrokenProcessPool(*"},
        {"status": "limitation", "desc": "the dead worker's own subtree is lost (it never got to send it back)",
         "absent": "app.crasher.die"},
        {"status": "works", "desc": "the root completes normally when the caller handles the failure, and the trace is written",
         "node": "main", "result": "pool broke", "lacks": ["exception"]},
    ],
}


def submit_fine(pool):
    return pool.submit(crasher.fine, 1).result()


def submit_crash(pool):
    return pool.submit(crasher.die, 1).result()


@perf.trace
def main():
    with ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn")) as pool:
        submit_fine(pool)
        try:
            submit_crash(pool)
        except BrokenProcessPool:
            return "pool broke"


if __name__ == "__main__":
    setup(__file__)
    main()
