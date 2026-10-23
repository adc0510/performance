"""asyncio: coroutines, gather, tasks, TaskGroup, async methods/generators, to_thread, executors."""

import asyncio
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

import perf
from _common import setup

EXPECT = {
    "summary": "async def entry point, await chains, gather, create_task, TaskGroup, async methods, async generators, to_thread, run_in_executor",
    "checks": [
        {"status": "works", "desc": "async functions are traced like sync ones (await chains nest)",
         "path": ["main", "__main__.fetch_all", "__main__.fetch", "__main__.parse"]},
        {"status": "works", "desc": "gather(): concurrent coroutines each nest under the coroutine that awaited them",
         "count": "__main__.fetch", "n": 6},
        {"status": "works", "desc": "args/result of awaited coroutines are captured",
         "node": "__main__.fetch", "args": [1], "result": {"id": 1}},
        {"status": "works", "desc": "create_task(): the task copies the context at creation → nests under the creator",
         "path": ["main", "__main__.spawn_tasks", "__main__.fetch"]},
        {"status": "works", "desc": "asyncio.TaskGroup children nest under the coroutine owning the group",
         "path": ["main", "__main__.with_task_group", "__main__.fetch"]},
        {"status": "works", "desc": "async methods traced as `Class.method`",
         "path": ["main", "Client.get", "__main__.fetch"]},
        {"status": "works", "fixes": "was a limitation: an async generator's node only covered creating it; its iteration work attached to the consumer",
         "desc": "async generators: the node runs from first iteration under the consumer, and the calls it makes nest under it",
         "path": ["main", "__main__.consume_stream", "__main__.stream", "__main__.parse"]},
        {"status": "works", "new": True, "desc": "...with yields / yielded recorded", "stdout": "*stream: yields=2 yielded=[{'id': 0}, {'id': 1}]*"},
        {"status": "works", "new": True, "desc": "@asynccontextmanager: setup/teardown under its node, the `async with` body under the caller",
         "path": ["main", "__main__.use_session", "__main__.session", "__main__.parse"]},
        {"status": "works", "new": True, "desc": "...body call placement", "stdout": "*async with body under use_session: True*"},
        {"status": "works", "desc": "asyncio.to_thread copies the context → work in the thread nests under the caller",
         "path": ["main", "__main__.blocking_io"]},
        {"status": "works", "fixes": "was a limitation: calls made on another thread were silently dropped", "desc": "loop.run_in_executor(ThreadPoolExecutor): the job nests under the awaiting coroutine",
         "path": ["main", "__main__.blocking_in_pool"]},
        {"status": "works", "desc": "stdout reports overlapping (concurrent) children so wall < sum of children",
         "stdout": "*fetch*"},
    ],
}


def parse(raw):
    return {"id": raw}


async def fetch(i):
    await asyncio.sleep(0.01)
    return parse(i)


async def fetch_all():
    return await asyncio.gather(fetch(1), fetch(2))


async def spawn_tasks():
    task = asyncio.create_task(fetch(3))
    return await task


async def with_task_group():
    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(fetch(i)) for i in (5, 6)]
    return [t.result() for t in tasks]


class Client:
    async def get(self, i):
        return await fetch(i)


async def stream(n):
    for i in range(n):
        yield parse(i)


@asynccontextmanager
async def session(name):
    parse("open")
    yield name
    parse("close")


async def use_session():
    async with session("s"):
        return parse("body")


async def consume_stream():
    return [x async for x in stream(2)]


def blocking_io(x):
    return x * 2


def blocking_in_pool(x):
    return x * 3


@perf.trace
async def main():
    await fetch_all()
    await spawn_tasks()
    await with_task_group()
    await Client().get(4)
    await consume_stream()
    await use_session()
    await asyncio.to_thread(blocking_io, 1)
    with ThreadPoolExecutor(1) as pool:
        await asyncio.get_running_loop().run_in_executor(pool, blocking_in_pool, 1)


if __name__ == "__main__":
    import json

    from run_all import walk

    setup(__file__)
    asyncio.run(main())
    nodes = list(walk(json.load(open(f"{perf.OUTPUT_DIR}/main.json"))["trace"]))
    st = next(n for n in nodes if n["name"] == "__main__.stream")
    print(f"stream: yields={st['yields']} yielded={st['yielded']}")
    use = next(n for n in nodes if n["name"] == "__main__.use_session")
    print("async with body under use_session:", [c["name"] for c in use["children"]] == ["__main__.session", "__main__.parse"])
