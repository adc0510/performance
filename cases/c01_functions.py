"""Module-level functions: call chains and every parameter kind."""

import perf
from _common import setup

EXPECT = {
    "summary": "Module-level functions: call chain, positional/keyword/default/*args/**kwargs/pos-only/kw-only capture",
    "checks": [
        {"status": "works", "desc": "root is the decorated function's __qualname__; module functions are named `module.func`",
         "path": ["main", "__main__.pipeline", "__main__.load", "__main__.parse"]},
        {"status": "works", "desc": "positional args captured by position in `args`, keywords in `kwargs`",
         "node": "__main__.parse", "args": ["a,b,c"], "kwargs": {"sep": ","}, "result": ["a", "b", "c"]},
        {"status": "works", "desc": "defaults the caller didn't pass are NOT recorded (only what was actually passed); `kwargs` is omitted when no keywords were passed",
         "node": "__main__.load", "args": ["a,b,c"], "lacks": ["kwargs"]},
        {"status": "works", "desc": "*args arrive flattened into `args`; **kwargs land in `kwargs`",
         "node": "__main__.variadic", "args": [1, 2, 3], "kwargs": {"x": 1, "y": 2}, "result": 9},
        {"status": "works", "desc": "positional-only and keyword-only parameters captured as passed",
         "node": "__main__.pos_kw_only", "args": [1, 2], "kwargs": {"c": 3}, "result": 6},
        {"status": "works", "desc": "None return is recorded as result: null",
         "node": "__main__.returns_none", "result": None},
        {"status": "works", "desc": "only the ROOT carries wall-clock start_timestamp/end_timestamp; every node has start (perf_counter) + time, rounded to 1 ns; no `end` (= start + time)",
         "node": "main", "has": ["start_timestamp", "end_timestamp", "start", "time"], "lacks": ["end"]},
        {"status": "works", "desc": "non-root nodes carry no timestamps",
         "node": "__main__.parse", "has": ["start", "time"], "lacks": ["start_timestamp", "end_timestamp", "end"]},
        {"status": "works", "desc": "the root node also gets its own args/result",
         "node": "main", "args": ["a,b,c"], "result": {"items": ["a", "b", "c"], "total": 15}},
        {"status": "works", "fixes": "was a limitation: module functions whose name starts with `_` were not instrumented",
         "desc": "private (`_name`) module functions are traced like public ones", "path": ["main", "__main__.pipeline", "__main__._private_helper"]},
    ],
}


def parse(raw, sep=";"):
    return raw.split(sep)


def load(raw, strict=True):
    return parse(raw, sep=",")


def variadic(*args, **kwargs):
    return sum(args) + sum(kwargs.values())


def pos_kw_only(a, /, b, *, c):
    return a + b + c


def returns_none():
    return None


def _private_helper():
    return 1


def pipeline(raw):
    items = load(raw)
    _private_helper()
    returns_none()
    return items


@perf.trace
def main(raw):
    items = pipeline(raw)
    total = variadic(1, 2, 3, x=1, y=2) + pos_kw_only(1, 2, c=3)
    return {"items": items, "total": total}


if __name__ == "__main__":
    setup(__file__)
    main("a,b,c")
