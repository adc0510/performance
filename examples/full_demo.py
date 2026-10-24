"""Full-feature demo for perf.

Run from the repo root:

    uv run python examples/full_demo.py

Each section below is its own @perf.trace entry point, so you get one printed
tree per feature group plus one JSON file per trace in examples/out/.
Needs Python 3.12+ for nested functions/lambdas and TRACE_LOCALS.
"""

from __future__ import annotations

import asyncio
import functools
import multiprocessing
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import NamedTuple

import perf

OUT = Path(__file__).parent / "out"

# ── global config ─────────────────────────────────────────────────────────────
perf.OUTPUT_DIR = str(OUT)                    # one JSON file per trace
perf.HOT_SPOTS = 10                           # flat summary under each tree
perf.SKIP_FUNCTIONS = ["__main__.noisy_*"]    # never traced (nor anything below)
perf.SKIP_VARIABLES = ["password", "*token*"] # dropped from captured values by name
perf.REDACT_PATTERNS = [r"(?<=password=)\w+", r'(?<=password=")\w+']  # scrubbed from any captured text (incl. traceback source lines)
perf.MAX_YIELDS_CAPTURE = 3


# ── 1. functions, recursion, closures, decorators ─────────────────────────────

def all_arg_kinds(a, /, b, c=3, *args, d, e=5, **kwargs):
    return a + b + c + sum(args) + d + e + sum(kwargs.values())


def fib(n: int) -> int:
    return n if n < 2 else fib(n - 1) + fib(n - 2)


def is_even(n: int) -> bool:
    return True if n == 0 else is_odd(n - 1)


def is_odd(n: int) -> bool:
    return False if n == 0 else is_even(n - 1)


def make_multiplier(k):
    def multiplier(x):                        # closure: traced as make_multiplier.<locals>.multiplier
        return x * k
    return multiplier


def timed(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)
    return wrapper


@timed
def decorated(x):
    return x + 1


@functools.lru_cache(maxsize=None)
def cached_square(x):
    return x * x


def add(a, b):
    return a + b


add_five = functools.partial(add, 5)


def noisy_logger(msg):                        # matches SKIP_FUNCTIONS: no node
    return add(len(msg), 0)


@perf.trace(OUTPUT_FILE="functions")
def functions_demo():
    all_arg_kinds(1, 2, 3, 4, 5, d=6, extra=7)
    fib(6)
    is_even(4)
    triple = make_multiplier(3)
    triple(7)
    list(map(lambda v: v * 2, [1, 2]))       # lambdas are traced on 3.12+
    decorated(1)
    cached_square(4)
    cached_square(4)                          # cache hit still gets a node
    add_five(10)
    noisy_logger("hidden")


# ── 2. classes and captured value types ───────────────────────────────────────

class Color(Enum):
    RED = 1
    GREEN = 2


class Point(NamedTuple):
    x: int
    y: int


@dataclass
class Order:
    id: int
    items: list = field(default_factory=list)
    api_token: str = "tok-123"                # dropped by SKIP_VARIABLES


class Cart:
    def __init__(self, owner):
        self.owner = owner
        self.items = []

    @classmethod
    def empty(cls, owner):
        return cls(owner)

    @staticmethod
    def tax(amount):
        return round(amount * 0.1, 2)

    @property
    def total(self):
        return sum(price for _, price in self.items)

    @functools.cached_property
    def summary(self):
        return f"{self.owner}: {len(self.items)} items"

    def add(self, name, price):
        self.items.append((name, price))
        return self

    def __call__(self, discount):
        return self.total - discount


@functools.singledispatch
def render(value):
    return str(value)


@render.register
def _(value: int):
    return f"int:{value}"


def echo(value):
    return value


@perf.trace(OUTPUT_FILE="classes")
def classes_demo():
    cart = Cart.empty("ann").add("apple", 1.5).add("pear", 2.0)
    Cart.tax(cart.total)
    cart.summary
    cart(0.5)
    render(3)
    render("x")
    # how each value type is captured (see c08 in TEST_CASES.md)
    for v in [
        (1, 2), {7}, {1: "one"}, Color.RED, Point(1, 2),
        Order(1, ["a"]), b"bytes", 3 + 4j, None,
        (n for n in range(3)),
    ]:
        echo(v)


# ── 3. generators and context managers ────────────────────────────────────────

def numbers(n):
    for i in range(n):
        yield fib(i)                          # work done while iterating nests under numbers


def delegating():
    yield from numbers(3)


@contextmanager
def opened(name):
    add(1, 1)                                 # setup
    yield name.upper()
    add(2, 2)                                 # teardown


@perf.trace(OUTPUT_FILE="generators")
def generators_demo():
    total = sum(numbers(5))
    list(delegating())
    with opened("file") as handle:
        echo(handle)
    return total


# ── 4. exceptions and SUPPRESS_ERRORS ─────────────────────────────────────────

class PaymentError(Exception):
    pass


def charge(user, password):
    raise PaymentError(f"login failed for user={user} password={password}")


def checkout(user):
    try:
        charge(user, password="hunter2")      # arg dropped, message redacted
    except PaymentError as exc:
        raise RuntimeError("checkout failed") from exc


def safe_checkout(user):
    try:
        checkout(user)
    except RuntimeError:
        return "fallback"


def fail_many():
    raise ExceptionGroup("batch", [ValueError("a"), KeyError("b")])


def load_items() -> list:
    raise OSError("disk gone")                # suppressed → returns []


def count_items() -> int:
    return len(load_items())


@perf.trace(OUTPUT_FILE="exceptions")
def exceptions_demo():
    safe_checkout("bob")
    try:
        fail_many()
    except* ValueError:
        pass
    except* KeyError:
        pass


@perf.trace(OUTPUT_FILE="suppress", SUPPRESS_ERRORS=True)
def suppress_demo() -> int:
    return count_items()                      # 0, not a crash


# ── 5. TRACE_LOCALS (line-by-line variable changes) ───────────────────────────

def price_order(order: Order) -> float:
    subtotal = 0.0
    for name in order.items:
        subtotal += len(name) * 1.25
    discount = 0.1 if subtotal > 5 else 0
    order.items.append("gift")                # in-place mutation is recorded
    total = subtotal * (1 - discount)
    del discount
    return total


@perf.trace(OUTPUT_FILE="locals", TRACE_LOCALS=["__main__.price_order"])
def locals_demo():
    return price_order(Order(7, ["apple", "kiwi"]))


# ── 6. size limits, spilling and output filtering ─────────────────────────────

def build_report(rows):
    return {"rows": rows, "count": len(rows)}


def small_step(x):
    return x


@perf.trace(
    OUTPUT_FILE="size",
    MAX_SIZE_CAPTURE=200,                     # bigger values are spilled ...
    SPILL_DIR=str(OUT / "spill"),             # ... to their own file here
    MAX_DEPTH=5,
    LOG_FUNCTIONS=["__main__.build_report"],  # JSON keeps only this path
)
def size_demo():
    small_step(1)
    build_report([{"id": i, "name": f"row-{i}"} for i in range(50)])
    echo([[[[[[["too deep"]]]]]]])


# ── 7. threads and processes ──────────────────────────────────────────────────

def cpu_work(n):
    return sum(i * i for i in range(n))


@perf.trace(OUTPUT_FILE="concurrency")
def concurrency_demo():
    t = threading.Thread(target=cpu_work, args=(10_000,))
    t.start()
    t.join()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(cpu_work, [1_000, 2_000]))
    with ProcessPoolExecutor(
        max_workers=2, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        pool.submit(cpu_work, 5_000).result()
        list(pool.map(fib, [5, 6]))


# ── 8. async ──────────────────────────────────────────────────────────────────

async def fetch(i):
    await asyncio.sleep(0.01 * i)
    return cpu_work(100 * i)


async def stream(n):
    for i in range(n):
        await asyncio.sleep(0)
        yield i


async def consume():
    return [i async for i in stream(4)]


@perf.trace(OUTPUT_FILE="async")
async def async_demo():
    await asyncio.gather(fetch(1), fetch(2))
    task = asyncio.create_task(fetch(3))
    async with asyncio.TaskGroup() as tg:
        tg.create_task(fetch(1))
        tg.create_task(consume())
    await asyncio.to_thread(cpu_work, 1_000)
    return await task


# ── 9. TIMEOUT (partial tree is kept) ─────────────────────────────────────────

def slow_step():
    time.sleep(2)


@perf.trace(OUTPUT_FILE="timeout", TIMEOUT=0.3)
def timeout_demo():
    add(1, 2)
    slow_step()


# ── 10. attaching to an existing function without editing it ─────────────────

if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    functions_demo()
    classes_demo()
    generators_demo()
    exceptions_demo()
    print("suppressed result:", suppress_demo())
    locals_demo()
    size_demo()
    concurrency_demo()
    asyncio.run(async_demo())
    try:
        timeout_demo()
    except BaseException as exc:              # TimeoutError (or similar)
        print("timeout raised:", type(exc).__name__)
    perf.trace(fib, OUTPUT_FILE="attached")(8)  # section 10
    print(f"\nJSON traces written to {OUT}")
