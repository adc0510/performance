"""Functions reached through references captured before the trace started."""

import perf
from _common import setup
from app import registry

EXPECT = {
    "summary": "Dispatch tables / decorator registries / stored callbacks / callbacks handed to stdlib (sorted key=, map)",
    "checks": [
        {"status": "works", "min_python": [3, 12], "fixes": "was a limitation: functions nobody looks up by name (nested defs, closures, lambdas, functions held in registries/attributes since before the trace) were invisible",
         "desc": "a function stored in a dict at import time (called through the ORIGINAL reference) is traced, under the dispatcher",
         "path": ["main", "app.registry.dispatch", "app.registry.handle_upper"]},
        {"status": "works", "min_python": [3, 12], "new": True, "desc": "...with its args/result", "node": "app.registry.handle_upper", "args": ["abc"], "result": "ABC"},
        {"status": "limitation", "max_python": [3, 11], "desc": "a function stored in a dict/list at import time is called through the ORIGINAL reference: no node",
         "absent": "app.registry.handle_upper"},
        {"status": "works", "min_python": [3, 12], "new": True, "desc": "decorator-based registries (@register) too",
         "path": ["main", "app.registry.dispatch_decorated", "app.registry.handle_reverse"]},
        {"status": "limitation", "max_python": [3, 11], "desc": "same for decorator-based registries (@register) — the registry holds the pre-patch function",
         "absent": "app.registry.handle_reverse"},
        {"status": "works", "desc": "looking the function up by name at call time (globals()/getattr(module)) gets the wrapper → traced",
         "node": "app.registry.handle_lower", "args": ["ABC"], "result": "abc"},
        {"status": "works", "desc": "the dispatcher itself is traced, so the input/output of the invisible handler is still visible one level up",
         "node": "app.registry.dispatch", "args": ["upper", "abc"], "result": "ABC"},
        {"status": "works", "min_python": [3, 12], "new": True, "desc": "a callback stored on an instance before the trace (self.on_done = fn) is traced",
         "path": ["main", "Job.run", "__main__.notify"]},
        {"status": "limitation", "max_python": [3, 11], "desc": "callback stored on an instance before the trace (self.on_done = fn) is not traced",
         "absent": "__main__.notify"},
        {"status": "works", "desc": "callback passed at call time by module-attribute reference is traced, wherever it's invoked (here: sorted(key=...))",
         "count": "__main__.sort_key", "n": 3},
        {"status": "works", "desc": "project functions run by stdlib (map) are traced and attach to the caller of map()",
         "path": ["main", "__main__.run_map", "__main__.sort_key"]},
    ],
}


def notify(msg):
    return f"notified {msg}"


def sort_key(item):
    return -item


class Job:
    def __init__(self, on_done):
        self.on_done = on_done

    def run(self):
        return self.on_done("job")


JOB = Job(notify)  # holds the pre-patch `notify`


def run_map():
    return list(map(sort_key, [1]))


@perf.trace
def main():
    registry.dispatch("upper", "abc")
    registry.dispatch_decorated("reverse", "abc")
    registry.dispatch_by_lookup("lower", "ABC")
    JOB.run()
    sorted([3, 1], key=sort_key)
    run_map()


if __name__ == "__main__":
    setup(__file__)
    main()
