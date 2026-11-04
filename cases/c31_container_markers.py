"""Values with nothing to show but their type, inside containers.

An object whose repr is the default `<x.Y object at 0x…>` is captured as a
"<TypeName>" marker. Inside a list, tuple, dict or object field it must keep
its place: dropping it shifts every later index and changes the container's
length, so a reader (or a replay) sees different data than the call got.
"""

import sqlite3
from collections import namedtuple

import perf
from _common import setup

EXPECT = {
    "summary": "default-repr objects inside lists, tuples, dicts, named tuples and object fields keep a <TypeName> marker in their place",
    "checks": [
        {"status": "works", "desc": "list: a marker in each element's place, length unchanged",
         "file": "main", "node": "*.count", "args": [["<Connection>", 1, "x", "<object>"]], "result": 4},
        {"status": "works", "desc": "tuple: same",
         "file": "main", "node": "*.first", "args": [["<object>", 2]]},
        {"status": "works", "desc": "dict value: the key stays, with the marker",
         "file": "main", "node": "*.keys", "args": [{"conn": "<Connection>", "n": 1}]},
        {"status": "works", "desc": "named tuple field and object attribute: marker, not a missing field",
         "file": "main", "node": "*.unpack", "args": [{"__type__": "Pair", "a": "<object>", "b": 3}, {"__type__": "Holder", "thing": "<object>", "n": 5}]},
    ],
}

Pair = namedtuple("Pair", "a b")


class Holder:
    def __init__(self, thing, n):
        self.thing = thing
        self.n = n


def count(items):
    return len(items)


def first(pair):
    return pair[1]


def keys(d):
    return sorted(d)


def unpack(pair, holder):
    return pair.b + holder.n


def main():
    conn = sqlite3.connect(":memory:")
    count([conn, 1, "x", object()])
    first((object(), 2))
    keys({"conn": conn, "n": 1})
    unpack(Pair(object(), 3), Holder(object(), 5))


if __name__ == "__main__":
    setup(__file__)
    perf.trace(main)()
