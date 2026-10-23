"""SUPPRESS_ERRORS: the fallback value for each kind of return annotation."""

from dataclasses import dataclass, field
from typing import Optional

import perf
from _common import setup

EXPECT = {
    "new": True,
    "summary": "SUPPRESS_ERRORS fallback per return annotation (builtins, Optional, generics, None, none, dataclass, plain class, unbuildable class), DEFAULT_RETURN, suppression at the root",
    "checks": [
        {"status": "works", "desc": "-> list gives []", "node": "__main__.ret_list", "result": [], "exception": "RuntimeError(*"},
        {"status": "works", "desc": "-> dict gives {}", "node": "__main__.ret_dict", "result": {}},
        {"status": "works", "desc": "-> int gives 0", "node": "__main__.ret_int", "result": 0},
        {"status": "works", "desc": "-> str gives ''", "node": "__main__.ret_str", "result": ""},
        {"status": "works", "desc": "-> bool gives False", "node": "__main__.ret_bool", "result": False},
        {"status": "works", "desc": "-> None gives None", "node": "__main__.ret_none", "result": None},
        {"status": "works", "desc": "-> Optional[int] gives the inner type's default (0), not None",
         "node": "__main__.ret_optional", "result": 0},
        {"status": "works", "desc": "-> list[int] (generic) gives []", "node": "__main__.ret_generic", "result": []},
        {"status": "works", "desc": "-> int | str (multi-type union) gives None", "node": "__main__.ret_union", "result": None},
        {"status": "works", "desc": "no annotation gives None", "node": "__main__.ret_unannotated", "result": None},
        {"status": "works", "desc": "-> dataclass builds one from field defaults / types",
         "node": "__main__.ret_dataclass", "result": {"__type__": "Report", "title": "", "rows": [], "ok": True}},
        {"status": "works", "desc": "-> a class whose constructor needs arguments gives None, plus a stderr warning",
         "node": "__main__.ret_unbuildable", "result": None},
        {"status": "works", "desc": "...the warning names the class it could not build",
         "stdout": "*could not build default for <class '__main__.NeedsArgs'>*"},
        {"status": "works", "desc": "the error is contained at the INNERMOST traced call: a helper that raises returns its own fallback, so its caller doesn't fail",
         "file": "innermost", "node": "__main__.calls_failing_helper", "result": "after helper: None", "lacks": ["exception"]},
        {"status": "works", "desc": "every suppressed call still records its original exception",
         "count": "__main__.ret_*", "n": 12},
        {"status": "works", "desc": "callers continue after a suppressed failure and see the fallback",
         "node": "main", "result": 12, "lacks": ["exception"]},
        {"status": "works", "desc": "DEFAULT_RETURN overrides the annotation for every suppressed call",
         "file": "override", "node": "__main__.ret_list", "result": "fallback"},
        {"status": "works", "desc": "a failure in the ROOT itself is suppressed too: root returns its own fallback, the script continues",
         "file": "root_fails", "node": "root_fails", "result": [], "exception": "KeyError(*"},
        {"status": "works", "new": True, "desc": "SUPPRESS_ERRORS never swallows a failing __init__ (or __call__/__enter__/__exit__): a half-built object is never returned — the error propagates",
         "file": "ctor", "node": "ctor", "result": "TypeError raised", "lacks": ["exception"]},
        {"status": "works", "new": True, "desc": "...the failing constructor is still recorded", "file": "ctor", "node": "NeedsArgs.__init__", "exception": "TypeError(*"},
        {"status": "works", "new": True, "desc": "building a fallback value (which may run project constructors, here the dataclass's __init__) is perf's own work and is not recorded as a call",
         "count": "Report.*", "n": 0},
        {"status": "works", "desc": "without SUPPRESS_ERRORS the same failure propagates (default)",
         "stdout": "*propagated: RuntimeError*"},
    ],
}


@dataclass
class Report:
    title: str
    rows: list = field(default_factory=list)
    ok: bool = True


class NeedsArgs:
    def __init__(self, x):
        self.x = x


def ret_list() -> list: raise RuntimeError("boom")
def ret_dict() -> dict: raise RuntimeError("boom")
def ret_int() -> int: raise RuntimeError("boom")
def ret_str() -> str: raise RuntimeError("boom")
def ret_bool() -> bool: raise RuntimeError("boom")
def ret_none() -> None: raise RuntimeError("boom")
def ret_optional() -> Optional[int]: raise RuntimeError("boom")
def ret_generic() -> list[int]: raise RuntimeError("boom")
def ret_union() -> int | str: raise RuntimeError("boom")
def ret_unannotated(): raise RuntimeError("boom")
def ret_dataclass() -> Report: raise RuntimeError("boom")
def ret_unbuildable() -> NeedsArgs: raise RuntimeError("boom")


# Names, looked up at call time: a tuple of the functions themselves would hold
# the pre-patch originals (see c12) and produce no nodes.
ALL = ("ret_list", "ret_dict", "ret_int", "ret_str", "ret_bool", "ret_none", "ret_optional",
       "ret_generic", "ret_union", "ret_unannotated", "ret_dataclass", "ret_unbuildable")


@perf.trace(SUPPRESS_ERRORS=True)
def main():
    return len([globals()[name]() for name in ALL])


@perf.trace(SUPPRESS_ERRORS=True, DEFAULT_RETURN="fallback", OUTPUT_FILE="override")
def override():
    return ret_list()


@perf.trace(SUPPRESS_ERRORS=True, OUTPUT_FILE="root_fails")
def root_fails() -> list:
    return [{}["missing"]]


def failing_helper():
    raise RuntimeError("helper")


def calls_failing_helper() -> str:
    return f"after helper: {failing_helper()}"


@perf.trace(SUPPRESS_ERRORS=True, OUTPUT_FILE="innermost")
def innermost():
    return calls_failing_helper()


@perf.trace(SUPPRESS_ERRORS=True, OUTPUT_FILE="ctor")
def ctor():
    try:
        NeedsArgs()
    except TypeError:
        return "TypeError raised"
    return "swallowed"


@perf.trace(OUTPUT_FILE="not_suppressed")
def not_suppressed():
    return ret_list()


if __name__ == "__main__":
    setup(__file__)
    main()
    override()
    root_fails()
    innermost()
    ctor()
    try:
        not_suppressed()
    except RuntimeError as exc:
        print("propagated:", type(exc).__name__)
