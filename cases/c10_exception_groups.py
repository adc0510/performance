"""ExceptionGroup (PEP 654): flat, nested, mixed types, propagated to the root."""

import sys

import perf
from _common import setup

EXPECT = {
    "new": True,
    "summary": "ExceptionGroup: flat, nested, mixed member types, except*, raised deep and propagated to the root",
    "checks": [
        {"status": "works", "desc": "the group's repr (type, message and members) is recorded as `exception`",
         "node": "__main__.raise_group", "exception": "ExceptionGroup('two failures', [ValueError('a'), KeyError('b')])"},
        {"status": "works", "desc": "every member, with its own traceback, appears in exception_debug.formatted",
         "stdout": "*formatted has members: True*"},
        {"status": "works", "desc": "nested groups keep their nesting in the repr",
         "node": "__main__.raise_nested", "exception": "ExceptionGroup('outer', [ExceptionGroup('inner', [ValueError('x')]), TypeError('y')])"},
        {"status": "works", "desc": "a group propagating through ancestors is recorded on each of them",
         "node": "__main__.middle", "exception": "ExceptionGroup('two failures'*"},
        {"status": "works", "desc": "`except*` handling a group: the handler's node has no exception and a normal result",
         "node": "__main__.handle_with_except_star", "result": "handled 2", "lacks": ["exception"]},
        {"status": "works", "desc": "a group reaching the root: trace written, root records it, and it still propagates to the caller",
         "file": "crash", "node": "crash_main", "exception": "ExceptionGroup(*"},
        {"status": "works", "desc": "...the caller receives the ORIGINAL group (not masked by trace writing)",
         "stdout": "*caller got: ExceptionGroup two failures 2*"},
    ],
}


def raise_group():
    raise ExceptionGroup("two failures", [ValueError("a"), KeyError("b")])


def raise_nested():
    raise ExceptionGroup("outer", [ExceptionGroup("inner", [ValueError("x")]), TypeError("y")])


def middle():
    raise_group()


def handle_with_except_star():
    caught = []
    try:
        middle()
    except* ValueError as eg:
        caught += eg.exceptions
    except* KeyError as eg:
        caught += eg.exceptions
    return f"handled {len(caught)}"


@perf.trace
def main():
    handle_with_except_star()
    try:
        raise_nested()
    except ExceptionGroup:
        pass


@perf.trace(OUTPUT_FILE="crash")
def crash_main():
    middle()


if __name__ == "__main__":
    import json

    setup(__file__)
    main()
    d = json.load(open(f"{perf.OUTPUT_DIR}/main.json"))
    sys.path.insert(0, ".")
    from run_all import resolve, walk

    node = next(n for n in walk(d["trace"]) if n["name"] == "__main__.raise_group")
    formatted = resolve(node["exception_debug"], d["variables"])["formatted"]
    print("formatted has members:", all(s in formatted for s in ("ValueError: a", "KeyError: 'b'", "two failures")))
    try:
        crash_main()
    except ExceptionGroup as eg:
        print("caller got:", type(eg).__name__, eg.message, len(eg.exceptions))
