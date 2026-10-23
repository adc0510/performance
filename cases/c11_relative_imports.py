"""Relative imports inside a multi-level package."""

import perf
from _common import setup
from app.sub import mid

EXPECT = {
    "new": True,
    "summary": "Relative imports (from . import x, from .x import f, from ..pkg import f) in a nested package",
    "checks": [
        {"status": "works", "desc": "nodes are named by the DEFINING module, whichever relative form imported them",
         "path": ["main", "app.sub.mid.combine", "app.util.normalize"]},
        {"status": "works", "desc": "`from .leaf import leaf_value` and `leaf.leaf_value` (via `from . import leaf`) are both traced as app.sub.leaf.leaf_value",
         "count": "app.sub.leaf.leaf_value", "n": 2},
        {"status": "works", "desc": "args/results captured through relative imports",
         "node": "app.sub.mid.combine", "args": ["A  B", 2], "result": "a b:4:6"},
        {"status": "works", "desc": "parent/child structure is stable across repeated runs",
         "file": "second", "path": ["second", "app.sub.mid.combine", "app.sub.leaf.leaf_value"]},
    ],
}


@perf.trace
def main():
    return mid.combine("A  B", 2)


@perf.trace(OUTPUT_FILE="second")
def second():
    return mid.combine("x", 1)


if __name__ == "__main__":
    setup(__file__)
    main()
    second()
