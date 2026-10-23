"""TIMEOUT: stop a hanging call but keep the partial tree."""

import asyncio
import time

import perf
from _common import setup

EXPECT = {
    "summary": "TIMEOUT on sync (SIGALRM) and async (wait_for) entry points — partial tree is kept",
    "checks": [
        {"status": "works", "desc": "sync: TimeoutError is raised inside the running call and recorded on every open node",
         "file": "sync", "node": "__main__.hang", "exception": "TimeoutError(*exceeded TIMEOUT*"},
        {"status": "works", "desc": "sync: calls that completed before the timeout are kept with their results",
         "file": "sync", "node": "__main__.step", "args": [1], "result": 1},
        {"status": "works", "desc": "async: the root records TimeoutError",
         "file": "async", "node": "async_main", "exception": "TimeoutError(*"},
        {"status": "works", "desc": "async: the coroutine that was cut off records CancelledError",
         "file": "async", "node": "__main__.ahang", "exception": "CancelledError(*"},
    ],
}


def step(x):
    return x


def hang():
    time.sleep(5)


async def ahang():
    await asyncio.sleep(5)


@perf.trace(TIMEOUT=0.3, OUTPUT_FILE="sync")
def sync_main():
    step(1)
    hang()


@perf.trace(TIMEOUT=0.3, OUTPUT_FILE="async")
async def async_main():
    step(2)
    await ahang()


if __name__ == "__main__":
    setup(__file__)
    for run in (sync_main, lambda: asyncio.run(async_main())):
        try:
            run()
        except TimeoutError:
            pass
