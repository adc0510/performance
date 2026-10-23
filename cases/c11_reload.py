"""importlib.reload() of an instrumented module."""

import importlib

import perf
from _common import setup
from app import reloadable

EXPECT = {
    "new": True,
    "summary": "importlib.reload of an instrumented module: before/after across traces, mid-trace reload, stale pre-reload references",
    "checks": [
        {"status": "works", "desc": "before reload: traced normally",
         "file": "before", "path": ["before", "app.reloadable.describe", "app.reloadable.version"]},
        {"status": "works", "desc": "after reload, the NEXT trace re-instruments the fresh functions: exactly one node per call, no duplicate wrapping",
         "file": "after", "path": ["after", "app.reloadable.describe", "app.reloadable.version"]},
        {"status": "works", "desc": "...one node each", "file": "after", "count": "app.reloadable.*", "n": 2},
        {"status": "works", "fixes": "was a limitation: code created during the trace (a class defined in a function, a module imported or reloaded mid-trace) was patched late or not at all, so first calls / its functions were missed",
         "desc": "reload DURING a trace: the reloaded module's fresh functions are traced immediately (2 calls before + 2 after the reload)",
         "file": "mid", "count": "app.reloadable.*", "n": 4},
        {"status": "works", "desc": "a reference taken before the reload still calls the OLD (wrapped) function and is traced once",
         "file": "stale", "count": "app.reloadable.describe", "n": 1},
    ],
}

OLD_DESCRIBE = None


@perf.trace(OUTPUT_FILE="before")
def before():
    return reloadable.describe()


@perf.trace(OUTPUT_FILE="mid")
def mid():
    reloadable.describe()               # describe → version
    importlib.reload(reloadable)
    return reloadable.describe()        # the fresh functions: describe → version


@perf.trace(OUTPUT_FILE="after")
def after():
    return reloadable.describe()


@perf.trace(OUTPUT_FILE="stale")
def stale():
    return OLD_DESCRIBE()


if __name__ == "__main__":
    setup(__file__)
    before()
    OLD_DESCRIBE = reloadable.describe  # the wrapper installed by `before`
    mid()
    after()
    stale()
