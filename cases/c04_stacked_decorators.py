"""Several decorators on one function, including ones that change args or raise."""

import functools

import perf
from _common import setup

EXPECT = {
    "new": True,
    "summary": "Stacked decorators: with/without functools.wraps, a wrapper that rewrites args, a wrapper that raises, @perf.trace on top of a stack",
    "checks": [
        {"status": "works", "desc": "a stack of wraps-decorators yields ONE node, named after the module attribute",
         "count": "__main__.stacked", "n": 1},
        {"status": "works", "desc": "the node sits OUTSIDE the whole stack: args/result are what the caller passed/got",
         "node": "__main__.stacked", "args": [3], "result": 54},
        {"status": "works", "desc": "a decorator that rewrites arguments: the node shows the caller's args, not the rewritten ones",
         "node": "__main__.rewritten", "args": [5], "result": 11},
        {"status": "works", "desc": "a decorator that raises before calling the function: the node records the decorator's exception",
         "node": "__main__.guarded", "exception": "PermissionError('denied')", "lacks": ["result"]},
        {"status": "works", "desc": "a stack mixing wraps and no-wraps decorators: still one node",
         "count": "__main__.mixed", "n": 1},
        {"status": "works", "desc": "@perf.trace on top of a decorator stack: the root is the stack's outer __qualname__; its body's calls nest under it",
         "file": "traced_stack", "path": ["traced_stack", "__main__.plain"]},
        {"status": "works", "desc": "...and the trace entry point never appears as a duplicate node of itself",
         "file": "traced_stack", "count": "*traced_stack", "n": 1},
    ],
}


def add_ten(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs) + 10

    return wrapper


def times_two(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs) * 2

    return wrapper


def no_wraps(fn):
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)

    return wrapper


def double_arg(fn):
    @functools.wraps(fn)
    def wrapper(x):
        return fn(x * 2)

    return wrapper


def deny(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        raise PermissionError("denied")

    return wrapper


@add_ten
@times_two
@add_ten
def stacked(x):
    return x * 4  # 12 → inner add_ten 22 → times_two 44 → outer add_ten 54


@double_arg
def rewritten(x):
    return x + 1


@deny
def guarded():
    return "never"


@no_wraps
@add_ten
@no_wraps
def mixed(x):
    return x


def plain():
    return 1


@perf.trace(OUTPUT_FILE="traced_stack")
@add_ten
def traced_stack():
    return plain()


@perf.trace
def main():
    stacked(3)
    rewritten(5)
    try:
        guarded()
    except PermissionError:
        pass
    mixed(1)


if __name__ == "__main__":
    setup(__file__)
    main()
    traced_stack()
