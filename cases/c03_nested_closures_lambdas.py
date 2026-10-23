"""Functions that aren't module attributes: nested defs, closures, lambdas."""

import perf
from _common import setup

EXPECT = {
    "summary": "Nested functions, closures/factories, lambdas, comprehensions (traced via sys.monitoring on Python 3.12+)",
    "checks": [
        {"status": "works", "min_python": [3, 12], "fixes": "was a limitation: functions nobody looks up by name (nested defs, closures, lambdas, functions held in registries/attributes since before the trace) were invisible",
         "desc": "a `def` nested inside a function is traced as `module.outer.<locals>.inner`, with its calls under it",
         "path": ["main", "__main__.outer", "__main__.outer.<locals>.inner", "__main__.leaf"]},
        {"status": "limitation", "max_python": [3, 11], "desc": "a `def` nested inside a function is invisible (needs sys.monitoring, Python 3.12+)",
         "absent": "*inner*"},
        {"status": "works", "min_python": [3, 12], "new": True, "desc": "a closure returned by a factory and called through a local variable is traced, with its own args/result",
         "node": "__main__.make_multiplier.<locals>.multiplier", "args": [5], "result": 15},
        {"status": "limitation", "max_python": [3, 11], "desc": "a closure returned by a factory into a LOCAL variable is invisible",
         "absent": "*.multiplier"},
        {"status": "works", "desc": "...but a closure stored in a public MODULE-level variable is a module function: traced under the variable name",
         "node": "__main__.TRIPLE", "args": [2], "result": 6},
        {"status": "works", "min_python": [3, 12], "new": True, "desc": "lambdas are traced as `module.enclosing.<locals>.<lambda>` (here: 2 calls through map + 1 immediate call)",
         "count": "__main__.main.<locals>.<lambda>", "n": 3},
        {"status": "limitation", "max_python": [3, 11], "desc": "lambdas are invisible", "absent": "*lambda*"},
        {"status": "works", "desc": "the factory itself is traced; its result is described as a function, not repr'd",
         "node": "__main__.make_multiplier", "args": [3],
         "result": {"__type__": "function", "qualname": "make_multiplier.<locals>.multiplier", "module": "__main__"}},
        {"status": "works", "min_python": [3, 12], "new": True, "desc": "module functions called from a lambda nest under the lambda's node",
         "path": ["main", "__main__.main.<locals>.<lambda>", "__main__.leaf"]},
        {"status": "works", "max_python": [3, 11], "desc": "module functions CALLED from nested functions/lambdas still appear, attached to the enclosing traced function",
         "path": ["main", "__main__.outer", "__main__.leaf"]},
        {"status": "works", "desc": "comprehensions run inline: calls made in them attach to the enclosing function",
         "path": ["main", "__main__.leaf"]},
        {"status": "works", "desc": "leaf is recorded once per call, whichever way it was reached (6 calls)",
         "count": "__main__.leaf", "n": 6},
    ],
}


def leaf(x):
    return x + 1


def outer(x):
    def inner(y):
        return leaf(y) * 2

    return inner(x)


def make_multiplier(k):
    def multiplier(x):
        return x * k

    return multiplier


TRIPLE = make_multiplier(3)  # created at import time


@perf.trace
def main():
    a = outer(1)
    double = make_multiplier(3)
    b = double(5) + TRIPLE(2)
    c = list(map(lambda v: leaf(v), [1, 2]))
    d = [leaf(v) for v in [3, 4]]
    e = (lambda: leaf(0))()
    return a, b, c, d, e


if __name__ == "__main__":
    setup(__file__)
    main()
