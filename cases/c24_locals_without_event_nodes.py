"""TRACE_LOCALS when perf's event-node tool slot is taken (e.g. by another profiler)."""

import sys

if sys.version_info >= (3, 12):
    sys.monitoring.use_tool_id(4, "another profiler")  # the slot perf uses for event nodes

import perf
from _common import locals_entries, setup, trace_nodes

EXPECT = {
    "new": True,
    "summary": "TRACE_LOCALS without event nodes: a tracked function called through an old reference (no node of its own) must not leak its locals into its caller's node",
    "checks": [
        {"status": "works", "fixes": "locals of a tracked function called WITHOUT a node of its own could be attached to whatever node was current — its caller's",
         "min_python": [3, 12], "desc": "the caller's locals contain only the caller's own variables",
         "stdout": "*caller: [(1, 'c = 1', {'c': 1}), (3, 'c = 2', {'c': 2})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "the call through the wrapper (its own node) still has its locals",
         "stdout": "*wrapped helper: [(1, 'h = 42', {'h': 42})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "the call through the old reference has no node, so there is nothing to attach locals to (only the wrapped call appears)",
         "count": "__main__.helper", "n": 1},
        {"status": "works", "min_python": [3, 12], "desc": "the same for a generator started inside the caller and finished AFTER it returned: the caller keeps its own locals",
         "stdout": "*gen caller: [(1, 'c = 1', {'c': 1}), (4, 'c = 2', {'c': 2})]*"},
    ],
}


def helper():
    h = 42
    return h


def gen_helper():
    g = 1
    yield g
    g = 2
    yield g


REGISTRY = {"h": helper, "g": gen_helper}  # originals, referenced before perf patches anything
HOLD = []


def caller():
    c = 1
    REGISTRY["h"]()
    c = 2
    return c


def gen_caller():
    c = 1
    HOLD.append(REGISTRY["g"]())  # the ORIGINAL generator function: no node of its own
    next(HOLD[-1])
    c = 2
    return c


@perf.trace(TRACE_LOCALS=["__main__.caller", "__main__.helper", "__main__.gen_caller", "__main__.gen_helper"])
def main():
    helper()   # through the wrapper: its node + line events for helper's code are switched on
    caller()   # calls the ORIGINAL helper: line events fire, but there is no node for it
    list(gen_helper())  # through the wrapper: line events on for gen_helper's code
    gen_caller()
    list(HOLD[-1])  # the original generator finishes after gen_caller returned


if __name__ == "__main__":
    setup(__file__)
    main()
    nodes = trace_nodes(f"{perf.OUTPUT_DIR}/main.json")
    print("caller:", locals_entries(next(n for n in nodes if n["name"] == "__main__.caller"), caller))
    print("wrapped helper:", locals_entries(next(n for n in nodes if n["name"] == "__main__.helper"), helper))
    print("gen caller:", locals_entries(next(n for n in nodes if n["name"] == "__main__.gen_caller"), gen_caller))
