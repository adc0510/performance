"""Rebinding module-level names and aliases, before and during a trace."""

import perf
from _common import setup
from app import util

EXPECT = {
    "new": True,
    "summary": "Rebinding: a module function replaced before the trace, replaced during the trace, aliases",
    "checks": [
        {"status": "works", "desc": "a function rebound BEFORE the trace (util.normalize = shout) is traced under the REPLACEMENT's own name — the node says which function actually ran",
         "file": "before", "node": "__main__.shout", "args": ["Hi"], "result": "HI!"},
        {"status": "works", "desc": "...and no node claims the original ran", "file": "before", "absent": "app.util.normalize"},
        {"status": "works", "min_python": [3, 12], "fixes": "was a limitation: functions nobody looks up by name (nested defs, closures, lambdas, functions held in registries/attributes since before the trace) were invisible",
         "desc": "a replacement bound DURING the trace (here a lambda) is traced from its first call",
         "file": "during", "path": ["during", "app.util.slugify", "__main__.during.<locals>.<lambda>"]},
        {"status": "limitation", "max_python": [3, 11], "desc": "a replacement bound DURING the trace is not instrumented until the next trace (the new function object was never patched)",
         "file": "during", "count": "*lambda*", "n": 0},
        {"status": "works", "desc": "...the caller that uses it is still traced and shows the replacement's effect in its result",
         "file": "during", "node": "app.util.slugify", "result": "HELLO-WORLD!"},
        {"status": "works", "desc": "a module-level alias (other_name = some_function) is traced under the ALIAS name when called through it",
         "file": "before", "node": "__main__.alias_of_helper", "result": 1},
        {"status": "works", "desc": "...and under its own name when called directly",
         "file": "before", "node": "__main__.helper", "result": 1},
    ],
}


def shout(text):
    return text.upper() + "!"


def helper():
    return 1


alias_of_helper = helper


@perf.trace(OUTPUT_FILE="before")
def before():
    util.normalize("Hi")
    alias_of_helper()
    helper()


@perf.trace(OUTPUT_FILE="during")
def during():
    util.normalize = lambda text: text.upper() + "!"  # replaced mid-trace
    return util.slugify("hello world")


if __name__ == "__main__":
    setup(__file__)
    original = util.normalize
    util.normalize = shout
    before()
    util.normalize = original
    during()
