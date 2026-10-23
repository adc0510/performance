"""When values are captured, and what the per-trace value pool does to repeated objects."""

import perf
from _common import setup

EXPECT = {
    "summary": "Snapshot timing: inputs before the call, outputs at return, every capture fresh; content dedup via {__ref__}",
    "checks": [
        {"status": "works", "desc": "args are snapshotted BEFORE the call: an arg the call mutates in place is recorded as it was received",
         "node": "__main__.append_item", "args": [[1]]},
        {"status": "works", "desc": "the same object captured again later is a fresh snapshot: mutations in between are visible",
         "node": "__main__.read_items", "args": [[1, 99, 100]], "result": [1, 99, 100]},
        {"status": "works", "desc": "`self` is snapshotted fresh on every call: each incr() sees the count it started with",
         "node": "Counter.incr", "args": [{"__type__": "Counter", "n": 0}]},
        {"status": "works", "desc": "...and report() sees the final state",
         "node": "Counter.report", "args": [{"__type__": "Counter", "n": 3}], "result": 3},
        {"status": "works", "desc": "the return value is snapshotted when the call returns",
         "node": "__main__.fresh_list", "result": [0, 1, 2]},
        {"status": "works", "desc": "small values stay inline in the tree",
         "node": "__main__.read_items", "raw": True, "args": [[1, 99, 100]]},
        {"status": "works", "desc": "dedup is bottom-up by CONTENT: any part ≥64 serialized bytes is stored once in the top-level `variables` map and referenced as {__ref__: vN} (resolve refs before reading); the unchanged payload is the same ref in args and result, while the changed field is inline",
         "node": "__main__.echo_big", "raw": True, "args": [{"payload": {"__ref__": "v0"}, "version": 1}],
         "result": {"payload": {"__ref__": "v0"}, "version": 2}},
        {"status": "works", "desc": "a big object that changed between captures gets a NEW ref holding its new content",
         "node": "__main__.echo_big", "result": {"payload": "*", "version": 2}},
    ],
}


def append_item(items):
    items.append(99)


def read_items(items):
    return list(items)


def echo_big(doc):
    doc["version"] = 2
    return doc


def fresh_list():
    return [0, 1, 2]


class Counter:
    def __init__(self):
        self.n = 0

    def incr(self):
        self.n += 1

    def report(self):
        return self.n


@perf.trace
def main():
    items = [1]
    append_item(items)
    items.append(100)
    read_items(items)
    c = Counter()
    for _ in range(3):
        c.incr()
    c.report()
    echo_big({"payload": "x" * 100, "version": 1})
    out = fresh_list()
    out.append(3)


if __name__ == "__main__":
    setup(__file__)
    main()
