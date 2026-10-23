"""Exactly where MAX_DEPTH cuts off nested values (MAX_DEPTH=3 here)."""

import perf
from _common import setup

EXPECT = {
    "new": True,
    "summary": "MAX_DEPTH boundary (=3): containers at depth < MAX_DEPTH expand, at depth ≥ MAX_DEPTH become a repr string; lists, dicts, mixed, objects",
    "checks": [
        {"status": "works", "desc": "list nesting depth MAX_DEPTH-1 (containers at depth 0..2): fully expanded",
         "node": "__main__.lists_below", "result": [[["x"]]]},
        {"status": "works", "desc": "list nesting depth MAX_DEPTH (container at depth 3): that container becomes its repr",
         "node": "__main__.lists_at", "result": [[["['x']"]]]},
        {"status": "works", "desc": "list nesting depth MAX_DEPTH+1: the cut is still at depth 3",
         "node": "__main__.lists_above", "result": [[["[['x']]"]]]},
        {"status": "works", "desc": "plain leaves (str/int/...) are never cut, at any depth",
         "node": "__main__.lists_below", "result": [[["x"]]]},
        {"status": "works", "desc": "dicts follow the same boundary",
         "node": "__main__.dicts_at", "result": {"a": {"b": {"c": "{'d': 1}"}}}},
        {"status": "works", "desc": "mixed list/dict shapes follow the same boundary",
         "node": "__main__.mixed_at", "result": {"a": [{"b": "[1]"}]}},
        {"status": "works", "fixes": "an object one level above MAX_DEPTH was captured as \"<unconvertible …: 'str' object is not a mapping>\"", "desc": "an object's fields count from the object's own level: an object at depth MAX_DEPTH-1 still shows its fields",
         "node": "__main__.object_below", "result": [[{"__type__": "Box", "v": 1}]]},
        {"status": "works", "desc": "an object AT depth MAX_DEPTH becomes its repr string like any other container",
         "node": "__main__.object_at", "result": [[["<Box v=1>"]]]},
        {"status": "works", "desc": "the output stays valid JSON at every boundary (the file parsed)",
         "count": "__main__.*", "n": 7},
    ],
}


class Box:
    def __init__(self, v):
        self.v = v

    def __repr__(self):
        return f"<Box v={self.v}>"


def lists_below():
    return [[["x"]]]


def lists_at():
    return [[[["x"]]]]


def lists_above():
    return [[[[["x"]]]]]


def dicts_at():
    return {"a": {"b": {"c": {"d": 1}}}}


def mixed_at():
    return {"a": [{"b": [1]}]}


def object_below():
    return [[Box(1)]]


def object_at():
    return [[[Box(1)]]]


@perf.trace(MAX_DEPTH=3)
def main():
    for fn in (lists_below, lists_at, lists_above, dicts_at, mixed_at, object_below, object_at):
        fn()


if __name__ == "__main__":
    setup(__file__)
    main()
