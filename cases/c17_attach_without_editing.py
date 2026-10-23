"""Attaching a trace to code you don't want to edit, and repeated/nested traces.

This is the driver-script pattern: import the target, wrap its entry point
with perf.trace at runtime, call it.
"""

import perf
from _common import setup
from app import registry, util
from app.repo_sql import SqlRepo

EXPECT = {
    "summary": "perf.trace(fn)(...) on an imported function / bound method / class method; repeated and nested traces; calls after a trace ends",
    "checks": [
        {"status": "works", "desc": "perf.trace(module.func)(...) — no edits to the target; root is the function's __qualname__ (no module prefix); first trace in the process → no duplicate",
         "file": "func", "path": ["slugify", "app.util.normalize"]},
        {"status": "works", "desc": "root args/result are captured",
         "file": "func", "node": "slugify", "args": ["Hello World"], "result": "hello-world"},
        {"status": "works", "desc": "perf.trace(obj.method)(...) on a bound method: root named `Class.method`, self is NOT in root args",
         "file": "method", "node": "SqlRepo.get", "args": ["k"], "result": "v"},
        {"status": "works", "desc": "...and the method's own callees nest under it — no duplicate root even though an earlier trace already instrumented SqlRepo.get",
         "file": "method", "path": ["SqlRepo.get", "Repo.get", "Repo._load"]},
        {"status": "works", "desc": "trace a deep function in isolation to see only its subtree",
         "file": "deep", "path": ["dispatch_by_lookup", "app.registry.handle_lower"]},
        {"status": "works", "desc": "the same traced function called twice writes the file twice (last call wins with a fixed OUTPUT_FILE)",
         "file": "twice", "node": "normalize", "args": ["Second"]},
        {"status": "works", "desc": "a trace started INSIDE another trace is written separately; afterwards the outer trace continues normally",
         "file": "outer", "path": ["run_outer", "app.util.slugify", "app.util.normalize"]},
        {"status": "works", "desc": "the inner trace has its own file",
         "file": "inner", "node": "normalize", "args": ["Inner"]},
        {"status": "works", "desc": "after a trace returns, later untraced calls are not recorded anywhere",
         "stdout": "*calls after trace end appended to old root: 0*"},
    ],
}


def run_outer():
    perf.trace(util.normalize, OUTPUT_FILE="inner")("Inner")
    return util.slugify("after inner")  # should be a child of the outer root


if __name__ == "__main__":
    setup(__file__)
    perf.trace(util.slugify, OUTPUT_FILE="func")("Hello World")

    repo = SqlRepo({"k": "v"})
    perf.trace(repo.get, OUTPUT_FILE="method")("k")

    perf.trace(registry.dispatch_by_lookup, OUTPUT_FILE="deep")("lower", "ABC")

    traced = perf.trace(util.normalize, OUTPUT_FILE="twice")
    traced("First")
    traced("Second")

    perf.trace(run_outer, OUTPUT_FILE="outer")()

    # A fresh traced call, then untraced calls in the same context afterwards.
    captured = []
    real_print_tree = perf.print_tree
    perf.print_tree = lambda root: captured.append(root)
    perf.trace(util.normalize, OUTPUT_FILE="stale")("x")
    perf.print_tree = real_print_tree
    before = len(captured[0]["children"])
    util.slugify("not traced")
    print("calls after trace end appended to old root:", len(captured[0]["children"]) - before)
