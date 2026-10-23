"""Generators, yield from, and @contextmanager: work is attributed to the generator."""

import time
from contextlib import contextmanager

import perf
from _common import setup

EXPECT = {
    "summary": "Generators (normal, raising, partially consumed, closed, thrown into, yield from), yielded values, active vs wall time, @contextmanager setup/body/teardown failures",
    "checks": [
        {"status": "works", "fixes": "was a limitation: a generator's node only covered creating it; the work done while iterating was attributed to the consumer",
         "desc": "the generator's node starts when it starts running, under its consumer; the calls it makes while iterating nest under IT",
         "path": ["main", "__main__.consume", "__main__.numbers", "__main__.square"]},
        {"status": "works", "new": True, "desc": "...with its args, the number of values it yielded, the first values themselves, and its return value",
         "node": "__main__.numbers", "args": [3], "result": None, "lacks": ["exception", "closed"]},
        {"status": "works", "new": True, "desc": "yielded values and count", "stdout": "*numbers(3): yields=3 yielded=[0, 1, 4]*"},
        {"status": "works", "new": True, "desc": "the consumer's own work between values stays under the consumer, not the generator",
         "path": ["main", "__main__.consume", "__main__.on_item"]},
        {"status": "works", "new": True, "desc": "active_time (generator code only) excludes the consumer's time between values; time is the full wall span",
         "stdout": "*slow consumer: active < 0.05 True time >= 0.2 True*"},
        {"status": "works", "new": True, "desc": "yield from: the delegate generator nests under the delegating one",
         "path": ["main", "__main__.consume", "__main__.chained", "__main__.numbers", "__main__.square"]},
        {"status": "works", "new": True, "desc": "a generator raising BEFORE its first yield records the exception, 0 yields",
         "node": "__main__.fails_at_start", "exception": "ValueError('at start')"},
        {"status": "works", "new": True, "desc": "a generator raising after a later yield records the exception and the values it did yield",
         "node": "__main__.fails_later", "exception": "ValueError('after 2')"},
        {"status": "works", "new": True, "desc": "a partially consumed generator closed by the consumer is marked closed (not an error); its cleanup runs under it",
         "path": ["main", "__main__.drive_failures", "__main__.take_two", "__main__.numbers_with_cleanup", "__main__.cleanup"]},
        {"status": "works", "new": True, "desc": "...closed flag recorded", "stdout": "*partial: closed=True yields=2*"},
        {"status": "works", "new": True, "desc": "an exception thrown INTO a generator (gen.throw) that it handles: no error recorded, iteration continues",
         "node": "__main__.resilient", "lacks": ["exception"]},
        {"status": "works", "new": True, "desc": "a generator created but never iterated runs no code and so records no node",
         "absent": "__main__.never_run"},
        {"status": "works", "new": True, "desc": "the wrapper is still a generator function (frameworks that check inspect.isgeneratorfunction keep working)",
         "stdout": "*isgeneratorfunction: True*"},
        {"status": "works", "fixes": "was a limitation: @contextmanager setup/teardown were attributed to the function containing the `with`",
         "desc": "@contextmanager: setup (before yield) and teardown (after yield) nest under the context manager's node",
         "path": ["main", "__main__.use_resource", "__main__.opened", "__main__.square"]},
        {"status": "works", "new": True, "desc": "...while the `with` body stays under the function that wrote the `with`",
         "node": "__main__.use_resource", "raw": True, "result": "db"},
        {"status": "works", "new": True, "desc": "...body call placement", "stdout": "*with-body square under use_resource: True*"},
        {"status": "works", "new": True, "desc": "context manager whose SETUP fails: recorded on its node, propagates",
         "node": "__main__.setup_fails", "exception": "RuntimeError('setup')"},
        {"status": "works", "new": True, "desc": "exception in the `with` BODY: the context manager sees it (recorded on its node) and it propagates",
         "node": "__main__.opened", "select": {"args": ["body-fails"]}, "exception": "KeyError('body')"},
        {"status": "works", "new": True, "desc": "context manager whose TEARDOWN fails: recorded on its node",
         "node": "__main__.teardown_fails", "exception": "RuntimeError('teardown')"},
    ],
}


def square(x):
    return x * x


def on_item(x):
    return x


def cleanup():
    return "cleaned"


def numbers(n):
    for i in range(n):
        yield square(i)


def chained(n):
    yield from numbers(n)


def consume(gen):
    total = 0
    for x in gen:
        total += on_item(x)
    return total


def fails_at_start():
    raise ValueError("at start")
    yield  # noqa


def fails_later():
    yield 1
    yield 2
    raise ValueError("after 2")


def numbers_with_cleanup():
    try:
        for i in range(10):
            yield i
    finally:
        cleanup()


def take_two():
    gen = numbers_with_cleanup()
    first = [next(gen), next(gen)]
    gen.close()
    return first


def resilient():
    while True:
        try:
            yield "ok"
        except KeyError:
            yield "recovered"
            return


def never_run():
    yield square(99)


def slow_numbers():
    for i in range(3):
        yield i


@contextmanager
def opened(name):
    square(1)  # setup
    try:
        yield name
    finally:
        square(2)  # teardown


@contextmanager
def setup_fails():
    raise RuntimeError("setup")
    yield  # noqa


@contextmanager
def teardown_fails():
    yield "x"
    raise RuntimeError("teardown")


def use_resource():
    with opened("db") as res:
        square(3)
        return res


def drive_failures():
    for make in (fails_at_start, fails_later):
        try:
            list(make())
        except ValueError:
            pass
    take_two()
    gen = resilient()
    next(gen)
    gen.throw(KeyError("thrown"))
    never_run()  # created, never iterated
    for cm in (setup_fails, teardown_fails):
        try:
            with cm():
                pass
        except RuntimeError:
            pass
    try:
        with opened("body-fails"):
            raise KeyError("body")
    except KeyError:
        pass


def slow_consumer():
    for _ in slow_numbers():
        time.sleep(0.1)


@perf.trace
def main():
    total = consume(numbers(3)) + consume(chained(2))
    drive_failures()
    slow_consumer()
    return total, use_resource()


if __name__ == "__main__":
    import inspect
    import json

    from run_all import walk

    setup(__file__)
    main()
    d = json.load(open(f"{perf.OUTPUT_DIR}/main.json"))
    nodes = list(walk(d["trace"]))
    num = next(n for n in nodes if n["name"] == "__main__.numbers" and n.get("args") == [3])
    print(f"numbers(3): yields={num['yields']} yielded={num['yielded']}")
    slow = next(n for n in nodes if n["name"] == "__main__.slow_numbers")
    print("slow consumer: active < 0.05", slow["active_time"] < 0.05, "time >= 0.2", slow["time"] >= 0.2)
    part = next(n for n in nodes if n["name"] == "__main__.numbers_with_cleanup")
    print(f"partial: closed={part.get('closed')} yields={part['yields']}")
    use = next(n for n in nodes if n["name"] == "__main__.use_resource")
    print("with-body square under use_resource:", [c["name"] for c in use["children"]] == ["__main__.opened", "__main__.square"])
    print("isgeneratorfunction:", inspect.isgeneratorfunction(numbers))
