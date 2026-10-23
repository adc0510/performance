"""Shared references and cycles inside captured values."""

import perf
from _common import setup

EXPECT = {
    "new": True,
    "summary": "Shared references vs. cycles: same object twice, self-referencing list/dict, object↔object cycles, shared object inside a cycle",
    "checks": [
        {"status": "works", "desc": "the same object in two fields is captured in full in both (sharing is not mistaken for a cycle)",
         "node": "__main__.shared_twice", "result": {"a": {"k": [1, 2]}, "b": {"k": [1, 2]}}},
        {"status": "works", "desc": "a list containing itself: the inner occurrence becomes `<circular list>`",
         "node": "__main__.self_list", "result": [1, "<circular list>"]},
        {"status": "works", "desc": "a dict containing itself: the inner occurrence becomes `<circular dict>`",
         "node": "__main__.self_dict", "result": {"name": "root", "me": "<circular dict>"}},
        {"status": "works", "desc": "two objects referencing each other: the cycle is cut where it closes (back at the first object)",
         "node": "__main__.mutual_objects",
         "result": {"__type__": "Node", "name": "a", "peer": {"__type__": "Node", "name": "b", "peer": "<circular Node>"}}},
        {"status": "works", "desc": "a shared object that is also part of a cycle: full where reached first, cut only on the cyclic path",
         "node": "__main__.shared_in_cycle",
         "result": {"left": {"__type__": "Node", "name": "s", "peer": {"__type__": "Node", "name": "t", "peer": "<circular Node>"}},
                    "right": {"__type__": "Node", "name": "s", "peer": {"__type__": "Node", "name": "t", "peer": "<circular Node>"}}}},
        {"status": "works", "desc": "cyclic values as ARGS are cut the same way, and the call still completes",
         "node": "__main__.takes_cycle", "args": [[1, "<circular list>"]], "result": 2},
        {"status": "works", "desc": "markers are deterministic: the same shape traced twice gives identical output",
         "stdout": "*deterministic: True*"},
    ],
}


class Node:
    def __init__(self, name):
        self.name = name
        self.peer = None


def shared_twice():
    shared = {"k": [1, 2]}
    return {"a": shared, "b": shared}


def self_list():
    x = [1]
    x.append(x)
    return x


def self_dict():
    d = {"name": "root"}
    d["me"] = d
    return d


def mutual_objects():
    a, b = Node("a"), Node("b")
    a.peer, b.peer = b, a
    return a


def shared_in_cycle():
    s, t = Node("s"), Node("t")
    s.peer, t.peer = t, s
    return {"left": s, "right": s}


def takes_cycle(x):
    return len(x)


@perf.trace
def main():
    shared_twice()
    self_list()
    self_dict()
    mutual_objects()
    shared_in_cycle()
    takes_cycle(self_list())


@perf.trace(OUTPUT_FILE="again")
def again():
    shared_in_cycle()


if __name__ == "__main__":
    import json

    setup(__file__)
    main()
    again()
    from run_all import resolve  # compare resolved values: ref names differ between files

    first = json.load(open(f"{perf.OUTPUT_DIR}/main.json"))
    second = json.load(open(f"{perf.OUTPUT_DIR}/again.json"))
    node = next(c for c in first["trace"]["children"] if c["name"] == "__main__.shared_in_cycle")
    print("deterministic:", resolve(node["result"], first["variables"])
          == resolve(second["trace"]["children"][0]["result"], second["variables"]))
