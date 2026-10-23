"""Recursion and mutual recursion."""

import perf
from _common import setup

EXPECT = {
    "summary": "Direct recursion, mutual recursion, deep recursion",
    "checks": [
        {"status": "works", "desc": "every recursive call is its own nested node (fib(6) = 25 calls)",
         "count": "__main__.fib", "n": 25},
        {"status": "works", "desc": "recursive nodes nest under each other",
         "path": ["main", "__main__.fib", "__main__.fib", "__main__.fib"]},
        {"status": "works", "desc": "mutual recursion alternates in the tree",
         "path": ["main", "__main__.is_even", "__main__.is_odd", "__main__.is_even"]},
        {"status": "works", "desc": "each frame keeps its own args/result",
         "node": "__main__.fib", "args": [6], "result": 8},
        {"status": "works", "desc": "deep recursion (200 levels) still traced fully",
         "count": "__main__.countdown", "n": 201},
        {"status": "works", "desc": "stdout tree collapses repeated siblings as `name ×N`",
         "stdout": "*×*"},
    ],
}


def fib(n):
    return n if n < 2 else fib(n - 1) + fib(n - 2)


def is_even(n):
    return True if n == 0 else is_odd(n - 1)


def is_odd(n):
    return False if n == 0 else is_even(n - 1)


def countdown(n):
    return 0 if n == 0 else countdown(n - 1)


@perf.trace
def main():
    for _ in range(3):
        is_even(4)
    return fib(6), countdown(200)


if __name__ == "__main__":
    setup(__file__)
    main()
