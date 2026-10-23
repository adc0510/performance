"""Less common callable shapes and hostile values."""

import functools
import sys

import perf
from _common import setup

EXPECT = {
    "summary": "singledispatch, cached_property, __getattr__ proxies, monkeypatched methods, values whose repr/attrs raise",
    "checks": [
        {"status": "works", "desc": "@singledispatch: the dispatcher is traced under its name",
         "node": "__main__.render", "args": [5], "result": "int:5"},
        {"status": "works", "fixes": "was a limitation: @render.register implementations were invisible (the registry held the originals)",
         "desc": "...and the implementation it dispatched to is traced under it", "path": ["main", "__main__.render", "__main__._render_int"]},
        {"status": "works", "fixes": "was a limitation: @functools.cached_property was invisible",
         "desc": "@functools.cached_property: a node on the cache MISS (the only time the function runs), with its calls under it",
         "path": ["main", "Service.config", "__main__.compute"]},
        {"status": "works", "new": True, "desc": "...the cache hit (second access) records nothing, because nothing ran", "count": "Service.config", "n": 1},
        {"status": "works", "new": True, "desc": "capture never calls a user `.dict()` / `.model_dump()` that merely exists on a non-pydantic object (it would run user code with side effects)",
         "stdout": "*user dict() called by capture: False*"},
        {"status": "works", "new": True, "desc": "user code that perf's own capture runs (a __repr__ calling a project function) is never recorded as a call",
         "absent": "__main__.repr_helper"},
        {"status": "works", "min_python": [3, 12], "fixes": "was a limitation: functions nobody looks up by name (nested defs, closures, lambdas, functions held in registries/attributes since before the trace) were invisible",
         "desc": "functions produced dynamically by __getattr__ are traced when called", "node": "__main__.Service.__getattr__.<locals>.<lambda>", "result": "dynamic_method"},
        {"status": "limitation", "max_python": [3, 11], "desc": "methods produced dynamically by __getattr__ are invisible; the proxy's own methods are fine",
         "absent": "*lambda*"},
        {"status": "works", "desc": "a method monkeypatched onto a class before the trace starts is traced under its attribute name",
         "node": "Service.patched", "result": "patched"},
        {"status": "works", "desc": "an argument whose __repr__ raises does not break the trace (captured as <unreprable …> or its fields)",
         "node": "__main__.takes_hostile", "result": "ok"},
        {"status": "limitation", "new": True, "desc": "patching REBINDS every module-level name bound to a project function (its own name, aliases, any global variable holding it): a reference taken before the trace is no longer `is`-identical to that global while tracing (identity checks like `cb is mod.handler` change result)",
         "stdout": "*identity before trace preserved: False*"},
    ],
}


@functools.singledispatch
def render(value):
    return f"obj:{value}"


@render.register
def _render_int(value: int):
    return f"int:{value}"


def compute():
    return 42


EARLY_REF = compute  # taken at import time, before any trace


class Service:
    @functools.cached_property
    def config(self):
        return compute()

    def __getattr__(self, name):
        if name.startswith("dynamic"):
            return lambda: name
        raise AttributeError(name)


def patched(self):
    return "patched"


Service.patched = patched


DICT_CALLS = []


class Sneaky:
    """Not pydantic, but has a `dict` method and a property: capture must not run them."""

    def __init__(self):
        self.x = 1

    def dict(self, **kwargs):
        DICT_CALLS.append(1)
        return {"called": True}

    @property
    def model_dump(self):
        DICT_CALLS.append(2)
        return lambda **kw: {}


def repr_helper():
    return "Pretty()"


class Pretty:
    __slots__ = ()

    def __repr__(self):
        return repr_helper()


class Hostile:
    __slots__ = ()

    def __repr__(self):
        raise RuntimeError("no repr for you")


def takes_hostile(x):
    return "ok"


@perf.trace
def main():
    render(5)
    svc = Service()
    _ = svc.config
    _ = svc.config
    takes_hostile(Sneaky())
    takes_hostile(Pretty())
    svc.dynamic_method()
    svc.patched()
    takes_hostile(Hostile())
    print("user dict() called by capture:", bool(DICT_CALLS))
    print("identity before trace preserved:", EARLY_REF is sys.modules[__name__].compute)


if __name__ == "__main__":
    setup(__file__)
    main()
