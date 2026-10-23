"""asyncio failure paths: gather, return_exceptions, cancellation, TaskGroup."""

import asyncio

import perf
from _common import setup

EXPECT = {
    "new": True,
    "summary": "Async failures: gather with a failing child, gather(return_exceptions=True), explicit cancel, TaskGroup child failure, failing async root",
    "checks": [
        {"status": "works", "desc": "gather(): the failing coroutine records its exception",
         "node": "__main__.fail_after", "args": [0.01, "gather"], "exception": "ValueError('gather')"},
        {"status": "works", "desc": "gather(): the awaiting coroutine records the propagated exception",
         "node": "__main__.gather_fails", "exception": "ValueError('gather')"},
        {"status": "works", "desc": "gather(): a sibling that finished BEFORE the failure keeps its node and result",
         "node": "__main__.ok_after", "args": [0.0, "fast"], "result": "fast", "lacks": ["exception"]},
        {"status": "works", "desc": "gather(return_exceptions=True): the child still records its exception, the caller gets it as a value",
         "node": "__main__.gather_collects", "result": ["ok", "ValueError('collected')"], "lacks": ["exception"]},
        {"status": "works", "desc": "explicit task.cancel(): the cancelled coroutine records CancelledError (distinct from app errors)",
         "node": "__main__.sleeper", "exception": "CancelledError(*"},
        {"status": "works", "desc": "...and the code that cancelled and awaited it handles it normally",
         "node": "__main__.cancel_one", "result": "cancelled", "lacks": ["exception"]},
        {"status": "works", "desc": "TaskGroup: the failing child records its exception",
         "node": "__main__.fail_after", "select": {"args": [0.02, "taskgroup"]}, "exception": "ValueError('taskgroup')"},
        {"status": "works", "desc": "TaskGroup: the sibling still running when it failed is cancelled → CancelledError",
         "node": "__main__.ok_after", "select": {"args": [5, "slow"]}, "exception": "CancelledError(*"},
        {"status": "works", "desc": "TaskGroup: the sibling that finished before the failure keeps its result",
         "node": "__main__.ok_after", "select": {"args": [0.0, "early"]}, "result": "early"},
        {"status": "works", "desc": "TaskGroup: the owner records the ExceptionGroup",
         "node": "__main__.taskgroup_fails", "exception": "ExceptionGroup(*ValueError('taskgroup')*"},
        {"status": "works", "desc": "a failing async root still writes its trace and the exception reaches the caller",
         "file": "crash", "node": "crash_main", "exception": "ValueError('root')"},
        {"status": "works", "desc": "...caller sees the original exception", "stdout": "*caller got: ValueError root*"},
    ],
}


async def fail_after(delay, msg):
    await asyncio.sleep(delay)
    raise ValueError(msg)


async def ok_after(delay, value):
    await asyncio.sleep(delay)
    return value


async def sleeper():
    await asyncio.sleep(5)


async def gather_fails():
    await asyncio.gather(ok_after(0.0, "fast"), fail_after(0.01, "gather"))


async def gather_collects():
    results = await asyncio.gather(ok_after(0.0, "ok"), fail_after(0.0, "collected"), return_exceptions=True)
    return [r if isinstance(r, str) else repr(r) for r in results]


async def cancel_one():
    task = asyncio.create_task(sleeper())
    await asyncio.sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return "cancelled"


async def taskgroup_fails():
    async with asyncio.TaskGroup() as tg:
        tg.create_task(ok_after(0.0, "early"))
        tg.create_task(ok_after(5, "slow"))
        tg.create_task(fail_after(0.02, "taskgroup"))


@perf.trace
async def main():
    try:
        await gather_fails()
    except ValueError:
        pass
    await gather_collects()
    await cancel_one()
    try:
        await taskgroup_fails()
    except ExceptionGroup:
        pass


@perf.trace(OUTPUT_FILE="crash")
async def crash_main():
    await fail_after(0.0, "root")


if __name__ == "__main__":
    setup(__file__)
    asyncio.run(main())
    try:
        asyncio.run(crash_main())
    except ValueError as exc:
        print("caller got:", type(exc).__name__, exc)
