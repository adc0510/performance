"""Inheritance across modules, super(), ABCs, mixins, local and dynamic classes."""

import enum
from dataclasses import dataclass
from typing import NamedTuple

import perf
from _common import setup
from app.repo_sql import SqlRepo

EXPECT = {
    "summary": "Inheritance across modules, super(), ABC, mixins, dataclass/Enum/NamedTuple/slots/local/type()-built classes",
    "checks": [
        {"status": "works", "desc": "override calling super(): both the subclass and base method appear, each named by its DEFINING class",
         "path": ["main", "SqlRepo.get", "Repo.get", "Repo._load"]},
        {"status": "works", "desc": "an inherited (not overridden) method is named after the base class, even when called on a subclass",
         "node": "Repo.describe", "result": "SqlRepo(1)"},
        {"status": "works", "desc": "abstract method implementation is traced",
         "node": "SqlRepo.save", "result": True},
        {"status": "works", "desc": "mixin methods are traced under the mixin's name",
         "node": "JsonMixin.to_json"},
        {"status": "works", "fixes": "was a limitation: code created during the trace (a class defined in a function, a module imported or reloaded mid-trace) was patched late or not at all, so first calls / its functions were missed",
         "desc": "a class defined during the trace (here inside a function, no __init__) is instrumented when it is created: every call is traced, including the first",
         "count": "Local.ping", "n": 2},
        {"status": "works", "desc": "same WITH a Python __init__",
         "count": "LocalInit.ping", "n": 2},
        {"status": "works", "desc": "methods on a class built with type() are traced",
         "node": "Dyn.hello", "result": "hi"},
        {"status": "works", "desc": "dataclass methods are traced; the instance is captured with its fields",
         "node": "Point.norm2", "args": [{"__type__": "Point", "x": 3, "y": 4}], "result": 25},
        {"status": "works", "desc": "Enum methods are traced", "node": "Color.label", "result": "red"},
        {"status": "works", "desc": "NamedTuple methods are traced; `self` keeps its field names",
         "node": "Pair.swap", "args": [{"__type__": "Pair", "a": 1, "b": 2}], "result": {"__type__": "Pair", "a": 2, "b": 1}},
        {"status": "works", "desc": "__slots__ class methods are traced; `self` (no __dict__, default repr) shows as `<Slotted>`",
         "node": "Slotted.value", "args": ["<Slotted>"], "result": 9},
        {"status": "limitation", "desc": "two classes with the same name in different modules produce identically named nodes (no module in method node names)",
         "count": "Point.*", "n": 4},
    ],
}


class JsonMixin:
    def to_json(self):
        return str(vars(self))


class Account(JsonMixin):
    def __init__(self, name):
        self.name = name


@dataclass
class Point:
    x: int
    y: int

    def norm2(self):
        return self.x**2 + self.y**2


class Color(enum.Enum):
    RED = 1

    def label(self):
        return self.name.lower()


class Pair(NamedTuple):
    a: int
    b: int

    def swap(self):
        return Pair(self.b, self.a)


class Slotted:
    __slots__ = ("v",)

    def __init__(self, v):
        self.v = v

    def value(self):
        return self.v


Dyn = type("Dyn", (), {"hello": lambda self: "hi"})


def make_local():
    class Local:
        def ping(self):
            return "pong"

    return Local()


def make_local_init():
    class LocalInit:
        def __init__(self):
            self.ready = True

        def ping(self):
            return "pong"

    return LocalInit()


def other_point():
    # A second class also called Point, defined in a "different module" (simulated via __module__ = app.util).
    ns = {"__module__": "app.util", "__init__": lambda self: None, "norm2": lambda self: 1}
    return type("Point", (), ns)()


@perf.trace
def main():
    repo = SqlRepo({})
    repo.save("k", "v")
    repo.get("k")
    repo.describe()
    Account("ann").to_json()
    local = make_local()
    local.ping(), local.ping()
    local_init = make_local_init()
    local_init.ping(), local_init.ping()
    Dyn().hello()
    Point(3, 4).norm2()
    Color.RED.label()
    Pair(1, 2).swap()
    Slotted(9).value()
    other = other_point()
    other.norm2(), other.norm2()


if __name__ == "__main__":
    setup(__file__)
    main()
