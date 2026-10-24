"""How each kind of value is captured in args/result."""

import datetime
import decimal
import enum
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import perf
from _common import setup

EXPECT = {
    "summary": "Captured form of each value type: primitives, containers, stdlib types, objects, cycles, depth limit",
    "checks": [
        {"status": "works", "desc": "primitives pass through unchanged", "node": "__main__.prim", "result": [1, 2.5, True, None, "s"]},
        {"status": "works", "desc": "nested dict/list kept verbatim", "node": "__main__.nested", "result": {"a": [1, {"b": [2, 3]}]}},
        {"status": "limitation", "desc": "tuple → list (tuple-ness lost)", "node": "__main__.tup", "result": [1, 2]},
        {"status": "limitation", "desc": "set/frozenset → list", "node": "__main__.aset", "result": [7]},
        {"status": "limitation", "desc": "non-str dict keys become their repr", "node": "__main__.intkeys", "result": {"1": "one", "(2, 3)": "pair"}},
        {"status": "works", "desc": "bytes → repr string", "node": "__main__.raw", "result": "b'\\x00ab'"},
        {"status": "works", "desc": "datetime / Decimal / UUID / Path → repr string",
         "node": "__main__.stdlib_vals",
         "result": ["datetime.datetime(2026, 1, 2, 3, 4)", "Decimal('1.50')", "UUID('12345678-1234-5678-1234-567812345678')", "PosixPath('/tmp/x')"]},
        {"status": "works", "desc": "dataclass → {__type__, fields}", "node": "__main__.dc", "result": {"__type__": "User", "name": "ann", "age": 3}},
        {"status": "works", "fixes": "was a limitation: a NamedTuple was captured as a plain list (field names lost)",
         "desc": "NamedTuple → {__type__, field: value, …}", "node": "__main__.nt", "result": {"__type__": "NT", "a": 1, "b": 2}},
        {"status": "works", "desc": "plain object → {__type__, __dict__ fields}", "node": "__main__.obj", "result": {"__type__": "Box", "content": "x"}},
        {"status": "limitation", "desc": "object with no attributes and default repr → `<TypeName>` marker",
         "node": "__main__.opaque", "result": "<Opaque>"},
        {"status": "works", "desc": "object with custom __repr__ and no attributes → its repr", "node": "__main__.custom_repr", "result": "Money(5 EUR)"},
        {"status": "works", "fixes": "was a limitation: an Enum member was captured with its whole Enum class dumped",
         "desc": "Enum member → {__enum__: Class.MEMBER, value}", "node": "__main__.color", "result": {"__enum__": "Color.RED", "value": 1}},
        {"status": "works", "new": True, "desc": "a class passed as a value → {__class__: module.QualName}", "node": "__main__.a_class", "result": {"__class__": "__main__.User"}},
        {"status": "works", "desc": "functions/methods passed as values → {__type__: function|method, qualname, module}",
         "node": "__main__.takes_callable", "args": [{"__type__": "function", "qualname": "prim", "module": "__main__"}]},
        {"status": "works", "new": True, "desc": "a dict of only functions (a registry, ≥ 4 entries) → {__functions__: [keys]}; fewer stay per-function",
         "node": "__main__.registry", "result": [{"__functions__": ["abs", "len", "prim", "nested"]},
                                                 {"one": {"__type__": "function", "qualname": "prim", "module": "__main__"}}]},
        {"status": "works", "desc": "cycles are cut with a `<circular list>` marker",
         "node": "__main__.cyclic", "result": [1, "<circular list>"]},
        {"status": "limitation", "desc": "beyond MAX_DEPTH (20) the remainder is a repr string",
         "node": "__main__.deep", "result": [[[[[[[[[[[[[[[[[[[["[[[[['leaf']]]]]"]]]]]]]]]]]]]]]]]]]]},
        {"status": "works", "desc": "exceptions passed as values → repr", "node": "__main__.exc_value", "result": "ValueError('boom')"},
        {"status": "limitation", "desc": "generators as values → repr (contents not consumed)", "node": "__main__.gen_value", "result": "<generator object *"},
    ],
}


@dataclass
class User:
    name: str
    age: int


class NT(NamedTuple):
    a: int
    b: int


class Box:
    def __init__(self, content):
        self.content = content


class Opaque:
    pass


class Money:
    def __repr__(self):
        return "Money(5 EUR)"


class Color(enum.Enum):
    RED = 1


def prim():
    return [1, 2.5, True, None, "s"]


def nested():
    return {"a": [1, {"b": [2, 3]}]}


def tup():
    return (1, 2)


def aset():
    return {7}


def intkeys():
    return {1: "one", (2, 3): "pair"}


def raw():
    return b"\x00ab"


def stdlib_vals():
    return [
        datetime.datetime(2026, 1, 2, 3, 4),
        decimal.Decimal("1.50"),
        uuid.UUID("12345678-1234-5678-1234-567812345678"),
        Path("/tmp/x"),
    ]


def dc():
    return User("ann", 3)


def nt():
    return NT(1, 2)


def obj():
    return Box("x")


def opaque():
    return Opaque()


def custom_repr():
    return Money()


def color():
    return Color.RED


def takes_callable(fn):
    return fn.__name__


def registry():
    return {"abs": abs, "len": len, "prim": prim, "nested": nested}, {"one": prim}


def a_class():
    return User


def cyclic():
    x = [1]
    x.append(x)
    return x


def deep():
    v = "leaf"
    for _ in range(25):
        v = [v]
    return v


def exc_value():
    return ValueError("boom")


def gen_value():
    return (i for i in range(3))


@perf.trace
def main():
    for fn in (prim, nested, tup, aset, intkeys, raw, stdlib_vals, dc, nt, obj, opaque,
               custom_repr, color, registry, a_class, cyclic, deep, exc_value, gen_value):
        fn()
    takes_callable(prim)


if __name__ == "__main__":
    setup(__file__)
    main()
