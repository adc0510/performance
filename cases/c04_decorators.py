"""Decorated functions: user decorators, functools caches, partials."""

import functools

import perf
from _common import setup

EXPECT = {
    "summary": "User decorators (with/without functools.wraps), lru_cache, cache, partial, callable objects",
    "checks": [
        {"status": "works", "desc": "decorator using functools.wraps: traced under the decorated name, node wraps the decorator's wrapper (args as passed)",
         "node": "__main__.with_wraps", "args": [2], "result": 4},
        {"status": "works", "desc": "decorator WITHOUT wraps: still traced under the module attribute name (wrapper.__module__ is this module)",
         "node": "__main__.without_wraps", "args": [2], "result": 4},
        {"status": "works", "fixes": "was a limitation: @functools.lru_cache / @cache functions were invisible",
         "desc": "@lru_cache / @cache: every call gets a node, hit or miss", "count": "__main__.cached_lru", "n": 2},
        {"status": "works", "new": True, "desc": "...the first call is marked `cache: miss` and its body's calls nest under it",
         "path": ["main", "__main__.cached_lru", "__main__.expensive"]},
        {"status": "works", "new": True, "desc": "...the repeat call is marked `cache: hit` (and has no children — the body didn't run)",
         "node": "__main__.cached_lru", "select": {"cache": "hit"}, "args": [3], "result": 9, "lacks": ["exception"]},
        {"status": "works", "new": True, "desc": "...miss marker on the first call", "node": "__main__.cached_plain", "select": {"cache": "miss"}, "args": [1]},
        {"status": "works", "new": True, "desc": "cache_info() / cache_clear() still work on the traced function",
         "stdout": "*cache_info works: True*"},
        {"status": "works", "desc": "project functions called inside a cached function appear once per miss",
         "count": "__main__.expensive", "n": 2},
        {"status": "works", "fixes": "was a limitation: a module-level functools.partial built at import time called the untraced original",
         "desc": "a module-level functools.partial built at import time is traced (its target gets a node with the full args)",
         "node": "__main__.add", "args": [1, 5], "result": 6},
        {"status": "works", "desc": "partial created during the trace from the module attribute is traced",
         "node": "__main__.mul", "args": [3, 4], "result": 12},
        {"status": "works", "fixes": "was a limitation: a callable instance's __call__ was not traced",
         "desc": "a callable instance's __call__ is traced as `Class.__call__`", "node": "Doubler.__call__", "args": ["<Doubler>", 7], "result": 14},
    ],
}


def twice(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)

    return wrapper


def twice_nowraps(fn):
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)

    return wrapper


@twice
def with_wraps(x):
    return x * 2


@twice_nowraps
def without_wraps(x):
    return x * 2


def expensive(x):
    return x**2


@functools.lru_cache(maxsize=None)
def cached_lru(x):
    return expensive(x)


@functools.cache
def cached_plain(x):
    return expensive(x + 100)


def add(a, b):
    return a + b


def mul(a, b):
    return a * b


ADD_ONE = functools.partial(add, 1)  # holds the pre-patch `add`


class Doubler:
    def __call__(self, x):
        return x * 2


@perf.trace
def main():
    with_wraps(2)
    without_wraps(2)
    cached_lru(3), cached_lru(3), cached_plain(1), cached_plain(1)
    ADD_ONE(5)
    functools.partial(mul, 3)(4)
    return Doubler()(7)


if __name__ == "__main__":
    setup(__file__)
    main()
    print("cache_info works:", cached_lru.cache_info().hits == 1 and cached_lru.cache_info().misses == 1)
